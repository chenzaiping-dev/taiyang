import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-me")
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    DEBUG = os.getenv("FLASK_DEBUG", "0") == "1"
    WTF_CSRF_TIME_LIMIT = None

    # 开发引导账号绕过 RBAC；生产环境必须设置为 False。
    ALLOW_BOOTSTRAP_ADMIN_BYPASS = os.getenv("ALLOW_BOOTSTRAP_ADMIN_BYPASS", "false").lower() == "true"
