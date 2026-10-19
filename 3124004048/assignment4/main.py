"""CLI 交互入口：在终端里和「待办小助理」对话。

用法：
    python main.py                          # 进入交互式对话
    python main.py --once "帮我加个任务…"     # 单次提问后退出（便于脚本化演示）
    python main.py --session demo           # 指定会话 ID（用于延续上下文）

交互内可用指令：``/help`` ``/new`` ``/history`` ``/tools`` ``/stats`` ``/exit``
"""

import argparse
import asyncio
import json
import logging
import sys
import uuid
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.core import AgentCore  # noqa: E402
from agent.llm import LLMError  # noqa: E402
from common import timeutil  # noqa: E402
from common.config import get_settings  # noqa: E402

console = Console()

BANNER = r"""
   ______            __      __          ___          __          _
  /_  __/___  ____  / /___  / /_  ___   /   |  ____  / /______   (_)________  _____
   / / / __ \/ __ \/ __/ / / __ \/ _ \ / /| | / __ \/ __/ ___/  / / ___/ __ \/ ___/
  / / / /_/ / /_/ / /_/ /_/ /_/ /  __// ___ |/ / / / /_(__  )   / (__  ) /_/ / /
 /_/  \____/\____/\__/\__/_.___/\___//_/  |_/_/ /_/\__/____/   /_/____/\____/_/
"""

HELP_TEXT = """
[bold]可用指令[/bold]
  [cyan]/new[/cyan]      开启新会话（清空当前对话记忆）
  [cyan]/history[/cyan]  查看当前会话的消息轨迹
  [cyan]/tools[/cyan]    查看已加载的 MCP 工具清单
  [cyan]/stats[/cyan]    查看任务统计
  [cyan]/help[/cyan]     显示本帮助
  [cyan]/exit[/cyan]     退出

[bold]试试这些说法[/bold]
  · 帮我记一下：明天下午3点前完成智能体作业文档，高优先级
  · 今天我先做哪个？帮我排个日程
  · 今天杭州天气怎么样，适合出门办事吗
  · 把今天的清单导出成文件
  · 以后问天气默认用上海
"""


def _configure_logging(verbose: bool) -> None:
    """日志默认只输出警告，避免刷屏；--verbose 时打开调试信息。"""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        stream=sys.stderr,
    )


async def _render_events(core: AgentCore, text: str, session_id: str) -> str:
    """消费 Agent 事件流并在终端渲染，返回最终回复文本。"""
    printed_any = False
    final_text = ""

    async for event in core.chat(text, session_id):
        kind = event["type"]

        if kind == "text_delta":
            console.print(event["text"], end="", markup=False, highlight=False, soft_wrap=True)
            printed_any = True

        elif kind == "tool_call":
            args = json.dumps(event["arguments"], ensure_ascii=False)
            if len(args) > 160:
                args = args[:160] + "…"
            console.print(f"  [dim]⚙ 调用工具[/dim] [magenta]{event['name']}[/magenta][dim]({args})[/dim]")

        elif kind == "tool_result":
            icon = "[green]✓[/green]" if event["ok"] else "[red]✗[/red]"
            color = "green" if event["ok"] else "red"
            console.print(
                f"  {icon} [dim]{event['name']}[/dim] "
                f"[{color}]{event['summary']}[/{color}] [dim]({event['duration_ms']}ms)[/dim]"
            )

        elif kind == "final":
            final_text = event["text"]
            if printed_any:
                console.print()
            console.print()
            console.print(Panel(Markdown(final_text), border_style="cyan", title="[bold]待办小助理[/bold]"))

        elif kind == "error":
            if printed_any:
                console.print()
            console.print(Panel(f"[red]{event['message']}[/red]", border_style="red", title="出错了"))

        elif kind == "done" and event.get("usage"):
            usage = event["usage"]
            console.print(
                f"[dim]· 本轮 {event['turns']} 次模型调用 · {event['elapsed_ms']}ms · "
                f"tokens {usage.get('total_tokens', '-')} · 模型 {event['model']}[/dim]"
            )

    return final_text


async def _print_tools(core: AgentCore) -> None:
    table = Table(title="已加载的 MCP 工具", show_lines=False, header_style="bold cyan")
    table.add_column("#", style="dim", width=3)
    table.add_column("工具名", style="magenta", no_wrap=True)
    table.add_column("作用")
    for index, item in enumerate(core.tool_catalog(), start=1):
        table.add_row(str(index), item["name"], item["description"])
    console.print(table)


