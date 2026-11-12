from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, abort

from app.extensions import db
from app.generated.models.warehouses import Warehouse

from app.generated.models.finished_goods_batches import FinishedGoodsBatch
from app.generated.models.finished_goods_documents import FinishedGoodsDocument
from app.generated.models.finished_goods_document_lines import FinishedGoodsDocumentLine
from app.generated.models.finished_goods_inventory import FinishedGoodsInventory
from app.generated.models.finished_goods_transactions import FinishedGoodsTransaction
from app.generated.models.process_orders import ProcessOrder

from app.services.process_order_service import _fg_warehouses
from app.services.finished_goods_query_service import FinishedGoodsQueryService
from app.services.finished_goods_service import (
    FinishedGoodsError, FinishedGoodsService, FG_INBOUND_TYPE_LABELS, FG_OUTBOUND_TYPE_LABELS,
    FG_TX_TYPE_LABELS, FG_TRANSACTION_TYPE_DIRECTION
)
from app.utils.datetime_utils import util_parse_date

bp = Blueprint("finished_goods", __name__, url_prefix="/finished-goods")

def _parse_qty(s):
    try:
        return float(s or 0)
    except ValueError:
        return None

FG_STATUS_LABELS = {
    "DRAFT": "草稿",
    "CONFIRMED": "已确认",
    "CANCELLED": "已取消",
}

FG_DOC_TYPE_LABELS = {
    "PRODUCTION_IN": "生产入库单",
    "OUTBOUND": "销售出库单",
    "STOCKTAKE": "盘点单",
    "RETURN": "退货单",
    "TRANSFER": "调拨单",
    "REVERSE": "撤销单",
}

@bp.get("/api/process-orders/search")
def api_search_process_orders():
    """
    成品入库页工艺单搜索。
    ?q=关键词  返回状态为 ORDERED/COMPLETED 的工艺单（最多 20 条）
    ?id=123    返回单条工艺单（用于回显已保存的 process_order_id）
    """
    po_id = request.args.get("id", "").strip()
    if po_id.isdigit():
        po = db.session.get(ProcessOrder, int(po_id))
        if not po:
            return jsonify({"code": 0, "data": []})
        return jsonify({
            "code": 0,
            "data": [_serialize_po(po)],
        })

    kw = request.args.get("q", "").strip()
    q = ProcessOrder.query.filter(
        ProcessOrder.status.in_(["ORDERED", "PRODUCING", "RECEIVED", "COMPLETED"])
    )
    if kw:
        like = f"%{kw}%"
        q = q.filter(
            (ProcessOrder.process_order_no.like(like)) |
            (ProcessOrder.product_name.like(like)) |
            (ProcessOrder.batch_no.like(like))
        )
    items = q.order_by(ProcessOrder.id.desc()).limit(20).all()
    return jsonify({"code": 0, "data": [_serialize_po(po) for po in items]})


def _serialize_po(po):
    return {
        "id": po.id,
        "process_order_no": po.process_order_no,
        "batch_no": po.batch_no,
        "product_name": po.product_name,
        "product_specification": po.product_specification,
        "quantity": str(po.planned_quantity),
        "unit": po.unit or "KG",
    }

