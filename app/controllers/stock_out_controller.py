from datetime import datetime
from decimal import Decimal

from flask import (
    Blueprint, request, jsonify, render_template, redirect, url_for, flash, session
)
from marshmallow import ValidationError

from app.extensions import db
from app.generated.models.materials import Material
from app.generated.models.warehouses import Warehouse
from app.generated.models.inventory_documents import InventoryDocument
from app.generated.models.inventory_document_lines import InventoryDocumentLine
from app.schemas.stock_out_schema import StockOutRequestSchema
from app.services.form_parser import parse_stock_out_form, FormParseError
from app.services.inventory_service import (
    StockOutService, InventoryError, OUTBOUND_TYPE_LABELS, _rm_warehouses, tx_type_map
)
from app.services.inventory_query_service import InventoryQueryService
from app.services.permission_service import can_view_inventory_cost
from app.core.money_redaction import redact_money_fields

bp = Blueprint("stock_out", __name__, url_prefix="/inventory")
schema = StockOutRequestSchema()


@bp.post("/api/stock-out")
def api_stock_out():
    payload = request.get_json(silent=True) or {}
    try:
        data = schema.load(payload)
    except ValidationError as e:
        return jsonify({"code": 422, "msg": f"参数校验失败: {e.messages}"}), 422

    try:
        result = StockOutService.create_stock_out(
            warehouse_id=data.get("warehouse_id"),
            lines=[
                {"line_no": line.get("line_no"),
                 "warehouse_id": line.get("warehouse_id") or data.get("warehouse_id"),
                 "material_id": line["material_id"], "quantity": line["quantity"]}
                for line in data["lines"]
            ],
            outbound_type=data["outbound_type"],
            document_no=data.get("document_no"),
            document_time=data.get("document_time"),
            process_order_id=data.get("process_order_id"),
            remark=data.get("remark"),
            created_by=session.get("employee_id"),
            auto_confirm=data.get("auto_confirm", True),
            # 原料出库不允许部分出库
            allow_partial=False,
        )
    except InventoryError as e:
        return jsonify({"code": 400, "msg": str(e)}), 400
    if not can_view_inventory_cost():
        result = redact_money_fields(result)
    return jsonify({"code": 0, "msg": "出库成功", "data": result})


@bp.post("/api/stock-out/<int:document_id>/confirm")
def api_confirm(document_id):
    try:
        result = StockOutService.confirm_document(document_id, allow_partial=False)
    except InventoryError as e:
        return jsonify({"code": 400, "msg": str(e)}), 400
    if not can_view_inventory_cost():
        result = redact_money_fields(result)
    return jsonify({"code": 0, "msg": "确认成功", "data": result})


def _inventory_rows(posted_lines=None, existing_lines=None):
    """获取实时库存行，并合并草稿/表单中已选择但当前库存为零的行。"""
    rows_by_key = {}
    for raw in InventoryQueryService.query_stock_out_available_inventory():
        key = (int(raw["warehouse_id"]), int(raw["material_id"]))
        rows_by_key[key] = {
            **raw,
            "warehouse_id": key[0], "material_id": key[1],
            "available_qty": Decimal(str(raw.get("available_qty") or 0)),
            "quantity": "",
        }

    source_lines = list(existing_lines or []) + list(posted_lines or [])
    for line in source_lines:
        try:
            key = (int(line["warehouse_id"]), int(line["material_id"]))
        except (KeyError, TypeError, ValueError):
            continue
        if key not in rows_by_key:
            material = db.session.get(Material, key[1])
            warehouse = db.session.get(Warehouse, key[0])
            if not material or not warehouse:
                continue
            rows_by_key[key] = {
                "warehouse_id": key[0], "warehouse_code": warehouse.warehouse_code,
                "warehouse_name": warehouse.warehouse_name,
                "material_id": key[1], "material_code": material.material_code,
                "material_name": material.material_name, "unit": material.unit,
                "available_qty": Decimal("0"), "quantity": "",
            }

    # 先用草稿数量，再以本次 POST 数量覆盖
    for line in source_lines:
        try:
            key = (int(line["warehouse_id"]), int(line["material_id"]))
            if line.get("quantity") is not None:
                rows_by_key[key]["quantity"] = str(line.get("quantity") or "")
        except (KeyError, TypeError, ValueError):
            continue

    return sorted(
        rows_by_key.values(),
        key=lambda row: (row["warehouse_code"], row["material_code"]),
    )


