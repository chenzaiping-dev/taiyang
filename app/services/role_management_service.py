"""Role and permission administration service.

Uses the existing RBAC tables; no ORM models or schema changes are required.
"""
import re
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from app.extensions import db

ALLOWED_DATA_SCOPES = {"all", "own", "assigned", "factory"}
ROLE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{1,49}$")


def list_roles():
    return db.session.execute(text("""
        SELECT r.id, r.role_code, r.role_name, r.description, r.status,
               COUNT(DISTINCT er.employee_id) AS employee_count,
               COUNT(DISTINCT rp.permission_id) AS permission_count
          FROM roles r
          LEFT JOIN employee_roles er ON er.role_id = r.id
          LEFT JOIN role_permissions rp ON rp.role_id = r.id
         GROUP BY r.id, r.role_code, r.role_name, r.description, r.status
         ORDER BY CASE WHEN r.role_code = 'super_admin' THEN 0 ELSE 1 END,
                  r.id
    """)).mappings().all()


def get_role(role_id):
    return db.session.execute(text("""
        SELECT id, role_code, role_name, description, status
          FROM roles WHERE id = :role_id
    """), {"role_id": role_id}).mappings().first()


def list_permissions_with_grants(role_id):
    rows = db.session.execute(text("""
        SELECT p.id, p.permission_code, p.permission_name, p.module_code,
               p.description, rp.data_scope,
               CASE WHEN rp.permission_id IS NULL THEN 0 ELSE 1 END AS granted
          FROM permissions p
          LEFT JOIN role_permissions rp
            ON rp.permission_id = p.id AND rp.role_id = :role_id
         ORDER BY p.module_code, p.permission_code
    """), {"role_id": role_id}).mappings().all()
    grouped = {}
    for row in rows:
        item = dict(row)
        grouped.setdefault(item["module_code"], []).append(item)
    return grouped


def list_employees_with_role(role_id):
    return db.session.execute(text("""
        SELECT e.id, e.username, e.real_name, e.phone, e.status,
               CASE WHEN er.employee_id IS NULL THEN 0 ELSE 1 END AS assigned
          FROM employees e
          LEFT JOIN employee_roles er
            ON er.employee_id = e.id AND er.role_id = :role_id
         ORDER BY e.status DESC, e.real_name, e.id
    """), {"role_id": role_id}).mappings().all()


def create_role(role_code, role_name, description=""):
    role_code = (role_code or "").strip().lower()
    role_name = (role_name or "").strip()
    description = (description or "").strip() or None
    if role_code == "super_admin":
        raise ValueError("super_admin 是系统保留角色，请通过权限迁移 SQL 初始化。")
    if not ROLE_CODE_RE.fullmatch(role_code):
        raise ValueError("角色编码须为 2-50 位小写字母、数字或下划线，且以字母开头。")
    if not role_name or len(role_name) > 50:
        raise ValueError("角色名称不能为空，且最多 50 个字符。")
    if len(description or "") > 255:
        raise ValueError("角色说明最多 255 个字符。")
    db.session.execute(text("""
        INSERT INTO roles (role_code, role_name, description, status)
        VALUES (:role_code, :role_name, :description, 1)
    """), {"role_code": role_code, "role_name": role_name, "description": description})
    db.session.commit()


def save_role_permissions(role_id, submitted_permission_ids, submitted_scopes):
    """Replace a role's permission set atomically; unknown IDs/scopes are rejected."""
    role = get_role(role_id)
    if not role:
        raise LookupError("角色不存在。")
    if role["role_code"] == "super_admin":
        raise ValueError("超级管理员角色拥有系统全部权限，不允许在此修改权限清单。")

    ids = set()
    for raw in submitted_permission_ids:
        try:
            ids.add(int(raw))
        except (TypeError, ValueError):
            raise ValueError("提交的权限编号无效。")

    known = db.session.execute(text("SELECT id FROM permissions")).scalars().all()
    known_ids = set(known)
    if not ids.issubset(known_ids):
        raise ValueError("提交内容包含不存在的权限，请刷新页面后重试。")

    scopes = {}
    for permission_id in ids:
        scope = (submitted_scopes.get(str(permission_id)) or "all").strip()
        if scope not in ALLOWED_DATA_SCOPES:
            raise ValueError(f"权限 {permission_id} 的数据范围无效。")
        scopes[permission_id] = scope

    try:
        db.session.execute(text("DELETE FROM role_permissions WHERE role_id = :role_id"), {"role_id": role_id})
        if scopes:
            db.session.execute(text("""
                INSERT INTO role_permissions (role_id, permission_id, data_scope)
                VALUES (:role_id, :permission_id, :data_scope)
            """), [
                {"role_id": role_id, "permission_id": permission_id, "data_scope": scope}
                for permission_id, scope in scopes.items()
            ])
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise


def save_role_employees(role_id, submitted_employee_ids, current_employee_id):
    """Replace employee-role assignments atomically."""
    role = get_role(role_id)
    if not role:
        raise LookupError("角色不存在。")
    employee_ids = set()
    for raw in submitted_employee_ids:
        try:
            employee_ids.add(int(raw))
        except (TypeError, ValueError):
            raise ValueError("提交的员工编号无效。")

    active_ids = set(db.session.execute(text("""
        SELECT id FROM employees WHERE status = 1
    """)).scalars().all())
    if not employee_ids.issubset(active_ids):
        raise ValueError("只能新增启用状态员工的角色分配，请刷新页面后重试。")

    # Preserve existing assignments for disabled employees. They are shown as
    # checked/disabled in the UI and cannot be newly assigned while inactive.
    existing_inactive_ids = set(db.session.execute(text("""
        SELECT er.employee_id
          FROM employee_roles er
          JOIN employees e ON e.id = er.employee_id
         WHERE er.role_id = :role_id AND e.status <> 1
    """), {"role_id": role_id}).scalars().all())
    employee_ids.update(existing_inactive_ids)

    if role["role_code"] == "super_admin":
        if not employee_ids:
            raise ValueError("超级管理员角色至少必须分配给一名员工。")
        if current_employee_id not in employee_ids:
            raise ValueError("不能通过当前页面移除自己的超级管理员身份。")

    try:
        db.session.execute(text("DELETE FROM employee_roles WHERE role_id = :role_id"), {"role_id": role_id})
        if employee_ids:
            db.session.execute(text("""
                INSERT INTO employee_roles (employee_id, role_id)
                VALUES (:employee_id, :role_id)
            """), [
                {"employee_id": employee_id, "role_id": role_id}
                for employee_id in sorted(employee_ids)
            ])
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise


def set_role_status(role_id, status):
    role = get_role(role_id)
    if not role:
        raise LookupError("角色不存在。")
    if role["role_code"] == "super_admin" and not status:
        raise ValueError("超级管理员角色不能停用。")
    db.session.execute(text("UPDATE roles SET status = :status WHERE id = :role_id"), {
        "status": 1 if status else 0, "role_id": role_id
    })
    db.session.commit()
