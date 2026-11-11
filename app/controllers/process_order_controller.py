from datetime import datetime
from flask import Blueprint, request, jsonify, render_template, abort, flash, redirect, url_for
from marshmallow import ValidationError
import re

from app.extensions import db
from app.generated.models.materials import Material
from app.generated.models.warehouses import Warehouse
from app.generated.models.factories import Factory
from app.generated.models.sales_performance import SalesPerformance
from app.generated.models.employees import Employee
from app.generated.models.process_orders import ProcessOrder
from app.generated.models.process_order_materials import ProcessOrderMaterial

from app.schemas.process_order_schema import ProcessOrderRequestSchema
from app.services.process_order_service import (
    ProcessOrderService, ProcessOrderError, PO_STATUS_LABELS, _fg_warehouses
)
from app.services.production_plan_service import PLAN_STATUS_LABELS
from app.services.process_order_adjustment_service import (
    ProcessOrderAdjustmentService, ProcessOrderAdjustmentError
)
from app.services.form_parser import parse_process_order_form, FormParseError
from app.services.inventory_service import _rm_warehouses, InventoryError
from app.generated.models.inventory_documents import InventoryDocument
from app.generated.models.process_order_material_adjustments import ProcessOrderMaterialAdjustment


bp = Blueprint("process_order", __name__, url_prefix="/process-orders")
schema = ProcessOrderRequestSchema()

def _load_options(exclude_order_id=None):
    q = ProcessOrder.query
    if exclude_order_id:
        q = q.filter(ProcessOrder.id != exclude_order_id)
    # 只列出"有配比明细"的（有配比的通常是 DRAFT/ORDERED）
    copy_from_orders = (
        q.filter(ProcessOrder.status.in_(
            ["DRAFT", "ORDERED", "PRODUCING", "COMPLETED", "RECEIVED"]
        ))
        .order_by(ProcessOrder.id.desc())
        .limit(200)
        .all()
    )
    return {
        "factories": Factory.query.filter_by(status=1).order_by(Factory.id).all(),
        "performances": SalesPerformance.query.order_by(
            SalesPerformance.id.desc()
        ).limit(200).all(),
        "employees": Employee.query.filter_by(status=1).order_by(Employee.id).all(),
        "materials": Material.query.filter_by(status=1).order_by(
            Material.material_code
        ).all(),
        "copy_from_orders": copy_from_orders,   # ⭐
    }


