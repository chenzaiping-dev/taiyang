import os
from flask import Flask, redirect, url_for
from config import Config
from app.extensions import db, csrf
from app.core.reflection import reflect_database
from app.core.auth import auth_bp
from app.core.admin import admin_bp
from app.core.documents import documents_bp


def create_app(test_config=None):
    app = Flask(__name__, template_folder="../templates")
    app.config.from_object(Config)
    if test_config:
        app.config.update(test_config)

    if not app.config.get("SQLALCHEMY_DATABASE_URI"):
        raise RuntimeError("请在 .env 中配置 DATABASE_URL")

    db.init_app(app)
    csrf.init_app(app)

    with app.app_context():
        # 反射现有 taiyang 数据库，不会自动创建或修改任何业务表
        reflect_database(db)

    from app.generated.registry import GENERATED_BLUEPRINTS

    for module_path, expected_name in GENERATED_BLUEPRINTS:
        module = __import__(module_path, fromlist=["bp"])
        print(module.bp)
        app.register_blueprint(module.bp)

    from app.controllers.purchase_in_controller import bp as purchase_bp
    app.register_blueprint(purchase_bp)

    from app.controllers.stock_out_controller import bp as stock_out_bp
    app.register_blueprint(stock_out_bp)

    from app.controllers.process_order_controller import bp as process_bp
    app.register_blueprint(process_bp)

    from app.controllers.inventory_query_controller import bp as inventory_query_bp
    app.register_blueprint(inventory_query_bp)

    from app.controllers.finished_goods_controller import bp as fg_bp
    app.register_blueprint(fg_bp)

    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(documents_bp)

    @app.get("/")
    def index():
        return redirect(url_for("admin.dashboard"))

    return app