def _render_stock_out_form(*, edit_mode=False, document_id=None, form_data=None,
                           errors=None, existing_lines=None):
    form_data = form_data or {}
    rows = _inventory_rows(
        posted_lines=form_data.get("lines"), existing_lines=existing_lines
    )
    warehouses = _rm_warehouses()
    return render_template(
        "inventory/stock_out_form.html",
        edit_mode=edit_mode,
        document_id=document_id,
        warehouses=warehouses,
        inventory_rows=rows,
        outbound_types=OUTBOUND_TYPE_LABELS,
        form_data={
            "outbound_type": form_data.get("outbound_type", "PRODUCTION_OUT"),
            "process_order_id": form_data.get("process_order_id", ""),
            "document_time": form_data.get("document_time") or datetime.now().strftime("%Y-%m-%dT%H:%M"),
            "remark": form_data.get("remark", ""),
            "auto_confirm": bool(form_data.get("auto_confirm", False)),
        },
        errors=errors or [],
    )


@bp.get("/stock-out")
def stock_out_page():
    return _render_stock_out_form()


@bp.post("/stock-out")
def stock_out_submit():
    try:
        data = parse_stock_out_form(request.form)
        result = StockOutService.create_stock_out(
            lines=data["lines"],
            outbound_type=data.get("outbound_type", "PRODUCTION_OUT"),
            process_order_id=data.get("process_order_id"),
            document_time=(datetime.fromisoformat(data["document_time"]) if data["document_time"] else None),
            remark=data["remark"],
            created_by=session.get("employee_id"),
            auto_confirm=data["auto_confirm"],
            allow_partial=False,
        )
    except (FormParseError, InventoryError) as e:
        return _render_stock_out_form(
            form_data={
                "lines": _posted_lines(),
                "outbound_type": request.form.get("outbound_type", "PRODUCTION_OUT"),
                "process_order_id": request.form.get("process_order_id", ""),
                "document_time": request.form.get("document_time", ""),
                "remark": request.form.get("remark", ""),
                "auto_confirm": request.form.get("auto_confirm") == "on",
            }, errors=[str(e)],
        ), 400

    return render_template(
        "inventory/stock_out_success.html", result=result,
        can_view_cost=can_view_inventory_cost(),
    )


def _posted_lines():
    indexes = set()
    for key in request.form.keys():
        if key.startswith("lines[") and "][material_id]" in key:
            raw = key.split("[")[1].split("]")[0]
            if raw.isdigit():
                indexes.add(int(raw))
    lines = []
    for idx in sorted(indexes):
        lines.append({
            "warehouse_id": request.form.get(f"lines[{idx}][warehouse_id]"),
            "material_id": request.form.get(f"lines[{idx}][material_id]"),
            "quantity": request.form.get(f"lines[{idx}][quantity]", ""),
        })
    return lines


@bp.get("/stock-out/<int:document_id>/edit")
def stock_out_edit_page(document_id):
    doc = db.session.get(InventoryDocument, document_id)
    if not doc or doc.status != "DRAFT":
        flash("只有草稿可编辑", "error")
        return redirect(url_for("inventory_query.document_detail_page", direction="out", document_id=document_id))

    doc_lines = (
        InventoryDocumentLine.query.filter_by(document_id=doc.id)
        .order_by(InventoryDocumentLine.line_no.asc()).all()
    )
    if not doc_lines:
        flash("单据无明细，无法编辑", "error")
        return redirect(url_for("inventory_query.document_detail_page", direction="out", document_id=document_id))

    tx_to_outbound = {v: k for k, v in tx_type_map.items() if k in OUTBOUND_TYPE_LABELS}
    outbound_type = tx_to_outbound.get(doc_lines[0].transaction_type, "PRODUCTION_OUT")
    existing_lines = [
        {"warehouse_id": line.warehouse_id, "material_id": line.material_id,
         "quantity": str(line.quantity)}
        for line in doc_lines
    ]
    return _render_stock_out_form(
        edit_mode=True, document_id=doc.id,
        form_data={
            "lines": existing_lines,
            "outbound_type": outbound_type,
            "process_order_id": doc_lines[0].process_order_id or "",
            "document_time": doc.document_time.strftime("%Y-%m-%dT%H:%M") if doc.document_time else "",
            "remark": doc.remark or "",
        },
    )


@bp.post("/stock-out/<int:document_id>/edit")
def stock_out_edit_submit(document_id):
    try:
        data = parse_stock_out_form(request.form)
        StockOutService.update_draft(
            document_id=document_id,
            lines=data["lines"],
            outbound_type=data.get("outbound_type", "PRODUCTION_OUT"),
            process_order_id=data.get("process_order_id"),
            document_time=(datetime.fromisoformat(data["document_time"]) if data["document_time"] else None),
            remark=data["remark"],
        )
        flash("出库草稿已更新", "success")
        return redirect(url_for("inventory_query.document_detail_page", direction="out", document_id=document_id))
    except (FormParseError, InventoryError) as e:
        return _render_stock_out_form(
            edit_mode=True, document_id=document_id,
            form_data={
                "lines": _posted_lines(),
                "outbound_type": request.form.get("outbound_type", "PRODUCTION_OUT"),
                "process_order_id": request.form.get("process_order_id", ""),
                "document_time": request.form.get("document_time", ""),
                "remark": request.form.get("remark", ""),
            }, errors=[str(e)],
        ), 400
