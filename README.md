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
- 管理员登录： http://127.0.0.1:5000/auth/admin/login
员工登录： http://127.0.0.1:5000/auth/employee/login
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


## RBAC 权限框架（本次新增）

本次在已有 `employees / roles / permissions / employee_roles / role_permissions` 五张表上接入权限校验，不新增重复的 RBAC 主表。成品管理是第一批接入模块。

### 1. 执行权限目录迁移

```sql
SOURCE migrations/20261113_finished_goods_permissions.sql;
```

该脚本新增 `super_admin` 角色和成品管理权限字典，但**不会自动给现有角色授权**。请根据岗位职责，在 `employee_roles`、`role_permissions` 中分配角色与权限。不要把普通 `admin` 自动当作超级管理员。

例如，给指定员工授予超级管理员角色（先把用户名替换为真实账号）：

```sql
INSERT INTO employee_roles (employee_id, role_id)
SELECT e.id, r.id
FROM employees e JOIN roles r ON r.role_code = 'super_admin'
WHERE e.username = 'your_admin_username'
ON DUPLICATE KEY UPDATE employee_id = VALUES(employee_id);
```

给某个业务角色授予一组成品权限（示例为全部成品权限；请按岗位删减）：

```sql
INSERT INTO role_permissions (role_id, permission_id, data_scope)
SELECT r.id, p.id, 'all'
FROM roles r JOIN permissions p ON p.module_code = 'finished_goods'
WHERE r.role_code = 'order_followup'
ON DUPLICATE KEY UPDATE data_scope = VALUES(data_scope);
```


### 2. 成品管理权限编码

| 权限编码 | 作用 |
| --- | --- |
| `finished_goods.menu.view` | 成品菜单入口 |
| `finished_goods.inventory.view` | 成品库存数量 |
| `finished_goods.cost.view` | 查看成品单价、库存成本 |
| `finished_goods.cost.edit` | 录入/修改成品成本 |
| `finished_goods.inbound.create` | 创建入库单、搜索来源工艺单 |
| `finished_goods.outbound.create` | 创建出库单 |
| `finished_goods.documents.view` | 查看入库/出库单据 |
| `finished_goods.documents.confirm` | 确认单据并产生库存变化 |
| `finished_goods.documents.edit` | 编辑草稿单据 |
| `finished_goods.documents.reverse` | 撤销已确认单据 |
| `finished_goods.transactions.view` | 查看成品流水 |
| `finished_goods.summary.view` | 查看出入库汇总 |

`role_permissions.data_scope` 可使用 `all / own / assigned / factory`。`PermissionService.get_data_scopes()` 已提供统一读取入口；当前成品查询尚未把 `own / assigned / factory` 自动转换成查询过滤条件，因为现有员工表没有 `factory_id`，也没有通用的员工-仓库/工厂授权关系表。**当前成品模块按权限编码鉴权，数据范围需在确定业务归属字段/关联表后再接入查询 Service；不能把读取到 data_scope 误认为数据已经隔离。**

### 3. 超级管理员

系统识别 `roles.role_code = 'super_admin'` 为超级管理员，该角色拥有所有权限编码。必须由可信人员手动给指定员工分配该角色。普通 `admin` 角色没有隐式全权限。

员工登录已接入 `employees` 表：只允许 `status=1` 的员工登录，并使用 Werkzeug `password_hash` 校验密码；登录成功后设置 `session['employee_id']`，权限由员工角色关联实时加载。`user.sql` 中的示例 `hashed_password1` 等不是可用的 Werkzeug 哈希，正式使用前需为员工设置有效密码哈希。

开发阶段的 bootstrap 登录是独立的显式绕过：`.env` 中 `ALLOW_BOOTSTRAP_ADMIN_BYPASS=true` 才会在成品模块放行；生产环境必须保持 `false`，并清空 `BOOTSTRAP_ADMIN_PASSWORD`。bootstrap 账号使用占位员工 ID=1，仅供本地开发，不应用于真实库存业务。

### 4. 本次代码接入范围

- `app/services/permission_service.py`：RBAC 查询、超级管理员判断、字段权限、数据范围读取。
- `app/core/permission_hooks.py`：成品管理 Blueprint 的集中路由权限映射。
- `app/__init__.py`：注册权限钩子和 Jinja 权限辅助函数。
- `templates/inventory/base.html`：按权限隐藏成品管理菜单项。
- `templates/finished_goods/`：按字段权限隐藏成本单价/库存成本；无成本维护权限时，服务端忽略伪造的成本 POST 字段。

