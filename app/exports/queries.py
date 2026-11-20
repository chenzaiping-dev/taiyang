"""Business query layer shared by HTML list pages and XLSX exports."""

from app.utils.datetime_utils import util_parse_date, util_parse_date_end
from app.services.inventory_query_service import InventoryQueryService
from app.services.finished_goods_query_service import FinishedGoodsQueryService
from app.services.sales_performance_service import SalesPerformanceService
from app.services.shipment_plan_service import ShipmentPlanService


EXPORT_PAGE_SIZE = 2_000

LABELS = {
    "transaction_type": {
        "PURCHASE_IN": "采购入库", "PRODUCTION_OUT": "生产出库",
        "STOCKTAKE_GAIN": "盘盈入库", "STOCKTAKE_LOSS": "盘亏出库",
        "RETURN_IN": "退货入库", "RETURN_OUT": "退货出库",
        "ADJUSTMENT_RED": "红冲出库", "ADJUSTMENT_BLUE": "蓝补出库",
        "IN_REVERSE": "入库撤销", "OUT_REVERSE": "出库撤销",
        "PRODUCTION_IN": "生产入库", "OUTBOUND": "销售出库",
        "TRANSFER_IN": "调拨入库", "TRANSFER_OUT": "调拨出库",
    },
    "document_type": {
        "PURCHASE_IN": "采购入库单", "PRODUCTION_OUT": "生产出库单",
        "STOCKTAKE": "盘点单", "RETURN": "退货单",
        "ADJUSTMENT_RED": "配比调整红冲单", "ADJUSTMENT_BLUE": "配比调整蓝补单",
        "REVERSE": "库存撤销单", "PRODUCTION_IN": "生产入库单",
        "OUTBOUND": "销售出库单", "TRANSFER": "调拨单",
        "OUT_REVERSE": "出库撤销单", "IN_REVERSE": "入库撤销单",
    },
    "document_status": {
        "DRAFT": "草稿", "CONFIRMED": "已确认", "CANCELLED": "已取消",
    },
    "status": {
        "DRAFT": "草稿", "CONFIRMED": "已确认",
        "PARTIALLY_SHIPPED": "部分发货", "SHIPPED": "已发货",
        "CANCELLED": "已取消", "SUBMITTED": "已提交",
        "PLANNED": "已计划生产", "PROCESS_ORDERED": "已下工艺单",
        "FINISHED_GOODS_IN": "已入成品库", "SHIPMENT_PLANNED": "已排期发货",
        "COMPLETED": "已完成", "PRODUCED": "已生产",
        "SHIPPED": "已发货",
    },
    "payment_status": {"PAID": "已收款", "UNPAID": "未收款", "PARTIAL": "部分收款"},
    "quality_status": {"PENDING": "待检", "QUALIFIED": "合格", "REJECTED": "不合格"},
}


def _label_rows(kind, rows):
    for row in rows:
        for key, labels in LABELS.items():
            if key in row and row[key] in labels:
                row[key] = labels[row[key]]
        if kind == "sales_performances":
            for key in ("is_joint_sales", "is_changzhou_factory"):
                if key in row:
                    row[key] = "是" if row[key] else "否"
    return rows



def _positive_int(value):
    return int(value) if str(value or "").isdigit() and int(value) > 0 else None


def _pagination(params):
    page = _positive_int(params.get("_page")) or 1
    page_size = _positive_int(params.get("_page_size")) or EXPORT_PAGE_SIZE
    return page, page_size


def material_stock_layers(params):
    page, page_size = _pagination(params)
    material_code = (params.get("material_code") or "").strip() or None
    warehouse_id = _positive_int(params.get("warehouse_id"))
    group_by_warehouse = params.get("group_by_warehouse") == "1" or warehouse_id is not None
    return InventoryQueryService.query_material_stock_with_cost_layers(
        material_code=material_code, warehouse_id=warehouse_id,
        group_by_warehouse=group_by_warehouse, page=page, page_size=page_size,
    )


def material_transactions(params):
    page, page_size = _pagination(params)
    return InventoryQueryService.list_transactions(
        material_code=(params.get("material_code") or "").strip() or None,
        warehouse_id=_positive_int(params.get("warehouse_id")),
        transaction_type=(params.get("transaction_type") or "").strip() or None,
        document_no=(params.get("document_no") or "").strip() or None,
        date_from=util_parse_date(params.get("date_from")),
        date_to=util_parse_date_end(params.get("date_to")),
        page=page, page_size=page_size,
    )


def finished_goods_stock(params):
    page, page_size = _pagination(params)
    return FinishedGoodsQueryService.list_warehouse_stock(
        warehouse_id=_positive_int(params.get("warehouse_id")),
        batch_keyword=(params.get("batch_keyword") or "").strip() or None,
        page=page, page_size=page_size,
    )


def finished_goods_transactions(params):
    page, page_size = _pagination(params)
    return FinishedGoodsQueryService.list_transactions(
        warehouse_id=_positive_int(params.get("warehouse_id")),
        batch_keyword=(params.get("batch_keyword") or "").strip() or None,
        tx_type=(params.get("transaction_type") or "").strip() or None,
        document_no=(params.get("document_no") or "").strip() or None,
        date_from=util_parse_date(params.get("date_from")),
        date_to=util_parse_date_end(params.get("date_to")),
        page=page, page_size=page_size,
    )


def shipment_plans(params):
    page, page_size = _pagination(params)
    return ShipmentPlanService.list_plans_query(
        params=params,
        page=page,
        page_size=page_size,
    )


def shipment_plan_details(params):
    page, page_size = _pagination(params)
    return ShipmentPlanService.list_plan_details_query(
        params=params, page=page, page_size=page_size,
    )


def sales_performances(params):
    page, page_size = _pagination(params)
    return SalesPerformanceService.list_performances(
        performance_no=(params.get("performance_no") or "").strip() or None,
        company_name=(params.get("company_name") or "").strip() or None,
        product_name=(params.get("product_name") or "").strip() or None,
        status=(params.get("status") or "").strip() or None,
        payment_status=(params.get("payment_status") or "").strip() or None,
        date_from=util_parse_date(params.get("date_from")),
        date_to=util_parse_date_end(params.get("date_to")),
        salesperson_id=_positive_int(params.get("salesperson_id")),
        page=page, page_size=page_size,
    )


QUERY_FUNCTIONS = {
    "material_stock_layers": material_stock_layers,
    "material_transactions": material_transactions,
    "finished_goods_stock": finished_goods_stock,
    "finished_goods_transactions": finished_goods_transactions,
    "shipment_plans": shipment_plans,
    "shipment_plan_details": shipment_plan_details,
    "sales_performances": sales_performances,
}


def run_query(kind, params=None, page=None, page_size=None):
    if kind not in QUERY_FUNCTIONS:
        raise ValueError("Unknown export query")
    query_params = dict(params or {})
    if page is not None:
        query_params["_page"] = page
    if page_size is not None:
        query_params["_page_size"] = page_size
    rows, total = QUERY_FUNCTIONS[kind](query_params)
    return rows, total


def prepare_export_rows(kind, rows):
    # Copy rows so label conversion never changes values used by HTML list templates.
    return _label_rows(kind, [dict(row) for row in rows])


def iter_export_rows(kind, params=None, chunk_size=2000):
    """Fetch export data in bounded pages to keep memory usage predictable."""
    page = 1
    while True:
        rows, total = run_query(kind, params, page=page, page_size=chunk_size)
        if not rows:
            break
        yield from prepare_export_rows(kind, rows)
        if page * chunk_size >= total:
            break
        page += 1
