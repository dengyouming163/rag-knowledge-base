"""租户作用域连接：配合 chunks 表的 RLS 使用。

开了 `FORCE ROW LEVEL SECURITY` 之后，任何访问 chunks 的连接都必须先声明
`app.tenant_id`，否则策略判定为假、返回 0 行（fail-closed）。

这里用 `set_config(..., true)`（等价于 `SET LOCAL`）：只对当前事务生效，
连接归还连接池时自动失效，不会把上一个租户的身份泄漏给下一个请求。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine


@contextmanager
def tenant_scope(
    engine: Engine, tenant_id: str, *, write: bool = False
) -> Iterator[Connection]:
    """在事务内设置 app.tenant_id，后续 SQL 受 RLS 约束。

    write=True 时自动提交/回滚；write=False 时只读，关闭即释放。
    """
    if not tenant_id:
        raise ValueError("tenant_scope 需要非空 tenant_id，否则 RLS 会静默返回 0 行")

    if write:
        with engine.begin() as connection:
            connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                {"tenant_id": tenant_id},
            )
            yield connection
        return

    with engine.connect() as connection:
        connection.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": tenant_id},
        )
        yield connection
