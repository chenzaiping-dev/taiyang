from datetime import datetime
from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort, session
)

from app.extensions import db
from app.services.draft_delete_service import DraftDeleteService, DraftDeleteError
from app.generated.models.sales_performance import SalesPerformance
from app.generated.models.warehouses import Warehouse

from app.services.shipment_plan_service import ShipmentPlanService, ShipmentPlanError
from app.services.permission_service import can_edit_shipment_money
from app.exports.queries import run_query

bp = Blueprint("shipment_plan", __name__, url_prefix="/shipment-plans")

SP_STATUS_LABELS = {
    "DRAFT": "草稿",
    "CONFIRMED": "已确认",
    "PARTIALLY_SHIPPED": "部分发货",
    "SHIPPED": "已发货",
    "CANCELLED": "已取消",
}


def _parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _parse_form(form):
    destination = form.get("destination", "").strip()
    transport_method = form.get("transport_method", "").strip() or None
    planned_ship_time = _parse_dt(form.get("planned_ship_time", "").strip())
    remark = form.get("remark", "").strip() or None

    lines = []
    idx = 0
    while True:
        prefix = f"lines[{idx}]"
        if prefix + "[quantity]" not in form:
            break
        qty_raw = form.get(prefix + "[quantity]", "").strip()
        res_raw = form.get(prefix + "[reservation_id]", "").strip()
        wh_raw = form.get(prefix + "[warehouse_id]", "").strip()
        batch_raw = form.get(prefix + "[batch_id]", "").strip()
        if qty_raw and res_raw and wh_raw and batch_raw:
            try:
                if float(qty_raw) > 0:
                    lines.append({
                        "reservation_id": int(res_raw),
                        "warehouse_id": int(wh_raw),
                        "batch_id": int(batch_raw),
                        "quantity": qty_raw,
                        "product_name": form.get(prefix + "[product_name]", "").strip(),
                        "product_specification": form.get(
                            prefix + "[product_specification]", "").strip() or None,
                        "unit": form.get(prefix + "[unit]", "KG").strip() or "KG",
                        "unit_price": form.get(prefix + "[unit_price]", "").strip() or None,
                    })
            except ValueError:
                pass
        idx += 1

    return {
        "destination": destination,
        "transport_method": transport_method,
        "planned_ship_time": planned_ship_time,
        "remark": remark,
        "lines": lines,
    }


# ==================== 新建 ====================
@bp.get("/new")
def new_page():
    performance_id = request.args.get("performance_id", type=int)
    if not performance_id:
        flash("缺少业绩单ID，请从业绩单进入", "error")
        return redirect(url_for("process_order.list_page"))
    perf = db.session.get(SalesPerformance, performance_id)
    if not perf:
        abort(404)

    reservations = ShipmentPlanService.get_available_reservations(performance_id)

    return render_template(
        "shipment_plan/form.html",
        mode="create",
        plan=None,
        performance=perf,
        reservations=reservations,
        form_data={
            "performance_id": performance_id,
            "destination": "",
            "transport_method": "",
            "planned_ship_time": datetime.now().strftime("%Y-%m-%dT%H:%M"),
            "remark": "",
            "lines": [],
        },
        errors=[],
    )


@bp.post("/new")
def create_submit():
    performance_id = request.form.get("performance_id", type=int)
    if not performance_id:
        return redirect(url_for("shipment_plan.new_page"))

    try:
        data = _parse_form(request.form)
        if not can_edit_shipment_money():
            for line in data.get("lines", []):
                line["unit_price"] = None
        plan = ShipmentPlanService.create_plan(
            performance_id=performance_id,
            created_by=session.get("employee_id"),
            **data,
        )
        flash(f"发货计划创建成功，单号 {plan.shipment_plan_no}", "success")
        return redirect(url_for("shipment_plan.detail_page", plan_id=plan.id))
    except ShipmentPlanError as e:
        perf = db.session.get(SalesPerformance, performance_id)
        return (
            render_template(
                "shipment_plan/form.html",
                mode="create", plan=None, performance=perf,
                reservations=ShipmentPlanService.get_available_reservations(
                    performance_id),
                form_data={
                    "performance_id": performance_id,
                    "destination": request.form.get("destination", ""),
                    "transport_method": request.form.get("transport_method", ""),
                    "planned_ship_time": request.form.get("planned_ship_time", ""),
                    "remark": request.form.get("remark", ""),
                    "lines": data["lines"] if 'data' in locals() else [],
                },
                errors=[str(e)],
            ),
            400,
        )


