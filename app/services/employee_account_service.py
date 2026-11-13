"""Employee account administration and password reset helpers."""
from sqlalchemy import text
from werkzeug.security import generate_password_hash
from app.extensions import db

DEFAULT_INITIAL_PASSWORD = "123456"


def create_employee(username: str, real_name: str, phone: str = "", nickname: str = ""):
    username = (username or "").strip()
    real_name = (real_name or "").strip()
    phone = (phone or "").strip() or None
    nickname = (nickname or "").strip() or None
    if not username or len(username) > 50:
        raise ValueError("员工账号不能为空，且最多 50 个字符。")
    if not real_name or len(real_name) > 50:
        raise ValueError("员工姓名不能为空，且最多 50 个字符。")
    if len(phone or "") > 30 or len(nickname or "") > 50:
        raise ValueError("联系电话或代号长度超出限制。")
    password_hash = generate_password_hash(DEFAULT_INITIAL_PASSWORD, method="pbkdf2:sha256")
    try:
        result = db.session.execute(text("""
            INSERT INTO employees
                (username, password_hash, must_change_password, real_name, phone, nickname, status)
            VALUES
                (:username, :password_hash, 1, :real_name, :phone, :nickname, 1)
        """), {
            "username": username,
            "password_hash": password_hash,
            "real_name": real_name,
            "phone": phone,
            "nickname": nickname,
        })
        db.session.commit()
        return result.lastrowid
    except Exception:
        db.session.rollback()
        raise


def list_employees():
    return db.session.execute(text("""
        SELECT id, username, real_name, phone, nickname, status,
               COALESCE(must_change_password, 0) AS must_change_password
          FROM employees
         ORDER BY status DESC, id
    """)).mappings().all()


def reset_employee_password(employee_id: int, new_password: str = DEFAULT_INITIAL_PASSWORD):
    employee = db.session.execute(text("""
        SELECT id, username FROM employees WHERE id = :employee_id
    """), {"employee_id": employee_id}).mappings().first()
    if not employee:
        raise LookupError("员工不存在。")
    if len(new_password) < 6:
        raise ValueError("初始密码至少需要 6 位。")
    password_hash = generate_password_hash(new_password, method="pbkdf2:sha256")
    db.session.execute(text("""
        UPDATE employees
           SET password_hash = :password_hash,
               must_change_password = 1
         WHERE id = :employee_id
    """), {"password_hash": password_hash, "employee_id": employee_id})
    db.session.commit()
    return employee
