"""Web 层：FastAPI 应用与请求 / 响应模型。"""

__all__ = ["create_app"]


def create_app():
    """惰性创建 FastAPI 应用（避免导入包时即加载 Agent 依赖链）。"""
    from api.main import create_app as _create

    return _create()
