"""MCP Server 入口：以 stdio 传输暴露「待办管理」工具能力。

运行方式：

    python -m mcp_server.server          # 供 Agent / Claude Desktop 等 MCP 客户端连接

⚠ 重要约束：**stdout 是 JSON-RPC 协议通道**，任何 ``print()`` 都会污染协议流
并导致客户端解析失败。因此本模块所有日志一律写入 stderr / 日志文件。

独立性保证：本 Server 不依赖 Agent 与大模型，可被任意 MCP 客户端
（Claude Desktop、MCP Inspector、自研客户端）直接复用与验证。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from mcp_server.tools import register_all

PROJECT_ROOT = Path(__file__).resolve().parent.parent

INSTRUCTIONS = """你是一个「待办管理」能力服务端，通过工具向调用方提供确定性的任务管理能力。

能力范围：
- 任务增删改查（tasks 工具组）
- 优先级打分、日程编排、每日简报、清单导出（planner 工具组）
- 天气查询与出行建议（weather 工具组）
- 临期提醒与桌面通知（reminders 工具组）
- 用户长期偏好读写（prefs 工具组）

使用建议：
1. 数据库是任务状态的唯一事实来源，做任何判断前先用 list_tasks 查询真实数据。
2. add_task 的 due_date 参数支持中文口语时间（如「明天下午3点」「下周一」）。
3. delete_task / delete_tasks 需要显式 confirm=true，先向用户确认再删除。
4. 所有工具返回 JSON 字符串，通过 ok 字段判断成败；失败时 error 字段是给用户看的原因。
"""

logger = logging.getLogger(__name__)


def build_server() -> FastMCP:
    """构建并返回注册了全部工具的 FastMCP 实例。"""
    mcp = FastMCP(name="todo-assistant", instructions=INSTRUCTIONS)
    register_all(mcp)
    return mcp


def configure_logging(level: int = logging.INFO) -> None:
    """把日志固定到 stderr，避免污染 stdout 上的 JSON-RPC 协议流。"""
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def _load_env() -> None:
    """加载项目根目录的 .env（缺失时静默跳过）。"""
    try:
        from dotenv import load_dotenv
    except ImportError:  # 未安装 python-dotenv 时仅依赖系统环境变量
        return
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path)
        logger.info("已加载环境变量文件: %s", env_path)


def main() -> None:
    configure_logging()
    _load_env()
    mcp = build_server()
    logger.info("todo-assistant MCP Server 启动（stdio），工作目录: %s", PROJECT_ROOT)
    mcp.run()  # 默认 stdio 传输


if __name__ == "__main__":
    main()
