USE `taiyang`;

-- 关闭外键检查，避免初始化时受历史外键关系影响
SET FOREIGN_KEY_CHECKS = 0;

-- 1. 权限关联表
DROP TABLE IF EXISTS `employee_roles`;
DROP TABLE IF EXISTS `role_permissions`;

-- 2. 成品预留：依赖生产计划、发货分配、仓库和批次
DROP TABLE IF EXISTS `finished_goods_reservations`;

-- 3. 发货分配及发货计划
DROP TABLE IF EXISTS `shipment_plan_allocations`;
DROP TABLE IF EXISTS `shipment_plan_lines`;
DROP TABLE IF EXISTS `shipment_plans`;

-- 4. 生产计划与工艺单关联
DROP TABLE IF EXISTS `production_plan_process_orders`;

-- 5. 成品库存流水及库存相关表
DROP TABLE IF EXISTS `finished_goods_transactions`;
DROP TABLE IF EXISTS `finished_goods_inventory`;
DROP TABLE IF EXISTS `finished_goods_document_lines`;
DROP TABLE IF EXISTS `finished_goods_documents`;

-- 6. 原料 FIFO 成本分配及成本层
-- 必须先删除成本分配，再删除成本层和原料库存流水
DROP TABLE IF EXISTS `inventory_cost_allocations`;
DROP TABLE IF EXISTS `inventory_cost_layers`;
DROP TABLE IF EXISTS `inventory_transactions`;
DROP TABLE IF EXISTS `inventory_documents`;

-- 7. 工艺单原料明细
DROP TABLE IF EXISTS `process_order_materials`;

-- 8. 成品批次
DROP TABLE IF EXISTS `finished_goods_batches`;

-- 9. 生产计划
DROP TABLE IF EXISTS `production_plans`;

-- 10. 工艺单
DROP TABLE IF EXISTS `process_orders`;

-- 11. 业绩单
DROP TABLE IF EXISTS `sales_performance`;

-- 12. 仓库
DROP TABLE IF EXISTS `warehouses`;

-- 13. 原料及供应商
DROP TABLE IF EXISTS `materials`;
DROP TABLE IF EXISTS `suppliers`;

-- 14. 员工、角色、权限等基础表
DROP TABLE IF EXISTS `employees`;
DROP TABLE IF EXISTS `roles`;
DROP TABLE IF EXISTS `permissions`;

-- 15. 加工厂
DROP TABLE IF EXISTS `factories`;

-- 恢复外键检查
SET FOREIGN_KEY_CHECKS = 1;

