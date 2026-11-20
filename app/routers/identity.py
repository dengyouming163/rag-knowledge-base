"""身份卡与权限解释：只读、租户作用域、角色来自数据库。

驾驶舱要回答的第一类问题是"为什么我看/看不到"：
- 身份卡：我是谁、什么角色、有哪些标签、能进哪些库；
- 权限解释：这次判定是 ACL 拦的，还是 RLS / 文档状态拦的。

两条安全边界：
- 租户只来自请求头，绝不接受请求体传入；
- 角色只从 users.role 读，不接受请求头声明。
"""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.deps import EngineDep, TenantId, UserId, ops_actions_enabled
from app.services import ensure_kb
from src.acl import acl_condition
from src.db.session import tenant_scope

router = APIRouter(prefix="/v1", tags=["identity"])


def _user_row(engine: Engine, tenant_id: str, user_id: str) -> dict:
    """用户不存在时返回空字典，由调用方降级为 guest。"""
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT user_id, display_name, tags, role FROM users "
                    "WHERE tenant_id = :t AND user_id = :u"
                ),
                {"t": tenant_id, "u": user_id},
            )
            .mappings()
            .first()
        )
    return dict(row) if row else {}


def _rls_metadata(engine: Engine) -> dict:
    """只读元数据：chunks 的 RLS 开关与策略条数，供驾驶舱展示隔离强度。"""
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = 'chunks' AND relkind = 'r'"
                )
            )
            .mappings()
            .first()
        )
        policies = connection.execute(
            text("SELECT count(*) FROM pg_policies WHERE tablename = 'chunks'")
        ).scalar_one()
    return {
        "enabled": bool(row["relrowsecurity"]) if row else False,
        "forced": bool(row["relforcerowsecurity"]) if row else False,
        "policy_count": int(policies),
    }


@router.get("/identity")
def identity(
    tenant_id: TenantId,
    user_id: UserId,
    engine: EngineDep,
) -> dict:
    user = _user_row(engine, tenant_id, user_id)
    role = str(user.get("role") or "guest")
    tags = list(user.get("tags") or [])
    is_admin = role == "admin"
    with engine.connect() as connection:
        kbs = (
            connection.execute(
                text("SELECT id FROM knowledge_bases WHERE tenant_id = :t ORDER BY id"),
                {"t": tenant_id},
            )
            .scalars()
            .all()
        )
    actions_enabled = ops_actions_enabled()
    return {
        "tenant": tenant_id,
        "user": user_id,
        "role": role,
        "tags": tags,
        "is_admin": is_admin,
        "kbs": list(kbs),
        "isolation": {
            "tenant_scope": True,
            "acl": "admin_bypass" if is_admin else "public_or_tag_intersection",
            "rls": _rls_metadata(engine),
        },
        "ops": {
            "actions_enabled": actions_enabled,
            "can_trigger": bool(is_admin and actions_enabled),
        },
    }


@router.get("/ops/permissions")
def permissions(
    kb_id: str,
    tenant_id: TenantId,
    user_id: UserId,
    engine: EngineDep,
) -> dict:
    # 跨租户与不存在都走同一个 404，不泄露"这个库存在"
    ensure_kb(engine, tenant_id, kb_id)
    user = _user_row(engine, tenant_id, user_id)
    role = str(user.get("role") or "guest")
    tags = list(user.get("tags") or [])
    is_admin = role == "admin"
    rls = _rls_metadata(engine)

    # 不带 app.tenant_id 的裸查询会被 RLS 过滤成 0，正好用来证明策略生效
    with engine.connect() as connection:
        bare_count = connection.execute(
            text("SELECT count(*) FROM chunks WHERE kb_id = :kb"),
            {"kb": kb_id},
        ).scalar_one()

    condition, params = acl_condition(tags, is_admin)
    with tenant_scope(engine, tenant_id) as connection:
        # 口径与真实召回一致（src/store_pg.py 的检索 SQL）：父块与无向量的块不参与召回；
        # 用 EXISTS 而不是 JOIN documents：documents 也有 meta 列，
        # JOIN 会让 acl_condition 里的未限定 `meta` 变成歧义列。
        scoped_count = connection.execute(
            text(
                "SELECT count(*) FROM chunks c "
                "WHERE c.tenant_id = :t AND c.kb_id = :kb "
                "AND c.is_parent = false AND c.embedding IS NOT NULL "
                "AND EXISTS ("
                "SELECT 1 FROM documents d WHERE d.id = c.doc_id "
                "AND d.tenant_id = c.tenant_id AND d.kb_id = c.kb_id AND d.status = 'ready'"
                ") AND " + condition
            ),
            {"t": tenant_id, "kb": kb_id, **params},
        ).scalar_one()

    decision = "allow" if scoped_count else "deny"
    if decision == "allow":
        reason = (
            "租户匹配，ACL 可见，RLS 已启用"
            if rls["enabled"]
            else "租户匹配，ACL 可见，RLS 未启用"
        )
    else:
        reason = "租户匹配，但当前身份没有可见 chunk（ACL 为空或文档未 ready）"
    return {
        "tenant": tenant_id,
        "user": user_id,
        "role": role,
        "tags": tags,
        "is_admin": is_admin,
        "kb": {"id": kb_id, "exists": True, "tenant_match": True},
        "acl": {
            "mode": "admin_bypass" if is_admin else "public_or_tag_intersection",
            "user_tags": tags,
        },
        "rls": {
            **rls,
            "bare_count": int(bare_count),
            "scoped_count": int(scoped_count),
        },
        "decision": decision,
        "reason": reason,
    }
