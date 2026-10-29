# 太洋业务管理系统：Flask + MVC + Jinja2 脚手架

本项目对应开发顺序 1、2、3、4：
1. 最小数据库反射/代码生成入口；
2. 原料、供应商 CRUD；
3. 加工厂、仓库及员工/角色/权限只读入口；
4. 库存单据列表与确认流程占位。

## 技术栈
- Flask App Factory + Blueprint
- SQLAlchemy 2.x / Flask-SQLAlchemy
- MySQL + PyMySQL
- Jinja2 + Bootstrap
- Flask-WTF CSRF

## 安装与运行

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
copy .env.example .env  # Windows
# cp .env.example .env  # macOS/Linux
```

编辑 `.env`，设置真实数据库地址和初始管理员密码。确保 MySQL 已经执行你的 `user.sql`，且数据库名为 `taiyang`。

```bash
python run.py
```

访问：
- 登录： http://127.0.0.1:5000/auth/login
- 管理首页： http://127.0.0.1:5000/admin

初始管理员从 `.env` 读取。不要将示例密码用于正式环境。

## 运行结构反射/代码生成入口

```bash
python -m codegen.introspect
```

输出 `build/schema.json`，其中包含字段、主键、外键、索引和唯一约束。

当前版本采用“反射元数据 + 配置驱动通用 CRUD”，先避免大量重复生成 Python 文件。若希望生成独立模型和 Controller，可在此基础上为 `codegen/templates/` 增加 Jinja2 模板，并输出到 `app/generated/`。

## 重要边界
- 未配置在 `app/core/table_config.py` 中的表不允许访问。
- `inventory_transactions`、`inventory_cost_layers`、`inventory_cost_allocations`、`finished_goods_transactions` 和 `finished_goods_inventory` 只读。
- 库存单据确认是占位入口，不会修改数据库库存。实现真实确认前必须新增专门的 Service 和事务测试。
- 当前登录是开发用 bootstrap admin，不是完整的员工/角色权限体系。上线前必须实现真实认证、角色权限、审计日志、速率限制和安全配置。
- 此脚手架没有实现生产/发货的真实状态流转，不应直接用于生产环境。
- 本项目不运行 `db.create_all()`，也不修改现有数据库表结构。

## 当前范围
基础资料 CRUD 已有可运行的通用入口。正式上线前建议继续增加：
- 服务器端表单验证（长度、必填、枚举值、范围）
- 统一分页、排序白名单、字段格式化
- 每张表独立权限
- 审计日志
- 测试库集成测试
- 业务 Service（库存、FIFO、生产、发货）
