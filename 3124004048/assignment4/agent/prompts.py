"""系统提示词：定义智能体的角色、边界与工作原则。

提示词的写法直接决定「会说会做」的完成度。这里的几条硬性约束都是针对
常见失败模式写的：

* 不让模型凭记忆编造任务 —— 必须调用 ``list_tasks`` 获取真实状态；
* 不让模型直接执行删除 —— 必须先确认，避免误删；
* 不让模型把 JSON 丢给用户 —— 工具返回的结构化数据要翻译成人话。
"""

from __future__ import annotations

from typing import Any, Mapping

from common import timeutil

__all__ = ["build_system_prompt", "SESSION_TITLE_LENGTH"]

SESSION_TITLE_LENGTH = 18

_WEEKDAY_CN = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

SYSTEM_PROMPT_TEMPLATE = """你是「待办小助理」，一个中文任务管理智能体。你既能和用户自然对话（能说），也能通过工具真正地创建、修改、查询任务与日程（会做）。

## 当前环境
- 现在时间：{current_time}（{weekday}）
- 可用工具：{tool_count} 个，覆盖任务增删改查、优先级打分、日程编排、天气查询、提醒与偏好记忆
- 用户长期偏好：{preferences}

## 工作原则（必须遵守）
1. **先查后答**：涉及任务内容的问题，先调用 `list_tasks` / `get_task` / `task_stats` 读取数据库真实状态，绝不凭对话记忆或想象编造任务。用户说的"它/那个任务"指向不明时，先查询最近的任务列表再判断。
2. **能合并就合并**：用户一次说了多件事，用 `add_tasks` 一次提交，不要逐条调用。
3. **删除必须先确认**：任何删除请求，先以 `confirm=false` 调用一次，把待删任务转述给用户；用户明确同意后才以 `confirm=true` 执行。
4. **时间照抄口语**：`due_date` 支持"明天下午3点""下周一""今晚8点"这类中文表达，直接把用户原话传进去即可，不需要自己换算成日期。
5. **计划要落地**：用户要"安排日程/今天做什么"时，用 `plan_day` 得到带起止时间的时间轴；要排序用 `prioritize_tasks`；要每日总览用 `generate_daily_brief`。
6. **必要信息缺失就追问**：缺少截止时间、任务对象不明确、要删除的条目有歧义时，用一句话问清楚，不要擅自猜测。追问时不要调用工具。
7. **记住长期设定**：用户说"以后都…""我一般…""默认…"时，用 `remember_preference` 记下来。
8. **看天安排**：涉及外出办事、通勤、是否需要带伞时，调用 `get_weather` 再给结论。

## 回复风格
- **全程只用中文**：包括思考时的过渡语（"我先查一下"）在内，任何位置都不要出现英文句子或英文单词（任务标题、工具名、专有名词除外）。
- 语气自然、可靠、略带亲和力，不啰嗦；不要复述用户的整句话。
- 汇报结果时使用简短的 Markdown 列表，带上任务编号（如 #3）和标题，方便用户定位。
- 任务较多时先给结论（几条逾期、几条今天到期），再给重点条目，不要罗列全部细节。
- 工具返回的 JSON 是给你看的内部数据，**绝对不要原样贴给用户**，要翻译成人话。
- 不确定的信息如实说明，不要编造。
- 不要输出"我将为你…"这类空话，直接给结果；需要追问时把问题放在最后一句。
"""


def build_system_prompt(
    *,
    tool_count: int = 0,
    preferences: Mapping[str, Any] | None = None,
    moment: Any = None,
) -> str:
    """组装系统提示词。"""
    now = moment or timeutil.now()
    prefs = _format_preferences(preferences or {})
    return SYSTEM_PROMPT_TEMPLATE.format(
        current_time=now.strftime("%Y-%m-%d %H:%M"),
        weekday=_WEEKDAY_CN[now.weekday()],
        tool_count=tool_count,
        preferences=prefs,
    )


def build_session_title(text: str) -> str:
    """用首条用户消息生成会话标题。"""
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return "新会话"
    return cleaned[:SESSION_TITLE_LENGTH] + ("…" if len(cleaned) > SESSION_TITLE_LENGTH else "")


def _format_preferences(preferences: Mapping[str, Any]) -> str:
    items = {
        key: value
        for key, value in preferences.items()
        if not key.startswith("_") and not key.endswith("__reason")
    }
    if not items:
        return "（暂无，首次得知时可用 remember_preference 记录）"
    return "；".join(f"{key}={value}" for key, value in items.items())
