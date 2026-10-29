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
from app.services.inventory_service import PurchaseInService, InventoryError
from app.services.form_parser import parse_purchase_in_form, FormParseError

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
    print("purchase_in_page")
    warehouses = Warehouse.query.filter_by(status=1).order_by(Warehouse.id).all()
    suppliers = Supplier.query.filter_by(status=1).order_by(Supplier.id).all()
    materials = Material.query.filter_by(status=1).order_by(Material.material_code).all()

    # 默认渲染 1 行空明细
    default_lines = [{"material_id": "", "quantity": "", "unit_cost": ""}]
    return render_template(
        "inventory/purchase_in_form.html",
        warehouses=warehouses,
        suppliers=suppliers,
        materials=materials,
        form_data={
            "warehouse_id": "",
            "supplier_id": "",
            "document_time": datetime.now().strftime("%Y-%m-%dT%H:%M"),
            "remark": "",
            "auto_confirm": True,
            "lines": default_lines,
        },
        errors=[],
    )


# ========== 模板页面：POST 提交 ==========
@bp.post("/purchase-in")
def purchase_in_submit():
    try:
        print("purchase_in_submit")
        data = parse_purchase_in_form(request.form)
        print("data:", data)
        result = PurchaseInService.create_purchase_in(
            warehouse_id=data["warehouse_id"],
            supplier_id=data["supplier_id"],
            lines=data["lines"],
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


def _render_form_with_error(error_msg: str):
    warehouses = Warehouse.query.filter_by(status=1).order_by(Warehouse.id).all()
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
        "lines": lines,
    }

    return (
        render_template(
            "inventory/purchase_in_form.html",
            warehouses=warehouses,
            suppliers=suppliers,
            materials=materials,
            form_data=form_data,
            errors=[error_msg],
        ),
        400,
    )