# ==================== 编辑 ====================
@bp.get("/<int:plan_id>/edit")
def edit_page(plan_id):
    context = ShipmentPlanService.get_plan_edit_context(plan_id)
    if not context:
        abort(404)
    plan = context["plan"]
    if plan.status != "DRAFT":
        flash("只有草稿状态的发货计划可编辑", "error")
        return redirect(url_for("shipment_plan.detail_page", plan_id=plan_id))

    return render_template(
        "shipment_plan/form.html",
        mode="edit", plan=plan, performance=context["performance"],
        reservations=context["reservations"],
        form_data={
            "performance_id": plan.performance_id,
            "destination": plan.destination,
            "transport_method": plan.transport_method or "",
            "planned_ship_time": (
                plan.planned_ship_time.strftime("%Y-%m-%dT%H:%M")
                if plan.planned_ship_time else ""
            ),
            "remark": plan.remark or "",
            "lines": [],
        },
        existing_map=context["existing_map"],
        errors=[],
    )


@bp.post("/<int:plan_id>/edit")
def update_submit(plan_id):
    plan = ShipmentPlanService.get_plan(plan_id)
    if not plan:
        abort(404)

    try:
        data = _parse_form(request.form)
        if not can_edit_shipment_money():
            existing_prices = ShipmentPlanService.get_existing_allocation_prices(plan_id)
            for line in data.get("lines", []):
                line["unit_price"] = existing_prices.get(
                    (line["warehouse_id"], line["batch_id"]), 0
                )
        ShipmentPlanService.update_plan(plan_id=plan_id, **data)
        flash("发货计划已更新", "success")
        return redirect(url_for("shipment_plan.detail_page", plan_id=plan_id))
    except ShipmentPlanError as e:
        error_context = ShipmentPlanService.get_update_error_context(plan_id)
        if not error_context:
            abort(404)
        return (
            render_template(
                "shipment_plan/form.html",
                mode="edit", plan=error_context["plan"],
                performance=error_context["performance"],
                reservations=error_context["reservations"],
                form_data={
                    "performance_id": error_context["plan"].performance_id,
                    "destination": request.form.get("destination", ""),
                    "transport_method": request.form.get("transport_method", ""),
                    "planned_ship_time": request.form.get("planned_ship_time", ""),
                    "remark": request.form.get("remark", ""),
                    "lines": data["lines"] if "data" in locals() else [],
                },
                existing_map={},
                errors=[str(e)],
            ),
            400,
        )