@bp.get("/")
def list_page():
    """
    工艺单列表页。
    支持 ?keyword=&status=&factory_id=&page=
    """
    keyword = request.args.get("keyword", "").strip() or None
    status = request.args.get("status", "").strip() or None
    factory_id_raw = request.args.get("factory_id", "").strip()
    factory_id = int(factory_id_raw) if factory_id_raw.isdigit() else None
    page = max(int(request.args.get("page", 1) or 1), 1)
    page_size = 20

    items, total = ProcessOrderService.list_orders(
        keyword=keyword, status=status,
        factory_id=factory_id,
        page=page, page_size=page_size,
    )

    total_pages = max((total + page_size - 1) // page_size, 1)

    return render_template(
        "process_order/list.html",
        items=items, total=total, page=page, page_size=page_size,
        total_pages=total_pages,
        keyword=keyword or "", status=status or "",
        factory_id=factory_id or "",
        factories=Factory.query.filter_by(status=1).order_by(Factory.id).all(),
        status_labels=PO_STATUS_LABELS,
        plan_status_labels=PLAN_STATUS_LABELS,   # ⭐
        warehouses=_rm_warehouses(),
        # ⭐ 新增：成品仓列表
        fg_warehouses=_fg_warehouses()
    )


@bp.get("/<int:order_id>")
def detail_page(order_id: int):
    order = db.session.get(ProcessOrder, order_id)
    if not order:
        abort(404)

    materials = (
        ProcessOrderMaterial.query
        .filter_by(process_order_id=order_id)
        .order_by(ProcessOrderMaterial.id.asc())
        .all()
    )
    material_warehouse = (
        db.session.get(Warehouse, order.material_warehouse_id)
        if order.material_warehouse_id else None
    )

    # ⭐ 一次性批量取原料编码 + 名称，避免 N+1
    material_map = {}
    if materials:
        mat_ids = [m.material_id for m in materials]
        for mat in Material.query.filter(Material.id.in_(mat_ids)).all():
            material_map[mat.id] = mat

    factory = db.session.get(Factory, order.factory_id)
    performance = db.session.get(SalesPerformance, order.performance_id)
    follow = db.session.get(Employee, order.followup_employee_id)
    creator = db.session.get(Employee, order.created_by)

    return render_template(
        "process_order/detail.html",
        order=order,
        materials=materials,
        material_warehouse=material_warehouse,
        material_map=material_map,   # ⭐ 传给模板
        status_labels=PO_STATUS_LABELS,
        factory=factory,
        performance=performance,
        follow=follow,
        creator=creator,
    )

# ---------- JSON API ----------
@bp.post("/api")
def api_create():
    payload = request.get_json(silent=True) or {}
    try:
        data = schema.load(payload)
    except ValidationError as e:
        return jsonify({"code": 422, "msg": "参数校验失败", "errors": e.messages}), 422

    try:
        result = ProcessOrderService.create_process_order(
            factory_id=data["factory_id"],
            performance_id=data["performance_id"],
            followup_employee_id=data["followup_employee_id"],
            created_by=data["created_by"],
            batch_no=data["batch_no"],
            product_name=data["product_name"],
            product_specification=data.get("product_specification"),
            unit=data.get("unit", "KG"),
            planned_quantity=data["planned_quantity"],
            order_time=data.get("order_time"),
            planned_cost_price=data.get("planned_cost_price", "0"),
            planned_total_cost=data.get("planned_total_cost", "0"),
            remark=data.get("remark"),
            process_order_no=data.get("process_order_no"),
            status=data.get("status", "DRAFT"),
            materials=[
                {
                    "material_id": m["material_id"],
                    "ratio_value": m["ratio_value"],
                    "quantity": m.get("quantity", "0"),
                    "cost": m.get("cost", "0"),
                    "remark": m.get("remark"),
                }
                for m in data["materials"]
            ],
        )
    except ProcessOrderError as e:
        return jsonify({"code": 400, "msg": str(e)}), 400

    return jsonify({"code": 0, "msg": "工艺单创建成功", "data": result})


def _build_form_data_from_request(request_form, defaults=None) -> dict:
    """从 request.form 重建 form_data（失败回显，或编辑初始值）"""
    materials = []
    idx = 0
    while True:
        key = f"materials[{idx}][material_id]"
        if key not in request_form:
            break
        materials.append({
            "material_id":   request_form.get(key, ""),
            "ratio_value":   request_form.get(f"materials[{idx}][ratio_value]", ""),
            "quantity":      request_form.get(f"materials[{idx}][quantity]", ""),
            "cost":          request_form.get(f"materials[{idx}][cost]", ""),
            "remark":        request_form.get(f"materials[{idx}][remark]", ""),
        })
        idx += 1
    if not materials:
        materials = [{"material_id": "", "ratio_value": "",
                      "quantity": "", "cost": "", "remark": ""}]

    base = {
        "process_order_no": "", "factory_id": "", "performance_id": "",
        "followup_employee_id": "", "created_by": "",
        "batch_no": "", "product_name": "", "product_specification": "",
        "unit": "KG", "planned_quantity": "",
        "order_time": datetime.now().strftime("%Y-%m-%dT%H:%M"),
        "planned_cost_price": "0", "planned_total_cost": "0", "remark": "",
        "status": "DRAFT", "materials": materials,
    }
    for k in base:
        if k == "materials":
            continue
        if k in request_form:
            base[k] = request_form.get(k, "")
    if defaults:
        base.update({k: v for k, v in defaults.items() if k != "materials"})
    return base


def _render_new_form(form_data=None, errors=None):
    ctx = _load_options()
    return render_template(
        "process_order/form.html",
        **ctx,
        form_data=form_data or _build_form_data_from_request({}),
        errors=errors or [],
        mode="create",     # 关键：模板根据 mode 决定 action/标题/按钮
        order_id=None,
        status_labels=PO_STATUS_LABELS,
    )


# ---------- 新建 ----------
@bp.get("/new")
def new_page():
    return _render_new_form()


@bp.post("/new")
def create_submit():
    try:
        data = parse_process_order_form(request.form)
        result = ProcessOrderService.create_process_order(**data)
    except (FormParseError, ProcessOrderError) as e:
        fd = _build_form_data_from_request(request.form)
        return _render_new_form(form_data=fd, errors=[str(e)]), 400

    return render_template("process_order/success.html", result=result, status_labels=PO_STATUS_LABELS)

# ---------- 编辑 ----------
@bp.get("/<int:order_id>/edit")
def edit_page(order_id: int):
    order = db.session.get(ProcessOrder, order_id)
    if not order:
        abort(404)

    if order.status != "DRAFT":
        # 非草稿不允许编辑（后端硬拦截，不依赖前端隐藏按钮）
        return (
            render_template(
                "process_order/forbidden.html",
                order=order,
                reason=f"当前状态【{order.status}】不允许编辑，仅草稿可编辑",
            ),
            403,
        )

    materials = (
        ProcessOrderMaterial.query
        .filter_by(process_order_id=order_id)
        .order_by(ProcessOrderMaterial.id.asc())
        .all()
    )

    form_data = {
        "process_order_no": order.process_order_no,
        "factory_id": order.factory_id,
        "performance_id": order.performance_id,
        "followup_employee_id": order.followup_employee_id,
        "created_by": order.created_by,
        "batch_no": order.batch_no,
        "product_name": order.product_name,
        "product_specification": order.product_specification or "",
        "unit": order.unit or "KG",
        "planned_quantity": str(order.planned_quantity),
        "order_time": order.order_time.strftime("%Y-%m-%dT%H:%M")
                      if order.order_time else "",
        "planned_cost_price": str(order.planned_cost_price),
        "planned_total_cost": str(order.planned_total_cost),
        "remark": order.remark or "",
        "status": order.status,
        "materials": [
            {
                "material_id": m.material_id,
                "ratio_value": str(m.ratio_value),
                "quantity": str(m.quantity),
                "cost": str(m.cost),
                "remark": m.remark or "",
            }
            for m in materials
        ] or [{"material_id": "", "ratio_value": "",
               "quantity": "", "cost": "", "remark": ""}],
    }

    ctx = _load_options(exclude_order_id=order.id)
    return render_template(
        "process_order/form.html",
        **ctx,
        form_data=form_data,
        errors=[],
        mode="edit",
        order_id=order.id,
        order=order,
        status_labels=PO_STATUS_LABELS,
    )

@bp.post("/<int:order_id>/edit")
def update_submit(order_id: int):
    order = db.session.get(ProcessOrder, order_id)
    if not order:
        abort(404)

    try:
        data = parse_process_order_form(request.form)
        # 编辑不允许改单号/创建人
        data.pop("process_order_no", None)
        data.pop("created_by", None)
        result = ProcessOrderService.update_process_order(
            order_id=order_id, **data,
        )
    except (FormParseError, ProcessOrderError) as e:
        # 失败时把表单当前值回显
        fd = _build_form_data_from_request(request.form, defaults={
            "process_order_no": order.process_order_no,
            "created_by": order.created_by,
        })
        ctx = _load_options(exclude_order_id=order_id)
        return (
            render_template(
                "process_order/form.html",
                **ctx, form_data=fd, errors=[str(e)],
                mode="edit", order_id=order_id, order=order, status_labels=PO_STATUS_LABELS,
            ),
            400,
        )

    return render_template("process_order/success.html", result=result)

def _action_url(action: str, order_id: int) -> str:
    return url_for(f"process_order.{action}", order_id=order_id)

@bp.post("/<int:order_id>/place-order")
def place_order(order_id: int):
    # 出库仓库来源优先级：表单 > 默认原料仓
    warehouse_id = request.form.get("warehouse_id", type=int)
    if not warehouse_id:
        default_wh = (
            Warehouse.query
            .filter_by(warehouse_type="raw_material", status=1)
            .order_by(Warehouse.id.asc())
            .first()
        )
        if not default_wh:
            flash("未配置可用的原料仓，无法下单", "error")
            return redirect(request.referrer or url_for("process_order.list_page"))
        warehouse_id = default_wh.id

    try:
        ProcessOrderService.place_order(
            order_id=order_id,
            out_warehouse_id=warehouse_id,
            operator_id=1,     # 从 session 取当前用户
            remark=request.form.get("remark"),
            allow_partial=False,
        )
        flash("下单成功，已自动生成原料领料出库单", "success")
    except ProcessOrderError as e:
        flash(str(e), "error")

    return redirect(request.referrer or url_for("process_order.list_page"))

@bp.post("/<int:order_id>/produce")
def produce(order_id: int):
    """生产完：ORDERED -> PRODUCING"""
    try:
        ProcessOrderService.change_status(order_id, "produce")
        flash("开始生产", "success")
    except ProcessOrderError as e:
        flash(str(e), "error")
    return redirect(request.referrer or url_for("process_order.list_page"))


@bp.post("/<int:order_id>/receive")
def receive(order_id: int):
    """入库：PRODUCING -> RECEIVED，同时写成品库入库"""
    warehouse_id = request.form.get("warehouse_id", type=int)
    if not warehouse_id:
        # 回退到默认成品仓
        default_wh = (
            Warehouse.query
            .filter_by(warehouse_type="finished_goods", status=1)
            .order_by(Warehouse.id.asc())
            .first()
        )
        if not default_wh:
            flash("未配置成品仓，无法入库", "error")
            return redirect(request.referrer or url_for("process_order.list_page"))
        warehouse_id = default_wh.id

    try:
        result = ProcessOrderService.receive_to_finished_goods(
            order_id=order_id,
            warehouse_id=warehouse_id,
            operator_id=1,
            remark=request.form.get("remark"),
        )
        flash(
            f"入库成功：批次 {result['batch_no']}，数量 {result['quantity']}，"
            f"单价 {result['unit_cost']}",
            "success",
        )
        return redirect(url_for("finished_goods.document_detail_page",
            direction="in", document_id=result["finished_goods"]["document_id"]))
    except ProcessOrderError as e:
        flash(str(e), "error")

    return redirect(request.referrer or url_for("process_order.list_page"))

@bp.post("/<int:order_id>/cancel")
def cancel(order_id: int):
    """取消：DRAFT/ORDERED/PRODUCING/RECEIVED → CANCELLED，并按状态冲销库存"""
    try:
        result = ProcessOrderService.cancel_with_reversal(
            order_id=order_id,
            reason=request.form.get("remark") or None,
            operator_id=1,
        )
        msgs = ["已取消"]
        if result.get("finished_goods_reverse"):
            msgs.append(
                f"成品入库已冲销（反向单 {result['finished_goods_reverse']['reverse_document_no']}）"
            )
        if result.get("raw_material_reverse"):
            msgs.append(
                f"原料出库已冲销（红冲单 {result['raw_material_reverse']['document_no']}）"
            )
        flash("，".join(msgs), "success")
    except ProcessOrderError as e:
        flash(str(e), "error")
    return redirect(request.referrer or url_for("process_order.list_page"))

# ---------------- JSON API（可选） ----------------
@bp.post("/api/<int:order_id>/<action>")
def api_change_status(order_id: int, action: str):
    if action not in ("place-order", "ship-out", "cancel"):
        return jsonify({"code": 400, "msg": "不支持的操作"}), 400
    try:
        if action == "cancel":
            result = ProcessOrderService.cancel_with_reversal(
                order_id=order_id, reason=None, operator_id=1,
            )
        else:
            action_key = action.replace("-", "_")
            result = ProcessOrderService.change_status(order_id, action_key)
    except ProcessOrderError as e:
        return jsonify({"code": 400, "msg": str(e)}), 400
    return jsonify({"code": 0, "msg": "操作成功", "data": result})

def _render_error(error_msg: str):
    ctx = _load_options()

    materials = []
    idx = 0
    while True:
        key = f"materials[{idx}][material_id]"
        if key not in request.form:
            break
        materials.append({
            "material_id": request.form.get(key, ""),
            "ratio_value": request.form.get(f"materials[{idx}][ratio_value]", ""),
            "quantity": request.form.get(f"materials[{idx}][quantity]", ""),
            "cost": request.form.get(f"materials[{idx}][cost]", ""),
            "remark": request.form.get(f"materials[{idx}][remark]", ""),
        })
        idx += 1
    if not materials:
        materials = [{"material_id": "", "ratio_value": "",
                      "quantity": "", "cost": "", "remark": ""}]

    form_data = {
        "process_order_no": request.form.get("process_order_no", ""),
        "factory_id": request.form.get("factory_id", ""),
        "performance_id": request.form.get("performance_id", ""),
        "followup_employee_id": request.form.get("followup_employee_id", ""),
        "created_by": request.form.get("created_by", ""),
        "batch_no": request.form.get("batch_no", ""),
        "product_name": request.form.get("product_name", ""),
        "product_specification": request.form.get("product_specification", ""),
        "unit": request.form.get("unit", "KG"),
        "planned_quantity": request.form.get("planned_quantity", ""),
        "order_time": request.form.get("order_time", ""),
        "planned_cost_price": request.form.get("planned_cost_price", "0"),
        "planned_total_cost": request.form.get("planned_total_cost", "0"),
        "remark": request.form.get("remark", ""),
        "status": request.form.get("status", "DRAFT"),
        "materials": materials,
    }
    return (
        render_template(
            "process_order/form.html",
            **ctx,
            form_data=form_data,
            errors=[error_msg],
            status_labels=PO_STATUS_LABELS,
        ),
        400,
    )

@bp.post("/<int:order_id>/adjust-materials")
def adjust_materials(order_id):
    """
    配比调整。表单字段：
      materials[0][material_id] / [ratio_value] / [remark] ...
      reason
    """
    # 解析新配比
    indexes = set()
    for key in request.form.keys():
        m = re.match(r"materials\[(\d+)\]\[material_id\]", key)
        if m:
            indexes.add(int(m.group(1)))

    new_materials = []
    for idx in sorted(indexes):
        mid = request.form.get(f"materials[{idx}][material_id]", "").strip()
        ratio = request.form.get(f"materials[{idx}][ratio_value]", "").strip()
        if not mid or not ratio:
            continue
        try:
            new_materials.append({
                "material_id": int(mid),
                "ratio_value": ratio,
                "remark": request.form.get(
                    f"materials[{idx}][remark]", "").strip() or None,
            })
        except ValueError:
            continue

    # 若为空，说明用户把所有行都删了 → 报错
    if not new_materials:
        flash("请至少保留一条原料", "error")
        return redirect(request.referrer or url_for(
            "process_order.detail_page", order_id=order_id))

    try:
        result = ProcessOrderAdjustmentService.adjust_materials(
            order_id=order_id,
            new_materials=new_materials,
            reason=request.form.get("reason", "").strip() or None,
            operator_id=1,
        )
        flash(
            f"配比调整成功，红冲单 {result['reverse_doc_no'] or '-'}，"
            f"补充单 {result['consume_doc_no'] or '-'}",
            "success",
        )
    except (ProcessOrderAdjustmentError, InventoryError) as e:
        flash(str(e), "error")

    #print("提交的 new_materials:", new_materials)
    return redirect(url_for("process_order.detail_page", order_id=order_id))

@bp.get("/<int:order_id>/adjust-materials")
def adjust_materials_page(order_id):
    order = db.session.get(ProcessOrder, order_id)
    if not order:
        abort(404)
    if order.status not in ("ORDERED", "PRODUCING", "COMPLETED", "RECEIVED"):
        flash(f"当前状态【{PO_STATUS_LABELS.get(order.status, order.status)}】"
              f"不允许调整配比", "error")
        return redirect(url_for("process_order.detail_page", order_id=order_id))

    materials = (
        ProcessOrderMaterial.query
        .filter_by(process_order_id=order_id)
        .order_by(ProcessOrderMaterial.id.asc())
        .all()
    )

    # 原料基础信息映射
    material_map = {}
    if materials:
        ids = [m.material_id for m in materials]
        for mat in Material.query.filter(Material.id.in_(ids)).all():
            material_map[mat.id] = mat

    all_materials = (
        Material.query.filter_by(status=1)
        .order_by(Material.material_code.asc())
        .all()
    )

    return render_template(
        "process_order/adjust_materials.html",
        order=order,
        materials=materials,
        material_map=material_map,
        all_materials=all_materials,
        status_labels=PO_STATUS_LABELS,
        errors=[],
    )

# 追加到 app/controllers/process_order_controller.py
@bp.get("/<int:order_id>/adjustments")
def adjustments_list_page(order_id: int):
    order = db.session.get(ProcessOrder, order_id)
    if not order:
        abort(404)

    adjustments = (
        ProcessOrderMaterialAdjustment.query
        .filter_by(process_order_id=order_id)
        .order_by(ProcessOrderMaterialAdjustment.adjust_time.desc())
        .all()
    )

    # 关联单据批量取
    doc_ids = set()
    for a in adjustments:
        if a.red_document_id:
            doc_ids.add(a.red_document_id)
        if a.blue_document_id:
            doc_ids.add(a.blue_document_id)
    docs = {}
    if doc_ids:
        for d in InventoryDocument.query.filter(
                InventoryDocument.id.in_(doc_ids)).all():
            docs[d.id] = d

    # 原料信息批量取
    mat_ids = set()
    for a in adjustments:
        for snap in (a.old_snapshot or []) + (a.new_snapshot or []):
            if isinstance(snap, dict) and snap.get("material_id"):
                mat_ids.add(int(snap["material_id"]))
    material_map = {}
    if mat_ids:
        for m in Material.query.filter(Material.id.in_(mat_ids)).all():
            material_map[m.id] = m

    return render_template(
        "process_order/adjustments.html",
        order=order,
        adjustments=adjustments,
        docs=docs,
        material_map=material_map,
        status_labels=PO_STATUS_LABELS,
    )

@bp.get("/api/<int:order_id>/materials")
def api_get_order_materials(order_id: int):
    """
    返回指定工艺单的原料配比明细，供其他工艺单页面"复制配比"使用。
    """
    order = db.session.get(ProcessOrder, order_id)
    if not order:
        return jsonify({"code": 404, "msg": "工艺单不存在"}), 404

    rows = (
        ProcessOrderMaterial.query
        .filter_by(process_order_id=order_id)
        .order_by(ProcessOrderMaterial.id.asc())
        .all()
    )
    return jsonify({
        "code": 0,
        "data": {
            "process_order_no": order.process_order_no,
            "product_name": order.product_name,
            "product_specification": order.product_specification,
            "materials": [
                {
                    "material_id": r.material_id,
                    "ratio_value": str(r.ratio_value),
                    "quantity": str(r.quantity),
                    "cost": str(r.cost),
                    "remark": r.remark or "",
                }
                for r in rows
            ],
        },
    })