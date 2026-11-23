"""文档级 ACL：把"这个人能不能看这条块"变成 SQL 条件。

模型很简单：
- 块上带标签（`meta.acl_tags`，如 `["dept:legal", "level:internal"]`）；
- 用户带标签（`users.tags`）；
- 无标签 = 公开；有标签则要求两者有交集。

为什么在 SQL 里过滤而不是取回后再筛：后者会让 top_k 被无权结果占满，
出现"明明有 5 条可见资料却只返回 1 条"的召回塌陷。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .db.session import tenant_scope

# 字段缺失或空数组都视为公开
PUBLIC_CONDITION = "coalesce(meta->'acl_tags', '[]'::jsonb) = '[]'::jsonb"


def acl_condition(
    user_tags: Sequence[str] | None, is_admin: bool = False
) -> tuple[str, dict]:
    """返回 (SQL 片段, 参数)。片段直接拼进 WHERE，参数走绑定变量。

    管理员：返回常量 TRUE，跳过内容可见性判断（租户与 kb 作用域仍然生效）。
    普通用户：无标签时 `?| '{}'` 恒为假，只会命中公开块——刻意 fail-closed。
    """
    if is_admin:
        return "TRUE", {}
    return (
        f"({PUBLIC_CONDITION} OR meta->'acl_tags' ?| CAST(:user_tags AS text[]))",
        {"user_tags": list(user_tags or [])},
    )


def is_visible(user_tags: Sequence[str] | None, acl_tags: Sequence[str] | None) -> bool:
    """内存侧判定，供 BM25 这类"倒排表没有 SQL 过滤能力"的路径使用。"""
    if not acl_tags:
        return True
    return bool(set(acl_tags) & set(user_tags or []))


def load_user_context(engine: Engine, tenant_id: str, user_id: str) -> tuple[list[str], bool]:
    """一次查出 (标签, 是否管理员)。用户不存在时是 ([], False)，只能看公开内容。"""
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT tags, role FROM users WHERE tenant_id = :tenant_id AND user_id = :user_id"),
            {"tenant_id": tenant_id, "user_id": user_id},
        ).first()
    if row is None:
        return [], False
    tags, role = row
    return list(tags or []), role == "admin"


def load_user_tags(engine: Engine, tenant_id: str, user_id: str) -> list[str]:
    """查用户标签。用户不存在时返回空列表（等价于只能看公开内容）。"""
    tags, _ = load_user_context(engine, tenant_id, user_id)
    return tags


def set_chunk_acl(
    engine: Engine, tenant_id: str, chunk_ids: Iterable[str], tags: Sequence[str]
) -> int:
    """给一批块打标签（入库或后台授权时使用）。返回受影响行数。

    chunks 开了 RLS，必须带 tenant_id 走 tenant_scope，否则 UPDATE 命中 0 行。
    """
    ids = list(chunk_ids)
    if not ids:
        return 0
    payload = '["' + '","'.join(tags) + '"]' if tags else "[]"
    with tenant_scope(engine, tenant_id, write=True) as connection:
        result = connection.execute(
            text(
                "UPDATE chunks SET meta = jsonb_set(meta, '{acl_tags}', CAST(:payload AS jsonb)), "
                "updated_at = now() WHERE chunk_id = ANY(CAST(:ids AS text[]))"
            ),
            {"payload": payload, "ids": ids},
        )
    return int(result.rowcount or 0)