其他原料、生产、发货、业绩表模块尚未接入路由权限，后续可沿用同一服务与 `ENDPOINT_PERMISSIONS` 模式逐模块迁移。员工密码哈希也已从通用员工 CRUD 的列表/表单字段中移除，密码应通过独立重置流程维护。


## 角色权限管理页面（本次新增）

超级管理员登录后，在管理首页点击“角色权限管理”，或访问 `/admin/roles`。页面包括：

- 角色列表及角色创建；
- 角色权限矩阵，按 `permissions.module_code` 分组；
- 每个权限独立设置 `all / own / assigned / factory` 数据范围；
- 将启用员工分配给角色；
- 角色启用/停用；
- `super_admin` 自动拥有全部权限，权限清单不可编辑，角色不可停用；
- 不允许通过页面移除当前登录员工的超级管理员身份，也不允许把超级管理员角色清空。

角色权限管理路由由 `is_super_admin()` 保护，不依赖前端隐藏按钮。页面沿用现有 `roles / permissions / role_permissions / employees / employee_roles` 表，不需要新增数据库表。

主要文件：

- `app/controllers/role_management_controller.py`：页面路由与超级管理员保护；
- `app/services/role_management_service.py`：角色、权限、员工角色关联的事务操作；
- `templates/admin/roles.html`：角色列表与创建；
- `templates/admin/role_permissions.html`：权限矩阵、数据范围和员工分配。

注意：`data_scope` 当前只是配置并保存。除非具体业务查询 Service 实现对应的 `own / assigned / factory` 过滤条件，否则不能认为业务数据已经按范围隔离。

## 员工登录与密码管理

- 管理员登录：`/auth/admin/login`，使用 `.env` 中配置的 `BOOTSTRAP_ADMIN_USERNAME` / `BOOTSTRAP_ADMIN_PASSWORD`（开发引导账号）。
- 员工登录：`/auth/employee/login`，使用 `employees.username` 和 `employees.password_hash`。
- 员工修改密码：`/auth/change-password`，员工登录后可自行修改；新密码至少 8 位。
- 超级管理员员工账号管理：`/admin/employees`，可查看员工并将密码重置为初始密码 `123456`。重置后员工下次登录必须先修改密码。

升级已有数据库时执行 `migrations/20261113_employee_password_management.sql`，新增 `employees.must_change_password` 字段。该字段用于强制首次登录修改密码。若使用全新数据库，`user.sql` 已包含该字段。

密码哈希不能由普通 SQL 明文计算。可使用项目 Python 环境中的 Werkzeug `generate_password_hash('123456', method='pbkdf2:sha256')` 生成哈希，再将哈希写入 `employees.password_hash`。示例：

```python
from werkzeug.security import generate_password_hash
print(generate_password_hash('123456', method='pbkdf2:sha256'))
```

将生成的字符串粘贴到 SQL：

```sql
UPDATE employees
SET password_hash = '<上一步生成的完整哈希>',
    must_change_password = 1
WHERE username = '员工账号';
```

正式环境不要将明文密码或密码哈希写入版本控制，也不要通过 SQL 日志、聊天记录或工单公开真实员工密码。建议管理员通过员工账号管理页面执行重置。

### 示例员工初始密码 SQL（仅开发/测试）

`user.sql` 中的 chen1 至 chen4 示例账号初始密码均为 `123456`，并设置 `must_change_password=1`。首次登录后会强制修改。若要给已有员工设置同样的初始密码，可先在项目虚拟环境执行：

```bash
python scripts/generate_password_hash.py 123456
```

复制终端输出的完整哈希后执行（把占位符替换为生成结果）：

```sql
UPDATE employees
SET password_hash = '<Werkzeug生成的完整哈希>',
    must_change_password = 1
WHERE username = 'chen1';
```

不要在生产环境给所有员工长期使用同一个初始密码；应由管理员逐个重置，并要求员工首次登录立即修改。


## 业务模块 RBAC 权限

本版本已将原料、工艺单、生产计划、发货计划和业绩表接入统一路由权限校验，并为原料成本、生产成本、发货单价、业绩金额/成本/利润增加独立的查看/维护权限。

已有数据库执行：

```sql
SOURCE migrations/20261113_business_module_permissions.sql;
```

新建数据库请使用更新后的 `user.sql`。迁移只增加权限字典，不会自动给角色授权；请在「角色权限管理」页面按岗位分配。

详细权限编码、金额字段规则、路由映射及数据范围限制请见 [RBAC_MODULE_PERMISSIONS.md](RBAC_MODULE_PERMISSIONS.md)。
