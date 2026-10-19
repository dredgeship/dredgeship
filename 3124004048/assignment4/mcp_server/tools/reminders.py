"""提醒工具组：临期任务提醒 + 桌面通知（可选依赖，缺失时优雅降级）。

提醒策略上做了一个刻意的取舍：**不接入短信/邮件/微信等外部推送通道**。
原因是一旦引入推送就必须依赖第三方账号与密钥，作业演示的可复现性会变差。
因此改为「三级提醒」：

1. 终端 / 对话中的文字提醒（永远可用）；
2. 网页界面里的到期高亮与提示条（由前端读取任务数据实现）；
3. Windows 桌面 Toast 通知（安装了 ``plyer`` 才启用，否则静默降级）。
"""

import logging
import platform
from typing import Any

from common import timeutil
from mcp_server.tools.base import fail, ok, task_repo, tool

logger = logging.getLogger(__name__)

__all__ = ["register"]


def register(mcp: Any) -> None:
    @tool(mcp)
    def get_due_reminders(
        within_hours: int = 24,
        include_overdue: bool = True,
    ) -> str:
        """获取临近截止的任务提醒（含已逾期）。

        用户问「有什么快到期的」「提醒我一下」「今天还有什么要交」时调用。

        Args:
            within_hours: 提前提醒的时间窗口（小时），默认 24。
            include_overdue: 是否包含已经逾期的任务，默认 True。
        """
        repo = task_repo()
        upcoming = repo.due_within(within_hours)
        if not include_overdue:
            upcoming = [t for t in upcoming if not t["overdue"]]

        if not upcoming:
            return ok(
                message=f"未来 {within_hours} 小时内没有临近截止的任务",
                count=0,
                reminders=[],
            )

        overdue = [t for t in upcoming if t["overdue"]]
        soon = [t for t in upcoming if not t["overdue"]]

        lines: list[str] = []
        if overdue:
            lines.append(f"⚠ 已逾期 {len(overdue)} 条：")
            lines.extend(f"  · #{t['id']} {t['title']}（原定 {t['due_human']}）" for t in overdue)
        if soon:
            lines.append(f"⏰ {within_hours} 小时内到期 {len(soon)} 条：")
            lines.extend(f"  · #{t['id']} {t['title']}（{t['due_human']}）" for t in soon)

        return ok(
            message="\n".join(lines),
            count=len(upcoming),
            overdue_count=len(overdue),
            upcoming_count=len(soon),
            reminders=upcoming,
            within_hours=within_hours,
        )

    @tool(mcp)
    def send_desktop_notification(
        title: str = "待办提醒",
        message: str = "",
    ) -> str:
        """发送一条系统桌面通知（Windows Toast）。仅在用户明确要求弹窗提醒时使用。

        若运行环境未安装可选依赖 ``plyer``，会自动降级为日志输出并如实告知用户，
        不会报错。

        Args:
            title: 通知标题。
            message: 通知正文。
        """
        text = (message or "").strip()
        if not text:
            return fail("通知内容不能为空")
        delivered = _send_toast(title, text)
        if delivered:
            return ok(message=f"已发送桌面通知：{title}", delivered=True)
        return ok(
            message=(
                "当前环境不支持桌面弹窗（需安装可选依赖 plyer，且仅桌面系统可用），"
                f"已改为文字提醒：{title} - {text}"
            ),
            delivered=False,
            fallback_text=f"{title}：{text}",
        )

    @tool(mcp)
    def reminder_overview() -> str:
        """一次性给出提醒全景：逾期、今日到期、未来三天到期，并标注当前时间。"""
        repo = task_repo()
        overdue = repo.overdue()
        today_tasks = [
            t for t in repo.list_by_date(timeutil.today_str(), include_done=False)
            if not t["overdue"]
        ]
        three_days = [t for t in repo.due_within(72) if not t["overdue"]]

        lines = [f"当前时间：{timeutil.humanize(timeutil.now(), ref=timeutil.now())}"]
        lines.append(f"逾期：{len(overdue)} 条" + (f"，最急的是「{overdue[0]['title']}」" if overdue else ""))
        lines.append(f"今日到期：{len(today_tasks)} 条")
        lines.append(f"未来三天到期：{len(three_days)} 条")
        return ok(
            message="\n".join(lines),
            now=timeutil.now_str(),
            overdue=overdue,
            due_today=today_tasks,
            due_in_three_days=three_days,
        )


# --------------------------------------------------------------------------- #
# 桌面通知（可选）
# --------------------------------------------------------------------------- #
def _send_toast(title: str, message: str) -> bool:
    """尝试发送桌面通知；任何不可用情况都返回 ``False``（降级）。"""
    if platform.system() != "Windows":
        logger.info("非 Windows 平台，跳过桌面通知")
        return False
    try:
        from plyer import notification  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 - 可选依赖
        logger.info("未安装 plyer，桌面通知降级为文字提醒")
        return False
    try:
        notification.notify(title=title[:64], message=message[:256], timeout=10, app_name="待办助理")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("桌面通知发送失败：%s", exc)
        return False
