from datetime import datetime
from flask import (
    Blueprint, request, jsonify, render_template, redirect, url_for, flash
)
from marshmallow import ValidationError

from app.extensions import db
from app.generated.models.materials import Material
from app.generated.models.warehouses import Warehouse
from app.generated.models.suppliers import Supplier

from app.generated.models.inventory_documents import InventoryDocument
from app.generated.models.inventory_transactions import InventoryTransaction
from app.generated.models.inventory_cost_layers import InventoryCostLayer

from app.schemas.purchase_in_schema import PurchaseInRequestSchema
from app.services.form_parser import parse_purchase_in_form, FormParseError
from app.services.inventory_service import (
    PurchaseInService, InventoryError, INBOUND_TYPE_LABELS, _rm_warehouses, tx_type_map
)

bp = Blueprint("purchase_in", __name__, url_prefix="/inventory")
schema = PurchaseInRequestSchema()


# ========== 已有 JSON API（保留） ==========
@bp.post("/api/purchase-in")
def api_purchase_in():
    payload = request.get_json(silent=True) or {}
    try:
        data = schema.load(payload)
    except ValidationError as e:
        return jsonify({"code": 422, "msg": f"参数校验失败: {e.messages}"}), 422

    try:
        result = PurchaseInService.create_purchase_in(
            warehouse_id=data["warehouse_id"],
            supplier_id=data["supplier_id"],
            lines=[
                {"line_no": l.get("line_no"), "material_id": l["material_id"],
                 "quantity": l["quantity"], "unit_cost": l["unit_cost"]}
                for l in data["lines"]
            ],
            document_no=data.get("document_no"),
            document_time=data.get("document_time"),
            remark=data.get("remark"),
            auto_confirm=data.get("auto_confirm", True),
        )
    except InventoryError as e:
        return jsonify({"code": 400, "msg": str(e)}), 400
    return jsonify({"code": 0, "msg": "采购入库成功", "data": result})


# ========== 模板页面：GET 渲染表单 ==========
@bp.get("/purchase-in")
def purchase_in_page():
    suppliers = Supplier.query.filter_by(status=1).order_by(Supplier.id).all()
    materials = Material.query.filter_by(status=1).order_by(Material.material_code).all()

    # 默认渲染 1 行空明细
    default_lines = [{"material_id": "", "quantity": "", "unit_cost": ""}]
    return render_template(
        "inventory/purchase_in_form.html",
        warehouses=_rm_warehouses(),
        suppliers=suppliers,
        materials=materials,
        inbound_types=INBOUND_TYPE_LABELS,   # ⭐
        form_data={
            "warehouse_id": "",
            "supplier_id": "",
            "inbound_type": "PURCHASE_IN",       # ⭐
            "document_time": datetime.now().strftime("%Y-%m-%dT%H:%M"),
            "remark": "",
            "auto_confirm": False,
            "lines": default_lines,
        },
        errors=[],
    )


# ========== 模板页面：POST 提交 ==========
@bp.post("/purchase-in")
def purchase_in_submit():
    try:
        data = parse_purchase_in_form(request.form)
        print("data:", data)
        result = PurchaseInService.create_purchase_in(
            warehouse_id=data["warehouse_id"],
            supplier_id=data["supplier_id"],
            lines=data["lines"],
            inbound_type=data.get("inbound_type", "PURCHASE_IN"),   # ⭐
            document_time=(
                datetime.fromisoformat(data["document_time"])
                if data["document_time"] else None
            ),
            remark=data["remark"],
            auto_confirm=data["auto_confirm"],
        )
        print("result:", result)
    except (FormParseError, InventoryError) as e:
        # 失败时重新渲染表单，回填输入 + 错误信息
        print("purchase_in_submit error:", str(e))
        return _render_form_with_error(str(e))

    return render_template(
        "inventory/purchase_in_success.html",
        result=result,
    )


