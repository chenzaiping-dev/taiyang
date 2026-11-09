from datetime import datetime
from flask import (
    Blueprint, request, jsonify, render_template, redirect, url_for, flash
)
from marshmallow import ValidationError

from app.extensions import db
from app.generated.models.materials import Material
from app.generated.models.warehouses import Warehouse

from app.generated.models.inventory_documents import InventoryDocument
from app.generated.models.inventory_transactions import InventoryTransaction

from app.schemas.stock_out_schema import StockOutRequestSchema
from app.services.form_parser import parse_stock_out_form, FormParseError
from app.services.inventory_service import (
    StockOutService, InventoryError, OUTBOUND_TYPE_LABELS, _rm_warehouses, tx_type_map
)

bp = Blueprint("stock_out", __name__, url_prefix="/inventory")
schema = StockOutRequestSchema()

# ---------- JSON API ----------
@bp.post("/api/stock-out")
def api_stock_out():
    payload = request.get_json(silent=True) or {}
    try:
        data = schema.load(payload)
    except ValidationError as e:
        return jsonify({"code": 422, "msg": f"参数校验失败: {e.messages}"}), 422

    try:
        result = StockOutService.create_stock_out(
            warehouse_id=data["warehouse_id"],
            lines=[
                {"line_no": l.get("line_no"), "material_id": l["material_id"],
                 "quantity": l["quantity"]}
                for l in data["lines"]
            ],
            outbound_type=data["outbound_type"],
            document_no=data.get("document_no"),
            document_time=data.get("document_time"),
            process_order_id=data.get("process_order_id"),
            remark=data.get("remark"),
            auto_confirm=data.get("auto_confirm", True),
            allow_partial=data.get("allow_partial", False),
        )
    except InventoryError as e:
        return jsonify({"code": 400, "msg": str(e)}), 400
    return jsonify({"code": 0, "msg": "出库成功", "data": result})


@bp.post("/api/stock-out/<int:document_id>/confirm")
def api_confirm(document_id):
    try:
        result = StockOutService.confirm_document(document_id)
    except InventoryError as e:
        return jsonify({"code": 400, "msg": str(e)}), 400
    return jsonify({"code": 0, "msg": "确认成功", "data": result})


# ---------- HTML Form ----------
@bp.get("/stock-out")
def stock_out_page():
    materials = Material.query.filter_by(status=1).order_by(Material.material_code).all()
    return render_template(
        "inventory/stock_out_form.html",
        warehouses=_rm_warehouses(),
        materials=materials,
        outbound_types=OUTBOUND_TYPE_LABELS,
        form_data={
            "warehouse_id": "",
            "outbound_type": "PRODUCTION_OUT",
            "process_order_id": "",
            "document_time": datetime.now().strftime("%Y-%m-%dT%H:%M"),
            "remark": "",
            "auto_confirm": False,
            "allow_partial": False,
            "lines": [{"material_id": "", "quantity": ""}],
        },
        errors=[],
    )


@bp.post("/stock-out")
def stock_out_submit():
    try:
        data = parse_stock_out_form(request.form)
        result = StockOutService.create_stock_out(
            warehouse_id=data["warehouse_id"],
            lines=data["lines"],
            outbound_type=data.get("outbound_type", "PRODUCTION_OUT"),   # ⭐
            process_order_id=data.get("process_order_id"),
            document_time=(
                datetime.fromisoformat(data["document_time"])
                if data["document_time"] else None
            ),
            remark=data["remark"],
            auto_confirm=data["auto_confirm"],
            allow_partial=data["allow_partial"],
        )
    except (FormParseError, InventoryError) as e:
        return _render_stock_out_error(str(e))

    return render_template("inventory/stock_out_success.html", result=result)


