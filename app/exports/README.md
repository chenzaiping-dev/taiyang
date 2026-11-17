# Excel 导出架构

本次增加的 Excel 导出统一放在 `app/exports/`：

- `queries.py`：业务查询层。列表页与导出页共用 `run_query()`，筛选条件和查询 SQL 不再重复维护。
- `definitions/columns.py`：各业务表的导出列定义集中管理；金额/成本字段带有权限编码，按当前登录用户权限动态裁剪。
- `base.py`：通用 XLSX 写盘器，只负责把行数据写入工作表，使用 openpyxl write-only 模式，支持分批迭代和自动筛选。
- `controllers/export_controller.py`：导出 HTTP 入口，负责选择查询类型、列定义并返回 XLSX 文件。

## 导出入口

| 页面 | 导出地址 |
|---|---|
| 原料库存查询（FIFO成本层） | `/exports/material-stock-with-cost-layers.xlsx` |
| 原料出入库流水 | `/exports/material-transactions.xlsx` |
| 成品库库存 | `/exports/finished-goods-warehouses.xlsx` |
| 成品库流水 | `/exports/finished-goods-transactions.xlsx` |
| 发货计划 | `/exports/shipment-plans.xlsx` |
| 业绩单列表 | `/exports/sales-performances.xlsx` |

导出链接会带上当前列表页的筛选参数；导出查询忽略分页参数，按 2,000 行一批读取，直到读取完符合筛选条件的记录。列表页仍按原分页大小展示。

## 权限

导出入口复用对应列表页的查看权限；列定义另外检查金额权限：
- 原料 FIFO 成本层、原料流水成本：`inventory.cost.view`（兼容已有 cost helper 的 edit 授权）。
- 成品库存成本：`finished_goods.cost.view`（兼容 edit 授权）。
- 业绩销售金额：`sales_performance.money.view`（兼容 edit 授权）。
- 业绩成本/利润：`sales_performance.cost.view`（兼容 edit 授权）。

导出列权限在服务端执行，不依赖前端隐藏字段。XLSX 写入器也会对用户可控文本做公式注入防护。

## 安装

新增依赖：`openpyxl>=3.1,<4`。部署环境执行：

```bash
pip install -r requirements.txt
```

注意：列表模板保留原有定制化 HTML 表格（例如业务链接、状态徽标、合计行）；其筛选和数据查询已与导出共用查询层。列标题/字段/金额权限由 `definitions/columns.py` 统一维护导出列。
