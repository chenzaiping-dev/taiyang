"""Single source of truth for export/list column metadata."""
EXPORT_COLUMNS = {
    "material_stock_layers": [
        ("layer_id","成本层ID",12,None), ("material_code","原料编码",16,None),
        ("material_name","原料名称",20,None), ("unit","单位",10,None),
        ("warehouse_code","仓库编码",14,None), ("warehouse_name","仓库名称",18,None),
        ("layer_type","成本层类型",16,None), ("effective_time","生效时间",20,None),
        ("unit_cost","单位成本",14,"inventory.cost.view"),
        ("original_qty","原始数量",14,None), ("original_cost","原始成本",14,"inventory.cost.view"),
        ("remaining_qty","剩余数量",14,None), ("remaining_cost","剩余成本",14,"inventory.cost.view"),
    ],
    "material_transactions": [
        ("id","流水ID",12,None), ("transaction_time","发生时间",20,None),
        ("transaction_type","流水类型",18,None), ("material_code","原料编码",16,None),
        ("material_name","原料名称",20,None), ("unit","单位",10,None),
        ("warehouse_code","仓库编码",14,None), ("warehouse_name","仓库名称",18,None),
        ("quantity_delta","数量变动",14,None), 
        ("unit_cost","单位成本",14,"inventory.cost.view"),
        ("total_cost","成本金额",14,"inventory.cost.view"),
        ("document_no","单据编号",20,None), ("document_type","单据类型",18,None),
        ("supplier_name","供应商",20,None), ("process_order_no","工艺单号",20,None),
        ("source_entry","操作入口",18,None), ("creator_name","操作人",14,None),
    ],
    "finished_goods_stock": [
        ("id","库存记录ID",14,None), ("warehouse_code","仓库编码",14,None),
        ("warehouse_name","仓库名称",18,None), ("batch_no","批号",18,None),
        ("product_name","产品名称",20,None), ("product_specification","产品规格",20,None),
        ("process_order_no", "工艺单号", 20, None),
        ("unit","单位",10,None), ("quality_status","质量状态",14,None),
        ("total_qty","库存数量",14,None), ("locked_qty","锁定数量",14,None),
        ("available_qty","可用数量",14,None), ("updated_at","更新时间",20,None),
        ("unit_cost","单位成本",14,"finished_goods.cost.view"),
        ("total_cost","库存成本",14,"finished_goods.cost.view"),
    ],
    "finished_goods_transactions": [
        ("id","流水ID",12,None), ("transaction_time","发生时间",20,None),
        ("transaction_type","流水类型",18,None), ("warehouse_code","仓库编码",14,None),
        ("warehouse_name","仓库名称",18,None), ("batch_no","批号",18,None),
        ("product_name","产品名称",20,None), ("unit","单位",10,None),
        ("quantity_delta","数量变动",14,None),
        ("unit_cost","单位成本",14,"finished_goods.cost.view"),
        ("total_cost","成本金额",14,"finished_goods.cost.view"),
        ("document_no","单据编号",20,None), ("document_type","单据类型",18,None),
        ("process_order_no","工艺单号",20,None), ("source_entry","操作入口",18,None),
        ("creator_name","操作人",14,None),
    ],
    "shipment_plans": [
        ("id","计划ID",12,None), ("shipment_plan_no","发货计划单号",22,None),
        ("performance_no","业绩单号",22,None), ("company_name","客户公司",22,None),
        ("product_name","产品名称",20,None), ("product_specification","产品规格",20,None),
        ("destination","收货地址",28,None),
        ("planned_ship_time","计划发货时间",20,None), ("status","状态",16,None),
        ("planned_qty","计划数量",14,None), ("shipped_qty","已发数量",14,None),
        ("created_by_name","制单人",14,None), ("created_at","创建时间",20,None),
    ],
    "shipment_plan_details": [
        ("id", "计划ID", 12, None), ("shipment_plan_no", "发货计划单号", 22, None),
        ("performance_no", "业绩单号", 22, None), ("company_name", "客户公司", 22, None),
        ("product_name", "产品名称", 20, None), ("product_specification","产品规格",20,None),
        ("process_order_no", "工艺单号", 20, None), ("batch_no", "产品批号", 18, None),
        ("warehouse_code", "仓库编码", 14, None), ("warehouse_name", "仓库名称", 18, None),
        ("shipment_quantity", "发货数量", 14, None), ("destination", "收货地址", 28, None),
        ("planned_ship_time", "计划发货时间", 20, None), ("status", "状态", 16, None),
        ("planned_qty", "计划总数量", 14, None), ("shipped_qty", "已发总数量", 14, None),
        ("created_by_name", "制单人", 14, None), ("created_at", "创建时间", 20, None),
    ],
    "sales_performances": [
        ("id","记录ID",12,None), ("performance_no","业绩单号",22,None),
        ("company_name","客户公司",22,None), ("product_name","产品名称",20,None),
        ("product_specification","产品规格",20,None), ("unit","单位",10,None),
        ("quantity","数量",14,None), ("payment_status","收款状态",14,None),
        ("salesperson_name","主销售员",14,None), ("co_salesperson_name","共同销售员",14,None),
        ("is_joint_sales","是否共同销售",14,None), ("is_changzhou_factory","是否常州工厂",16,None),
        ("expected_delivery_date","客户要求交期",20,None), ("status","状态",16,None),
        ("created_at","创建时间",20,None),
        ("unit_price","销售单价",14,"sales_performance.money.view"),
        ("amount","销售金额",14,"sales_performance.money.view"),
        ("cost","成本",14,"sales_performance.cost.view"),
        ("profit","利润",14,"sales_performance.cost.view"),
        ("attributed_quantity", "归属数量", 14, None),
        ("attributed_amount", "归属销售额", 16, "sales_performance.money.view"),
    ],
}


def get_columns(kind, has_permission):
    return [
        {"key": key, "title": title, "width": width}
        for key, title, width, permission in EXPORT_COLUMNS[kind]
        if permission is None or has_permission(permission)
    ]