# ==================== 列表 ====================
@bp.get("/")
def list_page():
    keyword = request.args.get("keyword", "").strip() or None
    status = request.args.get("status", "").strip() or None
    performance_id = request.args.get("performance_id", "").strip() or None
    performance_no = request.args.get("performance_no", "").strip() or None
    company_name = request.args.get("company_name", "").strip() or None
    product_name = request.args.get("product_name", "").strip() or None
    ship_time_from_raw = request.args.get("ship_time_from", "").strip() or None
    ship_time_to_raw = request.args.get("ship_time_to", "").strip() or None

    # ⭐ 支持两种输入：数字 → 直接当 ID；非数字 → 按业绩单号反查
    page = max(int(request.args.get("page", 1) or 1), 1)
    page_size = 20
    rows, total = run_query("shipment_plans", request.args, page=page, page_size=page_size)

    total_pages = max((total + page_size - 1) // page_size, 1)
    return render_template(
        "shipment_plan/list.html",
        rows=rows, total=total, page=page,
        page_size=page_size, total_pages=total_pages,
        keyword=keyword or "",
        status=status or "",
        performance_id=performance_id or "",
        performance_no=performance_no or "",
        company_name=company_name or "",
        product_name=product_name or "",
        ship_time_from=ship_time_from_raw or "",
        ship_time_to=ship_time_to_raw or "",
        status_labels=SP_STATUS_LABELS,
    )


# ==================== 发货计划明细列表 ====================
@bp.get("/details/")
def detail_list_page():
    keyword = request.args.get("keyword", "").strip()
    status = request.args.get("status", "").strip()
    performance_no = request.args.get("performance_no", "").strip()
    company_name = request.args.get("company_name", "").strip()
    product_name = request.args.get("product_name", "").strip()
    warehouse_id = request.args.get("warehouse_id", "").strip()
    batch_keyword = request.args.get("batch_keyword", "").strip()
    ship_time_from = request.args.get("ship_time_from", "").strip()
    ship_time_to = request.args.get("ship_time_to", "").strip()

    page = max(request.args.get("page", 1, type=int) or 1, 1)
    page_size = 20
    rows, total = run_query(
        "shipment_plan_details", request.args, page=page, page_size=page_size
    )
    total_pages = max((total + page_size - 1) // page_size, 1)
    detail_status_labels = {
        key: value for key, value in SP_STATUS_LABELS.items() if key != "DRAFT"
    }
    warehouses = (
        db.session.query(Warehouse.id, Warehouse.warehouse_name)
        .filter(
            Warehouse.status == 1,
            Warehouse.warehouse_type.in_(["finished_goods", "general"]),
        )
        .order_by(Warehouse.warehouse_code)
        .all()
    )
    return render_template(
        "shipment_plan/detail_list.html",
        rows=rows, total=total, page=page, page_size=page_size,
        total_pages=total_pages, keyword=keyword, status=status,
        performance_no=performance_no, company_name=company_name,
        product_name=product_name, warehouse_id=warehouse_id,
        batch_keyword=batch_keyword, ship_time_from=ship_time_from,
        ship_time_to=ship_time_to, status_labels=detail_status_labels,
        warehouses=warehouses,
    )


# ==================== 详情 ====================
@bp.get("/<int:plan_id>")
def detail_page(plan_id):
    context = ShipmentPlanService.get_detail_context(plan_id)
    if not context:
        abort(404)

    return render_template(
        "shipment_plan/detail.html",
        plan=context["plan"],
        creator=context["creator"],
        performance=context["performance"],
        document=context["document"],
        lines=context["lines"],
        allocations_map=context["allocations_map"],
        status_labels=SP_STATUS_LABELS,
    )


@bp.post("/<int:plan_id>/old_ship")
def ship_submit(plan_id):
    allocations = []
    idx = 0
    while True:
        prefix = f"alloc[{idx}]"
        if prefix + "[allocation_id]" not in request.form:
            break
        aid = request.form.get(prefix + "[allocation_id]", "").strip()
        qty = request.form.get(prefix + "[quantity]", "").strip()
        if aid and qty:
            try:
                if float(qty) > 0:
                    allocations.append({
                        "allocation_id": int(aid),
                        "quantity": qty,
                    })
            except ValueError:
                pass
        idx += 1

    try:
        ShipmentPlanService.record_shipment(plan_id, allocations)
        flash("发货已登记", "success")
    except ShipmentPlanError as e:
        flash(str(e), "error")
    return redirect(url_for("shipment_plan.detail_page", plan_id=plan_id))

# ==================== 状态操作 ====================

@bp.post("/<int:plan_id>/mark-planned")
def mark_planned(plan_id):
    try:
        ShipmentPlanService.mark_planned(plan_id)
        flash("已标记为【已计划】", "success")
    except ShipmentPlanError as e:
        flash(str(e), "error")
    return redirect(url_for("shipment_plan.list_page"))


@bp.post("/<int:plan_id>/delete")
def delete_draft(plan_id):
    try:
        DraftDeleteService.delete_shipment_plan(plan_id)
        flash("发货计划草稿已删除", "success")
    except DraftDeleteError as e:
        db.session.rollback()
        flash(str(e), "error")
    except Exception:
        db.session.rollback()
        flash("删除失败：存在未满足的关联约束或数据库异常，请检查关联业务单据", "error")
    return redirect(url_for("shipment_plan.list_page"))


@bp.post("/<int:plan_id>/cancel")
def cancel_plan(plan_id):
    try:
        ShipmentPlanService.cancel_plan(plan_id, operator_id=session.get("employee_id"))
        flash("发货计划已取消", "success")
    except ShipmentPlanError as e:
        flash(str(e), "error")
    return redirect(url_for("shipment_plan.list_page"))


@bp.post("/<int:plan_id>/ship")
def ship_plan(plan_id):
    """列表页【发货】按钮：整体发货 + 出库"""
    operator_id = session.get("employee_id")
    try:
        ShipmentPlanService.ship_plan(plan_id, operator_id=operator_id)
        flash("发货成功，已出库", "success")
    except ShipmentPlanError as e:
        flash(str(e), "error")
    return redirect(url_for("shipment_plan.list_page"))