def _render_stock_out_error(error_msg: str, edit_mode=False, document_id=None):
    materials = (
        Material.query.filter_by(status=1)
        .order_by(Material.material_code).all()
    )

    lines = []
    idx = 0
    while True:
        key = f"lines[{idx}][material_id]"
        if key not in request.form:
            break
        lines.append({
            "material_id": request.form.get(key, ""),
            "quantity": request.form.get(f"lines[{idx}][quantity]", ""),
        })
        idx += 1
    if not lines:
        lines = [{"material_id": "", "quantity": ""}]

    form_data = {
        "warehouse_id": request.form.get("warehouse_id", ""),
        "process_order_id": request.form.get("process_order_id", ""),
        "document_time": request.form.get("document_time", ""),
        "remark": request.form.get("remark", ""),
        "auto_confirm": request.form.get("auto_confirm") == "on",
        "allow_partial": request.form.get("allow_partial") == "on",
        "outbound_type": request.form.get("outbound_type", "PRODUCTION_OUT"),
        "lines": lines,
    }

    return (
        render_template(
            "inventory/stock_out_form.html",
            edit_mode=edit_mode,
            document_id=document_id,
            warehouses=_rm_warehouses(),
            materials=materials,
            outbound_types=OUTBOUND_TYPE_LABELS,
            form_data=form_data,
            errors=[error_msg],
        ),
        400,
    )

@bp.get("/stock-out/<int:document_id>/edit")
def stock_out_edit_page(document_id):
    doc = db.session.get(InventoryDocument, document_id)
    if not doc or doc.status != "DRAFT":
        flash("只有草稿可编辑", "error")
        return redirect(url_for(
            "inventory_query.document_detail_page",
            direction="out", document_id=document_id,
        ))

    txs = (
        InventoryTransaction.query
        .filter_by(document_id=doc.id)
        .order_by(InventoryTransaction.line_no.asc())
        .all()
    )
    if not txs:
        flash("单据无明细，无法编辑", "error")
        return redirect(url_for(
            "inventory_query.document_detail_page",
            direction="out", document_id=document_id,
        ))

    # 反推 outbound_type：document_type 是多对一映射，用 tx.transaction_type 更精确
    tx_to_outbound = {
        v: k for k, v in tx_type_map.items() if k in OUTBOUND_TYPE_LABELS
    }
    outbound_type = tx_to_outbound.get(
        txs[0].transaction_type, "PRODUCTION_OUT"
    )

    materials = (
        Material.query.filter_by(status=1)
        .order_by(Material.material_code).all()
    )

    form_lines = [
        {
            "material_id": t.material_id,
            "quantity": str(-t.quantity_delta),   # 出库流水是负数，回显取正
        }
        for t in txs
    ]

    return render_template(
        "inventory/stock_out_form.html",
        edit_mode=True,
        document_id=doc.id,
        warehouses=_rm_warehouses(),
        materials=materials,
        outbound_types=OUTBOUND_TYPE_LABELS,
        form_data={
            "warehouse_id": txs[0].warehouse_id,
            "outbound_type": outbound_type,
            "process_order_id": txs[0].process_order_id or "",
            "document_time": (
                doc.document_time.strftime("%Y-%m-%dT%H:%M")
                if doc.document_time else ""
            ),
            "remark": doc.remark or "",
            "auto_confirm": False,
            "allow_partial": False,
            "lines": form_lines,
        },
        errors=[],
    )


@bp.post("/stock-out/<int:document_id>/edit")
def stock_out_edit_submit(document_id):
    try:
        data = parse_stock_out_form(request.form)
        StockOutService.update_draft(
            document_id=document_id,
            warehouse_id=data["warehouse_id"],
            lines=data["lines"],
            outbound_type=data.get("outbound_type", "PRODUCTION_OUT"),
            process_order_id=data.get("process_order_id"),
            document_time=(
                datetime.fromisoformat(data["document_time"])
                if data["document_time"] else None
            ),
            remark=data["remark"],
        )
        flash("出库草稿已更新", "success")
        return redirect(url_for(
            "inventory_query.document_detail_page",
            direction="out", document_id=document_id,
        ))
    except (FormParseError, InventoryError) as e:
        return _render_stock_out_error(
            str(e), edit_mode=True, document_id=document_id
        )