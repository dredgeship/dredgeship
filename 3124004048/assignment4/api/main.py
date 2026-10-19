"""FastAPI 应用：把 Agent 能力暴露成 REST + SSE 接口，并托管网页前端。

设计要点：

* **单一内核**：Web 与 CLI 共用同一个 :class:`AgentCore`，避免出现"两套逻辑"。
* **SSE 而非 WebSocket**：对话是单向流式推送，SSE 更简单、可自动重连，
  且不需要额外的连接管理。
* **复用 MCP 能力层**：计划视图等只读数据直接经 MCP 工具获取
  （``core.mcp.call_tool``），与 Agent 走同一条链路，杜绝两份实现。

启动：``python -m api.main``  →  http://127.0.0.1:8000
接口文档：http://127.0.0.1:8000/docs
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.schemas import ChatRequest, MetaResponse, TaskCreate, TaskOut, TaskUpdate  # noqa: E402
from api.voice import router as voice_router  # noqa: E402
from common import timeutil  # noqa: E402
from common.config import get_settings  # noqa: E402
from storage.repository import (  # noqa: E402
    ConversationRepository,
    PreferenceRepository,
    TaskRepository,
)

logger = logging.getLogger(__name__)

WEB_DIR = PROJECT_ROOT / "web"

# 参与缓存版本号的静态资源。内容一变，URL 上的 ?v= 就变，浏览器必然重新拉取。
WEB_ASSET_FILES = ("app.js", "voice.js", "style.css")


class FreshStaticFiles(StaticFiles):
    """给静态资源补上 ``Cache-Control``。

    Starlette 的 ``StaticFiles`` 默认**不发送** ``Cache-Control``，浏览器于是启用
    "启发式缓存"（按 ``Last-Modified`` 推算新鲜期），可能不回源就继续吃旧文件。
    本项目实际踩过这个坑：前端加了「录音后回放」开关，用户看到的是新页面，跑的却是
    旧 ``voice.js``——按钮点了没反应，很难往缓存上想。

    这里统一要求浏览器每次回源校验（配合 ETag 走 304，开销极小），
    同时 ``index.html`` 里的资源 URL 带内容版本号，双保险。
    """

    def file_response(self, *args: Any, **kwargs: Any) -> Any:
        response = super().file_response(*args, **kwargs)
        response.headers.setdefault("Cache-Control", "no-cache, must-revalidate")
        return response


def asset_version() -> str:
    """按前端资源内容算出版本号（内容不变则版本号稳定）。"""
    digest = hashlib.sha1()
    for name in WEB_ASSET_FILES:
        try:
            digest.update((WEB_DIR / name).read_bytes())
        except OSError:  # pragma: no cover - 文件缺失时退化
            continue
    return digest.hexdigest()[:10]

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    # 关闭 Nginx 等反向代理的缓冲，保证逐条事件实时到达浏览器
    "X-Accel-Buffering": "no",
}


# --------------------------------------------------------------------------- #
# 应用生命周期
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时**后台**拉起 Agent（含 MCP Server 子进程）与语音子系统，关闭时优雅释放。

    两者的冷启动都要 1~10 秒：MCP 子进程需要启动解释器并导入依赖，语音需要拉起
    PowerShell 并加载 System.Speech。若在 lifespan 里同步等待，网页在此期间完全
    打不开。因此改为后台任务初始化，接口先可用，相关接口再按需等待就绪。

    刻意让**语音与 Agent 相互独立**：
    * 语音先初始化 —— 它完全不依赖大模型，没有密钥也能用；
    * Agent 就绪后再把它的 LLM 客户端注入语音，用于识别结果纠错；
    * 任一侧失败都只记录警告，不影响另一侧，也不阻止服务启动。
    """
    import asyncio

    from agent.core import AgentCore
    from agent.voice_service import VoiceService

    app.state.core = None
    app.state.startup_error = ""
    app.state.ready = asyncio.Event()
    app.state.voice = VoiceService(get_settings())
    app.state.voice_ready = asyncio.Event()

    async def boot() -> None:
        try:
            await app.state.voice.initialize()
            if app.state.voice.available:
                logger.info(
                    "语音就绪（完全离线）：识别 %s / 合成 %s",
                    app.state.voice.engine_name,
                    app.state.voice.voice_name,
                )
            else:
                logger.info("语音不可用：%s", app.state.voice.reason)
        except Exception as exc:  # noqa: BLE001 - 语音失败绝不能影响主服务
            logger.warning("语音初始化异常：%s", exc)
        finally:
            app.state.voice_ready.set()

        try:
            app.state.core = await AgentCore.create()
            # 语音的"识别纠错"复用 Agent 的模型客户端，避免再建一个连接
            app.state.voice.attach_llm(app.state.core.llm)
            logger.info("Agent 就绪，工具 %s 个", len(app.state.core.mcp.tools))
        except Exception as exc:  # noqa: BLE001 - 启动失败也要让服务可用，便于前端提示
            app.state.startup_error = str(exc)
            logger.error("Agent 启动失败：%s", exc)
        finally:
            app.state.ready.set()

    boot_task = asyncio.create_task(boot(), name="agent-boot")
    try:
        yield
    finally:
        try:
            await asyncio.wait_for(asyncio.shield(boot_task), timeout=30)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            boot_task.cancel()
        core = getattr(app.state, "core", None)
        if core is not None:
            await core.aclose()
            logger.info("Agent 已关闭")
        voice = getattr(app.state, "voice", None)
        if voice is not None:
            await voice.aclose()
            logger.info("语音已关闭")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="待办小助理 API",
        description="基于 MCP + DeepSeek 的任务管理智能体服务端",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(voice_router)

    tasks = TaskRepository()
    conversations = ConversationRepository()
    preferences = PreferenceRepository()

    # ------------------------------------------------------------------ #
    # 依赖与工具函数
    # ------------------------------------------------------------------ #
    async def wait_ready(request: Request, timeout: float):
        """等待 Agent 就绪；超时或启动失败返回 ``None``。"""
        import asyncio

        core = getattr(request.app.state, "core", None)
        if core is not None:
            return core
        ready = getattr(request.app.state, "ready", None)
        if ready is None:
            return None
        try:
            await asyncio.wait_for(ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        return getattr(request.app.state, "core", None)

    async def require_core(request: Request, timeout: float = 60.0):
        core = await wait_ready(request, timeout)
        if core is None:
            raise HTTPException(
                status_code=503,
                detail=f"智能体尚未就绪：{getattr(request.app.state, 'startup_error', '') or '仍在启动中，请稍后重试'}",
            )
        return core

    async def call_mcp(request: Request, tool: str, arguments: dict[str, Any], timeout: float = 25.0) -> dict[str, Any]:
        """经 MCP 调用工具并解析 JSON 结果（与 Agent 走同一条链路）。"""
        core = await wait_ready(request, timeout)
        if core is None:
            error = getattr(request.app.state, "startup_error", "") or "MCP 仍在启动中"
            return {"ok": False, "error": error}
        try:
            raw = await core.mcp.call_tool(tool, arguments)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return {"ok": True, "message": raw}

    # ------------------------------------------------------------------ #
    # 元信息
    # ------------------------------------------------------------------ #
    @app.get("/api/health")
    async def health(request: Request) -> dict[str, Any]:
        core = getattr(request.app.state, "core", None)
        ready = getattr(request.app.state, "ready", None)
        starting = core is None and ready is not None and not ready.is_set()
        return {
            "status": "ok" if core is not None else ("starting" if starting else "degraded"),
            "agent_ready": core is not None,
            "starting": starting,
            "error": getattr(request.app.state, "startup_error", ""),
            "time": timeutil.now_str(),
        }

    @app.get("/api/meta", response_model=MetaResponse)
    async def meta(request: Request) -> MetaResponse:
        core = getattr(request.app.state, "core", None)
        voice = getattr(request.app.state, "voice", None)
        return MetaResponse(
            model=core.llm.model if core else settings.model,
            base_url=settings.base_url,
            has_api_key=settings.has_api_key,
            db_path=str(tasks.db.path),
            tools=core.tool_catalog() if core else [],
            stats=tasks.stats(),
            mcp_server=core.mcp.server_info if core else {},
            voice=await voice.status() if voice else {},
        )

    # ------------------------------------------------------------------ #
    # 对话（SSE 流式）
    # ------------------------------------------------------------------ #
    @app.post("/api/chat")
    async def chat(request: Request, payload: ChatRequest) -> StreamingResponse:
        core = await require_core(request)

        async def event_stream() -> AsyncIterator[str]:
            try:
                async for event in core.chat(
                    payload.message,
                    payload.session_id,
                    temperature=payload.temperature,
                ):
                    yield _sse(event)
            except Exception as exc:  # noqa: BLE001 - 必须给前端一个明确的错误事件
                logger.exception("对话流异常")
                yield _sse({"type": "error", "message": f"{type(exc).__name__}: {exc}", "kind": "stream"})
            finally:
                yield _sse({"type": "end"})

        return StreamingResponse(event_stream(), media_type="text/event-stream", headers=SSE_HEADERS)

    @app.get("/api/chat/history")
    async def chat_history(
        session_id: str = Query(..., min_length=1),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        session = conversations.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"会话 {session_id} 不存在")
        return {
            "session": session,
            "messages": conversations.recent_messages(session_id, limit=limit),
        }

    @app.get("/api/sessions")
    async def list_sessions(limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
        return {"sessions": conversations.list_sessions(limit)}

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, Any]:
        if conversations.get_session(session_id) is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        conversations.delete_session(session_id)
        preferences.delete(f"_memory_watermark:{session_id}")
        return {"ok": True, "deleted": session_id}

    # ------------------------------------------------------------------ #
    # 任务 CRUD
    # ------------------------------------------------------------------ #
    @app.get("/api/tasks/today")
    async def tasks_today(request: Request, date: str | None = None) -> dict[str, Any]:
        target = timeutil.format_date(timeutil.parse_date(date) or timeutil.today())
        plan = await call_mcp(request, "plan_day", {"date": target})
        return {
            "date": target,
            "date_human": timeutil.humanize(target),
            "stats": tasks.stats(),
            "overdue": tasks.overdue(),
            "due_today": tasks.list_by_date(target, include_done=True),
            "plan": plan,
        }

    @app.get("/api/tasks", response_model=dict)
    async def list_tasks(
        status: str = Query(default="open"),
        priority: str | None = None,
        tag: str | None = None,
        keyword: str | None = None,
        only_overdue: bool = False,
        limit: int = Query(default=200, ge=1, le=500),
    ) -> dict[str, Any]:
        items = tasks.list(
            status=status,
            priority=priority,
            tag=tag,
            keyword=keyword,
            include_overdue_only=only_overdue,
            limit=limit,
        )
        return {"count": len(items), "tasks": items, "stats": tasks.stats()}

    @app.post("/api/tasks", response_model=TaskOut, status_code=201)
    async def create_task(payload: TaskCreate) -> dict[str, Any]:
        try:
            return tasks.create(
                payload.title,
                priority=payload.priority,
                status=payload.status,
                due_date=payload.due_date,
                tags=payload.tags,
                estimated_minutes=payload.estimated_minutes,
                notes=payload.notes,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/tasks/{task_id}", response_model=TaskOut)
    async def get_task(task_id: int) -> dict[str, Any]:
        task = tasks.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")
        return task

    @app.patch("/api/tasks/{task_id}", response_model=TaskOut)
    async def update_task(task_id: int, payload: TaskUpdate) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        for key in ("title", "priority", "status", "tags", "estimated_minutes", "notes"):
            value = getattr(payload, key)
            if value is not None:
                fields[key] = value
        if payload.clear_due_date:
            fields["due_date"] = None
        elif payload.due_date is not None:
            fields["due_date"] = payload.due_date

        try:
            updated = tasks.update(task_id, **fields) if fields else tasks.get(task_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if updated is None:
            raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")
        return updated

    @app.post("/api/tasks/{task_id}/complete", response_model=TaskOut)
    async def complete_task(task_id: int) -> dict[str, Any]:
        updated = tasks.complete(task_id)
        if updated is None:
            raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")
        return updated

    @app.delete("/api/tasks/{task_id}")
    async def delete_task(task_id: int) -> dict[str, Any]:
        if not tasks.delete(task_id):
            raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")
        return {"ok": True, "deleted": task_id}

    # ------------------------------------------------------------------ #
    # 只读能力（复用 MCP 工具）
    # ------------------------------------------------------------------ #
    @app.get("/api/priorities")
    async def priorities(request: Request, date: str | None = None, limit: int = 10) -> dict[str, Any]:
        return await call_mcp(request, "prioritize_tasks", {"date": date, "limit": limit})

    @app.get("/api/brief")
    async def brief(request: Request, date: str | None = None, city: str | None = None) -> dict[str, Any]:
        return await call_mcp(request, "generate_daily_brief", {"date": date, "city": city})

    @app.get("/api/weather")
    async def weather(
        request: Request,
        city: str | None = None,
        date: str | None = None,
        lat: float | None = None,
        lon: float | None = None,
    ) -> dict[str, Any]:
        """查天气。``lat``/``lon`` 同时给出时按坐标查（浏览器定位场景）。"""
        return await call_mcp(
            request,
            "get_weather",
            {"city": city, "date": date, "latitude": lat, "longitude": lon},
        )

    @app.get("/api/reminders")
    async def reminders(request: Request, within_hours: int = 24) -> dict[str, Any]:
        return await call_mcp(request, "get_due_reminders", {"within_hours": within_hours})

    @app.get("/api/preferences")
    async def get_preferences() -> dict[str, Any]:
        return {
            "preferences": {
                key: value
                for key, value in preferences.all().items()
                if not key.startswith("_")
            }
        }

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("未处理异常: %s", request.url.path)
        return JSONResponse(status_code=500, content={"detail": f"服务端异常：{exc}"})

    # ------------------------------------------------------------------ #
    # 静态前端（必须最后挂载，否则会抢占 /api 路由）
    # ------------------------------------------------------------------ #
    if WEB_DIR.exists():

        @app.get("/", include_in_schema=False, response_class=HTMLResponse)
        async def index_page() -> HTMLResponse:
            """动态注入资源版本号，让老缓存的 app.js / voice.js 立即失效。

            路由必须注册在 ``app.mount("/")`` **之前**，否则会被静态挂载抢走。
            """
            try:
                html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
            except OSError:  # pragma: no cover - 前端文件缺失
                return HTMLResponse("<h1>前端资源缺失</h1>", status_code=500)
            return HTMLResponse(
                html.replace("__ASSET_VERSION__", asset_version()),
                headers={"Cache-Control": "no-cache, must-revalidate"},
            )

        app.mount("/", FreshStaticFiles(directory=str(WEB_DIR), html=True), name="web")
    else:  # pragma: no cover
        logger.warning("未找到前端目录：%s", WEB_DIR)

    return app


def _sse(payload: dict[str, Any]) -> str:
    """把事件编码为 SSE 数据帧。"""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


app = create_app()


def main() -> None:
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )
    settings = get_settings()
    if not settings.has_api_key:
        logger.warning("%s", settings.missing_api_key_hint())
    logger.info("服务地址 http://%s:%s", settings.web_host, settings.web_port)
    uvicorn.run(
        "api.main:app",
        host=settings.web_host,
        port=settings.web_port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
