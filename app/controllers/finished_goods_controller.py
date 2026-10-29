from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash

from app.extensions import db
from app.generated.models.warehouses import Warehouse

from app.generated.models.finished_goods_batches import FinishedGoodsBatch
from app.generated.models.finished_goods_documents import FinishedGoodsDocument
from app.generated.models.finished_goods_document_lines import FinishedGoodsDocumentLine
from app.generated.models.finished_goods_inventory import FinishedGoodsInventory
from app.generated.models.finished_goods_transactions import FinishedGoodsTransaction

from app.services.finished_goods_query_service import FinishedGoodsQueryService
from app.services.finished_goods_service import FinishedGoodsError, FinishedGoodsService


bp = Blueprint("finished_goods", __name__, url_prefix="/finished-goods")

INBOUND_TYPE_LABELS = {
    "PRODUCTION_IN": "生产入库",
    "CUSTOMER_RETURN": "客户退货",
    "ADJUSTMENT": "盘点/调整",
}

TX_TYPE_LABELS = {
    "PRODUCTION_IN": "生产入库",
    "OUTBOUND": "出库",
    "TRANSFER_OUT": "调拨出库",
    "TRANSFER_IN": "调拨入库",
    "CUSTOMER_RETURN": "客户退货",
    "ADJUSTMENT": "库存调整",
}


def _fg_warehouses():
    return (
        Warehouse.query
        .filter(
            Warehouse.status == 1,
            Warehouse.warehouse_type.in_(["finished_goods", "general"]),
        )
        .order_by(Warehouse.id.asc())
        .all()
    )


def _parse_qty(s):
    try:
        return float(s or 0)
    except ValueError:
        return None


# ==================== 入库 ====================
@bp.get("/inbound")
def inbound_page():
    return render_template(
        "finished_goods/inbound.html",
        warehouses=_fg_warehouses(),
        inbound_types=INBOUND_TYPE_LABELS,
        form_data={
            "warehouse_id": "", "inbound_type": "PRODUCTION_IN",
            "document_time": datetime.now().strftime("%Y-%m-%dT%H:%M"),
            "remark": "", "lines": [{}],
        },
        errors=[],
    )


@bp.post("/inbound")
def inbound_submit():
    wh_raw = request.form.get("warehouse_id", "").strip()
    warehouse_id = int(wh_raw) if wh_raw.isdigit() else None
    inbound_type = request.form.get("inbound_type", "PRODUCTION_IN")
    doc_time_str = request.form.get("document_time", "").strip()
    remark = request.form.get("remark", "").strip() or None

    # 汇总明细（行号按 batch_no 或 batch_id 的索引顺序）
    lines = []
    line_idx = 0
    while True:
        prefix = f"lines[{line_idx}]"
        if prefix + "[quantity]" not in request.form:
            break
        qty = _parse_qty(request.form.get(prefix + "[quantity]"))
        if qty is None or qty <= 0:
            line_idx += 1
            continue
        batch_id_raw = request.form.get(prefix + "[batch_id]", "").strip()
        lines.append({
            "batch_id": int(batch_id_raw) if batch_id_raw else None,
            "batch_no": request.form.get(prefix + "[batch_no]", "").strip(),
            "product_name": request.form.get(prefix + "[product_name]", "").strip(),
            "product_specification": request.form.get(
                prefix + "[product_specification]", "").strip() or None,
            "unit": request.form.get(prefix + "[unit]", "KG").strip() or "KG",
            "quality_status": request.form.get(prefix + "[quality_status]",
                                              "QUALIFIED"),
            "process_order_id": None,
            "quantity": request.form.get(prefix + "[quantity]").strip(),
            "unit_cost": request.form.get(prefix + "[unit_cost]", "").strip() or None,
            "remark": request.form.get(prefix + "[remark]", "").strip() or None,
        })
        line_idx += 1

    try:
        doc_time = (datetime.fromisoformat(doc_time_str)
                    if doc_time_str else None)
        result = FinishedGoodsService.create_inbound(
            warehouse_id=warehouse_id,
            lines=lines,
            inbound_type=inbound_type,
            document_time=doc_time,
            created_by=1,
            remark=remark,
        )
        flash(f"入库成功，单号 {result['document_no']}", "success")
        return redirect(url_for("finished_goods.transactions_page",
                                document_no=result["document_no"]))
    except FinishedGoodsError as e:
        return render_template(
            "finished_goods/inbound.html",
            warehouses=_fg_warehouses(),
            inbound_types=INBOUND_TYPE_LABELS,
            form_data={
                "warehouse_id": wh_raw, "inbound_type": inbound_type,
                "document_time": doc_time_str, "remark": remark or "",
                "lines": lines or [{}],
            },
            errors=[str(e)],
        ), 400


