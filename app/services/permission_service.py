"""RBAC permission service backed by the existing RBAC tables.

Permission checks fail closed when the employee/role/permission relation is missing.
The bootstrap login is an explicitly separate development-only bypass.
"""
from flask import current_app, g, session
from sqlalchemy import text
from app.extensions import db

SUPER_ADMIN_ROLE = "super_admin"


def _bootstrap_bypass():
    return bool(
        current_app.config.get("ALLOW_BOOTSTRAP_ADMIN_BYPASS", False)
        and session.get("auth_source") == "bootstrap"
        and session.get("is_admin") is True
    )


def _employee_id():
    value = session.get("employee_id")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _load_permission_rows():
    employee_id = _employee_id()
    if not employee_id:
        return []
    cache = getattr(g, "_rbac_permission_rows", None)
    if cache is not None:
        return cache
    try:
        rows = db.session.execute(text("""
            SELECT p.permission_code, rp.data_scope, r.role_code
              FROM employee_roles er
              JOIN employees e ON e.id = er.employee_id AND e.status = 1
              JOIN roles r ON r.id = er.role_id AND r.status = 1
              LEFT JOIN role_permissions rp ON rp.role_id = r.id
              LEFT JOIN permissions p ON p.id = rp.permission_id
             WHERE er.employee_id = :employee_id
        """), {"employee_id": employee_id}).mappings().all()
    except Exception:
        # A missing/misconfigured RBAC schema must never grant access.
        db.session.rollback()
        rows = []
    g._rbac_permission_rows = [dict(row) for row in rows]
    return g._rbac_permission_rows


def is_super_admin():
    if _bootstrap_bypass():
        return True
    return any(row.get("role_code") == SUPER_ADMIN_ROLE for row in _load_permission_rows())


def has_permission(permission_code: str) -> bool:
    if not _employee_id():
        return False
    if _bootstrap_bypass() or is_super_admin():
        return True
    return any(row.get("permission_code") == permission_code for row in _load_permission_rows())


def require_permission(permission_code: str):
    """Raise 403 when the current employee does not hold the requested permission."""
    from flask import abort
    if not has_permission(permission_code):
        abort(403)


def get_data_scopes(permission_code: str):
    """Return granted scopes for a permission across all employee roles.

    Multiple roles are additive. `all` dominates; otherwise callers can union
    own/assigned/factory records according to the business model.
    """
    if _bootstrap_bypass() or is_super_admin():
        return {"all"}
    scopes = {
        row["data_scope"] for row in _load_permission_rows()
        if row.get("permission_code") == permission_code and row.get("data_scope")
    }
    return scopes


def can_view_finished_goods_cost():
    # 维护成本的角色必然需要在界面上看到成本字段。
    return has_permission("finished_goods.cost.view") or has_permission("finished_goods.cost.edit")


def can_edit_finished_goods_cost():
    return has_permission("finished_goods.cost.edit")
