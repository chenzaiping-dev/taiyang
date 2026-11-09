from flask import Blueprint, render_template, request, flash, redirect, abort, url_for
from sqlalchemy import distinct

from app.extensions import db
from app.generated.models.employees import Employee
from app.generated.models.materials import Material
from app.generated.models.warehouses import Warehouse
from app.generated.models.inventory_cost_layers import InventoryCostLayer
from app.services.inventory_query_service import InventoryQueryService
from app.services.inventory_service import (
    PurchaseInService, StockOutService, InventoryError, 
    INBOUND_TYPE_LABELS, OUTBOUND_TYPE_LABELS, _rm_warehouses
)

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
        warehouses=_rm_warehouses(),
        available_materials=available_materials,
    )

@bp.get("/material-stock-with-cost-layers")
def material_stock_with_cost_layers_page():
    print("here")
    material_code = request.args.get("material_code", "").strip() or None
    wh_raw = request.args.get("warehouse_id", "").strip()
    warehouse_id = int(wh_raw) if wh_raw.isdigit() else None

    # ⭐ 选中仓库时，自动开启按仓库拆分
    group_by_warehouse = (
        request.args.get("group_by_warehouse") == "1" or warehouse_id is not None
    )

    page = max(int(request.args.get("page", 1) or 1), 1)
    page_size = 50

    rows, total = InventoryQueryService.query_material_stock_with_cost_layers(
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
        "inventory/material_stock_with_cost_layers.html",
        rows=rows,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
        material_code=material_code or "",
        warehouse_id=warehouse_id or "",
        current_wh=current_wh,            # ⭐
        group_by_warehouse=group_by_warehouse,
        warehouses=_rm_warehouses(),
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

    return render_template(
        "inventory/material_io_summary.html",
        rows=rows, total=total, page=page,
        page_size=page_size, total_pages=total_pages,
        material_code=material_code or "",
        warehouse_id=warehouse_id or "",
        date_from=date_from_raw or "",
        date_to=date_to_raw or "",
        current_wh=current_wh,
        warehouses=_rm_warehouses(),
    )

TRANSACTION_TYPE_LABELS = {
    "PURCHASE_IN":   "采购入库",
    "PRODUCTION_OUT": "生产出库",
    "STOCKTAKE_GAIN": "盘盈入库",
    "STOCKTAKE_LOSS": "盘亏出库",
    "RETURN_IN":     "退货入库",
    "RETURN_OUT":    "退货出库",
    "ADJUSTMENT_RED": "红冲出库",
    "ADJUSTMENT_BLUE": "蓝补出库"
}

TRANSACTION_TYPE_DIRECTION = {
    "PURCHASE_IN":   "in",
    "PRODUCTION_OUT": "out",
    "STOCKTAKE_GAIN": "in",
    "STOCKTAKE_LOSS": "out",
    "RETURN_IN":     "in",
    "RETURN_OUT":    "out",
    "ADJUSTMENT_RED": "out",
    "ADJUSTMENT_BLUE": "out"
}

DOC_STATUS_LABELS = {
    "DRAFT": "草稿",
    "CONFIRMED": "已确认",
    "CANCELLED": "已取消",
}

DOC_TYPE_LABELS = {
    "PURCHASE_IN": "采购入库单",
    "PRODUCTION_OUT": "生产出库单",
    "STOCKTAKE": "盘点单",
    "RETURN": "退货单",
    "ADJUSTMENT_RED": "红冲出库单",
    "ADJUSTMENT_BLUE": "蓝补出库单",
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
        warehouses=_rm_warehouses(),
        transaction_types=TRANSACTION_TYPE_LABELS,
        transaction_directions=TRANSACTION_TYPE_DIRECTION,
        document_types=DOC_TYPE_LABELS,
    )

# ==================== 通用状态/类型标签 ====================

def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        return None


# ==================== 通用列表 ====================
@bp.get("/documents/<direction>")
def document_list_page(direction):
    """
    direction='in'  → 入库单列表
    direction='out' → 出库单列表
    """
    if direction not in ("in", "out"):
        abort(404)
    direction_code = "IN" if direction == "in" else "OUT"

    keyword = request.args.get("keyword", "").strip() or None
    status = request.args.get("status", "").strip() or None
    inbound_type = request.args.get("inbound_type", "").strip() or None
    outbound_type = request.args.get("outbound_type", "").strip() or None
    date_from_raw = request.args.get("date_from", "").strip() or None
    date_to_raw = request.args.get("date_to", "").strip() or None
    page = max(int(request.args.get("page", 1) or 1), 1)
    page_size = 50

    rows, total = InventoryQueryService.list_documents(
        direction=direction_code,
        keyword=keyword, status=status,
        inbound_type=inbound_type,
        outbound_type=outbound_type,
        date_from=_parse_date(date_from_raw),
        date_to=_parse_date(date_to_raw),
        page=page, page_size=page_size,
    )
    total_pages = max((total + page_size - 1) // page_size, 1)

    return render_template(
        "inventory/document_list.html",
        direction=direction,
        direction_code=direction_code,
        rows=rows, total=total, page=page,
        page_size=page_size, total_pages=total_pages,
        keyword=keyword or "",
        status=status or "",
        inbound_type=inbound_type or "",
        outbound_type=outbound_type or "",
        date_from=date_from_raw or "",
        date_to=date_to_raw or "",
        status_labels=DOC_STATUS_LABELS,
        doc_type_labels=DOC_TYPE_LABELS,
        inbound_types=INBOUND_TYPE_LABELS,
        outbound_types=OUTBOUND_TYPE_LABELS,
        new_doc_url=(
            url_for("purchase_in.purchase_in_page")
            if direction == "in"
            else url_for("stock_out.stock_out_page")
        ),
        confirm_endpoint=(
            "inventory_query.purchase_in_confirm"
            if direction == "in"
            else "inventory_query.stock_out_confirm"
        ),
    )


# ==================== 通用详情 ====================
@bp.get("/documents/<direction>/<int:document_id>")
def document_detail_page(direction, document_id):
    if direction not in ("in", "out"):
        abort(404)

    detail = InventoryQueryService.get_document_detail(document_id)
    if not detail:
        return render_template(
            "inventory/document_detail.html",
            direction=direction,
            detail=None,
            error="单据不存在",
            doc_type_labels=DOC_TYPE_LABELS,
            status_labels=DOC_STATUS_LABELS,
            transaction_types=TRANSACTION_TYPE_LABELS,
            confirm_endpoint=None,
            list_url=url_for("inventory_query.document_list_page",
                             direction=direction),
        ), 404

    # 校验方向匹配：入库单不能在出库视图里打开
    doc_type = detail["document"]["document_type"]
    is_inbound = doc_type in ("PURCHASE_IN", "STOCKTAKE", "RETURN") and all(
        l["transaction_type"] in ("PURCHASE_IN", "STOCKTAKE_GAIN", "RETURN_IN")
        for l in detail["lines"]
    )
    if direction == "in" and not is_inbound:
        abort(404)
    if direction == "out" and is_inbound:
        abort(404)

    confirm_endpoint = (
        "inventory_query.purchase_in_confirm"
        if direction == "in"
        else "inventory_query.stock_out_confirm"
    )
    edit_url = None
    if detail["document"]["status"] == "DRAFT":
        edit_url = (
            url_for("purchase_in.purchase_in_edit_page", document_id=document_id)
            if direction == "in"
            else url_for("stock_out.stock_out_edit_page", document_id=document_id)
        )

    return render_template(
        "inventory/document_detail.html",
        direction=direction,
        detail=detail,
        doc_type_labels=DOC_TYPE_LABELS,
        status_labels=DOC_STATUS_LABELS,
        transaction_types=TRANSACTION_TYPE_LABELS,
        confirm_endpoint=confirm_endpoint,
        edit_url=edit_url,
        list_url=url_for("inventory_query.document_list_page",
                         direction=direction),
    )


# ==================== 保留确认路由 ====================
@bp.post("/stock-out/<int:document_id>/confirm")
def stock_out_confirm(document_id):
    try:
        StockOutService.confirm_document(document_id)
        flash("出库单已确认，FIFO 成本已分配", "success")
    except InventoryError as e:
        flash(str(e), "error")
    return redirect(
        request.referrer
        or url_for("inventory_query.document_detail_page",
                   direction="out", document_id=document_id)
    )


@bp.post("/purchase-in/<int:document_id>/confirm")
def purchase_in_confirm(document_id):
    try:
        PurchaseInService.confirm_document(document_id)
        flash("入库单已确认，FIFO 成本层已生成", "success")
    except InventoryError as e:
        flash(str(e), "error")
    return redirect(
        request.referrer
        or url_for("inventory_query.document_detail_page",
                   direction="in", document_id=document_id)
    )