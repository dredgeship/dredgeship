"""任务管理工具组：增、删、改、查、批量与统计。

工具描述的写作原则：说明「做什么 + 何时用 + 关键参数语义」，
因为这段文本会直接成为模型选择工具的依据。
"""

from typing import Any, Literal

from mcp_server.tools.base import fail, ok, task_repo, tool

__all__ = ["register"]


def register(mcp: Any) -> None:
    """把任务相关工具注册到 MCP Server。"""

    # ------------------------------------------------------------------ #
    # 新增
    # ------------------------------------------------------------------ #
    @tool(mcp)
    def add_task(
        title: str,
        priority: Literal["low", "medium", "high"] = "medium",
        due_date: str | None = None,
        tags: list[str] | None = None,
        estimated_minutes: int = 30,
        notes: str = "",
        status: Literal["todo", "doing", "done"] = "todo",
    ) -> str:
        """新建一条待办任务。

        用户说「记一下 / 帮我加个任务 / 提醒我……」时调用。

        Args:
            title: 任务标题，动词开头、可独立看懂，例如「提交软件工程作业」。
            priority: 优先级。用户说「急 / 重要 / 马上」用 high，「有空再说」用 low。
            due_date: 截止时间，支持「2026-10-15」「2026-10-15 14:30」「明天下午3点」
                「下周一」等口语表达；不填视为无截止时间。
            tags: 标签，例如 ["学习", "小组作业"]，用于分组筛选。
            estimated_minutes: 预计耗时（分钟），用于日程排期，默认 30。
            notes: 补充说明（地点、参与人、材料等）。
            status: 初始状态，默认 todo（待办）。
        """
        repo = task_repo()
        task = repo.create(
            title,
            priority=priority,
            status=status,
            due_date=due_date,
            tags=tags,
            estimated_minutes=estimated_minutes,
            notes=notes,
        )
        return ok(
            message=f"已创建任务 #{task['id']}「{task['title']}」"
            + (f"，截止 {task['due_human']}" if task["due_date"] else "，未设置截止时间"),
            task=task,
        )

    @tool(mcp)
    def add_tasks(tasks: list[dict[str, Any]]) -> str:
        """批量新建多条任务，一次提交多条时使用（比逐条调用更快）。

        Args:
            tasks: 任务对象数组，每个对象的字段与 add_task 参数同名：
                ``{"title": "...", "priority": "high", "due_date": "明天", "tags": ["学习"]}``。
                只有 ``title`` 必填。
        """
        if not tasks:
            return fail("tasks 不能为空")

        repo = task_repo()
        created: list[dict[str, Any]] = []
        errors: list[str] = []
        for index, raw in enumerate(tasks, start=1):
            if not isinstance(raw, dict):
                errors.append(f"第 {index} 项不是对象，已跳过")
                continue
            title = str(raw.get("title") or "").strip()
            if not title:
                errors.append(f"第 {index} 项缺少 title，已跳过")
                continue
            try:
                created.append(
                    repo.create(
                        title,
                        priority=str(raw.get("priority") or "medium"),
                        status=str(raw.get("status") or "todo"),
                        due_date=raw.get("due_date"),
                        tags=raw.get("tags"),
                        estimated_minutes=int(raw.get("estimated_minutes") or 30),
                        notes=str(raw.get("notes") or ""),
                    )
                )
            except ValueError as exc:
                errors.append(f"第 {index} 项「{title}」创建失败：{exc}")

        titles = "、".join(f"#{t['id']}「{t['title']}」" for t in created)
        message = f"已成功创建 {len(created)} 条任务" + (f"：{titles}" if titles else "")
        if errors:
            message += f"；{len(errors)} 条失败：" + "；".join(errors)
        return ok(message=message, created=created, errors=errors)

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    @tool(mcp)
    def list_tasks(
        status: Literal["open", "todo", "doing", "done", "all"] = "open",
        priority: Literal["low", "medium", "high"] | None = None,
        tag: str | None = None,
        keyword: str | None = None,
        due_before: str | None = None,
        only_overdue: bool = False,
        limit: int = 50,
    ) -> str:
        """查询任务清单。任何「我现在有哪些任务 / 有没有做某事的任务 / 查一下」都先调用本工具。

        只有依赖数据库真实状态（而不是对话记忆）才能得到正确答案，
        因此每次涉及任务内容时都应重新查询。

        Args:
            status: 状态筛选。open=未完成（默认，同时含 todo 与 doing）；
                done=已完成；all=全部。
            priority: 按优先级筛选，为空表示不限。
            tag: 按标签精确筛选，例如「学习」。
            keyword: 标题/备注关键词模糊搜索。
            due_before: 只看该时间之前到期的任务，支持「今天」「明天」「2026-10-20」。
            only_overdue: 为 true 时只返回已逾期的未完成任务。
            limit: 最多返回条数，默认 50。
        """
        repo = task_repo()
        tasks = repo.list(
            status=status,
            priority=priority,
            tag=tag,
            keyword=keyword,
            due_before=due_before,
            include_overdue_only=only_overdue,
            limit=limit,
        )
        scope = {
            "open": "未完成", "todo": "待办", "doing": "进行中",
            "done": "已完成", "all": "全部",
        }.get(status, status)
        return ok(
            message=f"共找到 {len(tasks)} 条{scope}任务",
            count=len(tasks),
            tasks=tasks,
        )

    @tool(mcp)
    def get_task(task_id: int) -> str:
        """按 ID 查询单条任务详情（用户提到「第 3 条 / #5」时使用）。

        Args:
            task_id: 任务 ID。
        """
        task = task_repo().get(task_id)
        if not task:
            return fail(f"未找到 ID 为 {task_id} 的任务", code="not_found")
        return ok(message=f"任务 #{task_id}「{task['title']}」", task=task)

    @tool(mcp)
    def task_stats() -> str:
        """统计任务总览：各状态数量、逾期数量、今日完成数量。适合回答「我还有多少事没做」。"""
        return ok(message="任务统计完成", stats=task_repo().stats())

    # ------------------------------------------------------------------ #
    # 修改
    # ------------------------------------------------------------------ #
    @tool(mcp)
    def update_task(
        task_id: int,
        title: str | None = None,
        priority: Literal["low", "medium", "high"] | None = None,
        due_date: str | None = None,
        status: Literal["todo", "doing", "done"] | None = None,
        tags: list[str] | None = None,
        estimated_minutes: int | None = None,
        notes: str | None = None,
        clear_due_date: bool = False,
    ) -> str:
        """修改已有任务。只传需要改的字段，未传的字段保持不变。

        Args:
            task_id: 要修改的任务 ID。
            due_date: 新的截止时间，支持「后天上午」「2026-10-20」等口语表达。
            clear_due_date: 为 true 时清除截止时间（用户说「不用设截止了」）。
            status: 改为 done 表示完成，改为 doing 表示开始做。
        """
        repo = task_repo()
        existing = repo.get(task_id)
        if existing is None:
            return fail(f"未找到 ID 为 {task_id} 的任务", code="not_found")

        # 只收集用户真正想改的字段：未传的字段保持原值
        fields: dict[str, Any] = {}
        if title is not None:
            fields["title"] = title
        if priority is not None:
            fields["priority"] = priority
        if status is not None:
            fields["status"] = status
        if tags is not None:
            fields["tags"] = tags
        if estimated_minutes is not None:
            fields["estimated_minutes"] = estimated_minutes
        if notes is not None:
            fields["notes"] = notes
        if clear_due_date:
            fields["due_date"] = None
        elif due_date is not None:
            fields["due_date"] = due_date

        if not fields:
            return ok(message=f"任务 #{task_id} 没有需要修改的内容", task=existing)

        updated = repo.update(task_id, **fields)
        if updated is None:
            return fail(f"任务 #{task_id} 更新失败", code="not_found")
        return ok(message=f"已更新任务 #{task_id}「{updated['title']}」", task=updated)

    @tool(mcp)
    def complete_task(task_id: int) -> str:
        """把任务标记为已完成。

        Args:
            task_id: 任务 ID。
        """
        repo = task_repo()
        existing = repo.get(task_id)
        if existing is None:
            return fail(f"未找到 ID 为 {task_id} 的任务", code="not_found")
        if existing["status"] == "done":
            return ok(message=f"任务 #{task_id}「{existing['title']}」本来就是已完成状态", task=existing)
        task = repo.complete(task_id)
        return ok(message=f"已完成任务 #{task_id}「{existing['title']}」，辛苦啦", task=task)

    @tool(mcp)
    def complete_tasks(task_ids: list[int]) -> str:
        """批量标记完成。

        Args:
            task_ids: 任务 ID 数组，例如 [3, 5, 8]。
        """
        repo = task_repo()
        done: list[str] = []
        missing: list[int] = []
        for task_id in task_ids:
            task = repo.complete(int(task_id))
            if task is None:
                missing.append(int(task_id))
            else:
                done.append(f"#{task['id']}「{task['title']}」")
        message = f"已完成 {len(done)} 条任务" + ("：" + "、".join(done) if done else "")
        if missing:
            message += f"；未找到 ID：{missing}"
        return ok(message=message, completed_ids=[int(i) for i in task_ids if int(i) not in missing])

    # ------------------------------------------------------------------ #
    # 删除（需显式确认，降低误删风险）
    # ------------------------------------------------------------------ #
    @tool(mcp)
    def delete_task(task_id: int, confirm: bool = False) -> str:
        """删除任务。出于安全考虑必须显式确认。

        正确用法：先不带 confirm 调用一次，把返回的待确认信息转述给用户；
        用户明确同意后，再带 confirm=True 调用。

        Args:
            task_id: 要删除的任务 ID。
            confirm: 用户已明确同意删除时传 True。
        """
        task = task_repo().get(task_id)
        if task is None:
            return fail(f"未找到 ID 为 {task_id} 的任务", code="not_found")
        if not confirm:
            return ok(
                message=f"请确认是否删除任务 #{task_id}「{task['title']}」，"
                "用户同意后再以 confirm=true 调用本工具。",
                requires_confirmation=True,
                task=task,
            )
        task_repo().delete(task_id)
        return ok(message=f"已删除任务 #{task_id}「{task['title']}」", deleted_id=int(task_id))

    @tool(mcp)
    def delete_tasks(task_ids: list[int], confirm: bool = False) -> str:
        """批量删除任务（同样需要显式确认）。

        Args:
            task_ids: 任务 ID 数组。
            confirm: 用户已明确同意删除时传 True。
        """
        repo = task_repo()
        if not task_ids:
            return fail("task_ids 不能为空")
        tasks = repo.get_many(task_ids)
        if not tasks:
            return fail("未找到任何匹配的任务", code="not_found")
        if not confirm:
            names = "、".join(f"#{t['id']}「{t['title']}」" for t in tasks)
            return ok(
                message=f"请确认是否删除以下任务：{names}。用户同意后再以 confirm=true 调用。",
                requires_confirmation=True,
                tasks=tasks,
            )
        deleted = repo.delete_many([t["id"] for t in tasks])
        return ok(message=f"已删除 {deleted} 条任务", deleted_count=deleted)
