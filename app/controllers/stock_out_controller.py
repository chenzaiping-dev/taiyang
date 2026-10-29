from datetime import datetime
from flask import Blueprint, request, jsonify, render_template
from marshmallow import ValidationError

from app.extensions import db
from app.generated.models.materials import Material
from app.generated.models.warehouses import Warehouse

from app.schemas.stock_out_schema import StockOutRequestSchema
from app.services.inventory_service import StockOutService, InventoryError
from app.services.form_parser import parse_stock_out_form, FormParseError

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
            out_type=data["out_type"],
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
    warehouses = Warehouse.query.filter_by(status=1).order_by(Warehouse.id).all()
    materials = Material.query.filter_by(status=1).order_by(Material.material_code).all()
    return render_template(
        "inventory/stock_out_form.html",
        warehouses=warehouses,
        materials=materials,
        form_data={
            "warehouse_id": "",
            "out_type": "PRODUCTION_OUT",
            "process_order_id": "",
            "document_time": datetime.now().strftime("%Y-%m-%dT%H:%M"),
            "remark": "",
            "auto_confirm": True,
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
            out_type=data["out_type"],
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


def _render_stock_out_error(error_msg: str):
    warehouses = Warehouse.query.filter_by(status=1).order_by(Warehouse.id).all()
    materials = Material.query.filter_by(status=1).order_by(Material.material_code).all()

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
        "out_type": request.form.get("out_type", "PRODUCTION_OUT"),
        "process_order_id": request.form.get("process_order_id", ""),
        "document_time": request.form.get("document_time", ""),
        "remark": request.form.get("remark", ""),
        "auto_confirm": request.form.get("auto_confirm") == "on",
        "allow_partial": request.form.get("allow_partial") == "on",
        "lines": lines,
    }

    return (
        render_template(
            "inventory/stock_out_form.html",
            warehouses=warehouses,
            materials=materials,
            form_data=form_data,
            errors=[error_msg],
        ),
        400,
    )