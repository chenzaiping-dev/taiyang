# 太洋 ERP：业务模块 RBAC 权限接入说明

本补丁在现有 `employees / roles / permissions / employee_roles / role_permissions` 五张表上扩展权限目录，不新增 RBAC 表。

## 1. 升级

已有数据库执行：

```sql
SOURCE migrations/20261113_business_module_permissions.sql;
```

新建数据库使用 `user.sql`，本补丁已将新增权限目录一并写入 `user.sql`。

迁移脚本只写入 `permissions` 字典，不会自动给任何角色授权。请登录超级管理员账号，在「角色权限管理」页面按岗位分配权限。

## 2. 权限模块

| 模块编码 | 模块 | 主要权限 |
|---|---|---|
| `inventory` | 原料管理 | 原料资料、供应商资料、库存、入库、出库、单据、流水、成本 |
| `process_order` | 工艺单 | 查看、新建、编辑、下单、生产、入库、取消、配比调整 |
| `production` | 生产计划 | 查看、新建、编辑、确认、执行、完成、取消、重新打开 |
| `shipment` | 发货管理 | 查看、新建、编辑、确认、发货、取消、金额 |
| `sales_performance` | 业绩表管理 | 查看、新建、编辑、状态流转、金额 |

## 3. 金额字段权限

金额查看和维护分开配置：

| 模块 | 查看权限 | 维护权限 | 控制字段 |
|---|---|---|---|
| 原料 | `inventory.cost.view` | `inventory.cost.edit` | 采购单价、FIFO 单价、成本金额、库存成本 |
| 工艺单/生产 | `production.cost.view` | `production.cost.edit` | 预计单位成本、预计总成本、实际成本、原料成本 |
| 发货 | `shipment.money.view` | `shipment.money.edit` | 发货明细单价 |
| 业绩表 | `sales_performance.money.view` | `sales_performance.money.edit` | 销售单价、销售金额、成本、利润 |
| 成品 | `finished_goods.cost.view` | `finished_goods.cost.edit` | 沿用上一版成品成本权限 |

`*.edit` 同时允许查看对应金额。没有金额维护权限时，Controller 会忽略客户端提交的金额字段；更新已有记录时保留数据库原值。原料采购入库必须同时拥有 `inventory.purchase_in.create` 和 `inventory.cost.edit`，因为采购入库必须录入采购单价。

对于 JSON API，未获得成本查看权限的调用者不会收到成本字段。

## 4. 路由保护

`app/core/business_permission_hooks.py` 将业务 Blueprint 的每个 endpoint 映射到一个或多个权限编码。已纳入：

- `inventory_query`
- `purchase_in`
- `stock_out`
- `process_order`
- `production_plan`
- `shipment_plan`
- `sales_performance`

受管 Blueprint 中未登记的新增 endpoint 会默认返回 403（fail closed）。新增路由时必须先在 `ENDPOINT_REQUIREMENTS` 中声明权限。

原料资料和供应商资料通过通用 CRUD 路由 `/admin/t/materials`、`/admin/t/suppliers` 访问，分别使用 `inventory.materials.*`、`inventory.suppliers.*` 权限。其他通用 CRUD 表仍保留管理员访问限制。

## 5. 数据范围说明

`role_permissions.data_scope` 仍支持 `all / own / assigned / factory`。本补丁尚未为所有业务查询建立统一的 owner、assignment、factory 关联过滤器。

为避免将 `own / assigned / factory` 错误地当作全部数据访问，本补丁采取 fail-closed：业务权限的数据范围不是 `all` 时，相关路由暂时拒绝访问。待每个模块的查询过滤实现并测试后，再开放对应数据范围。不要仅在角色页面选择非 `all` 就认为数据隔离已经完成。

## 6. 安全和兼容说明

- 菜单/按钮隐藏只是 UX；真正授权在服务端路由执行。
- 超级管理员仍按现有 `super_admin` 角色判断。
- 不把普通 `admin` 自动视为 `super_admin`。
- 原料采购入库必须拥有成本维护权限。
- 生产计划完成时系统自动计算成本/利润属于业务计算，不依赖员工手动编辑成本权限；页面/API 输出仍按金额查看权限控制。
- `ALLOW_BOOTSTRAP_ADMIN_BYPASS` 仅用于本地开发。生产环境保持 `false`，通过员工账号和 `super_admin` 角色管理。

## 7. 检查

本次静态检查包括 Python AST 语法解析、Jinja 模板解析，以及业务 Blueprint 路由与权限映射覆盖检查。由于附件没有 `app/generated/` 反射生成产物，且执行环境没有连接项目 MySQL，未进行真实 Flask 启动、数据库读写或浏览器端到端测试。
