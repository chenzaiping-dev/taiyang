"""Super-admin-only UI for role and permission administration."""
from functools import wraps
from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for
from app.core.auth import login_required
from app.services.permission_service import is_super_admin
from app.services import role_management_service as service

bp = Blueprint("role_management", __name__, url_prefix="/admin/roles")


def super_admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_super_admin():
            abort(403)
        return view(*args, **kwargs)
    return wrapped


@bp.get("")
@login_required
@super_admin_required
def role_list():
    return render_template("admin/roles.html", roles=service.list_roles())


@bp.post("/create")
@login_required
@super_admin_required
def role_create():
    try:
        service.create_role(
            request.form.get("role_code"),
            request.form.get("role_name"),
            request.form.get("description"),
        )
        flash("角色创建成功。", "success")
    except ValueError as exc:
        flash(str(exc), "warning")
    except Exception:
        from app.extensions import db
        db.session.rollback()
        flash("角色创建失败：角色编码可能已经存在。", "danger")
    return redirect(url_for("role_management.role_list"))


@bp.route("/<int:role_id>", methods=["GET", "POST"])
@login_required
@super_admin_required
def role_detail(role_id):
    role = service.get_role(role_id)
    if role is None:
        abort(404)

    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "save_permissions":
                if role["role_code"] == "super_admin":
                    raise ValueError("超级管理员角色拥有系统全部权限，不允许在此修改权限清单。")
                service.save_role_permissions(
                    role_id,
                    request.form.getlist("permission_ids"),
                    request.form,
                )
                flash("角色权限保存成功。", "success")
            elif action == "save_employees":
                service.save_role_employees(
                    role_id,
                    request.form.getlist("employee_ids"),
                    session.get("employee_id"),
                )
                flash("员工角色分配保存成功。", "success")
            elif action == "toggle_status":
                service.set_role_status(role_id, request.form.get("status") == "1")
                flash("角色状态已更新。", "success")
            else:
                abort(400)
        except (ValueError, LookupError) as exc:
            flash(str(exc), "warning")
        except Exception:
            from app.extensions import db
            db.session.rollback()
            flash("保存失败，请检查数据库约束或日志。", "danger")
        return redirect(url_for("role_management.role_detail", role_id=role_id))

    return render_template(
        "admin/role_permissions.html",
        role=role,
        permission_groups=service.list_permissions_with_grants(role_id),
        employees=service.list_employees_with_role(role_id),
        data_scopes=service.ALLOWED_DATA_SCOPES,
    )