CREATE TABLE employees (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    username VARCHAR(50) NOT NULL COMMENT '登录账号',
    password_hash VARCHAR(255) NOT NULL COMMENT '密码哈希',
    real_name VARCHAR(50) NOT NULL COMMENT '员工姓名',
    phone VARCHAR(30) DEFAULT NULL COMMENT '联系电话',
    nickname VARCHAR(50) DEFAULT NULL COMMENT '代号，销售非空',

    status TINYINT NOT NULL DEFAULT 1 COMMENT '1启用，0禁用',

    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    PRIMARY KEY (id),
    UNIQUE KEY uk_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
COMMENT='员工账号表';

CREATE TABLE roles (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    role_code VARCHAR(50) NOT NULL COMMENT '角色编码',
    role_name VARCHAR(50) NOT NULL COMMENT '角色名称',
    description VARCHAR(255) DEFAULT NULL,
    status TINYINT NOT NULL DEFAULT 1 COMMENT '1启用，0禁用',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (id),
    UNIQUE KEY uk_role_code (role_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
COMMENT='角色表';

CREATE TABLE permissions (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    permission_code VARCHAR(100) NOT NULL COMMENT '权限唯一编码',
    permission_name VARCHAR(100) NOT NULL COMMENT '权限名称',
    module_code VARCHAR(50) NOT NULL COMMENT '业务模块',
    description VARCHAR(255) DEFAULT NULL,

    PRIMARY KEY (id),
    UNIQUE KEY uk_permission_code (permission_code),
    KEY idx_module_code (module_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
COMMENT='系统权限表';

CREATE TABLE employee_roles (
    employee_id BIGINT UNSIGNED NOT NULL,
    role_id BIGINT UNSIGNED NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (employee_id, role_id),
    KEY idx_role_id (role_id),

    CONSTRAINT fk_er_employee
        FOREIGN KEY (employee_id) REFERENCES employees(id),
    CONSTRAINT fk_er_role
        FOREIGN KEY (role_id) REFERENCES roles(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
COMMENT='员工角色关联表';

CREATE TABLE role_permissions (
    role_id BIGINT UNSIGNED NOT NULL,
    permission_id BIGINT UNSIGNED NOT NULL,

    data_scope VARCHAR(20) NOT NULL DEFAULT 'all'
        COMMENT 'all全部，own本人，assigned分配给本人，factory所属工厂',

    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (role_id, permission_id),

    CONSTRAINT fk_rp_role
        FOREIGN KEY (role_id) REFERENCES roles(id),
    CONSTRAINT fk_rp_permission
        FOREIGN KEY (permission_id) REFERENCES permissions(id),

    CONSTRAINT chk_data_scope
        CHECK (data_scope IN ('all', 'own', 'assigned', 'factory'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
COMMENT='角色权限关联表';

CREATE TABLE factories (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    factory_code VARCHAR(50) NOT NULL COMMENT '加工厂编码',
    factory_name VARCHAR(150) NOT NULL COMMENT '加工厂名称',
    contact_person VARCHAR(50) DEFAULT NULL COMMENT '联系人',
    contact_phone VARCHAR(30) DEFAULT NULL COMMENT '联系电话',
    address VARCHAR(255) DEFAULT NULL COMMENT '加工厂地址',

    remark VARCHAR(500) DEFAULT NULL COMMENT '备注',
    status TINYINT NOT NULL DEFAULT 1 COMMENT '1启用，0停用',

    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    PRIMARY KEY (id),
    UNIQUE KEY uk_factory_code (factory_code),
    KEY idx_factory_name (factory_name),
    KEY idx_factory_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
COMMENT='加工厂信息表';

CREATE TABLE warehouses (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    warehouse_code VARCHAR(50) NOT NULL COMMENT '仓库编码',
    warehouse_name VARCHAR(150) NOT NULL COMMENT '仓库名称',

    warehouse_type ENUM('raw_material', 'finished_goods', 'general')
        NOT NULL DEFAULT 'raw_material'
        COMMENT '仓库类型：raw_material原料仓，finished_goods成品仓，general普通仓',

    factory_id BIGINT UNSIGNED DEFAULT NULL COMMENT '所属加工厂ID；独立仓库可为空',
    address VARCHAR(255) DEFAULT NULL COMMENT '仓库地址',
    manager_employee_id BIGINT UNSIGNED DEFAULT NULL COMMENT '仓库负责人ID',

    remark VARCHAR(500) DEFAULT NULL COMMENT '备注',
    status TINYINT NOT NULL DEFAULT 1 COMMENT '1启用，0停用',

    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    PRIMARY KEY (id),
    UNIQUE KEY uk_warehouse_code (warehouse_code),
    KEY idx_warehouse_type (warehouse_type),
    KEY idx_factory_id (factory_id),
    KEY idx_manager_employee (manager_employee_id),
    KEY idx_warehouse_status (status),

    CONSTRAINT fk_warehouse_manager
        FOREIGN KEY (manager_employee_id) REFERENCES employees(id),
    CONSTRAINT fk_warehouse_factory
        FOREIGN KEY (factory_id) REFERENCES factories(id)
) ENGINE=InnoDB  DEFAULT CHARSET=utf8mb4
COMMENT='仓库信息表';

CREATE TABLE materials (
    id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
    material_code VARCHAR(50) NOT NULL COMMENT '原料编码',
    material_name VARCHAR(200) NOT NULL COMMENT '原料名称',
    unit VARCHAR(20) DEFAULT 'KG' COMMENT '计量单位',
    status TINYINT NOT NULL DEFAULT 1 COMMENT '1启用，0禁用',

    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,    
    UNIQUE KEY uk_material_code (material_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
COMMENT='原料基础资料';

CREATE TABLE suppliers (
    id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
    name VARCHAR(200) NOT NULL COMMENT '供应商名称',
    status TINYINT NOT NULL DEFAULT 1 COMMENT '1启用，0禁用',

    contact VARCHAR(100) COMMENT '联系人',
    phone VARCHAR(50) COMMENT '联系电话',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
COMMENT='供应商';

CREATE TABLE sales_performance (
    id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT COMMENT '业绩ID',

    performance_no VARCHAR(64) NOT NULL COMMENT '业绩表单号',
    sales_order_no VARCHAR(64) NULL COMMENT '销售订单编号',

    performance_date DATE NOT NULL COMMENT '日期',
    company_name VARCHAR(200) NOT NULL COMMENT '公司名称',
    product_name VARCHAR(200) NOT NULL COMMENT '产品名称',
    product_specification VARCHAR(500) NULL COMMENT '产品规格',
    unit VARCHAR(20) DEFAULT 'KG' COMMENT '单位',

    unit_price DECIMAL(24, 6) NOT NULL DEFAULT 0 COMMENT '销售单价',
    quantity DECIMAL(18, 6) NOT NULL DEFAULT 0 COMMENT '销售数量',
    amount DECIMAL(24, 6) NOT NULL DEFAULT 0 COMMENT '销售金额',

    payment_status ENUM('UNPAID', 'PAID')
        NOT NULL DEFAULT 'UNPAID' COMMENT '收款状态: UNPAID未收款, PAID已收款',

    cost DECIMAL(24, 6) NOT NULL DEFAULT 0 COMMENT '成本',
    profit DECIMAL(24, 6) NOT NULL DEFAULT 0 COMMENT '利润',

    main_salesperson_id BIGINT UNSIGNED NULL COMMENT '主销售员ID',
    is_changzhou_factory TINYINT(1) NOT NULL DEFAULT 0
        COMMENT '是否走常州工厂',
    is_joint_sales TINYINT(1) NOT NULL DEFAULT 0
        COMMENT '是否共同销售',
    co_salesperson_id BIGINT UNSIGNED NULL COMMENT '共同销售员ID',

    expected_delivery_date DATE NULL COMMENT '客户期待发货日期',

    status ENUM(
        'DRAFT',
        'SUBMITTED',
        'PRODUCTION_PLANNED',
        'FINISHED_GOODS_STOCKED',
        'SHIPMENT_SCHEDULED',
        'COMPLETED',
        'CANCELLED'
    ) NOT NULL DEFAULT 'DRAFT' COMMENT '业绩单状态: DRAFT草稿, SUBMITTED已提交, PRODUCTION_PLANNED生产计划中,
        FINISHED_GOODS_STOCKED成品已入库, SHIPMENT_SCHEDULED发货已排程,
        COMPLETED已完成, CANCELLED已取消',

    remark VARCHAR(1000) NULL COMMENT '备注',

    created_by BIGINT UNSIGNED NULL COMMENT '创建人ID',
    updated_by BIGINT UNSIGNED NULL COMMENT '修改人ID',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY uk_performance_no (performance_no),
    KEY idx_performance_date (performance_date),
    KEY idx_company_name (company_name),
    KEY idx_status (status),
    KEY idx_delivery_date (expected_delivery_date),
    KEY idx_sales_order_no (sales_order_no),

    CHECK (unit_price >= 0),
    CHECK (quantity > 0),
    CHECK (amount >= 0),
    CHECK (cost >= 0),
    CONSTRAINT fk_sales_perf_created_by
        FOREIGN KEY (created_by) REFERENCES employees(id),
    CONSTRAINT fk_sales_perf_updated_by
        FOREIGN KEY (updated_by) REFERENCES employees(id),
    CONSTRAINT fk_sales_perf_main_salesperson
        FOREIGN KEY (main_salesperson_id) REFERENCES employees(id),
    CONSTRAINT fk_sales_perf_co_salesperson
        FOREIGN KEY (co_salesperson_id) REFERENCES employees(id)

) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
COMMENT='销售业绩表';

CREATE TABLE process_orders (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,

    process_order_no VARCHAR(50) NOT NULL COMMENT '工艺单编号',
    factory_id BIGINT UNSIGNED NOT NULL COMMENT '加工厂ID',

    batch_no VARCHAR(100) NOT NULL COMMENT '生产批号',
    product_name VARCHAR(200) NOT NULL COMMENT '产品名称',
    product_specification VARCHAR(500) NULL COMMENT '产品规格',
    unit VARCHAR(20) DEFAULT 'KG' COMMENT '成品计量单位',
    quantity DECIMAL(18, 6) NOT NULL COMMENT '计划生产数量',

    performance_id BIGINT UNSIGNED NOT NULL COMMENT '关联业绩表ID快照',
    order_time DATETIME NOT NULL COMMENT '下单时间',

    followup_employee_id BIGINT UNSIGNED NOT NULL COMMENT '跟单员ID',

    cost_price DECIMAL(24, 6) DEFAULT 0 COMMENT '预计单位成本',
    total_cost DECIMAL(24, 6) DEFAULT 0 COMMENT '预计总成本',
    actual_total_cost DECIMAL(24, 6) DEFAULT 0 COMMENT '实际生产总成本',
    completed_quantity DECIMAL(18, 6) DEFAULT 0 COMMENT '实际完工数量',
    
    status ENUM(
        'DRAFT',
        'ORDERED',
        'PRODUCING',
        'COMPLETED',
        'RECEIVED',
        'CANCELLED'
    ) NOT NULL DEFAULT 'DRAFT' COMMENT '工艺单状态: DRAFT草稿, ORDERED已下单, PRODUCING生产中,
        COMPLETED已完工, RECEIVED已入库, CANCELLED已取消',

    remark VARCHAR(500) DEFAULT NULL COMMENT '备注',

    created_by BIGINT UNSIGNED NOT NULL COMMENT '创建人ID',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_process_order_no (process_order_no),

    KEY idx_factory_status (factory_id, status),
    KEY idx_batch_no (batch_no),
    KEY idx_product_name (product_name),
    KEY idx_performance_id (performance_id),
    KEY idx_followup_status (followup_employee_id, status),
    KEY idx_order_time (order_time),

    CONSTRAINT fk_process_order_performance
        FOREIGN KEY (performance_id)
        REFERENCES sales_performance(id),
    CONSTRAINT fk_process_order_created_by
        FOREIGN KEY (created_by) REFERENCES employees(id),
    CONSTRAINT fk_process_order_followup_employee
        FOREIGN KEY (followup_employee_id) REFERENCES employees(id),
    CONSTRAINT fk_process_order_factory
        FOREIGN KEY (factory_id) REFERENCES factories(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
COMMENT='工艺单主表';

CREATE TABLE process_order_materials (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,

    process_order_id BIGINT UNSIGNED NOT NULL COMMENT '工艺单ID',
    material_id BIGINT UNSIGNED NOT NULL COMMENT '原料ID',
    ratio_value DECIMAL(18, 8) NOT NULL COMMENT '原料配比，小数形式，同一工艺单配比合计为1',
    quantity DECIMAL(18, 6) NOT NULL DEFAULT 0 COMMENT '数量',
    cost DECIMAL(24, 6) NOT NULL DEFAULT 0 COMMENT '成本',
    remark VARCHAR(255) DEFAULT NULL,

    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    PRIMARY KEY (id),

    UNIQUE KEY uk_order_material (
        process_order_id,
        material_id
    ),

    KEY idx_material_id (material_id),

    CONSTRAINT fk_pom_process_order
        FOREIGN KEY (process_order_id)
        REFERENCES process_orders(id),

    CONSTRAINT fk_pom_material
        FOREIGN KEY (material_id)
        REFERENCES materials(id),

    CONSTRAINT chk_pom_ratio
        CHECK (ratio_value >= 0 AND ratio_value <= 1),

    CONSTRAINT chk_pom_quantity
        CHECK (quantity >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
COMMENT='工艺单原料配比明细表';

--- 业务单据表 inventory_documents
--- 这张表记录一次业务的整体信息，例如一次采购入库、一张生产领料单或一次库存盘点。
CREATE TABLE inventory_documents (
    id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
    document_no VARCHAR(50) NOT NULL COMMENT '单据编号',

    document_type ENUM(
        'PURCHASE_IN',
        'PRODUCTION_OUT',
        'STOCKTAKE',
        'ADJUSTMENT'
    ) NOT NULL COMMENT '单据类型: PURCHASE_IN采购入库, PRODUCTION_OUT生产出库,
        STOCKTAKE库存盘点, ADJUSTMENT库存调整',

    document_time DATETIME NOT NULL COMMENT '业务发生时间',

    status ENUM(
        'DRAFT',
        'CONFIRMED',
        'CANCELLED'
    ) NOT NULL DEFAULT 'DRAFT' COMMENT '单据状态: DRAFT, CONFIRMED, CANCELLED',

    remark VARCHAR(500) COMMENT '备注',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    confirmed_at DATETIME,

    UNIQUE KEY uk_document_no (document_no),
    KEY idx_document_time (document_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
COMMENT='库存业务单据';

CREATE TABLE inventory_transactions (
    id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,

    document_id BIGINT UNSIGNED NOT NULL COMMENT '业务单据ID',
    line_no INT NOT NULL DEFAULT 1 COMMENT '单据行号',
    material_id BIGINT UNSIGNED NOT NULL COMMENT '原料ID',

    transaction_type ENUM(
        'PURCHASE_IN',
        'PRODUCTION_OUT',
        'STOCKTAKE_GAIN',
        'STOCKTAKE_LOSS',
        'RETURN_IN'
    ) NOT NULL COMMENT '业务类型: PURCHASE_IN:采购入库, PRODUCTION_OUT:生产出库, 
        STOCKTAKE_GAIN:库存盘点-入库, STOCKTAKE_LOSS:库存盘点-出库, RETURN_IN:退货入库',

    transaction_time DATETIME NOT NULL COMMENT '业务生效时间',

    quantity_delta DECIMAL(18, 6) NOT NULL
        COMMENT '库存变化数量：入库正数，出库负数',

    unit_cost DECIMAL(24, 6) DEFAULT NULL
        COMMENT '入库成本单价或调整计价单价',

    total_cost DECIMAL(24, 6) DEFAULT NULL
        COMMENT '本流水对应的成本金额',

    supplier_id BIGINT UNSIGNED DEFAULT NULL COMMENT '供应商ID',
    process_order_id BIGINT UNSIGNED DEFAULT NULL COMMENT '领料工艺ID',
    warehouse_id BIGINT UNSIGNED NOT NULL COMMENT '发生库存变动的仓库ID',

    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,

    UNIQUE KEY uk_document_line (document_id, line_no),
    KEY idx_material_time (material_id, transaction_time, id),
    KEY idx_warehouse_material_time
        (warehouse_id, material_id, transaction_time, id),

    CONSTRAINT fk_it_warehouse
        FOREIGN KEY (warehouse_id) REFERENCES warehouses(id),
    CONSTRAINT fk_it_document
        FOREIGN KEY (document_id) REFERENCES inventory_documents(id),
    CONSTRAINT fk_it_material
        FOREIGN KEY (material_id) REFERENCES materials(id),
    CONSTRAINT fk_it_supplier
        FOREIGN KEY (supplier_id) REFERENCES suppliers(id),
    CONSTRAINT fk_it_process_orders
        FOREIGN KEY (process_order_id) REFERENCES process_orders(id),

    CHECK (quantity_delta <> 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
COMMENT='统一库存业务流水';

CREATE TABLE inventory_cost_layers (
    id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,

    material_id BIGINT UNSIGNED NOT NULL COMMENT '原料ID',
    source_transaction_id BIGINT UNSIGNED NOT NULL
        COMMENT '创建该成本层的库存流水ID',

    layer_type ENUM(
        'PURCHASE',
        'STOCKTAKE_RESET',
        'RETURN'
    ) NOT NULL DEFAULT 'PURCHASE' COMMENT '成本层类型: PURCHASE采购, STOCKTAKE_RESET盘点重置, RETURN退货',

    effective_time DATETIME NOT NULL COMMENT '成本层生效时间',

    original_qty DECIMAL(18, 6) NOT NULL COMMENT '初始数量',
    remaining_qty DECIMAL(18, 6) NOT NULL COMMENT '剩余数量',

    unit_cost DECIMAL(24, 6) NOT NULL COMMENT '成本单价',
    original_cost DECIMAL(24, 6) NOT NULL COMMENT '初始成本金额',
    warehouse_id BIGINT UNSIGNED NOT NULL COMMENT '成本层所属仓库',

    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,

    UNIQUE KEY uk_source_transaction (source_transaction_id),
    KEY idx_fifo_layer (material_id, effective_time, id),
    KEY idx_warehouse_fifo (warehouse_id, material_id, effective_time, id),
    CONSTRAINT fk_icl_warehouse
        FOREIGN KEY (warehouse_id) REFERENCES warehouses(id),

    FOREIGN KEY (material_id) REFERENCES materials(id),
    FOREIGN KEY (source_transaction_id)
        REFERENCES inventory_transactions(id),

    CHECK (original_qty > 0),
    CHECK (remaining_qty >= 0 AND remaining_qty <= original_qty),
    CHECK (unit_cost >= 0),
    CHECK (original_cost >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
COMMENT='FIFO库存成本层';

CREATE TABLE inventory_cost_allocations (
    id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,

    out_transaction_id BIGINT UNSIGNED NOT NULL
        COMMENT '对应的出库流水ID',
    cost_layer_id BIGINT UNSIGNED NOT NULL
        COMMENT '被消耗的FIFO成本层ID',

    quantity DECIMAL(18, 6) NOT NULL COMMENT '本层消耗数量',
    unit_cost DECIMAL(24, 6) NOT NULL COMMENT '出库时的成本单价',
    cost_amount DECIMAL(24, 6) NOT NULL COMMENT '本次分配成本金额',

    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,

    UNIQUE KEY uk_out_layer (out_transaction_id, cost_layer_id),
    KEY idx_out_transaction (out_transaction_id),
    KEY idx_cost_layer (cost_layer_id),

    FOREIGN KEY (out_transaction_id)
        REFERENCES inventory_transactions(id),
    FOREIGN KEY (cost_layer_id)
        REFERENCES inventory_cost_layers(id),

    CHECK (quantity > 0),
    CHECK (unit_cost >= 0),
    CHECK (cost_amount >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
COMMENT='FIFO出库成本分配明细';


CREATE TABLE finished_goods_batches (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,

    batch_no VARCHAR(100) NOT NULL COMMENT '成品批次号',
    process_order_id BIGINT UNSIGNED DEFAULT NULL COMMENT '来源工艺单ID；非生产来源可为空',
    product_name VARCHAR(200) NOT NULL COMMENT '产品名称快照',
    product_specification VARCHAR(500) NULL COMMENT '产品规格快照',
    unit VARCHAR(20) DEFAULT 'KG' COMMENT '成品计量单位',

    batch_source ENUM(
        'PRODUCTION',
        'RETURN',
        'OTHER'
    ) NOT NULL DEFAULT 'PRODUCTION' COMMENT '批次来源: PRODUCTION生产入库, RETURN退货待确认, OTHER其他来源',

    quality_status ENUM(
        'PENDING',
        'QUALIFIED',
        'REJECTED'
    ) NOT NULL DEFAULT 'QUALIFIED' COMMENT '质量状态: PENDING待检, QUALIFIED合格, REJECTED不合格',

    remark VARCHAR(500) DEFAULT NULL,

    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    PRIMARY KEY (id),
    UNIQUE KEY uk_fg_batch_no (batch_no),
    KEY idx_fg_process_order (process_order_id),
    KEY idx_fg_product_name (product_name),

    CONSTRAINT fk_fg_batch_process_order
        FOREIGN KEY (process_order_id)
        REFERENCES process_orders(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
COMMENT='成品批次档案表';

--- 记录一次成品业务的单据头
CREATE TABLE finished_goods_documents (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,

    document_no VARCHAR(50) NOT NULL COMMENT '成品业务单号',

    document_type ENUM(
        'PRODUCTION_IN',
        'OUTBOUND',
        'TRANSFER',
        'CUSTOMER_RETURN',
        'ADJUSTMENT'
    ) NOT NULL COMMENT '单据类型: PRODUCTION_IN生产入库, OUTBOUND销售出库, TRANSFER仓库调拨, CUSTOMER_RETURN客户退货, ADJUSTMENT库存调整',

    document_time DATETIME NOT NULL COMMENT '业务发生时间',
    status ENUM(
        'DRAFT',
        'CONFIRMED',
        'CANCELLED'
    ) NOT NULL DEFAULT 'DRAFT' COMMENT '单据状态: DRAFT草稿, CONFIRMED已确认, CANCELLED已取消',

    related_document_id BIGINT UNSIGNED DEFAULT NULL COMMENT '关联原单据，例如退货关联原出库单',
    remark VARCHAR(500) DEFAULT NULL,

    created_by BIGINT UNSIGNED DEFAULT NULL COMMENT '制单人ID',
    confirmed_by BIGINT UNSIGNED DEFAULT NULL COMMENT '确认人ID',

    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    confirmed_at DATETIME DEFAULT NULL,

    PRIMARY KEY (id),
    UNIQUE KEY uk_fg_document_no (document_no),
    KEY idx_fg_doc_type_status (document_type, status),
    KEY idx_fg_doc_time (document_time),
    KEY idx_fg_related_document (related_document_id),
    CONSTRAINT fk_fg_doc_created_by
        FOREIGN KEY (created_by) REFERENCES employees(id),
    CONSTRAINT fk_fg_doc_confirmed_by
            FOREIGN KEY (confirmed_by) REFERENCES employees(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
COMMENT='成品库存业务单据';

--- 记录单据涉及的批次、数量、仓库
CREATE TABLE finished_goods_document_lines (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,

    document_id BIGINT UNSIGNED NOT NULL COMMENT '单据ID',
    line_no INT NOT NULL DEFAULT 1 COMMENT '单据行号',
    batch_id BIGINT UNSIGNED NOT NULL COMMENT '成品批次ID',
    from_warehouse_id BIGINT UNSIGNED DEFAULT NULL COMMENT '调出仓库；入库时可为空',
    to_warehouse_id BIGINT UNSIGNED DEFAULT NULL COMMENT '调入仓库；出库时可为空',
    quantity DECIMAL(18, 6) NOT NULL COMMENT '本行业务数量，始终为正数',
    unit_cost DECIMAL(24, 6) DEFAULT NULL COMMENT '可选成本快照，不用于FIFO分配',
    related_outbound_line_id BIGINT UNSIGNED DEFAULT NULL COMMENT '退货时关联原出库明细行',
    remark VARCHAR(500) DEFAULT NULL,

    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (id),
    UNIQUE KEY uk_fg_document_line (document_id, line_no),

    KEY idx_fg_line_batch (batch_id),
    KEY idx_fg_line_from_warehouse (from_warehouse_id, batch_id),
    KEY idx_fg_line_to_warehouse (to_warehouse_id, batch_id),
    KEY idx_fg_line_original_outbound (related_outbound_line_id),

    CONSTRAINT fk_fg_line_document
        FOREIGN KEY (document_id)
        REFERENCES finished_goods_documents(id),

    CONSTRAINT fk_fg_line_batch
        FOREIGN KEY (batch_id)
        REFERENCES finished_goods_batches(id),

    CONSTRAINT fk_fg_line_from_warehouse
        FOREIGN KEY (from_warehouse_id)
        REFERENCES warehouses(id),

    CONSTRAINT fk_fg_line_to_warehouse
        FOREIGN KEY (to_warehouse_id)
        REFERENCES warehouses(id),

    CONSTRAINT chk_fg_line_quantity
        CHECK (quantity > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
COMMENT='成品库存业务单据明细';

--- 快速查询每个仓库、每个批次的现存量
CREATE TABLE finished_goods_inventory (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,

    warehouse_id BIGINT UNSIGNED NOT NULL COMMENT '仓库ID',
    batch_id BIGINT UNSIGNED NOT NULL COMMENT '成品批次ID',

    quantity DECIMAL(18, 6) NOT NULL DEFAULT 0 COMMENT '当前库存数量',
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    PRIMARY KEY (id),
    UNIQUE KEY uk_fg_inventory_warehouse_batch
        (warehouse_id, batch_id),

    KEY idx_fg_inventory_batch (batch_id),

    CONSTRAINT fk_fg_inventory_warehouse
        FOREIGN KEY (warehouse_id)
        REFERENCES warehouses(id),

    CONSTRAINT fk_fg_inventory_batch
        FOREIGN KEY (batch_id)
        REFERENCES finished_goods_batches(id),

    CONSTRAINT chk_fg_inventory_quantity
        CHECK (quantity >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
COMMENT='成品库存余额表';

--- 追溯每次入库、出库、调拨、退货造成的库存变化
CREATE TABLE finished_goods_transactions (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,

    document_id BIGINT UNSIGNED NOT NULL COMMENT '业务单据ID',
    document_line_id BIGINT UNSIGNED NOT NULL COMMENT '业务单据明细ID',
    batch_id BIGINT UNSIGNED NOT NULL COMMENT '成品批次ID',
    warehouse_id BIGINT UNSIGNED NOT NULL COMMENT '实际发生库存变化的仓库',

    transaction_type ENUM(
        'PRODUCTION_IN',
        'OUTBOUND',
        'TRANSFER_OUT',
        'TRANSFER_IN',
        'CUSTOMER_RETURN',
        'ADJUSTMENT'
    ) NOT NULL DEFAULT 'PRODUCTION_IN'
        COMMENT '流水类型: PRODUCTION_IN生产入库, OUTBOUND出库, TRANSFER_OUT调拨出库, 
        TRANSFER_IN调拨入库, CUSTOMER_RETURN客户退货, ADJUSTMENT库存调整',

    quantity_delta DECIMAL(18, 6) NOT NULL COMMENT '库存变化数量：入库正数，出库负数',

    transaction_time DATETIME NOT NULL COMMENT '库存变动生效时间',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (id),

    KEY idx_fg_tx_warehouse_batch_time
        (warehouse_id, batch_id, transaction_time, id),

    KEY idx_fg_tx_document (document_id),
    KEY idx_fg_tx_document_line (document_line_id),

    CONSTRAINT fk_fg_tx_document
        FOREIGN KEY (document_id)
        REFERENCES finished_goods_documents(id),

    CONSTRAINT fk_fg_tx_document_line
        FOREIGN KEY (document_line_id)
        REFERENCES finished_goods_document_lines(id),

    CONSTRAINT fk_fg_tx_batch
        FOREIGN KEY (batch_id)
        REFERENCES finished_goods_batches(id),

    CONSTRAINT fk_fg_tx_warehouse
        FOREIGN KEY (warehouse_id)
        REFERENCES warehouses(id),

    CONSTRAINT chk_fg_tx_quantity
        CHECK (quantity_delta <> 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
COMMENT='成品库存变动流水';

CREATE TABLE production_plans (
    id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT COMMENT '生产计划ID',

    plan_no VARCHAR(64) NOT NULL COMMENT '生产计划单号',
    performance_id BIGINT UNSIGNED NOT NULL COMMENT '业绩ID',

    total_quantity DECIMAL(18, 6) NOT NULL
        COMMENT '计划覆盖总数量',

    process_quantity DECIMAL(18, 6) NOT NULL DEFAULT 0
        COMMENT '计划新生产数量',

    process_order_status ENUM(
        'NOT_REQUIRED',
        'PLANNED',
        'CREATED'
    ) NOT NULL DEFAULT 'PLANNED'
        COMMENT '工艺单状态',

    status ENUM(
        'DRAFT',
        'CONFIRMED',
        'IN_PROGRESS',
        'COMPLETED',
        'CANCELLED'
    ) NOT NULL DEFAULT 'DRAFT' COMMENT '生产计划状态',

    remark VARCHAR(1000) NULL COMMENT '备注',
    created_by BIGINT UNSIGNED NULL COMMENT '创建人ID',
    confirmed_by BIGINT UNSIGNED NULL COMMENT '确认人ID',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY uk_production_plan_no (plan_no),
    UNIQUE KEY uk_production_plan_performance (performance_id),
    KEY idx_production_plan_status (status),

    CONSTRAINT fk_production_plan_performance
        FOREIGN KEY (performance_id)
        REFERENCES sales_performance(id),
    CONSTRAINT fk_production_plan_created_by
        FOREIGN KEY (created_by) REFERENCES employees(id),
    CONSTRAINT fk_production_plan_confirmed_by
        FOREIGN KEY (confirmed_by) REFERENCES employees(id),

    CHECK (total_quantity > 0),
    CHECK (process_quantity >= 0),
    CHECK (process_quantity <= total_quantity)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
COMMENT='生产计划单';

CREATE TABLE production_plan_process_orders (
    id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT COMMENT '关联ID',

    production_plan_id BIGINT UNSIGNED NOT NULL COMMENT '生产计划ID',
    process_order_id BIGINT UNSIGNED NOT NULL COMMENT '工艺单ID',

    planned_quantity DECIMAL(18, 6) NOT NULL
        COMMENT '该工艺单计划生产数量',

    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,

    UNIQUE KEY uk_plan_process_order
        (production_plan_id, process_order_id),
    KEY idx_process_order_id (process_order_id),

    CONSTRAINT fk_pppo_plan
        FOREIGN KEY (production_plan_id)
        REFERENCES production_plans(id),

    CONSTRAINT fk_pppo_process_order
        FOREIGN KEY (process_order_id)
        REFERENCES process_orders(id),

    CHECK (planned_quantity > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
COMMENT='生产计划与工艺单关联表';

CREATE TABLE shipment_plans (
    id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT COMMENT '发货计划ID',

    shipment_plan_no VARCHAR(64) NOT NULL COMMENT '发货计划单号',
    performance_id BIGINT UNSIGNED NOT NULL COMMENT '业绩ID',

    destination VARCHAR(500) NOT NULL COMMENT '目的地',
    transport_method VARCHAR(100) NULL COMMENT '运输方式',

    planned_ship_time DATETIME NULL COMMENT '计划发货时间',
    actual_ship_time DATETIME NULL COMMENT '实际发货时间',

    status ENUM(
        'DRAFT',
        'PLANNED',
        'PARTIALLY_SHIPPED',
        'SHIPPED',
        'CANCELLED'
    ) NOT NULL DEFAULT 'DRAFT' COMMENT '发货状态',

    created_by BIGINT UNSIGNED NULL COMMENT '创建人ID',
    remark VARCHAR(1000) NULL COMMENT '备注',

    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY uk_shipment_plan_no (shipment_plan_no),
    KEY idx_shipment_performance (performance_id),
    KEY idx_shipment_status_time (status, planned_ship_time),

    CONSTRAINT fk_shipment_plan_performance
        FOREIGN KEY (performance_id)
        REFERENCES sales_performance(id),
    CONSTRAINT fk_shipment_plan_created_by
        FOREIGN KEY (created_by) REFERENCES employees(id)

) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
COMMENT='发货计划单';

CREATE TABLE shipment_plan_lines (
    id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT COMMENT '发货明细ID',

    shipment_plan_id BIGINT UNSIGNED NOT NULL COMMENT '发货计划ID',

    product_name VARCHAR(200) NOT NULL COMMENT '产品名称',
    product_specification VARCHAR(500) NULL COMMENT '产品规格',
    unit VARCHAR(20) DEFAULT 'KG' COMMENT '单位',

    unit_price DECIMAL(24, 6) NOT NULL DEFAULT 0 COMMENT '销售单价',
    quantity DECIMAL(18, 6) NOT NULL COMMENT '计划发货数量',
    shipped_quantity DECIMAL(18, 6) NOT NULL DEFAULT 0
        COMMENT '累计实际发货数量',

    remark VARCHAR(500) NULL COMMENT '备注',

    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    KEY idx_shipment_line_plan (shipment_plan_id),

    CONSTRAINT fk_shipment_line_plan
        FOREIGN KEY (shipment_plan_id)
        REFERENCES shipment_plans(id),

    CHECK (quantity > 0),
    CHECK (shipped_quantity >= 0),
    CHECK (shipped_quantity <= quantity)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
COMMENT='发货计划详单';

CREATE TABLE shipment_plan_allocations (
    id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT COMMENT '批次分配ID',

    shipment_line_id BIGINT UNSIGNED NOT NULL COMMENT '发货详单ID',
    warehouse_id BIGINT UNSIGNED NOT NULL COMMENT '仓库ID',
    batch_id BIGINT UNSIGNED NOT NULL COMMENT '成品批次ID',

    planned_quantity DECIMAL(18, 6) NOT NULL
        COMMENT '该批次计划发货数量',
    shipped_quantity DECIMAL(18, 6) NOT NULL DEFAULT 0
        COMMENT '该批次累计实际发货数量',

    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    KEY idx_allocation_line (shipment_line_id),
    KEY idx_allocation_warehouse_batch (warehouse_id, batch_id),

    CONSTRAINT fk_spa_line
        FOREIGN KEY (shipment_line_id)
        REFERENCES shipment_plan_lines(id),

    CONSTRAINT fk_spa_warehouse
        FOREIGN KEY (warehouse_id)
        REFERENCES warehouses(id),

    CONSTRAINT fk_spa_batch
        FOREIGN KEY (batch_id)
        REFERENCES finished_goods_batches(id),

    CHECK (planned_quantity > 0),
    CHECK (shipped_quantity >= 0),
    CHECK (shipped_quantity <= planned_quantity)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
COMMENT='发货计划批次分配表';

CREATE TABLE finished_goods_reservations (
    id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT COMMENT '库存预留ID',

    reservation_no VARCHAR(64) NOT NULL COMMENT '预留编号',

    reservation_type ENUM(
        'PRODUCTION_PLAN',
        'SHIPMENT'
    ) NOT NULL COMMENT '预留业务类型',

    production_plan_id BIGINT UNSIGNED NULL COMMENT '生产计划ID',
    shipment_allocation_id BIGINT UNSIGNED NULL COMMENT '发货批次分配ID',

    warehouse_id BIGINT UNSIGNED NOT NULL COMMENT '预留仓库ID',
    batch_id BIGINT UNSIGNED NOT NULL COMMENT '预留成品批次ID',

    reserved_quantity DECIMAL(18, 6) NOT NULL
        COMMENT '初始预留数量',

    remaining_quantity DECIMAL(18, 6) NOT NULL
        COMMENT '当前仍占用的预留数量',

    status ENUM(
        'ACTIVE',
        'RELEASED',
        'CONSUMED'
    ) NOT NULL DEFAULT 'ACTIVE' COMMENT '预留状态',

    remark VARCHAR(500) NULL COMMENT '备注',

    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY uk_reservation_no (reservation_no),

    KEY idx_reservation_inventory
        (warehouse_id, batch_id, status),

    KEY idx_reservation_production
        (production_plan_id, status),

    KEY idx_reservation_shipment
        (shipment_allocation_id, status),

    CONSTRAINT fk_reservation_production_plan
        FOREIGN KEY (production_plan_id)
        REFERENCES production_plans(id),

    CONSTRAINT fk_reservation_shipment_allocation
        FOREIGN KEY (shipment_allocation_id)
        REFERENCES shipment_plan_allocations(id),

    CONSTRAINT fk_reservation_warehouse
        FOREIGN KEY (warehouse_id)
        REFERENCES warehouses(id),

    CONSTRAINT fk_reservation_batch
        FOREIGN KEY (batch_id)
        REFERENCES finished_goods_batches(id),

    CHECK (reserved_quantity > 0),
    CHECK (remaining_quantity >= 0),
    CHECK (remaining_quantity <= reserved_quantity),

    CHECK (
        (reservation_type = 'PRODUCTION_PLAN'
            AND production_plan_id IS NOT NULL
            AND shipment_allocation_id IS NULL)
        OR
        (reservation_type = 'SHIPMENT'
            AND shipment_allocation_id IS NOT NULL
            AND production_plan_id IS NULL)
    ),

    CHECK (
        (status = 'ACTIVE' AND remaining_quantity > 0)
        OR
        (status IN ('RELEASED', 'CONSUMED')
            AND remaining_quantity = 0)
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
COMMENT='成品库存统一预留表';

INSERT INTO roles (role_code, role_name, description)
VALUES
('sales', '销售人员', '负责销售业绩及销售需求'),
('order_followup', '跟单员', '负责工艺单、生产跟进和发货计划'),
('factory_worker', '加工厂员工', '负责生产任务及完工登记'),
('admin', '管理人员', '负责系统管理及授权范围内的管理操作');

INSERT INTO employees (username, password_hash, real_name, nickname, status)
VALUES
('chen1', 'hashed_password1', '陈一', 'A', 1),
('chen2', 'hashed_password2', '陈二', 'B', 1),
('chen3', 'hashed_password3', '陈三', 'C', 1),
('chen4', 'hashed_password4', '陈四', 'D', 1);

INSERT INTO warehouses (warehouse_code, warehouse_name, warehouse_type, manager_employee_id, status)
VALUES
('CY1001', '金坛仓库1', 'raw_material', 1, 1),
('CY1002', '金坛仓库2', 'raw_material', 2, 1),
('CY1003', '金坛仓库3', 'finished_goods', 3, 1),
('CY1004', '金坛仓库4', 'finished_goods', 4, 1);

INSERT INTO materials (material_code, material_name, status)
VALUES
('YL10001', '丁醚', 1),
('YL10002', '乙醚', 1),
('YL10003', '丙醚', 1),
('YL10004', '丁醇', 1);

INSERT INTO suppliers (name, contact, status)
VALUES
('供应商1', '张一', 1),
('供应商2', '李二', 1),
('供应商3', '王三', 1);

INSERT INTO factories (factory_code, factory_name, status)
VALUES
('JGC10001', '加工厂1', 1),
('JGC10002', '加工厂2', 1);
