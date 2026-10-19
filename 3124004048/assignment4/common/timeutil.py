"""中文自然语言日期/时间解析与格式化。

智能体的时间输入来自用户口语（"明天下午三点"），也来自大模型生成的
ISO 字符串。本模块统一把两者解析成本地 naive ``datetime``，避免各处
重复实现导致格式不一致。

约定：全项目的时间均为**本地时间 naive datetime**，数据库中以
``YYYY-MM-DD HH:MM:SS`` 文本存储（字典序即时间序，便于索引与比较）。

支持的输入形式：
    - 绝对：``2026-10-15`` / ``2026/10/15`` / ``10月15日`` / ``2026-10-15 14:30``
    - 绝对（ISO）：``2026-10-15T14:30:00``
    - 相对日：今天 / 今日, 明天 / 明日, 后天, 大后天, 昨天
    - 星期：本周五, 下周一, 周三（默认理解为"最近的一个该星期"）
    - 时刻：``14:30`` / ``14点`` / ``14点30`` / ``下午3点`` / ``上午9点半``
    - 时段词：凌晨 / 早上 / 上午 / 中午 / 下午 / 傍晚 / 晚上
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

__all__ = [
    "now",
    "now_str",
    "today",
    "today_str",
    "parse_date",
    "parse_datetime",
    "format_dt",
    "format_date",
    "humanize",
]

# 截止时间的默认时刻：傍晚 18:00（符合"下班前完成"的直觉）
DEFAULT_DUE_TIME = "18:00"
# 日程起点默认时刻：上午 09:00
DEFAULT_START_TIME = "09:00"

_WEEKDAY_CN = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

# 时段词 -> 默认小时
_PERIOD_HOUR = {
    "凌晨": 5,
    "早上": 8,
    "早晨": 8,
    "上午": 10,
    "中午": 12,
    "正午": 12,
    "下午": 15,
    "傍晚": 18,
    "晚上": 20,
    "晚间": 20,
    "夜里": 21,
}

_DATE_PATTERNS = (
    # 2026-10-15 / 2026/10/15 / 2026.10.15 / 2026年10月15日
    re.compile(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?"),
    # 10-15 / 10/15 / 10月15日（缺省年份）
    re.compile(r"(?<![\d:])(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?(?!\s*[:：])"),
)

_TIME_PATTERN = re.compile(r"(\d{1,2})\s*[:：点]\s*(\d{1,2})?\s*分?\s*(半)?")
_COMPACT_HOUR_PATTERN = re.compile(r"(?<![\d:：])(\d{1,2})\s*[时点](?![\d:：])")


# --------------------------------------------------------------------------- #
# 当前时间
# --------------------------------------------------------------------------- #
def now() -> datetime:
    """当前本地时间（不含时区信息）。"""
    return datetime.now()


def now_str() -> str:
    """当前时间的标准存储格式。"""
    return format_dt(now())


def today() -> date:
    return date.today()


def today_str() -> str:
    return today().isoformat()


# --------------------------------------------------------------------------- #
# 格式化
# --------------------------------------------------------------------------- #
def format_dt(value: datetime) -> str:
    """datetime -> ``YYYY-MM-DD HH:MM:SS``。"""
    return value.strftime("%Y-%m-%d %H:%M:%S")


def format_date(value: date) -> str:
    return value.strftime("%Y-%m-%d")


def humanize(value: datetime | date | str | None, *, ref: datetime | None = None) -> str:
    """把时间转成人类友好的中文描述，用于对话回复与界面展示。"""
    if value is None:
        return "未指定"
    has_time = _has_time_component(value)

    dt = _coerce(value, default_time=DEFAULT_DUE_TIME)
    if dt is None:
        return str(value)

    ref_dt = ref or now()
    delta_days = (dt.date() - ref_dt.date()).days
    if delta_days == 0:
        day_part = "今天"
    elif delta_days == 1:
        day_part = "明天"
    elif delta_days == 2:
        day_part = "后天"
    elif delta_days == -1:
        day_part = "昨天"
    elif 0 < delta_days <= 6:
        day_part = f"周{'一二三四五六日'[dt.weekday()]}"
    else:
        day_part = f"{dt.month}月{dt.day}日"

    # 纯日期输入（或时刻恰为 00:00）时只回日期，避免"今天 18:00"这类噪音
    if not has_time or (dt.hour == 0 and dt.minute == 0 and dt.second == 0):
        return day_part
    return f"{day_part} {dt.hour:02d}:{dt.minute:02d}"


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def parse_date(value: str | date | datetime | None) -> date | None:
    """解析出日期部分，解析失败返回 ``None``。"""
    dt = parse_datetime(value)
    return dt.date() if dt else None


def parse_datetime(
    value: str | date | datetime | None,
    *,
    default_time: str = DEFAULT_DUE_TIME,
) -> datetime | None:
    """把口语/标准时间文本解析为 ``datetime``。

    Args:
        value: 待解析文本，可以是 ``date``/``datetime``/字符串/``None``。
        default_time: 文本中只含日期时补全的时刻，形如 ``"18:00"``。

    Returns:
        解析成功返回 datetime；无法识别返回 ``None``（调用方自行决定报错或忽略）。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, _time_from_str(default_time))

    raw = str(value).strip()
    if not raw:
        return None

    # 1) ISO 格式直接交给标准库，避免自定义正则误伤
    iso_candidate = raw.replace("/", "-").replace("年", "-").replace("月", "-").replace("日", "")
    try:
        return datetime.fromisoformat(iso_candidate).replace(microsecond=0)
    except ValueError:
        pass

    remaining = raw
    base_date: date | None = None

    # 2) 相对日
    for keyword, offset in (("大后天", 3), ("后天", 2), ("明天", 1), ("明日", 1),
                            ("今天", 0), ("今日", 0), ("昨天", -1), ("昨日", -1)):
        if keyword in remaining:
            base_date = today() + timedelta(days=offset)
            remaining = remaining.replace(keyword, " ")
            break

    # 3) 星期表达（本/这/下/下个 + 周/星期 + 数字）
    if base_date is None:
        match = re.search(r"(本|这|下个|下)?\s*(?:周|星期|礼拜)\s*([一二三四五六日天])", remaining)
        if match:
            modifier, weekday_cn = match.group(1), match.group(2)
            target = _WEEKDAY_CN[weekday_cn]
            current = today()
            if modifier in ("本", "这"):
                diff = target - current.weekday()
            elif modifier in ("下", "下个"):
                diff = 7 - current.weekday() + target
            else:
                # 未指定：理解为"最近的将来该星期"
                diff = (target - current.weekday()) % 7
            base_date = current + timedelta(days=diff)
            remaining = remaining[: match.start()] + " " + remaining[match.end():]

    # 4) 绝对日期
    if base_date is None:
        for pattern in _DATE_PATTERNS:
            match = pattern.search(remaining)
            if not match:
                continue
            groups = match.groups()
            try:
                if len(groups) == 3:
                    year, month, day = (int(g) for g in groups)
                else:
                    year, month, day = today().year, int(groups[0]), int(groups[1])
                base_date = date(year, month, day)
            except ValueError:
                continue
            remaining = remaining[: match.start()] + " " + remaining[match.end():]
            break

    # 5) 时刻
    explicit_time: tuple[int, int] | None = None
    match = _TIME_PATTERN.search(remaining)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2)) if match.group(2) else (30 if match.group(3) else 0)
        explicit_time = (hour, minute)
        remaining = remaining[: match.start()] + " " + remaining[match.end():]

    period_hour: int | None = None
    for keyword, hour in _PERIOD_HOUR.items():
        if keyword in remaining:
            period_hour = hour
            remaining = remaining.replace(keyword, " ")
            break

    if explicit_time is None:
        compact = _COMPACT_HOUR_PATTERN.search(remaining)
        if compact:
            explicit_time = (int(compact.group(1)), 0)

    # 6) 组装
    if base_date is None and explicit_time is None and period_hour is None:
        return None

    if explicit_time is None:
        if period_hour is not None:
            hour, minute = period_hour, 0
        else:
            fallback = _time_from_str(default_time)
            hour, minute = fallback.hour, fallback.minute
    else:
        hour, minute = explicit_time
        # "下午3点" -> 15:00；"晚上7点" -> 19:00
        if period_hour is not None and hour < 12 and period_hour >= 12:
            hour += 12

    target_date = base_date or today()
    try:
        return datetime(target_date.year, target_date.month, target_date.day, hour % 24, minute % 60)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# 内部辅助
# --------------------------------------------------------------------------- #
def _time_from_str(text: str) -> "datetime.time":
    parts = str(text).split(":")
    hour = int(parts[0]) if parts and parts[0].strip() else 18
    minute = int(parts[1]) if len(parts) > 1 and parts[1].strip() else 0
    return datetime(2000, 1, 1, hour % 24, minute % 60).time()


def _coerce(value: datetime | date | str, *, default_time: str) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, _time_from_str(default_time))
    return parse_datetime(value, default_time=default_time)


_TIME_HINT = re.compile(
    r"(\d{1,2}\s*[:：点])|(\d{1,2}\s*[时])|"
    r"(凌晨|早上|早晨|上午|中午|正午|下午|傍晚|晚上|晚间|夜里)"
)


def _has_time_component(value: datetime | date | str) -> bool:
    """判断输入是否显式包含时刻信息（用于决定展示时是否省略 HH:MM）。"""
    if isinstance(value, datetime):
        return True
    if isinstance(value, date):
        return False
    return bool(_TIME_HINT.search(str(value)))
