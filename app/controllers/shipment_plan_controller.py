from datetime import datetime
from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort
)

from app.extensions import db
from app.generated.models.employees import Employee
from app.generated.models.warehouses import Warehouse
from app.generated.models.sales_performance import SalesPerformance

from app.generated.models.finished_goods_batches import FinishedGoodsBatch
from app.generated.models.finished_goods_inventory import FinishedGoodsInventory
from app.generated.models.finished_goods_reservations import FinishedGoodsReservation
from app.generated.models.shipment_plans import ShipmentPlan
from app.generated.models.shipment_plan_lines import ShipmentPlanLine
from app.generated.models.shipment_plan_allocations import ShipmentPlanAllocation

from app.utils.datetime_utils import now_beijing

from app.services.shipment_plan_service import ShipmentPlanService, ShipmentPlanError
#from app.services.shipment_query_service import ShipmentQueryService

bp = Blueprint("shipment_plan", __name__, url_prefix="/shipment-plans")

STATUS_LABELS = {
    "DRAFT": "草稿",
    "PLANNED": "已计划",
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
        plan = ShipmentPlanService.create_plan(
            performance_id=performance_id,
            created_by=1,
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
    plan = db.session.get(ShipmentPlan, plan_id)
    if not plan:
        abort(404)
    #if plan.status not in ("DRAFT", "PLANNED"):
    if plan.status != "DRAFT":          # 仅草稿可编辑
        flash("只有草稿状态的发货计划可编辑", "error")
        return redirect(url_for("shipment_plan.detail_page", plan_id=plan_id))

    perf = db.session.get(SalesPerformance, plan.performance_id)
    reservations = ShipmentPlanService.get_available_reservations(
        plan.performance_id)

    # 现有 allocations → 按 (warehouse, batch) 回填数量
    existing = (
        db.session.query(
            ShipmentPlanAllocation.warehouse_id,
            ShipmentPlanAllocation.batch_id,
            ShipmentPlanAllocation.planned_quantity,
            ShipmentPlanLine.product_name,
            ShipmentPlanLine.product_specification,
            ShipmentPlanLine.unit,
            ShipmentPlanLine.unit_price,
        )
        .join(ShipmentPlanLine,
              ShipmentPlanLine.id == ShipmentPlanAllocation.shipment_line_id)
        .filter(ShipmentPlanLine.shipment_plan_id == plan.id)
        .all()
    )
    existing_map = {
        (r.warehouse_id, r.batch_id): dict(r._mapping)
        for r in existing
    }

    return render_template(
        "shipment_plan/form.html",
        mode="edit",
        plan=plan,
        performance=perf,
        reservations=reservations,
        form_data={
            "performance_id": plan.performance_id,
            "destination": plan.destination,
            "transport_method": plan.transport_method or "",
            "planned_ship_time": (
                plan.planned_ship_time.strftime("%Y-%m-%dT%H:%M")
                if plan.planned_ship_time else ""
            ),
            "remark": plan.remark or "",
            "lines": [],   # 模板会从 existing_map 回填
        },
        existing_map=existing_map,
        errors=[],
    )


@bp.post("/<int:plan_id>/edit")
def update_submit(plan_id):
    plan = db.session.get(ShipmentPlan, plan_id)
    if not plan:
        abort(404)

    try:
        data = _parse_form(request.form)
        ShipmentPlanService.update_plan(plan_id=plan_id, **data)
        flash("发货计划已更新", "success")
        return redirect(url_for("shipment_plan.detail_page", plan_id=plan_id))
    except ShipmentPlanError as e:
        perf = db.session.get(SalesPerformance, plan.performance_id)
        return (
            render_template(
                "shipment_plan/form.html",
                mode="edit", plan=plan, performance=perf,
                reservations=ShipmentPlanService.get_available_reservations(
                    plan.performance_id),
                form_data={
                    "performance_id": plan.performance_id,
                    "destination": request.form.get("destination", ""),
                    "transport_method": request.form.get("transport_method", ""),
                    "planned_ship_time": request.form.get("planned_ship_time", ""),
                    "remark": request.form.get("remark", ""),
                    "lines": data["lines"] if 'data' in locals() else [],
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
    perf_raw = request.args.get("performance_id", "").strip()

    # ⭐ 支持两种输入：数字 → 直接当 ID；非数字 → 按业绩单号反查
    performance_id = None
    if perf_raw:
        if perf_raw.isdigit():
            performance_id = int(perf_raw)
        else:
            perf = SalesPerformance.query.filter_by(
                performance_no=perf_raw
            ).first()
            if perf:
                performance_id = perf.id

    page = max(int(request.args.get("page", 1) or 1), 1)
    page_size = 20

    from sqlalchemy import func, select, or_
    # 已发货数量：每个计划的总和
    shipped_subq = (
        select(
            ShipmentPlanLine.shipment_plan_id.label("pid"),
            func.coalesce(func.sum(ShipmentPlanLine.shipped_quantity), 0).label("total_shipped"),
            func.coalesce(func.sum(ShipmentPlanLine.quantity), 0).label("total_planned"),
        )
        .group_by(ShipmentPlanLine.shipment_plan_id)
        .subquery()
    )

    base = (
        select(
            ShipmentPlan.id,
            ShipmentPlan.shipment_plan_no,
            ShipmentPlan.performance_id,
            SalesPerformance.performance_no,          # ⭐
            SalesPerformance.product_name.label("product_name"),
            ShipmentPlan.destination,
            ShipmentPlan.status,
            ShipmentPlan.created_at,
            func.coalesce(shipped_subq.c.total_planned, 0).label("planned_qty"),
            func.coalesce(shipped_subq.c.total_shipped, 0).label("shipped_qty"),
        )
        .join(SalesPerformance,
              SalesPerformance.id == ShipmentPlan.performance_id, isouter=True)
        .join(shipped_subq,
              shipped_subq.c.pid == ShipmentPlan.id, isouter=True)
        .order_by(ShipmentPlan.id.desc())
    )
    if keyword:
        like = f"%{keyword}%"
        base = base.where(ShipmentPlan.shipment_plan_no.like(like))
    if status:
        base = base.where(ShipmentPlan.status == status)
    if performance_id:
        base = base.where(ShipmentPlan.performance_id == performance_id)

    total = db.session.execute(
        select(func.count()).select_from(base.subquery())
    ).scalar() or 0
    total_pages = max((total + page_size - 1) // page_size, 1)
    base = base.offset((page - 1) * page_size).limit(page_size)
    rows = [dict(r) for r in db.session.execute(base).mappings().all()]

    return render_template(
        "shipment_plan/list.html",
        rows=rows, total=total, page=page,
        page_size=page_size, total_pages=total_pages,
        keyword=keyword or "",
        status=status or "",
        performance_id=performance_id or "",
        performance_id_raw=perf_raw or "",       # ⭐ 原始输入，用于回显
        status_labels=STATUS_LABELS,
    )


# ==================== 详情 ====================
@bp.get("/<int:plan_id>")
def detail_page(plan_id):
    plan = db.session.get(ShipmentPlan, plan_id)
    if not plan:
        abort(404)

    perf = db.session.get(SalesPerformance, plan.performance_id)

    lines = (
        ShipmentPlanLine.query
        .filter_by(shipment_plan_id=plan.id)
        .order_by(ShipmentPlanLine.id.asc())
        .all()
    )

    # 每个 line 的 allocations
    allocations_map = {}
    for line in lines:
        allocations_map[line.id] = (
            db.session.query(
                ShipmentPlanAllocation.id,
                ShipmentPlanAllocation.warehouse_id,
                Warehouse.warehouse_code,
                Warehouse.warehouse_name,
                ShipmentPlanAllocation.batch_id,
                FinishedGoodsBatch.batch_no,
                ShipmentPlanAllocation.planned_quantity,
                ShipmentPlanAllocation.shipped_quantity,
            )
            .join(Warehouse, Warehouse.id == ShipmentPlanAllocation.warehouse_id)
            .join(FinishedGoodsBatch,
                  FinishedGoodsBatch.id == ShipmentPlanAllocation.batch_id)
            .filter(ShipmentPlanAllocation.shipment_line_id == line.id)
            .all()
        )

    return render_template(
        "shipment_plan/detail.html",
        plan=plan,
        performance=perf,
        lines=lines,
        allocations_map=allocations_map,
        status_labels=STATUS_LABELS,
    )

# app/controllers/shipment_plan_controller.py 追加

@bp.post("/<int:plan_id>/old_ship")
def ship_submit(plan_id):
    from app.services.shipment_plan_service import ShipmentPlanService
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


@bp.post("/<int:plan_id>/cancel")
def cancel_plan(plan_id):
    try:
        ShipmentPlanService.cancel_plan(plan_id)
        flash("发货计划已取消", "success")
    except ShipmentPlanError as e:
        flash(str(e), "error")
    return redirect(url_for("shipment_plan.list_page"))


@bp.post("/<int:plan_id>/ship")
def ship_plan(plan_id):
    """列表页【发货】按钮：整体发货 + 出库"""
    operator_id = 1  # 接入登录后替换为 current_user.id
    try:
        ShipmentPlanService.ship_plan(plan_id, operator_id=operator_id)
        flash("发货成功，已出库", "success")
    except ShipmentPlanError as e:
        flash(str(e), "error")
    return redirect(url_for("shipment_plan.list_page"))