# ==================== 入库 ====================
@bp.get("/inbound")
def inbound_page():
    # ⭐ 候选工艺单：优先推荐已下单/已完工但还没入成品库的
    process_orders = (
        ProcessOrder.query
        .filter(ProcessOrder.status.in_(["ORDERED", "COMPLETED"]))
        .order_by(ProcessOrder.id.desc())
        .limit(500)
        .all()
    )
    return render_template(
        "finished_goods/inbound.html",
        warehouses=_fg_warehouses(),
        inbound_types=FG_INBOUND_TYPE_LABELS,
        process_orders=process_orders,          # ⭐
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

        # ⭐ 来源工艺单：支持直接传 ID，或传「工艺单号文本」由后端反查
        po_raw = (request.form.get(prefix + "[process_order_id]", "") or "").strip()
        process_order_id = None
        if po_raw.isdigit():
            process_order_id = int(po_raw)
        elif po_raw:
            po = ProcessOrder.query.filter_by(process_order_no=po_raw).first()
            if po:
                process_order_id = po.id

        lines.append({
            "batch_id": int(batch_id_raw) if batch_id_raw else None,
            "batch_no": request.form.get(prefix + "[batch_no]", "").strip(),
            "product_name": request.form.get(prefix + "[product_name]", "").strip(),
            "product_specification": request.form.get(
                prefix + "[product_specification]", "").strip() or None,
            "unit": request.form.get(prefix + "[unit]", "KG").strip() or "KG",
            "quality_status": request.form.get(prefix + "[quality_status]", "QUALIFIED"),
            "process_order_id": process_order_id,
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
            auto_confirm=False,      # ⭐ 草稿落地
        )
        flash(f"入库单已保存为草稿，单号 {result['document_no']}，请前往列表确认", "success")
        return redirect(url_for(
            "finished_goods.document_detail_page",
            direction="in", document_id=result["document_id"]))
    except FinishedGoodsError as e:
        for ln in lines:
            pid = ln.get("process_order_id")
            if pid and not ln.get("process_order_no"):
                po = db.session.get(ProcessOrder, int(pid))
                if po:
                    ln["process_order_no"] = po.process_order_no

        return render_template(
            "finished_goods/inbound.html",
            warehouses=_fg_warehouses(),
            inbound_types=FG_INBOUND_TYPE_LABELS,
            process_orders=(
                ProcessOrder.query
                .filter(ProcessOrder.status.in_(["ORDERED", "COMPLETED"]))
                .order_by(ProcessOrder.id.desc()).limit(500).all()
            ),
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
    warehouse_id = request.args.get("warehouse_id", type=int)
    keyword = request.args.get("batch_keyword", "").strip() or None
    page = max(int(request.args.get("page", 1) or 1), 1)
    page_size = 200

    rows, total = FinishedGoodsQueryService.list_all_stock_for_outbound(
        batch_keyword=keyword,
        warehouse_id=warehouse_id,
        page=page,
        page_size=page_size,
    )
    total_pages = max((total + page_size - 1) // page_size, 1)

    return render_template(
        "finished_goods/outbound.html",
        rows=rows,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
        warehouse_id=warehouse_id or "",
        outbound_types=FG_OUTBOUND_TYPE_LABELS,
        batch_keyword=keyword or "",
        warehouses=_fg_warehouses(),
        form_data={
            "document_time": datetime.now().strftime("%Y-%m-%dT%H:%M"),
            "remark": "",
        },
        errors=[],
    )

@bp.post("/outbound")
def outbound_submit():
    doc_time_str = request.form.get("document_time", "").strip()
    outbound_type = request.form.get("outbound_type", "OUTBOUND")
    remark = request.form.get("remark", "").strip() or None

    lines = []
    idx = 0
    while True:
        prefix = f"lines[{idx}]"
        if prefix + "[batch_id]" not in request.form:
            break
        batch_raw = request.form.get(prefix + "[batch_id]", "").strip()
        wh_raw = request.form.get(prefix + "[warehouse_id]", "").strip()
        qty_raw = request.form.get(prefix + "[quantity]", "").strip()
        if batch_raw.isdigit() and wh_raw.isdigit() and qty_raw:
            try:
                if float(qty_raw) > 0:
                    lines.append({
                        "warehouse_id": int(wh_raw),
                        "batch_id": int(batch_raw),
                        "quantity": qty_raw,
                        "remark": request.form.get(
                            prefix + "[remark]", "").strip() or None,
                    })
            except ValueError:
                pass
        idx += 1

    if not lines:
        return _render_outbound_error("请至少为一条库存填写出库数量")

    try:
        doc_time = datetime.fromisoformat(doc_time_str) if doc_time_str else None
        result = FinishedGoodsService.create_outbound(
            lines=lines,
            outbound_type=outbound_type,
            document_time=doc_time,
            created_by=1,
            remark=remark,
            auto_confirm=False,       # ⭐ 草稿落地
        )
        flash(f"出库单已保存为草稿，单号 {result['document_no']}，请前往列表确认", "success")
        return redirect(url_for(
            "finished_goods.document_detail_page",
            direction="out", document_id=result["document_id"]))
    except FinishedGoodsError as e:
        return _render_outbound_error(str(e))


def _render_outbound_error(msg):
    rows, total = FinishedGoodsQueryService.list_all_stock_for_outbound(
        batch_keyword=request.args.get("batch_keyword") or request.form.get("batch_keyword") or None,
        warehouse_id=(int(request.args.get("warehouse_id"))
                      if request.args.get("warehouse_id", "").isdigit() else None),
        page=1, page_size=200,
    )
    return (
        render_template(
            "finished_goods/outbound.html",
            rows=rows, total=total, page=1, page_size=200,
            total_pages=max((total + 199) // 200, 1),
            warehouse_id=request.form.get("warehouse_id", ""),
            outbound_types=FG_OUTBOUND_TYPE_LABELS,
            batch_keyword=request.form.get("batch_keyword", ""),
            warehouses=_fg_warehouses(),
            form_data={
                "document_time": request.form.get("document_time", ""),
                "remark": request.form.get("remark", ""),
            },
            errors=[msg],
        ),
        400,
    )


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
    keyword = request.args.get("batch_keyword", "").strip() or None
    tx_type = request.args.get("transaction_type", "").strip() or None
    document_no = request.args.get("document_no", "").strip() or None
    date_from_raw = request.args.get("date_from", "").strip() or None
    date_to_raw = request.args.get("date_to", "").strip() or None

    page = max(int(request.args.get("page", 1) or 1), 1)
    page_size = 50

    rows, total = FinishedGoodsQueryService.list_transactions(
        warehouse_id=warehouse_id, batch_keyword=keyword,
        tx_type=tx_type, document_no=document_no,
        date_from=util_parse_date(date_from_raw),
        date_to=util_parse_date(date_to_raw),
        page=page, page_size=page_size,
    )
    total_pages = max((total + page_size - 1) // page_size, 1)

    return render_template(
        "finished_goods/transactions.html",
        rows=rows, total=total, page=page,
        page_size=page_size, total_pages=total_pages,
        warehouse_id=warehouse_id or "",
        batch_keyword=keyword or "",
        transaction_type=tx_type or "",
        document_no=document_no or "",
        date_from=date_from_raw or "",
        date_to=date_to_raw or "",
        warehouses=_fg_warehouses(),
        tx_types=FG_TX_TYPE_LABELS,
        transaction_directions=FG_TRANSACTION_TYPE_DIRECTION
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

    page = max(int(request.args.get("page", 1) or 1), 1)
    page_size = 50

    rows, total = FinishedGoodsQueryService.query_io_summary(
        warehouse_id=warehouse_id,
        batch_keyword=batch_keyword,
        date_from=util_parse_date(date_from_raw),
        date_to=util_parse_date(date_to_raw),
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

@bp.get("/documents/<direction>")
def document_list_page(direction):
    if direction not in ("in", "out"):
        abort(404)
    direction_code = "IN" if direction == "in" else "OUT"

    keyword = request.args.get("keyword", "").strip() or None
    status = request.args.get("status", "").strip() or None
    inbound_type = request.args.get("inbound_type", "").strip() or None
    outbound_type = request.args.get("outbound_type", "").strip() or None
    wh_raw = request.args.get("warehouse_id", "").strip()
    warehouse_id = int(wh_raw) if wh_raw.isdigit() else None
    date_from_raw = request.args.get("date_from", "").strip() or None
    date_to_raw = request.args.get("date_to", "").strip() or None
    page = max(int(request.args.get("page", 1) or 1), 1)
    page_size = 50
    print(status, inbound_type, outbound_type)

    rows, total = FinishedGoodsQueryService.list_fg_documents(
        direction=direction_code,
        keyword=keyword, status=status,
        inbound_type=inbound_type,
        outbound_type=outbound_type,
        warehouse_id=warehouse_id,
        date_from=util_parse_date(date_from_raw),
        date_to=util_parse_date(date_to_raw),
        page=page, page_size=page_size,
    )
    total_pages = max((total + page_size - 1) // page_size, 1)

    return render_template(
        "finished_goods/document_list.html",
        direction=direction,
        direction_code=direction_code,
        rows=rows, total=total, page=page,
        page_size=page_size, total_pages=total_pages,
        keyword=keyword or "",
        status=status or "",
        inbound_type=inbound_type or "",
        outbound_type=outbound_type or "",
        warehouse_id=warehouse_id or "",
        date_from=date_from_raw or "",
        date_to=date_to_raw or "",
        status_labels=FG_STATUS_LABELS,
        doc_type_labels=FG_DOC_TYPE_LABELS,
        inbound_types=FG_INBOUND_TYPE_LABELS,
        outbound_types=FG_OUTBOUND_TYPE_LABELS,
        warehouses=_fg_warehouses(),
        new_doc_url=(
            url_for("finished_goods.inbound_page")
            if direction == "in"
            else url_for("finished_goods.outbound_page")
        ),
    )

@bp.get("/documents/<direction>/<int:document_id>")
def document_detail_page(direction, document_id):
    if direction not in ("in", "out"):
        abort(404)

    detail = FinishedGoodsQueryService.get_fg_document_detail(document_id)
    if not detail:
        return render_template(
            "finished_goods/document_detail.html",
            direction=direction,
            detail=None,
            error="单据不存在",
            status_labels=FG_STATUS_LABELS,
            doc_type_labels=FG_DOC_TYPE_LABELS,
            tx_types=FG_TX_TYPE_LABELS,
            list_url=url_for("finished_goods.document_list_page",
                             direction=direction),
        ), 404

    # ============ 统一从 ORM doc 取 related_document_id，绕开 dict 结构 ============
    doc_orm = db.session.get(FinishedGoodsDocument, document_id)
    rd_id = doc_orm.related_document_id if doc_orm else None

    related_direction = None
    related_document_no = None
    if rd_id:
        related = db.session.get(FinishedGoodsDocument, rd_id)
        if related:
            related_document_no = related.document_no
            is_in = related.document_type in (
                "PRODUCTION_IN", "STOCKTAKE", "RETURN", "TRANSFER", "REVERSE",
            )
            # to be continued
            related_direction = "in" if is_in else "out"

    # ============ 反查：是否被撤销 ============
    reverse_of_this = (
        FinishedGoodsDocument.query
        .filter_by(related_document_id=document_id, status="CONFIRMED")
        .first()
    )
    detail["is_reversed"] = bool(reverse_of_this)
    detail["reverse_document_id"] = reverse_of_this.id if reverse_of_this else None
    detail["reverse_document_no"] = (
        reverse_of_this.document_no if reverse_of_this else None
    )

    detail["can_confirm"] = (detail["document"]["status"] == "DRAFT")
    detail["can_edit"] = (detail["document"]["status"] == "DRAFT")
    detail["can_reverse"] = (detail["document"]["status"] == "CONFIRMED")

    return render_template(
        "finished_goods/document_detail.html",
        direction=direction,
        detail=detail,
        related_direction=related_direction,
        related_document_no=related_document_no,
        status_labels=FG_STATUS_LABELS,
        doc_type_labels=FG_DOC_TYPE_LABELS,
        tx_types=FG_TX_TYPE_LABELS,
        list_url=url_for("finished_goods.document_list_page",
                         direction=direction),
    )

@bp.post("/documents/<direction>/<int:document_id>/confirm")
def document_confirm(direction, document_id):
    if direction not in ("in", "out"):
        abort(404)
    try:
        if direction == "in":
            FinishedGoodsService.confirm_inbound_document(document_id, confirmed_by=1)
            flash("入库单已确认，库存已增加", "success")
        else:
            FinishedGoodsService.confirm_outbound_document(document_id, confirmed_by=1)
            flash("出库单已确认，库存已扣减", "success")
    except FinishedGoodsError as e:
        flash(str(e), "error")
    return redirect(url_for(
        "finished_goods.document_detail_page",
        direction=direction, document_id=document_id))

@bp.get("/documents/<direction>/<int:document_id>/edit")
def document_edit_page(direction, document_id):
    print(direction, document_id)
    if direction not in ("in", "out"):
        abort(404)
    doc = db.session.get(FinishedGoodsDocument, document_id)
    if not doc:
        abort(404)
    if doc.status != "DRAFT":
        flash("只有草稿状态可编辑", "error")
        return redirect(url_for(
            "finished_goods.document_detail_page",
            direction=direction, document_id=document_id))

    lines = FinishedGoodsDocumentLine.query.filter_by(
        document_id=doc.id
    ).order_by(FinishedGoodsDocumentLine.line_no.asc()).all()

    # 组装 form_data（复用 inbound/outbound 模板的字段名）
    if direction == "in":
        # ⭐ 批量取批次信息
        batch_ids = [l.batch_id for l in lines if l.batch_id]
        batch_map = {}
        if batch_ids:
            for b in FinishedGoodsBatch.query.filter(
                    FinishedGoodsBatch.id.in_(batch_ids)).all():
                batch_map[b.id] = b

        # 在批量取批次后，再批量取工艺单号
        po_ids = list({b.process_order_id for b in batch_map.values() if b.process_order_id})
        po_map = {}
        if po_ids:
            for po in ProcessOrder.query.filter(ProcessOrder.id.in_(po_ids)).all():
                po_map[po.id] = po

        form_lines = []
        for l in lines:
            b = batch_map.get(l.batch_id)
            form_lines.append({
                "batch_id": l.batch_id,
                "batch_no": b.batch_no if b else "",
                "product_name": b.product_name if b else "",
                "product_specification": b.product_specification if b else "",
                "unit": b.unit if b else "KG",
                "quality_status": b.quality_status if b else "QUALIFIED",
                "process_order_id": b.process_order_id if b else None,
                "process_order_id": b.process_order_id if b else None,
                "process_order_no": po_map[b.process_order_id].process_order_no
                                    if (b and b.process_order_id in po_map) else "",
                "quantity": str(l.quantity),
                "unit_cost": str(l.unit_cost) if l.unit_cost is not None else "",
                "remark": l.remark or "",
            })
        if not form_lines:
            form_lines = [{}]

        return render_template(
            "finished_goods/inbound.html",
            edit_mode=True,
            document_id=doc.id,
            warehouses=_fg_warehouses(),
            inbound_types=FG_INBOUND_TYPE_LABELS,
            process_orders=(
                ProcessOrder.query
                .filter(ProcessOrder.status.in_(["ORDERED", "COMPLETED"]))
                .order_by(ProcessOrder.id.desc()).limit(500).all()
            ),
            form_data={
                "warehouse_id": lines[0].to_warehouse_id if lines else "",
                "inbound_type": "PRODUCTION_IN",
                "document_time": doc.document_time.strftime("%Y-%m-%dT%H:%M") if doc.document_time else "",
                "remark": doc.remark or "",
                "lines": form_lines,
            },
            errors=[],
        )
    else:
        # 批量取仓库
        wh_ids = list({l.from_warehouse_id for l in lines if l.from_warehouse_id})
        wh_map = {}
        if wh_ids:
            for w in Warehouse.query.filter(Warehouse.id.in_(wh_ids)).all():
                wh_map[w.id] = w

        # 批量取批次
        batch_ids = [l.batch_id for l in lines if l.batch_id]
        batch_map = {}
        if batch_ids:
            for b in FinishedGoodsBatch.query.filter(
                    FinishedGoodsBatch.id.in_(batch_ids)).all():
                batch_map[b.id] = b

        rows = []
        for l in lines:
            w = wh_map.get(l.from_warehouse_id)
            b = batch_map.get(l.batch_id)
            total_qty, locked_qty, available_qty = (
                FinishedGoodsQueryService.get_batch_available_qty(
                    l.from_warehouse_id, l.batch_id
                )
            )

            rows.append({
                "id": l.id,
                "warehouse_id": l.from_warehouse_id,
                "warehouse_code": w.warehouse_code if w else "",
                "warehouse_name": w.warehouse_name if w else "",
                "batch_id": l.batch_id,
                "batch_no": b.batch_no if b else "",
                "product_name": b.product_name if b else "",
                "product_specification": b.product_specification if b else "",
                "unit": b.unit if b else "",
                "quality_status": b.quality_status if b else "",
                "total_qty": total_qty,
                "locked_qty": locked_qty,
                "available_qty": available_qty,
                "edit_quantity": str(l.quantity),
                "edit_remark": l.remark or "",
            })
        if not rows:
            rows = []

        return render_template(
            "finished_goods/outbound.html",
            edit_mode=True,
            document_id=doc.id,
            rows=rows,
            total=len(lines), page=1, page_size=200, total_pages=1,
            warehouse_id=lines[0].from_warehouse_id if lines else "",
            outbound_types=FG_OUTBOUND_TYPE_LABELS,
            batch_keyword="",
            warehouses=_fg_warehouses(),
            form_data={
                "outbound_type": "OUTBOUND",
                "document_time": doc.document_time.strftime("%Y-%m-%dT%H:%M") if doc.document_time else "",
                "remark": doc.remark or "",
            },
            errors=[],
        )

@bp.post("/documents/<direction>/<int:document_id>/edit")
def document_edit_submit(direction, document_id):
    if direction not in ("in", "out"):
        abort(404)
    doc = db.session.get(FinishedGoodsDocument, document_id)
    if not doc or doc.status != "DRAFT":
        flash("只有草稿状态可编辑", "error")
        return redirect(url_for(
            "finished_goods.document_list_page", direction=direction))

    inbound_type = request.args.get("inbound_type", "").strip() or None
    outbound_type = request.args.get("outbound_type", "").strip() or None
    if direction == "in":
        wh_raw = request.form.get("warehouse_id", "").strip()
        warehouse_id = int(wh_raw) if wh_raw.isdigit() else None
        lines = []
        idx = 0
        while True:
            prefix = f"lines[{idx}]"
            if prefix + "[quantity]" not in request.form:
                break
            qty = _parse_qty(request.form.get(prefix + "[quantity]"))
            if qty is None or qty <= 0:
                idx += 1
                continue
            batch_id_raw = request.form.get(prefix + "[batch_id]", "").strip()
            po_raw = (request.form.get(prefix + "[process_order_id]", "") or "").strip()
            process_order_id = int(po_raw) if po_raw.isdigit() else None
            lines.append({
                "batch_id": int(batch_id_raw) if batch_id_raw else None,
                "batch_no": request.form.get(prefix + "[batch_no]", "").strip(),
                "product_name": request.form.get(prefix + "[product_name]", "").strip(),
                "product_specification": request.form.get(prefix + "[product_specification]", "").strip() or None,
                "unit": request.form.get(prefix + "[unit]", "KG").strip() or "KG",
                "quality_status": request.form.get(prefix + "[quality_status]", "QUALIFIED"),
                "process_order_id": process_order_id,
                "quantity": request.form.get(prefix + "[quantity]").strip(),
                "unit_cost": request.form.get(prefix + "[unit_cost]", "").strip() or None,
                "remark": request.form.get(prefix + "[remark]", "").strip() or None,
            })
            idx += 1

        try:
            doc_time = datetime.fromisoformat(
                request.form.get("document_time", "").strip() or ""
            ) if request.form.get("document_time", "").strip() else None
            FinishedGoodsService.update_inbound_draft(
                document_id=document_id,
                warehouse_id=warehouse_id,
                lines=lines,
                document_time=doc_time,
                business_type=inbound_type,
                remark=request.form.get("remark", "").strip() or None,
            )
            flash("入库草稿已更新", "success")
            return redirect(url_for(
                "finished_goods.document_detail_page",
                direction="in", document_id=document_id))
        except FinishedGoodsError as e:
            flash(str(e), "error")
            return redirect(url_for(
                "finished_goods.document_edit_page",
                direction="in", document_id=document_id))
    else:
        lines = []
        idx = 0
        while True:
            prefix = f"lines[{idx}]"
            if prefix + "[batch_id]" not in request.form:
                break
            batch_raw = request.form.get(prefix + "[batch_id]", "").strip()
            wh_raw = request.form.get(prefix + "[warehouse_id]", "").strip()
            qty_raw = request.form.get(prefix + "[quantity]", "").strip()
            if batch_raw.isdigit() and wh_raw.isdigit() and qty_raw:
                try:
                    if float(qty_raw) > 0:
                        lines.append({
                            "warehouse_id": int(wh_raw),
                            "batch_id": int(batch_raw),
                            "quantity": qty_raw,
                            "remark": request.form.get(prefix + "[remark]", "").strip() or None,
                        })
                except ValueError:
                    pass
            idx += 1

        try:
            doc_time = datetime.fromisoformat(
                request.form.get("document_time", "").strip() or ""
            ) if request.form.get("document_time", "").strip() else None
            FinishedGoodsService.update_outbound_draft(
                document_id=document_id,
                lines=lines,
                document_time=doc_time,
                business_type=outbound_type,
                remark=request.form.get("remark", "").strip() or None,
            )
            flash("出库草稿已更新", "success")
            return redirect(url_for(
                "finished_goods.document_detail_page",
                direction="out", document_id=document_id))
        except FinishedGoodsError as e:
            flash(str(e), "error")
            return redirect(url_for(
                "finished_goods.document_edit_page",
                direction="out", document_id=document_id))

@bp.post("/documents/<direction>/<int:document_id>/reverse")
def document_reverse(direction, document_id):
    if direction not in ("in", "out"):
        abort(404)
    try:
        result = FinishedGoodsService.reverse_document(
            document_id=document_id,
            reason=request.form.get("reason", "").strip() or None,
            created_by=1,
        )
        flash(
            f"已撤销，生成新单号 {result['reverse_document_no']}，"
            f"关联原单 {result['original_document_no']}",
            "success",
        )
    except FinishedGoodsError as e:
        flash(str(e), "error")
    return redirect(url_for(
        "finished_goods.document_detail_page",
        direction=direction, document_id=document_id))