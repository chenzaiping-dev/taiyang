from flask import Blueprint, render_template, request
from sqlalchemy import distinct

from app.extensions import db
from app.generated.models.employees import Employee
from app.generated.models.materials import Material
from app.generated.models.warehouses import Warehouse
from app.generated.models.inventory_cost_layers import InventoryCostLayer
from app.services.inventory_query_service import InventoryQueryService

bp = Blueprint("inventory_query", __name__, url_prefix="/inventory")


@bp.get("/material-stock")
def material_stock_page():
    material_code = request.args.get("material_code", "").strip() or None
    wh_raw = request.args.get("warehouse_id", "").strip()
    warehouse_id = int(wh_raw) if wh_raw.isdigit() else None

    # ⭐ 选中仓库时，自动开启按仓库拆分
    group_by_warehouse = (
        request.args.get("group_by_warehouse") == "1" or warehouse_id is not None
    )

    page = max(int(request.args.get("page", 1) or 1), 1)
    page_size = 50

    rows, total = InventoryQueryService.query_material_stock(
        material_code=material_code,
        warehouse_id=warehouse_id,
        group_by_warehouse=group_by_warehouse,
        page=page,
        page_size=page_size,
    )
    total_pages = max((total + page_size - 1) // page_size, 1)

    # 当前选中的仓库对象（用于顶部信息条）
    current_wh = (
        Warehouse.query.get(warehouse_id) if warehouse_id else None
    )

    # ⭐ 仓库下拉：原料仓 + 普通仓（成品仓不涉及原料库存）
    warehouses = (
        Warehouse.query
        .filter(
            Warehouse.status == 1,
            Warehouse.warehouse_type.in_(["raw_material", "general"]),
        )
        .order_by(Warehouse.warehouse_type.asc(), Warehouse.id.asc())
        .all()
    )

    # 在 material_stock_page 里追加
    available_materials = []
    if warehouse_id:
        ids = (
            db.session.query(distinct(InventoryCostLayer.material_id))
            .filter(
                InventoryCostLayer.warehouse_id == warehouse_id,
                InventoryCostLayer.remaining_qty > 0,
            )
            .all()
        )
        ids = [i[0] for i in ids]
        if ids:
            available_materials = (
                Material.query.filter(Material.id.in_(ids))
                .order_by(Material.material_code.asc())
                .all()
            )

    return render_template(
        "inventory/material_stock.html",
        rows=rows,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
        material_code=material_code or "",
        warehouse_id=warehouse_id or "",
        current_wh=current_wh,            # ⭐
        group_by_warehouse=group_by_warehouse,
        warehouses=warehouses,
        available_materials=available_materials,
    )

from datetime import datetime

@bp.get("/material-io-summary")
def material_io_summary_page():
    material_code = request.args.get("material_code", "").strip() or None
    wh_raw = request.args.get("warehouse_id", "").strip()
    warehouse_id = int(wh_raw) if wh_raw.isdigit() else None
    date_from_raw = request.args.get("date_from", "").strip() or None
    date_to_raw = request.args.get("date_to", "").strip() or None

    def _parse_date(s):
        if not s:
            return None
        try:
            return datetime.strptime(s, "%Y-%m-%d")
        except ValueError:
            return None

    date_from = _parse_date(date_from_raw)
    date_to = _parse_date(date_to_raw)

    page = max(int(request.args.get("page", 1) or 1), 1)
    page_size = 50

    rows, total = InventoryQueryService.query_material_io_summary(
        material_code=material_code,
        warehouse_id=warehouse_id,
        date_from=date_from,
        date_to=date_to,
        page=page,
        page_size=page_size,
    )
    total_pages = max((total + page_size - 1) // page_size, 1)

    current_wh = Warehouse.query.get(warehouse_id) if warehouse_id else None
    warehouses = (
        Warehouse.query
        .filter(
            Warehouse.status == 1,
            Warehouse.warehouse_type.in_(["raw_material", "general"]),
        )
        .order_by(Warehouse.warehouse_type.asc(), Warehouse.id.asc())
        .all()
    )

    return render_template(
        "inventory/material_io_summary.html",
        rows=rows, total=total, page=page,
        page_size=page_size, total_pages=total_pages,
        material_code=material_code or "",
        warehouse_id=warehouse_id or "",
        date_from=date_from_raw or "",
        date_to=date_to_raw or "",
        current_wh=current_wh,
        warehouses=warehouses,
    )

TRANSACTION_TYPE_LABELS = {
    "PURCHASE_IN":   "采购入库",
    "PRODUCTION_OUT": "生产领料",
    "STOCKTAKE_GAIN": "盘盈入库",
    "STOCKTAKE_LOSS": "盘亏出库",
    "RETURN_IN":     "退货入库",
}

DOCUMENT_TYPE_LABELS = {
    "PURCHASE_IN":   "采购入库单",
    "PRODUCTION_OUT": "生产领料单",
    "STOCKTAKE":     "盘点单",
    "ADJUSTMENT":    "调整单",
}

@bp.get("/material-transactions")
def material_transactions_page():
    material_code = request.args.get("material_code", "").strip() or None
    wh_raw = request.args.get("warehouse_id", "").strip()
    warehouse_id = int(wh_raw) if wh_raw.isdigit() else None
    transaction_type = request.args.get("transaction_type", "").strip() or None
    document_no = request.args.get("document_no", "").strip() or None
    date_from_raw = request.args.get("date_from", "").strip() or None
    date_to_raw = request.args.get("date_to", "").strip() or None

    def _parse_date(s):
        if not s:
            return None
        try:
            return datetime.strptime(s, "%Y-%m-%d")
        except ValueError:
            return None

    date_from = _parse_date(date_from_raw)
    date_to = _parse_date(date_to_raw)

    page = max(int(request.args.get("page", 1) or 1), 1)
    page_size = 50

    rows, total = InventoryQueryService.query_material_transactions(
        material_code=material_code,
        warehouse_id=warehouse_id,
        transaction_type=transaction_type,
        document_no=document_no,
        date_from=date_from,
        date_to=date_to,
        page=page,
        page_size=page_size,
    )
    total_pages = max((total + page_size - 1) // page_size, 1)

    warehouses = (
        Warehouse.query
        .filter(
            Warehouse.status == 1,
            Warehouse.warehouse_type.in_(["raw_material", "general"]),
        )
        .order_by(Warehouse.warehouse_type.asc(), Warehouse.id.asc())
        .all()
    )

    return render_template(
        "inventory/material_transactions.html",
        rows=rows, total=total, page=page,
        page_size=page_size, total_pages=total_pages,
        material_code=material_code or "",
        warehouse_id=warehouse_id or "",
        transaction_type=transaction_type or "",
        document_no=document_no or "",
        date_from=date_from_raw or "",
        date_to=date_to_raw or "",
        warehouses=warehouses,
        transaction_types=TRANSACTION_TYPE_LABELS,
        document_types=DOCUMENT_TYPE_LABELS,
    )