from flask import Blueprint, abort, flash, redirect, render_template, session, url_for
from app.core.auth import login_required
from app.services.permission_service import is_super_admin
from app.services import employee_account_service as service
from app.extensions import db

bp = Blueprint("employee_accounts", __name__, url_prefix="/admin/employees")


def super_admin_only(view):
    from functools import wraps
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_super_admin():
            abort(403)
        return view(*args, **kwargs)
    return wrapped


@bp.get("")
@login_required
@super_admin_only
def employee_list():
    return render_template("admin/employees.html", employees=service.list_employees())


@bp.post("/create")
@login_required
@super_admin_only
def employee_create():
    try:
        employee_id = service.create_employee(
            request.form.get("username"),
            request.form.get("real_name"),
            request.form.get("phone"),
            request.form.get("nickname"),
        )
        flash(f"员工创建成功，员工 ID：{employee_id}；初始密码为 123456，请及时分配角色。", "success")
    except ValueError as exc:
        flash(str(exc), "warning")
    except Exception:
        db.session.rollback()
        flash("员工创建失败：请检查账号是否重复，以及数据库迁移是否完成。", "danger")
    return redirect(url_for("employee_accounts.employee_list"))


@bp.post("/<int:employee_id>/reset-password")
@login_required
@super_admin_only
def reset_password(employee_id):
    try:
        employee = service.reset_employee_password(employee_id)
        if session.get("employee_id") == employee_id:
            session["must_change_password"] = True
        flash(
            f"员工 {employee['real_name'] or employee['username']} 的密码已重置为 123456；员工下次登录必须修改密码。",
            "success",
        )
    except LookupError as exc:
        flash(str(exc), "warning")
    except Exception:
        db.session.rollback()
        flash("密码重置失败，请检查数据库是否已执行密码管理迁移。", "danger")
    return redirect(url_for("employee_accounts.employee_list"))