# ==================== 出库 ====================
@bp.get("/outbound")
def outbound_page():
    wh_raw = request.args.get("warehouse_id", "").strip()
    warehouse_id = int(wh_raw) if wh_raw.isdigit() else None
    batches = (
        FinishedGoodsQueryService.list_available_batches(warehouse_id)
        if warehouse_id else []
    )
    return render_template(
        "finished_goods/outbound.html",
        warehouses=_fg_warehouses(),
        batches=batches,
        selected_wh=warehouse_id,
        form_data={
            "warehouse_id": warehouse_id or "",
            "document_time": datetime.now().strftime("%Y-%m-%dT%H:%M"),
            "remark": "",
        },
        errors=[],
    )


@bp.post("/outbound")
def outbound_submit():
    wh_raw = request.form.get("warehouse_id", "").strip()
    warehouse_id = int(wh_raw) if wh_raw.isdigit() else None
    doc_time_str = request.form.get("document_time", "").strip()
    remark = request.form.get("remark", "").strip() or None

    lines = []
    idx = 0
    while True:
        prefix = f"lines[{idx}]"
        if prefix + "[quantity]" not in request.form:
            break
        qty = _parse_qty(request.form.get(prefix + "[quantity]"))
        batch_id_raw = request.form.get(prefix + "[batch_id]", "").strip()
        if qty and qty > 0 and batch_id_raw.isdigit():
            lines.append({
                "batch_id": int(batch_id_raw),
                "quantity": request.form.get(prefix + "[quantity]").strip(),
                "remark": request.form.get(prefix + "[remark]", "").strip() or None,
            })
        idx += 1

    if not lines:
        return render_template(
            "finished_goods/outbound.html",
            warehouses=_fg_warehouses(),
            batches=(
                FinishedGoodsQueryService.list_available_batches(warehouse_id)
                if warehouse_id else []
            ),
            selected_wh=warehouse_id,
            form_data={"warehouse_id": wh_raw, "document_time": doc_time_str,
                       "remark": remark or ""},
            errors=["请至少选择一条批次并填写数量"],
        ), 400

    try:
        doc_time = datetime.fromisoformat(doc_time_str) if doc_time_str else None
        result = FinishedGoodsService.create_outbound(
            warehouse_id=warehouse_id,
            lines=lines,
            document_time=doc_time,
            created_by=1,
            remark=remark,
        )
        flash(f"出库成功，单号 {result['document_no']}", "success")
        return redirect(url_for("finished_goods.transactions_page",
                                document_no=result["document_no"]))
    except FinishedGoodsError as e:
        return render_template(
            "finished_goods/outbound.html",
            warehouses=_fg_warehouses(),
            batches=(
                FinishedGoodsQueryService.list_available_batches(warehouse_id)
                if warehouse_id else []
            ),
            selected_wh=warehouse_id,
            form_data={"warehouse_id": wh_raw, "document_time": doc_time_str,
                       "remark": remark or ""},
            errors=[str(e)],
        ), 400


