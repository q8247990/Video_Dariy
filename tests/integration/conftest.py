"""tests/integration 目录共享夹具：FastAPI TestClient 工厂。

提供 ``make_http_client`` fixture，用于以一致的方式构造隔离的
:class:`fastapi.testclient.TestClient`：自动挂载指定 router、双重
覆盖 ``get_db`` 依赖，并可选地覆盖 ``get_current_user`` 注入默认
admin user。

四个现有 HTTP 集成测试文件
（``test_mcp_streamable_http``、``test_llm_provider_delete_http``、
``test_onboarding_http``、``test_daily_summaries_http``）共用本目录的
``make_http_client``，各自的 ``client`` fixture 只保留“本文件挂哪些
router、是否需要匿名”这一文件特有的差异。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Union

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import src.api.deps as api_deps
import src.db.session as db_session_module

#: 与现有 HTTP 集成测试 fixture 完全等价的默认 admin user：
#: ``SimpleNamespace(id=1, username="admin")``。
DEFAULT_ADMIN_USER: SimpleNamespace = SimpleNamespace(id=1, username="admin")


#: 路由规格：单个 :class:`APIRouter`（按 router 自带路径挂载）或
#: ``(APIRouter, prefix)`` 元组（按指定 prefix 挂载）。
RouterSpec = Union[APIRouter, tuple[APIRouter, str]]


@pytest.fixture
def make_http_client(pg_db: Session) -> Callable[..., Iterator[TestClient]]:
    """构造 FastAPI :class:`~fastapi.testclient.TestClient` 的工厂 fixture。

    自动应用以下依赖覆盖，匹配现有 HTTP 集成测试的写法：

    - ``src.api.deps.get_db`` → ``pg_db``
    - ``src.db.session.get_db`` → ``pg_db``（同一 Session 的双重覆盖，
      因为部分 router / endpoint 可能通过两条路径之一注入 DB 依赖）
    - ``src.api.deps.get_current_user`` → :data:`DEFAULT_ADMIN_USER`
      （仅当 ``authenticated=True`` 时）

    返回的工厂是一个 **生成器工厂**：调用方应当以 ``with`` 进入。
    进入上下文管理器时完成 ``TestClient.__enter__``，退出时关闭其
    持有的资源；夹具本体的 ``pg_db`` 由外层 ``pytest_db`` fixture 负责
    事务回滚，工厂不额外控制其生命周期。

    用法::

        @pytest.fixture
        def client(pg_db, make_http_client):
            with make_http_client(
                [(llm_providers.router, "/api/v1/providers")],
            ) as test_client:
                yield test_client

    匿名端点（例如 MCP）::

        @pytest.fixture
        def client(pg_db, make_http_client):
            with make_http_client([mcp_router], auth=False) as test_client:
                yield test_client

    端点契约测试（业务码 → HTTP 状态码映射 + 鉴权 401 路径）::

        from src.api.error_status import ResponseStatusMiddleware

        with make_http_client(
            [(router, "/api/v1/x")],
            middleware=[ResponseStatusMiddleware],
        ) as test_client:
            ...

        # 同一 factory 的匿名模式用于 401 断言（不覆盖 auth 依赖）
        with make_http_client([(router, "/api/v1/x")], auth=False) as anonymous:
            ...

    Parameters
    ----------
    routers : Sequence[RouterSpec]
        要挂载的路由集合。每个元素可以是 :class:`APIRouter`
        （按 router 内部路径挂载）或 ``(APIRouter, prefix)`` 元组。
    authenticated : bool, default True
        是否注入默认 admin user 的 ``get_current_user`` 覆盖。
        设为 ``False`` 时不覆盖 auth 依赖，由测试自身用 ``X-MCP-Token``
        等协议层凭证访问。
    middleware : Sequence[type], optional
        要挂载的 WSGI/ASGI 中间件类（例如 ``ResponseStatusMiddleware``，
        用于业务码 → HTTP 状态码映射的契约断言）。按列表顺序
        ``app.add_middleware`` 挂载。
    extra_overrides : Mapping, optional
        额外的依赖覆盖（键为 Depends 函数对象，值为替身 callable）。
        用于覆盖特定端点模块自己的依赖（例如端点模块内定义的
        orchestrator 依赖）。
    """

    @contextmanager
    def _factory(
        routers: Sequence[RouterSpec],
        *,
        authenticated: bool = True,
        middleware: Sequence[type] | None = None,
        extra_overrides: Mapping[Callable, Callable] | None = None,
    ) -> Iterator[TestClient]:
        app = FastAPI()

        for spec in routers:
            if isinstance(spec, tuple):
                router, prefix = spec
                app.include_router(router, prefix=prefix)
            else:
                app.include_router(spec)

        if middleware:
            for middleware_cls in middleware:
                app.add_middleware(middleware_cls)

        def _override_get_db() -> Iterator[Session]:
            yield pg_db

        app.dependency_overrides[api_deps.get_db] = _override_get_db
        app.dependency_overrides[db_session_module.get_db] = _override_get_db

        if authenticated:
            app.dependency_overrides[api_deps.get_current_user] = lambda: DEFAULT_ADMIN_USER

        if extra_overrides:
            app.dependency_overrides.update(extra_overrides)

        with TestClient(app) as test_client:
            yield test_client

    return _factory
