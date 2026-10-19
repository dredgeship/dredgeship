"""规划工具组：优先级打分、日程编排、每日简报、清单导出。

这里的算法都是**可解释的确定性规则**，不是黑盒：

排序分（0~100）由四部分构成 ——
    优先级权重(≤40) + 紧急度(≤45) + 进行中加成(6) + 高优临期组合加成(8) - 超长任务惩罚(3)

之所以不用模型直接排序，是因为「重要 × 紧急」这类判断需要稳定、可复现、
且能向用户解释理由（返回值里带 ``reasons``），否则同一批任务每次排序结果都可能不同。
"""

import re
from datetime import date as _date
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal

from common import timeutil
from mcp_server.tools.base import fail, ok, preference_repo, task_repo, tool

__all__ = ["register"]

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
EXPORT_DIR = PROJECT_ROOT / "exports"

_PRIORITY_WEIGHT = {"high": 40, "medium": 25, "low": 10}


def register(mcp: Any) -> None:
    # ------------------------------------------------------------------ #
    # 优先级排序
    # ------------------------------------------------------------------ #
    @tool(mcp)
    def prioritize_tasks(
        date: str | None = None,
        limit: int = 10,
        include_no_due: bool = True,
    ) -> str:
        """对未完成任务打分并排出「先做哪件」的顺序，返回分数与理由。

        用户问「先做哪个」「帮我排个优先级」「哪件最急」时调用。

        Args:
            date: 以该日期为「现在」计算紧急度，默认今天。
            limit: 返回条数上限，默认 10。
            include_no_due: 是否把没有截止时间的任务也纳入排序，默认 True。
        """
        reference = _reference_moment(date)
        repo = task_repo()
        candidates = repo.list(status="open", limit=500)
        if not include_no_due:
            candidates = [t for t in candidates if t["due_date"]]

        ranked = [_rank(task, reference) for task in candidates]
        ranked.sort(key=lambda item: item["score"], reverse=True)
        ranked = ranked[: max(1, int(limit))]

        if not ranked:
            return ok(message="目前没有未完成的任务，可以先休息一下", ranked=[])

        top = ranked[0]
        return ok(
            message=f"共 {len(ranked)} 条待办参与排序，最优先是「{top['title']}」（{top['score']} 分）",
            reference_time=timeutil.format_dt(reference),
            ranked=ranked,
        )

    # ------------------------------------------------------------------ #
    # 日程编排
    # ------------------------------------------------------------------ #
    @tool(mcp)
    def plan_day(
        date: str | None = None,
        start_time: str = "09:00",
        end_time: str = "18:00",
        lunch_start: str = "12:00",
        lunch_end: str = "13:30",
        break_minutes: int = 10,
        include_backlog: bool = True,
        max_tasks: int = 12,
        auto_start: bool = True,
    ) -> str:
        """把任务排进某一天的可用时间段，生成带起止时间的时间轴日程。

        用户说「帮我安排今天/明天的日程」「这一天的计划怎么排」时调用。

        Args:
            date: 目标日期，默认今天。支持「明天」「2026-10-16」。
            start_time: 当天可开始工作的时间，默认 09:00。
            end_time: 当天停止工作的时间，默认 18:00。
            lunch_start / lunch_end: 午休时段，编排时会自动跳过。
            break_minutes: 每个任务之间预留的缓冲时间，默认 10 分钟。
            include_backlog: 是否把「无截止时间的积压任务」也排进来，默认 True。
            max_tasks: 当天最多安排的任务条数，默认 12。
            auto_start: 若排的是今天且 09:00 已过，是否自动从当前时间之后起排，
                默认 True。避免排出"已经过去的时间段"这种无用日程。
        """
        target_day = timeutil.parse_date(date) or timeutil.today()
        repo = task_repo()

        day_start = _at(target_day, start_time)
        day_end = _at(target_day, end_time)
        if day_end <= day_start:
            return fail("end_time 必须晚于 start_time")

        auto_adjusted = False
        if auto_start and target_day == timeutil.today():
            now_moment = timeutil.now()
            if day_start < now_moment:
                day_start = _ceil_to_ten_minutes(now_moment)
                auto_adjusted = day_start < day_end
                if not auto_adjusted:
                    return ok(
                        message=(
                            f"今天可用的时间已经用完了（当前 {now_moment:%H:%M}，"
                            f"工作时段截止 {end_time}）。建议改为安排明天。"
                        ),
                        date=timeutil.format_date(target_day),
                        timeline=[],
                        unscheduled=[],
                        exhausted=True,
                    )

        lunch = _window(_at(target_day, lunch_start), _at(target_day, lunch_end))
        free_segments = _subtract([_window(day_start, day_end)], lunch)
        initial_segments = list(free_segments)  # 用于计算可用容量（与后续排布无关）

        overdue = repo.overdue()
        due_today = [
            t for t in repo.list_by_date(timeutil.format_date(target_day), include_done=False)
            if not t["overdue"]
        ]
        # 合并去重（逾期的可能同时出现在当天列表里）
        selected: dict[int, dict[str, Any]] = {int(t["id"]): t for t in overdue}
        for task in due_today:
            selected.setdefault(int(task["id"]), task)

        backlog: list[dict[str, Any]] = []
        if include_backlog and len(selected) < max_tasks:
            scheduled_ids = set(selected)
            backlog = [
                t for t in repo.list(status="open", limit=200)
                if int(t["id"]) not in scheduled_ids and not t["due_date"]
            ]
            backlog.sort(key=lambda t: (-_PRIORITY_WEIGHT.get(t["priority"], 25), t["id"]))
            for task in backlog[: max(0, int(max_tasks) - len(selected))]:
                selected[int(task["id"])] = task

        reference = _reference_moment(date)
        ranked = sorted(
            (_rank(t, reference) for t in selected.values()),
            key=lambda item: item["score"],
            reverse=True,
        )

        timeline: list[dict[str, Any]] = []
        unscheduled: list[dict[str, Any]] = []
        total_minutes = 0
        for item in ranked:
            duration = max(5, int(item["estimated_minutes"] or 30))
            due_dt = timeutil.parse_datetime(item["due_date"]) if item["due_date"] else None
            slot = _place(free_segments, duration, due_dt)
            if slot is None:
                reason = (
                    f"截止时间 {timeutil.humanize(item['due_date'])} 之前已无可用时段"
                    if due_dt
                    else "当天可用时间已被排满"
                )
                unscheduled.append({**item, "reason": reason})
                continue

            start_dt, end_dt = slot
            free_segments = _subtract(free_segments, _window(start_dt, end_dt))
            if break_minutes > 0:
                gap = timedelta(minutes=int(break_minutes))
                free_segments = _subtract(
                    free_segments,
                    _window(end_dt, min(end_dt + gap, day_end)),
                )
            total_minutes += duration
            timeline.append(
                {
                    **item,
                    "start": timeutil.format_dt(start_dt),
                    "end": timeutil.format_dt(end_dt),
                    "start_time": start_dt.strftime("%H:%M"),
                    "end_time": end_dt.strftime("%H:%M"),
                    "before_due": bool(due_dt and end_dt <= due_dt),
                }
            )

        # 可用容量取所有空闲区间的实际总长。不能简单用「工作时长 - 午休时长」：
        # 当起排时间晚于午休（例如下午 5 点才开始排）时，午休根本不在窗口内，
        # 相减会得到负数并让利用率恒为 0。
        capacity = sum(
            int((seg_end - seg_start).total_seconds() // 60)
            for seg_start, seg_end in initial_segments
        )
        utilization = round(total_minutes / capacity * 100, 1) if capacity > 0 else 0.0

        message = (
            f"{timeutil.humanize(target_day)}共安排 {len(timeline)} 条任务，"
            f"预计占用 {total_minutes} 分钟（约占可用时间的 {utilization}%）"
        )
        if auto_adjusted:
            message += f"；已按当前时间从 {day_start:%H:%M} 起排"
        if unscheduled:
            message += f"；另有 {len(unscheduled)} 条无法排入"
        return ok(
            message=message,
            date=timeutil.format_date(target_day),
            timeline=timeline,
            unscheduled=unscheduled,
            total_minutes=total_minutes,
            capacity_minutes=capacity,
            utilization_percent=utilization,
            lunch_break=f"{lunch_start}-{lunch_end}",
            day_start=timeutil.format_dt(day_start),
            auto_adjusted=auto_adjusted,
        )

    # ------------------------------------------------------------------ #
    # 每日简报
    # ------------------------------------------------------------------ #
    @tool(mcp)
    def generate_daily_brief(
        date: str | None = None,
        city: str | None = None,
        include_weather: bool = True,
    ) -> str:
        """生成当天的工作简报：天气、逾期提醒、优先任务、日程概要。

        用户说「今天有什么安排」「看看今天的计划」「早上好」时调用。

        Args:
            date: 目标日期，默认今天。
            city: 查询天气的城市，不填则使用用户偏好中的默认城市。
            include_weather: 是否附带天气与出行建议。
        """
        from mcp_server.tools.weather import fetch_weather

        target_day = timeutil.parse_date(date) or timeutil.today()
        day_text = timeutil.format_date(target_day)
        repo = task_repo()

        overdue = repo.overdue()
        due_today = [t for t in repo.list_by_date(day_text, include_done=False) if not t["overdue"]]
        stats = repo.stats()

        reference = _reference_moment(day_text)
        ranked = sorted(
            (_rank(t, reference) for t in repo.list(status="open", limit=200)),
            key=lambda item: item["score"],
            reverse=True,
        )[:5]

        weather_info: dict[str, Any] | None = None
        if include_weather:
            resolved_city = city or preference_repo().get("default_city") or None
            try:  # 天气失败不能影响简报主流程
                weather_info = {"ok": True, **fetch_weather(city=resolved_city, date=day_text)}
            except Exception as exc:  # noqa: BLE001
                weather_info = {"ok": False, "error": str(exc)}

        lines: list[str] = [f"# {timeutil.humanize(target_day)} 工作简报", ""]
        if weather_info:
            if weather_info.get("ok"):
                lines.append(
                    f"**天气**：{weather_info['city']} {weather_info['date']} "
                    f"{weather_info['description']}，"
                    f"{weather_info['temperature_min']}~{weather_info['temperature_max']}℃，"
                    f"降水概率 {weather_info['precipitation_probability']}%，"
                    f"风速 {weather_info['wind_speed_max']} km/h"
                )
                lines.append(f"**出行建议**：{weather_info['advice']}")
            else:
                lines.append(f"**天气**：暂时查不到（{weather_info.get('error', '未知原因')}）")
            lines.append("")

        if overdue:
            lines.append(f"**逾期 {len(overdue)} 条，建议优先处理**：")
            lines.extend(f"- #{t['id']} {t['title']}（原定 {t['due_human']}）" for t in overdue[:5])
            lines.append("")
        if due_today:
            lines.append(f"**{timeutil.humanize(target_day)}到期 {len(due_today)} 条**：")
            lines.extend(f"- #{t['id']} {t['title']}（{t['due_human']}）" for t in due_today[:5])
            lines.append("")
        if ranked:
            lines.append("**建议优先顺序**：")
            lines.extend(
                f"{index}. {item['title']}（{item['score']} 分：{'、'.join(item['reasons'])}）"
                for index, item in enumerate(ranked, start=1)
            )
            lines.append("")
        lines.append(
            f"**总体进度**：共 {stats['total']} 条，未完成 {stats['open']} 条，"
            f"逾期 {stats['overdue']} 条，今日已完成 {stats['done_today']} 条。"
        )
        if not overdue and not due_today and not ranked:
            lines.append("今天没有待办事项，可以安排一些自我提升的时间。")

        brief = "\n".join(lines)
        return ok(
            message=f"已生成{timeutil.humanize(target_day)}简报",
            date=day_text,
            brief=brief,
            weather=weather_info,
            overdue=overdue,
            due_today=due_today,
            top_priorities=ranked,
            stats=stats,
        )

    # ------------------------------------------------------------------ #
    # 清单导出
    # ------------------------------------------------------------------ #
    @tool(mcp)
    def export_checklist(
        date: str | None = None,
        fmt: Literal["md", "txt"] = "md",
        include_done: bool = False,
        filename: str | None = None,
    ) -> str:
        """把某天的任务清单导出为 Markdown / 纯文本文件，返回文件绝对路径。

        用户说「把今天的清单导出成文件」「生成待办清单文件」时调用。

        Args:
            date: 清单对应的日期，默认今天。
            fmt: 导出格式，md 或 txt。
            include_done: 是否包含已完成任务。
            filename: 自定义文件名（不含路径与扩展名），默认自动生成。
        """
        target_day = timeutil.parse_date(date) or timeutil.today()
        repo = task_repo()
        tasks = repo.list_by_date(timeutil.format_date(target_day), include_done=include_done)
        if not include_done:
            tasks = [t for t in tasks if t["status"] != "done"]

        safe_name = _safe_filename(filename) or f"待办清单_{timeutil.format_date(target_day)}"
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        path = EXPORT_DIR / f"{safe_name}.{fmt}"
        content = _render_checklist(target_day, tasks, fmt)
        path.write_text(content, encoding="utf-8")

        total_minutes = sum(int(t["estimated_minutes"] or 0) for t in tasks)
        return ok(
            message=f"已导出 {len(tasks)} 条任务到 {path}",
            path=str(path),
            filename=path.name,
            count=len(tasks),
            total_minutes=total_minutes,
            content_preview=content[:500],
        )


# --------------------------------------------------------------------------- #
# 排序算法
# --------------------------------------------------------------------------- #
def _rank(task: dict[str, Any], reference: datetime) -> dict[str, Any]:
    """计算单条任务的排序分与理由。"""
    score = 0
    reasons: list[str] = []

    priority = task["priority"]
    weight = _PRIORITY_WEIGHT.get(priority, 25)
    score += weight
    reasons.append(f"{task['priority_label']}优先级 +{weight}")

    urgency = 8
    due_dt = timeutil.parse_datetime(task["due_date"]) if task["due_date"] else None
    if due_dt is None:
        reasons.append("无截止时间 +8")
    else:
        hours = (due_dt - reference).total_seconds() / 3600
        if hours < 0:
            urgency = 45
            reasons.append(f"已逾期 {_hours_text(-hours)} +45")
        elif hours <= 12:
            urgency = 40
            reasons.append("12 小时内到期 +40")
        elif hours <= 24:
            urgency = 34
            reasons.append("24 小时内到期 +34")
        elif hours <= 48:
            urgency = 28
            reasons.append("两天内到期 +28")
        elif hours <= 72:
            urgency = 22
            reasons.append("三天内到期 +22")
        elif hours <= 168:
            urgency = 14
            reasons.append("一周内到期 +14")
        else:
            reasons.append("一周以后到期 +8")
        score += urgency

    if task["status"] == "doing":
        score += 6
        reasons.append("进行中 +6")

    if priority == "high" and urgency >= 34:
        score += 8
        reasons.append("高优且临期 +8")

    estimated = int(task["estimated_minutes"] or 0)
    if estimated > 120:
        score -= 3
        reasons.append("耗时超 2 小时 -3")

    return {
        "id": int(task["id"]),
        "title": task["title"],
        "status": task["status"],
        "priority": priority,
        "due_date": task["due_date"],
        "due_human": task["due_human"],
        "estimated_minutes": estimated,
        "tags": task["tags"],
        "overdue": task["overdue"],
        "score": max(0, min(100, score)),
        "reasons": reasons,
    }


# --------------------------------------------------------------------------- #
# 日程编排辅助
# --------------------------------------------------------------------------- #
def _reference_moment(day: str | None) -> datetime:
    """把「参考时刻」设为目标日期的 00:00，让当天到期的任务都算作临期。"""
    target = timeutil.parse_date(day)
    if target is None or target == timeutil.today():
        return timeutil.now()
    return datetime.combine(target, time(hour=9, minute=0))


def _at(day: _date, hhmm: str) -> datetime:
    parsed = _parse_hhmm(hhmm)
    if parsed is None:
        raise ValueError(f"时间格式应为 HH:MM，收到: {hhmm}")
    return datetime.combine(day, parsed)


def _ceil_to_ten_minutes(moment: datetime) -> datetime:
    """向上取整到下一个 10 分钟刻度（整点已过则顺延到下一个整点的 :00）。"""
    base = moment.replace(second=0, microsecond=0, minute=0)
    steps = moment.minute // 10 + 1
    return base + timedelta(minutes=steps * 10)


def _parse_hhmm(text: str) -> time | None:
    match = re.match(r"^\s*(\d{1,2})\s*[:：]\s*(\d{0,2})\s*$", str(text or ""))
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return time(hour=hour, minute=minute)


def _window(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    return (start, end)


def _subtract(
    segments: list[tuple[datetime, datetime]],
    hole: tuple[datetime, datetime],
) -> list[tuple[datetime, datetime]]:
    """从若干空闲区间中挖掉一个洞，返回剩余空闲区间。"""
    hole_start, hole_end = hole
    if hole_end <= hole_start:
        return segments
    result: list[tuple[datetime, datetime]] = []
    for seg_start, seg_end in segments:
        if hole_end <= seg_start or hole_start >= seg_end:
            result.append((seg_start, seg_end))
            continue
        if seg_start < hole_start:
            result.append((seg_start, hole_start))
        if hole_end < seg_end:
            result.append((hole_end, seg_end))
    return result


def _place(
    segments: list[tuple[datetime, datetime]],
    duration: int,
    due_dt: datetime | None,
) -> tuple[datetime, datetime] | None:
    """在空闲区间中找第一个能容纳 ``duration`` 分钟的位置。

    若任务有截止时间，则要求「开始 + 时长 ≤ 截止时间」，保证排期可兑现。
    """
    span = timedelta(minutes=duration)
    for seg_start, seg_end in segments:
        if seg_start + span > seg_end:
            continue
        if due_dt is not None and seg_start + span > due_dt:
            continue
        return seg_start, seg_start + span
    return None


def _hours_text(hours: float) -> str:
    if hours < 1:
        return f"{int(hours * 60)} 分钟"
    if hours < 24:
        return f"{hours:.1f} 小时"
    return f"{hours / 24:.1f} 天"


def _safe_filename(name: str | None) -> str:
    if not name:
        return ""
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]', "", str(name)).strip()
    return cleaned[:60]


def _render_checklist(day: _date, tasks: list[dict[str, Any]], fmt: str) -> str:
    """渲染导出内容。"""
    total_minutes = sum(int(t["estimated_minutes"] or 0) for t in tasks)
    hour, minute = divmod(total_minutes, 60)

    if fmt == "txt":
        lines = [f"待办清单 {timeutil.format_date(day)}", "=" * 32]
        for index, task in enumerate(tasks, start=1):
            mark = "x" if task["status"] == "done" else " "
            due = f"  截止 {task['due_human']}" if task["due_date"] else ""
            lines.append(f"[{mark}] {index}. {task['title']}（{task['priority_label']}）{due}")
        lines.append("-" * 32)
        lines.append(f"合计 {len(tasks)} 条，预计用时 {hour} 小时 {minute} 分钟")
        return "\n".join(lines) + "\n"

    lines = [f"# 待办清单 · {timeutil.humanize(day)}", ""]
    lines.append(f"- 任务数量：**{len(tasks)}**")
    lines.append(f"- 预计用时：**{hour} 小时 {minute} 分钟**")
    lines.append("")
    if not tasks:
        lines.append("_今天没有需要处理的任务。_")
        return "\n".join(lines) + "\n"

    lines.append("| # | 状态 | 任务 | 优先级 | 截止时间 | 预计用时 | 标签 |")
    lines.append("|---|------|------|--------|----------|----------|------|")
    for index, task in enumerate(tasks, start=1):
        status = {"todo": "待办", "doing": "进行中", "done": "已完成"}.get(task["status"], task["status"])
        tags = "、".join(task["tags"]) or "-"
        due = task["due_human"] or "-"
        lines.append(
            f"| {index} | {status} | {task['title']} | {task['priority_label']} | {due} "
            f"| {task['estimated_minutes']} 分钟 | {tags} |"
        )
    lines.append("")
    notes = [t for t in tasks if t["notes"]]
    if notes:
        lines.append("## 备注")
        lines.extend(f"- **{t['title']}**：{t['notes']}" for t in notes)
    return "\n".join(lines) + "\n"