# ==================== 仓库库存列表 ====================
@bp.get("/warehouses")
def warehouses_page():
    wh_raw = request.args.get("warehouse_id", "").strip()
    warehouse_id = int(wh_raw) if wh_raw.isdigit() else None
    keyword = request.args.get("batch_keyword", "").strip() or None
    page = max(int(request.args.get("page", 1) or 1), 1)
    page_size = 50

    rows, total = FinishedGoodsQueryService.list_warehouse_stock(
        warehouse_id=warehouse_id, batch_keyword=keyword,
        page=page, page_size=page_size,
    )
    total_pages = max((total + page_size - 1) // page_size, 1)

    return render_template(
        "finished_goods/warehouses.html",
        rows=rows, total=total, page=page,
        page_size=page_size, total_pages=total_pages,
        warehouse_id=warehouse_id or "",
        batch_keyword=keyword or "",
        warehouses=_fg_warehouses(),
        quality_labels={"PENDING": "待检", "QUALIFIED": "合格", "REJECTED": "不合格"},
    )


# ==================== 历史流水 ====================
@bp.get("/transactions")
def transactions_page():
    wh_raw = request.args.get("warehouse_id", "").strip()
    warehouse_id = int(wh_raw) if wh_raw.isdigit() else None
    batch_raw = request.args.get("batch_id", "").strip()
    batch_id = int(batch_raw) if batch_raw.isdigit() else None
    tx_type = request.args.get("transaction_type", "").strip() or None
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

    page = max(int(request.args.get("page", 1) or 1), 1)
    page_size = 50

    rows, total = FinishedGoodsQueryService.list_transactions(
        warehouse_id=warehouse_id, batch_id=batch_id,
        tx_type=tx_type, document_no=document_no,
        date_from=_parse_date(date_from_raw),
        date_to=_parse_date(date_to_raw),
        page=page, page_size=page_size,
    )
    total_pages = max((total + page_size - 1) // page_size, 1)

    return render_template(
        "finished_goods/transactions.html",
        rows=rows, total=total, page=page,
        page_size=page_size, total_pages=total_pages,
        warehouse_id=warehouse_id or "",
        batch_id=batch_id or "",
        transaction_type=tx_type or "",
        document_no=document_no or "",
        date_from=date_from_raw or "",
        date_to=date_to_raw or "",
        warehouses=_fg_warehouses(),
        tx_types=TX_TYPE_LABELS,
    )

@bp.get("/io-summary")
def io_summary_page():
    wh_raw = request.args.get("warehouse_id", "").strip()
    warehouse_id = int(wh_raw) if wh_raw.isdigit() else None
    batch_keyword = request.args.get("batch_keyword", "").strip() or None
    date_from_raw = request.args.get("date_from", "").strip() or None
    date_to_raw = request.args.get("date_to", "").strip() or None
    group_by = request.args.get("group_by", "batch")
    if group_by not in ("batch", "product"):
        group_by = "batch"

    def _parse_date(s):
        if not s:
            return None
        try:
            return datetime.strptime(s, "%Y-%m-%d")
        except ValueError:
            return None

    page = max(int(request.args.get("page", 1) or 1), 1)
    page_size = 50

    rows, total = FinishedGoodsQueryService.query_io_summary(
        warehouse_id=warehouse_id,
        batch_keyword=batch_keyword,
        date_from=_parse_date(date_from_raw),
        date_to=_parse_date(date_to_raw),
        group_by=group_by,
        page=page, page_size=page_size,
    )
    total_pages = max((total + page_size - 1) // page_size, 1)

    return render_template(
        "finished_goods/io_summary.html",
        rows=rows, total=total, page=page,
        page_size=page_size, total_pages=total_pages,
        warehouse_id=warehouse_id or "",
        batch_keyword=batch_keyword or "",
        date_from=date_from_raw or "",
        date_to=date_to_raw or "",
        group_by=group_by,
        warehouses=_fg_warehouses(),
        quality_labels={"PENDING": "待检", "QUALIFIED": "合格", "REJECTED": "不合格"},
    )