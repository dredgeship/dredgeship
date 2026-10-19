"""MCP 客户端：以 stdio 拉起 MCP Server，并把工具暴露给大模型。

## 为什么用「独占任务（actor）模式」而不是直接持有 session

``mcp`` 的 ``stdio_client`` 内部依赖 anyio 的 task group，而 task group 的
cancel scope **必须在创建它的那个任务里退出**。在 Web 服务里，启动时建的
session 往往被不同请求任务使用，直接跨任务 close 会抛
``Attempted to exit cancel scope in a different task``。

因此这里启动一个专属的 asyncio 任务持有 session，所有调用通过队列投递，
调用方只 ``await`` 一个 Future。好处是：

* 生命周期干净：session 只在自己的任务里创建与销毁；
* 天然串行：MCP 的 JSON-RPC 请求不会交叉，输出可预期；
* 复用高效：整个进程只拉一次子进程、只做一次 ``list_tools()``。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from common.config import PROJECT_ROOT, get_settings

logger = logging.getLogger(__name__)

__all__ = ["ToolSpec", "MCPToolClient", "ToolInvocationError"]

DEFAULT_CALL_TIMEOUT = 120.0


class ToolInvocationError(RuntimeError):
    """工具调用失败（服务端返回 isError，或链路异常）。"""


@dataclass(slots=True)
class ToolSpec:
    """MCP 工具的轻量描述，用于转换为 OpenAI 函数声明。"""

    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)

    def to_openai_tool(self) -> dict[str, Any]:
        """转换为 OpenAI Chat Completions 的 tools 条目。"""
        schema = _sanitize_schema(self.input_schema)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description or self.name,
                "parameters": schema,
            },
        }


@dataclass(slots=True)
class _Request:
    name: str | None
    arguments: dict[str, Any]
    future: asyncio.Future


class MCPToolClient:
    """MCP Server 的客户端封装（进程内复用单一连接）。"""

    def __init__(
        self,
        command: str | None = None,
        args: Sequence[str] | None = None,
        *,
        env: dict[str, str] | None = None,
        cwd: str | Path | None = None,
        call_timeout: float = DEFAULT_CALL_TIMEOUT,
    ) -> None:
        settings = get_settings()
        default_command = sys.executable
        default_args = ["-m", "mcp_server.server"]
        if settings.mcp_server_command:
            # 允许改用已安装到 PATH 的启动命令
            default_command, default_args = settings.mcp_server_command, ["-m", "mcp_server.server"]

        self.command = command or default_command
        self.args = list(args or default_args)
        self.cwd = str(cwd or PROJECT_ROOT)
        self.call_timeout = call_timeout
        self.env = self._build_env(env)

        self._tools: list[ToolSpec] = []
        self._queue: asyncio.Queue[_Request | None] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._fatal: BaseException | None = None
        self._server_info: dict[str, Any] = {}
        self._errlog_path: Path | None = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    @property
    def tools(self) -> list[ToolSpec]:
        return list(self._tools)

    @property
    def server_info(self) -> dict[str, Any]:
        return dict(self._server_info)

    @property
    def errlog_path(self) -> Path | None:
        return self._errlog_path

    async def start(self, timeout: float = 60.0) -> None:
        """启动 MCP Server 子进程并完成握手。"""
        if self._task is not None and not self._task.done():
            return
        self._ready.clear()
        self._fatal = None
        self._task = asyncio.create_task(self._run(), name="mcp-client-owner")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise ToolInvocationError(f"启动 MCP Server 超时（{timeout:g} 秒）") from exc
        if self._fatal is not None:
            raise ToolInvocationError(f"MCP Server 启动失败：{self._fatal}") from self._fatal
        logger.info("MCP 连接就绪，已加载 %s 个工具", len(self._tools))

    async def aclose(self) -> None:
        """优雅关闭：先请 owner 退出，再回收子进程。"""
        if self._task is None:
            return
        await self._queue.put(None)
        try:
            await asyncio.wait_for(self._task, timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning("MCP owner 任务未及时退出，强制取消")
            self._task.cancel()
        except Exception as exc:  # noqa: BLE001
            logger.warning("MCP owner 任务退出异常：%s", exc)
        finally:
            self._task = None
            self._tools = []

    async def __aenter__(self) -> "MCPToolClient":
        await self.start()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ #
    # 工具接口
    # ------------------------------------------------------------------ #
    async def list_tools(self) -> list[ToolSpec]:
        """返回工具清单（启动时已缓存，不做网络往返）。"""
        if not self._tools:
            await self.start()
        return list(self._tools)

    def openai_tools(self) -> list[dict[str, Any]]:
        """转换为 OpenAI ``tools`` 参数格式。"""
        return [spec.to_openai_tool() for spec in self._tools]

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> str:
        """调用工具并返回文本结果。

        Raises:
            ToolInvocationError: 链路不可用、超时或服务端返回错误。
        """
        if self._task is None or self._task.done():
            await self.start()

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        await self._queue.put(_Request(name=name, arguments=arguments or {}, future=future))
        try:
            return await asyncio.wait_for(future, timeout=timeout or self.call_timeout)
        except asyncio.TimeoutError as exc:
            raise ToolInvocationError(f"工具 {name} 执行超时（超过 {timeout or self.call_timeout:g} 秒）") from exc

    # ------------------------------------------------------------------ #
    # owner 任务
    # ------------------------------------------------------------------ #
    async def _run(self) -> None:
        error: BaseException | None = None
        errlog = self._open_errlog()
        try:
            params = StdioServerParameters(
                command=self.command,
                args=self.args,
                env=self.env,
                cwd=self.cwd,
            )
            async with stdio_client(params, errlog=errlog) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    init_result = await session.initialize()
                    server = getattr(init_result, "serverInfo", None)
                    self._server_info = {
                        "name": getattr(server, "name", "todo-assistant"),
                        "version": getattr(server, "version", ""),
                    }
                    listed = await session.list_tools()
                    self._tools = [
                        ToolSpec(
                            name=tool.name,
                            description=tool.description or "",
                            input_schema=dict(tool.inputSchema or {}),
                        )
                        for tool in listed.tools
                    ]
                    self._ready.set()

                    while True:
                        request = await self._queue.get()
                        if request is None:
                            break
                        if request.future.cancelled():
                            continue
                        try:
                            result = await session.call_tool(request.name or "", request.arguments)
                            text = _extract_text(result)
                            if getattr(result, "isError", False):
                                request.future.set_exception(ToolInvocationError(text))
                            else:
                                request.future.set_result(text)
                        except Exception as exc:  # noqa: BLE001 - 单次调用失败不影响连接
                            logger.warning("工具 %s 调用失败：%s", request.name, exc)
                            if not request.future.done():
                                request.future.set_exception(exc)
        except BaseException as exc:  # noqa: BLE001 - 记录并唤醒等待者
            error = exc
            self._fatal = exc
            logger.error("MCP 连接中断：%s", exc)
        finally:
            self._ready.set()
            self._fail_pending(error)
            try:
                errlog.close()
            except Exception:  # noqa: BLE001 - 关闭日志失败无需关心
                pass

    def _open_errlog(self):
        """打开 MCP Server 的 stderr 落点。

        Windows 控制台默认使用 GBK，直接复用 ``sys.stderr`` 会把子进程输出的
        UTF-8 中文显示成乱码；写入 UTF-8 日志文件既避免乱码，也便于排查问题。
        """
        try:
            log_dir = PROJECT_ROOT / ".data" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            self._errlog_path = log_dir / "mcp-server.log"
            return open(self._errlog_path, "a", encoding="utf-8", buffering=1)
        except OSError:
            self._errlog_path = None
            return sys.stderr

    def _fail_pending(self, error: BaseException | None) -> None:
        """把队列中排队的请求全部置为失败，避免调用方永久挂起。"""
        reason = error or ToolInvocationError("MCP 连接已关闭")
        while True:
            try:
                request = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if request is None:
                continue
            if not request.future.done():
                request.future.set_exception(reason)

    # ------------------------------------------------------------------ #
    # 环境变量
    # ------------------------------------------------------------------ #
    def _build_env(self, extra: dict[str, str] | None) -> dict[str, str]:
        env = dict(os.environ)
        # Windows 下强制 UTF-8，避免中文在管道中乱码
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env.setdefault("PYTHONUNBUFFERED", "1")
        env["TODO_DB_PATH"] = get_settings().db_path
        if extra:
            env.update(extra)
        return env


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def _extract_text(result: Any) -> str:
    """从 CallToolResult 中抽取文本内容。"""
    blocks = getattr(result, "content", None) or []
    chunks: list[str] = []
    for block in blocks:
        text = getattr(block, "text", None)
        if text is not None:
            chunks.append(str(text))
        else:  # 非文本块（如图片）退化为等价的 JSON 描述
            chunks.append(str(block))
    return "\n".join(chunks) if chunks else ""


def _sanitize_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """补全并清洗 JSON Schema，保证被各大 OpenAI 兼容服务接受。

    主要处理两件事：确保根节点是 object；递归剔除 ``$defs``/``title`` 等
    个别服务端不接受的装饰字段（保留 ``$defs`` 会因缺少 ``$ref`` 解析而报错）。
    """
    if not isinstance(schema, dict) or not schema:
        return {"type": "object", "properties": {}}

    cleaned = _strip_keys(schema, drop={"title", "$defs", "definitions", "additionalProperties"})
    cleaned.setdefault("type", "object")
    cleaned.setdefault("properties", {})
    return cleaned


def _strip_keys(node: Any, drop: set[str]) -> Any:
    if isinstance(node, dict):
        return {k: _strip_keys(v, drop) for k, v in node.items() if k not in drop}
    if isinstance(node, list):
        return [_strip_keys(item, drop) for item in node]
    return node
