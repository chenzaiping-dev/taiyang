import os
from functools import wraps
from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash
from sqlalchemy import select
from app.core.reflection import get_table

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("employee_id"):
            print("User not logged in")
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        # Starter scaffold: only the bootstrap admin session is allowed.
        # Replace this with employee_roles + role_permissions checks before production.
        if not session.get("is_admin"):
            flash("需要管理员权限。", "warning")
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)
    return wrapped


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        # Optional bootstrap login is for initial development only.
        bootstrap_user = os.getenv("BOOTSTRAP_ADMIN_USERNAME", "admin")
        bootstrap_password = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "")
        if bootstrap_password and username == bootstrap_user and password == bootstrap_password:
            session.clear()
            print(f"Bootstrap admin login: {username}")
            session["employee_id"] = 1
            session["username"] = username
            session["is_admin"] = True
            session["logged_in"] = True
            return redirect(url_for("admin.dashboard"))
        flash("账号或密码错误。请配置初始管理员，或实现 employees 表登录。", "danger")
    return render_template("auth/login.html")


@auth_bp.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
