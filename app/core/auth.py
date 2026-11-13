import os
from functools import wraps

from flask import (
    Blueprint, abort, flash, redirect, render_template, request, session, url_for
)
from sqlalchemy import text
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db


auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


@auth_bp.before_app_request
def enforce_first_login_password_change():
    """Force employees with a reset/initial password to change it before use."""
    if not session.get("logged_in") or session.get("auth_source") != "employee":
        return None
    if not session.get("must_change_password"):
        return None
    allowed = {"auth.change_password", "auth.logout"}
    if request.endpoint in allowed or request.path.startswith("/static/"):
        return None
    return redirect(url_for("auth.change_password"))


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            login_endpoint = "auth.admin_login" if request.path.startswith("/admin") else "auth.employee_login"
            return redirect(url_for(login_endpoint, next=request.path))
        return view(*args, **kwargs)
    return wrapped


def employee_login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in") or session.get("auth_source") != "employee":
            return redirect(url_for("auth.employee_login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("auth.admin_login", next=request.path))
        from app.services.permission_service import is_super_admin
        if not session.get("is_admin") and not is_super_admin():
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def _safe_next_url(target):
    # Only allow local relative paths to avoid open redirects.
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return None


def _employee_by_username(username):
    row = db.session.execute(text("""
        SELECT id, username, password_hash, real_name, status,
               COALESCE(must_change_password, 0) AS must_change_password
          FROM employees
         WHERE username = :username
         LIMIT 1
    """), {"username": username}).mappings().first()
    return row


def _employee_login_success(employee):
    session.clear()
    session["employee_id"] = int(employee["id"])
    session["username"] = employee.get("real_name") or employee["username"]
    session["is_admin"] = False
    session["auth_source"] = "employee"
    session["logged_in"] = True
    session["must_change_password"] = bool(employee.get("must_change_password"))


def _employee_landing():
    from app.services.permission_service import has_permission, is_super_admin
    if is_super_admin():
        return redirect(url_for("admin.dashboard"))

    # Choose the first module the employee can actually open.
    if has_permission("sales_performance.view"):
        return redirect(url_for("sales_performance.list_page"))
    if has_permission("sales_performance.create"):
        return redirect(url_for("sales_performance.new_page"))
    if has_permission("inventory.stock.view"):
        return redirect(url_for("inventory_query.material_stock_page"))
    if has_permission("inventory.purchase_in.create") and has_permission("inventory.cost.edit"):
        return redirect(url_for("purchase_in.purchase_in_page"))
    if has_permission("inventory.stock_out.create"):
        return redirect(url_for("stock_out.stock_out_page"))
    if has_permission("inventory.documents.view"):
        return redirect(url_for("inventory_query.document_list_page", direction="in"))
    if has_permission("inventory.summary.view"):
        return redirect(url_for("inventory_query.material_io_summary_page"))
    if has_permission("inventory.transactions.view"):
        return redirect(url_for("inventory_query.material_transactions_page"))
    if has_permission("inventory.materials.view"):
        return redirect(url_for("admin.table_list", table_name="materials"))
    if has_permission("process_order.view"):
        return redirect(url_for("process_order.list_page"))
    if has_permission("process_order.create"):
        return redirect(url_for("process_order.new_page"))
    if has_permission("production_plan.view"):
        return redirect(url_for("production_plan.list_page"))
    if has_permission("shipment.view"):
        return redirect(url_for("shipment_plan.list_page"))
    if has_permission("finished_goods.inventory.view"):
        return redirect(url_for("finished_goods.warehouses_page"))
    if has_permission("finished_goods.inbound.create"):
        return redirect(url_for("finished_goods.inbound_page"))
    if has_permission("finished_goods.outbound.create"):
        return redirect(url_for("finished_goods.outbound_page"))
    if has_permission("finished_goods.documents.view"):
        return redirect(url_for("finished_goods.document_list_page", direction="in"))
    return render_template("auth/no_permissions.html"), 403


@auth_bp.route("/login", methods=["GET"])
def login():
    """Backward-compatible alias: /auth/login opens the administrator login."""
    return redirect(url_for("auth.admin_login"))


@auth_bp.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        bootstrap_user = os.getenv("BOOTSTRAP_ADMIN_USERNAME", "admin")
        bootstrap_password = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "")
        if bootstrap_password and username == bootstrap_user and password == bootstrap_password:
            session.clear()
            session["username"] = username
            session["is_admin"] = True
            session["auth_source"] = "bootstrap"
            session["logged_in"] = True
            target = _safe_next_url(request.args.get("next") or request.form.get("next"))
            return redirect(target or url_for("admin.dashboard"))
        flash("管理员账号或密码错误。", "danger")
    return render_template("auth/admin_login.html")


@auth_bp.route("/employee/login", methods=["GET", "POST"])
def employee_login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        try:
            employee = _employee_by_username(username)
        except Exception:
            db.session.rollback()
            employee = None

        password_ok = False
        if employee and employee["status"] == 1:
            try:
                password_ok = check_password_hash(employee["password_hash"], password)
            except (TypeError, ValueError):
                password_ok = False

        if employee and employee["status"] == 1 and password_ok:
            _employee_login_success(employee)
            if session.get("must_change_password"):
                flash("这是初始密码，请先修改密码再继续使用。", "warning")
                return redirect(url_for("auth.change_password"))
            target = _safe_next_url(request.args.get("next") or request.form.get("next"))
            return redirect(target) if target else _employee_landing()
        flash("员工账号或密码错误，或员工账号已停用。", "danger")
    return render_template("auth/employee_login.html")


@auth_bp.route("/change-password", methods=["GET", "POST"])
@employee_login_required
def change_password():
    if request.method == "POST":
        old_password = request.form.get("old_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")
        employee_id = session.get("employee_id")
        employee = db.session.execute(text("""
            SELECT id, password_hash FROM employees
             WHERE id = :employee_id AND status = 1
        """), {"employee_id": employee_id}).mappings().first()
        if not employee:
            session.clear()
            flash("员工账号不存在或已停用，请重新登录。", "warning")
            return redirect(url_for("auth.employee_login"))
        try:
            old_ok = check_password_hash(employee["password_hash"], old_password)
        except (TypeError, ValueError):
            old_ok = False
        if not old_ok:
            flash("当前密码不正确。", "danger")
        elif len(new_password) < 8:
            flash("新密码至少需要 8 位。", "warning")
        elif new_password != confirm_password:
            flash("两次输入的新密码不一致。", "warning")
        elif new_password == old_password:
            flash("新密码不能与当前密码相同。", "warning")
        else:
            password_hash = generate_password_hash(new_password, method="pbkdf2:sha256")
            db.session.execute(text("""
                UPDATE employees
                   SET password_hash = :password_hash,
                       must_change_password = 0
                 WHERE id = :employee_id
            """), {"password_hash": password_hash, "employee_id": employee_id})
            db.session.commit()
            session["must_change_password"] = False
            flash("密码修改成功。", "success")
            return _employee_landing()
    return render_template("auth/change_password.html")


@auth_bp.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.employee_login"))