def _render_form_with_error(error_msg: str, edit_mode=False, document_id=None):
    suppliers = Supplier.query.filter_by(status=1).order_by(Supplier.id).all()
    materials = Material.query.filter_by(status=1).order_by(Material.material_code).all()

    # 从 form 里回填动态行
    lines = []
    idx = 0
    while True:
        key = f"lines[{idx}][material_id]"
        if key not in request.form:
            break
        lines.append(
            {
                "material_id": request.form.get(key, ""),
                "quantity": request.form.get(f"lines[{idx}][quantity]", ""),
                "unit_cost": request.form.get(f"lines[{idx}][unit_cost]", ""),
            }
        )
        idx += 1
    if not lines:
        lines = [{"material_id": "", "quantity": "", "unit_cost": ""}]

    form_data = {
        "warehouse_id": request.form.get("warehouse_id", ""),
        "supplier_id": request.form.get("supplier_id", ""),
        "document_time": request.form.get("document_time", ""),
        "remark": request.form.get("remark", ""),
        "auto_confirm": request.form.get("auto_confirm") == "on",
        "inbound_type": request.form.get("inbound_type", "PURCHASE_IN"),
        "lines": lines,
    }

    return (
        render_template(
            "inventory/purchase_in_form.html",
            edit_mode=edit_mode,
            document_id=document_id,
            warehouses=_rm_warehouses(),
            suppliers=suppliers,
            materials=materials,
            inbound_types=INBOUND_TYPE_LABELS,   # ⭐
            form_data=form_data,
            errors=[error_msg],
        ),
        400,
    )

@bp.get("/purchase-in/<int:document_id>/edit")
def purchase_in_edit_page(document_id):
    doc = db.session.get(InventoryDocument, document_id)
    if not doc or doc.status != "DRAFT":
        flash("只有草稿可编辑", "error")
        return redirect(url_for("inventory_query.document_detail_page",
                                direction="in", document_id=document_id))

    txs = (InventoryTransaction.query
           .filter_by(document_id=doc.id)
           .order_by(InventoryTransaction.line_no.asc()).all())
    if not txs:
        flash("单据无明细", "error")
        return redirect(url_for("inventory_query.document_detail_page",
                                direction="in", document_id=document_id))

    # 反推 inbound_type
    tx_to_inbound = {v: k for k, v in tx_type_map.items()
                     if k in INBOUND_TYPE_LABELS}
    inbound_type = tx_to_inbound.get(txs[0].transaction_type, "PURCHASE_IN")

    suppliers = Supplier.query.filter_by(status=1).order_by(Supplier.id).all()
    materials = Material.query.filter_by(status=1).order_by(Material.material_code).all()

    form_lines = [{
        "material_id": t.material_id,
        "quantity": str(t.quantity_delta),
        "unit_cost": str(t.unit_cost) if t.unit_cost is not None else "",
    } for t in txs]

    return render_template(
        "inventory/purchase_in_form.html",
        edit_mode=True,
        document_id=doc.id,
        warehouses=_rm_warehouses(),
        suppliers=suppliers,
        materials=materials,
        inbound_types=INBOUND_TYPE_LABELS,
        form_data={
            "warehouse_id": txs[0].warehouse_id,
            "supplier_id": txs[0].supplier_id or "",
            "inbound_type": inbound_type,
            "document_time": doc.document_time.strftime("%Y-%m-%dT%H:%M")
                             if doc.document_time else "",
            "remark": doc.remark or "",
            "auto_confirm": False,
            "lines": form_lines,
        },
        errors=[],
    )


@bp.post("/purchase-in/<int:document_id>/edit")
def purchase_in_edit_submit(document_id):
    try:
        data = parse_purchase_in_form(request.form)
        PurchaseInService.update_draft(
            document_id=document_id,
            warehouse_id=data["warehouse_id"],
            supplier_id=data["supplier_id"],
            lines=data["lines"],
            inbound_type=data.get("inbound_type", "PURCHASE_IN"),
            document_time=(datetime.fromisoformat(data["document_time"])
                           if data["document_time"] else None),
            remark=data["remark"],
        )
        flash("入库草稿已更新", "success")
        return redirect(url_for("inventory_query.document_detail_page",
                                direction="in", document_id=document_id))
    except (FormParseError, InventoryError) as e:
        return _render_form_with_error(str(e))