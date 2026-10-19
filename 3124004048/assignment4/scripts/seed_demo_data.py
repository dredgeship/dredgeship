"""一键灌入演示数据，让界面与演示脚本开箱即是"有内容"的状态。

用法：
    python scripts/seed_demo_data.py            # 追加演示任务
    python scripts/seed_demo_data.py --reset    # 先清空全部任务再灌入
    python scripts/seed_demo_data.py --api http://127.0.0.1:8000

设计说明：这里走 HTTP 接口而不是直接写库，好处是顺带验证了
「REST 接口 → 仓储 → SQLite」这条链路，一举两得。
"""

import argparse
import json
import sys
import urllib.error
import urllib.request

# (标题, 优先级, 截止时间口语表达, 标签, 预计分钟, 备注, 状态)
DEMO_TASKS = [
    ("提交软件工程作业文档", "high", "明天下午3点", ["学习", "大作业"], 90, "", "todo"),
    ("和组员讨论智能体分工", "high", "今天18:00", ["小组作业"], 60, "腾讯会议，需要提前准备分工表", "doing"),
    ("完成系统测试用例评审", "high", "明天上午10点", ["小组作业"], 90, "", "todo"),
    ("复习软件测试章节", "medium", "后天", ["学习"], 120, "重点：等价类划分与边界值", "todo"),
    ("整理上周会议纪要", "medium", "下周一", ["学习"], 45, "", "todo"),
    ("预约体检", "medium", "下周三", ["生活"], 30, "记得带上体检卡", "todo"),
    ("背诵英语单词 50 个", "low", "今天21:00", ["学习"], 30, "", "todo"),
    ("买牛奶和面包", "low", None, ["生活"], 20, "", "todo"),
    ("写周报", "low", "昨天", ["工作"], 30, "已过期，用于演示逾期提醒", "todo"),
]


def post(api: str, path: str, payload: dict) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        api + path,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def get(api: str, path: str) -> dict:
    with urllib.request.urlopen(api + path, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def delete(api: str, path: str) -> None:
    request = urllib.request.Request(api + path, method="DELETE")
    with urllib.request.urlopen(request, timeout=15):
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="灌入待办小助理的演示数据")
    parser.add_argument("--api", default="http://127.0.0.1:8000", help="FastAPI 服务地址")
    parser.add_argument("--reset", action="store_true", help="灌入前清空全部任务")
    args = parser.parse_args()

    try:
        health = get(args.api, "/api/health")
    except urllib.error.URLError as exc:
        print(f"无法连接服务 {args.api}：{exc}。请先运行 python -m api.main")
        return 1
    print(f"服务状态：{health.get('status')}（Agent 就绪={health.get('agent_ready')}）")

    if args.reset:
        existing = get(args.api, "/api/tasks?status=all&limit=500")["tasks"]
        for task in existing:
            delete(args.api, f"/api/tasks/{task['id']}")
        print(f"已清空 {len(existing)} 条历史任务")

    created = []
    for title, priority, due, tags, minutes, notes, status in DEMO_TASKS:
        payload = {
            "title": title,
            "priority": priority,
            "tags": tags,
            "estimated_minutes": minutes,
            "notes": notes,
            "status": status,
        }
        if due:
            payload["due_date"] = due
        try:
            created.append(post(args.api, "/api/tasks", payload))
        except urllib.error.HTTPError as exc:
            print(f"创建失败「{title}」：{exc.code} {exc.read().decode('utf-8', 'ignore')}")

    # 造一条"今日已完成"，让进度环不是 0
    try:
        extra = post(
            args.api,
            "/api/tasks",
            {"title": "晨会同步进度", "priority": "medium", "due_date": "今天09:00",
             "tags": ["工作"], "estimated_minutes": 15},
        )
        post(args.api, f"/api/tasks/{extra['id']}/complete", {})
        created.append(extra)
    except urllib.error.HTTPError as exc:
        print(f"创建已完成任务失败：{exc.code}")

    print(f"已创建 {len(created)} 条演示任务：")
    for task in created:
        print(f"  #{task['id']:>2} {task['title']}（{task['priority_label']}，"
              f"{task.get('due_human') or '无期限'}）")
    print("\n接下来可以：python main.py  或  打开 http://127.0.0.1:8000")
    return 0


if __name__ == "__main__":
    sys.exit(main())