async def _print_stats(core: AgentCore) -> None:
    raw = await core.mcp.call_tool("task_stats", {})
    payload = json.loads(raw)
    stats = payload.get("stats", {})
    table = Table(title="任务统计", header_style="bold cyan")
    table.add_column("指标", style="cyan")
    table.add_column("数量", justify="right")
    labels = [
        ("全部", "total"), ("待办", "todo"), ("进行中", "doing"),
        ("已完成", "done"), ("未完成", "open"), ("已逾期", "overdue"),
        ("今日完成", "done_today"),
    ]
    for label, key in labels:
        table.add_row(label, str(stats.get(key, 0)))
    console.print(table)


async def _print_history(core: AgentCore, session_id: str) -> None:
    history = core.memory.session_history(session_id)
    if not history:
        console.print("[dim]当前会话还没有消息。[/dim]")
        return
    console.print(f"[bold]会话 {session_id} 共 {len(history)} 条消息[/bold]")
    for message in history:
        role = message.get("role")
        if role == "tool":
            continue
        content = (message.get("content") or "").strip()
        if not content:
            continue
        color = "green" if role == "user" else "cyan"
        name = "你" if role == "user" else "助理"
        console.print(f"  [{color}]{name}[/{color}]：{content[:160]}")


async def run_once(core: AgentCore, text: str, session_id: str) -> int:
    console.print(f"[green]你[/green] › {text}")
    await _render_events(core, text, session_id)
    return 0


async def run_repl(core: AgentCore, session_id: str) -> int:
    console.print(BANNER, style="bold cyan", highlight=False)
    console.print(
        Panel(
            f"当前时间 {timeutil.now().strftime('%Y-%m-%d %H:%M')} · "
            f"模型 [magenta]{core.llm.model}[/magenta] · "
            f"MCP 工具 [magenta]{len(core.mcp.tools)}[/magenta] 个 · "
            f"会话 [magenta]{session_id}[/magenta]\n输入 [cyan]/help[/cyan] 查看指令，"
            "[cyan]/exit[/cyan] 退出。",
            border_style="cyan",
        )
    )

    while True:
        try:
            raw = await asyncio.to_thread(console.input, "\n[bold green]你 ›[/bold green] ")
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]再见！[/dim]")
            return 0

        text = (raw or "").strip()
        if not text:
            continue

        command = text.lower()
        if command in ("/exit", "/quit", "/q"):
            console.print("[dim]再见！[/dim]")
            return 0
        if command in ("/help", "/h", "?"):
            console.print(HELP_TEXT)
            continue
        if command == "/new":
            session_id = uuid.uuid4().hex[:12]
            console.print(f"[dim]已开启新会话：{session_id}[/dim]")
            continue
        if command == "/tools":
            await _print_tools(core)
            continue
        if command == "/stats":
            await _print_stats(core)
            continue
        if command == "/history":
            await _print_history(core, session_id)
            continue
        if command.startswith("/"):
            console.print(f"[yellow]未知指令：{command}（输入 /help 查看可用指令）[/yellow]")
            continue

        try:
            await _render_events(core, text, session_id)
        except LLMError as exc:
            console.print(Panel(f"[red]{exc}[/red]", border_style="red", title="调用失败"))
        except Exception as exc:  # noqa: BLE001 - CLI 不应因单轮失败而退出
            console.print(Panel(f"[red]{type(exc).__name__}: {exc}[/red]", border_style="red", title="异常"))


async def async_main(args: argparse.Namespace) -> int:
    settings = get_settings()
    if not settings.has_api_key:
        console.print(Panel(f"[yellow]{settings.missing_api_key_hint()}[/yellow]", border_style="yellow", title="缺少配置"))

    session_id = args.session or uuid.uuid4().hex[:12]
    console.print("[dim]正在启动 MCP Server …[/dim]")
    try:
        core = await AgentCore.create()
    except Exception as exc:  # noqa: BLE001
        console.print(Panel(f"[red]启动失败：{exc}[/red]", border_style="red"))
        return 1

    try:
        if args.once:
            return await run_once(core, args.once, session_id)
        return await run_repl(core, session_id)
    finally:
        await core.aclose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="todo-assistant",
        description="待办管理智能体（MCP + DeepSeek）—— 会说也会做的中文任务助理",
    )
    parser.add_argument("--once", metavar="TEXT", help="只处理一条指令后退出，便于脚本化演示")
    parser.add_argument("--session", metavar="ID", help="指定会话 ID，用于延续多轮上下文")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    _configure_logging(args.verbose)
    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        console.print("\n[dim]已中断。[/dim]")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
