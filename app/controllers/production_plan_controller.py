from datetime import datetime
from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort
)
from app.extensions import db
from app.generated.models.employees import Employee
from app.generated.models.warehouses import Warehouse
from app.generated.models.factories import Factory
from app.generated.models.sales_performance import SalesPerformance
from app.generated.models.production_plans import ProductionPlan
from app.generated.models.process_orders import ProcessOrder
from app.generated.models.production_plan_process_orders import ProductionPlanProcessOrder

from app.generated.models.finished_goods_batches import FinishedGoodsBatch
from app.generated.models.finished_goods_documents import FinishedGoodsDocument
from app.generated.models.finished_goods_document_lines import FinishedGoodsDocumentLine
from app.generated.models.finished_goods_inventory import FinishedGoodsInventory
from app.generated.models.finished_goods_transactions import FinishedGoodsTransaction
from app.generated.models.finished_goods_reservations import FinishedGoodsReservation

from app.services.production_plan_service import (
    ProductionPlanService, ProductionPlanError,
)

bp = Blueprint("production_plan", __name__, url_prefix="/production-plans")


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


def _available_factories():
    return Factory.query.filter_by(status=1).order_by(Factory.id).all()


def _stock_rows_for_performance(performance_id):
    """
    按业绩单的产品名称/规格，从成品库拉各批次现存 + 已锁定数量。
    """
    perf = db.session.get(SalesPerformance, performance_id)
    if not perf:
        return []
    q = (
        db.session.query(
            FinishedGoodsInventory.id,
            FinishedGoodsInventory.warehouse_id,
            Warehouse.warehouse_code,
            Warehouse.warehouse_name,
            FinishedGoodsInventory.batch_id,
            FinishedGoodsBatch.batch_no,
            FinishedGoodsBatch.product_name,
            FinishedGoodsBatch.product_specification,
            FinishedGoodsBatch.unit,
            FinishedGoodsInventory.quantity,
        )
        .join(Warehouse, Warehouse.id == FinishedGoodsInventory.warehouse_id)
        .join(FinishedGoodsBatch,
              FinishedGoodsBatch.id == FinishedGoodsInventory.batch_id)
        .filter(
            FinishedGoodsInventory.quantity > 0,
            FinishedGoodsBatch.product_name == perf.product_name,
        )
    )
    if perf.product_specification:
        q = q.filter(FinishedGoodsBatch.product_specification
                     == perf.product_specification)
    return [dict(r._mapping) for r in q.order_by(
        Warehouse.warehouse_code.asc(), FinishedGoodsBatch.batch_no.asc()
    ).all()]


# ==================== 新建 ====================
@bp.get("/new")
def new_page():
    performance_id = request.args.get("performance_id", type=int)
    if not performance_id:
        flash("缺少业绩单ID，请从业绩单页面进入", "error")
        return redirect(url_for("process_order.list_page"))

    perf = db.session.get(SalesPerformance, performance_id)
    if not perf:
        abort(404)

    # 若已存在计划 → 跳到编辑页
    existing = ProductionPlan.query.filter_by(performance_id=performance_id).first()
    if existing:
        return redirect(url_for("production_plan.edit_page",
                                plan_id=existing.id))

    return render_template(
        "production_plan/form.html",
        mode="create",
        plan=None,
        performance=perf,
        warehouses=_fg_warehouses(),
        factories=_available_factories(),
        stock_rows=_stock_rows_for_performance(performance_id),
        selected_reservations={},     # 新建为空
        existing_process_orders=[],   # 新建为空
        form_data={
            "performance_id": performance_id,
            "total_quantity": str(perf.quantity) if perf.quantity else "0",
            "process_quantity": "0",
            "remark": "",
        },
        errors=[],
    )


@bp.post("/new")
def create_submit():
    performance_id = request.form.get("performance_id", type=int)
    if not performance_id:
        flash("缺少业绩单ID", "error")
        return redirect(url_for("production_plan.new_page"))

    try:
        data = _parse_form(request.form)
        plan = ProductionPlanService.create_plan(
            performance_id=performance_id, **data,
        )
        flash(f"生产计划创建成功，单号 {plan.plan_no}", "success")
        return redirect(url_for("production_plan.detail_page", plan_id=plan.id))
    except ProductionPlanError as e:
        return _render_error(performance_id, str(e)), 400


# ==================== 编辑 ====================
@bp.get("/<int:plan_id>/edit")
def edit_page(plan_id):
    plan = db.session.get(ProductionPlan, plan_id)
    if not plan:
        abort(404)
    perf = db.session.get(SalesPerformance, plan.performance_id)

    # 已锁定：production_plan_id -> {(warehouse_id, batch_id): remaining_qty}
    reservations = FinishedGoodsReservation.query.filter_by(
        production_plan_id=plan.id,
        reservation_type="PRODUCTION_PLAN",
        status="ACTIVE",
    ).all()
    selected_reservations = {
        (r.warehouse_id, r.batch_id): r.remaining_quantity
        for r in reservations
    }

    # 关联的工艺单
    rels = ProductionPlanProcessOrder.query.filter_by(
        production_plan_id=plan.id
    ).all()
    existing_process_orders = []
    for rel in rels:
        po = db.session.get(ProcessOrder, rel.process_order_id)
        if po:
            existing_process_orders.append({
                "relation_id": rel.id,
                "process_order_id": po.id,
                "process_order_no": po.process_order_no,
                "factory_id": po.factory_id,
                "factory_name": (
                    db.session.get(Factory, po.factory_id).factory_name
                    if po.factory_id else ""
                ),
                "quantity": str(po.quantity),
                "planned_quantity": str(rel.planned_quantity),
                "batch_no": po.batch_no,
                "status": po.status,
            })

    return render_template(
        "production_plan/form.html",
        mode="edit",
        plan=plan,
        performance=perf,
        warehouses=_fg_warehouses(),
        factories=_available_factories(),
        stock_rows=_stock_rows_for_performance(plan.performance_id),
        selected_reservations=selected_reservations,
        existing_process_orders=existing_process_orders,
        form_data={
            "performance_id": plan.performance_id,
            "total_quantity": str(plan.total_quantity),
            "process_quantity": str(plan.process_quantity),
            "remark": plan.remark or "",
        },
        errors=[],
    )


@bp.post("/<int:plan_id>/edit")
def update_submit(plan_id):
    plan = db.session.get(ProductionPlan, plan_id)
    if not plan:
        abort(404)
    try:
        data = _parse_form(request.form)
        ProductionPlanService.update_plan(plan_id=plan_id, **data)
        flash("生产计划已更新", "success")
        return redirect(url_for("production_plan.detail_page", plan_id=plan_id))
    except ProductionPlanError as e:
        return _render_error(plan.performance_id, str(e),
                             plan_id=plan_id), 400


# ==================== 表单解析 ====================
def _parse_form(form):
    """
    解析表单 → dict：
    {
      total_quantity, process_quantity, remark,
      reservations: [{warehouse_id, batch_id, lock_qty}],
      process_orders: [{factory_id, quantity, batch_no(可选)}]
    }
    """
    try:
        total_qty = str(form.get("total_quantity", "0")).strip()
        process_qty = str(form.get("process_quantity", "0")).strip()
    except Exception:
        raise ProductionPlanError("数量格式错误")

    # 库存锁定
    reservations = []
    for idx in range(200):   # 上限 200 行，超过加 BIGINT 校验即可
        wh = form.get(f"stock[{idx}][warehouse_id]", "").strip()
        batch = form.get(f"stock[{idx}][batch_id]", "").strip()
        qty = form.get(f"stock[{idx}][lock_qty]", "").strip()
        if not wh or not batch:
            continue
        if qty and float(qty) > 0:
            reservations.append({
                "warehouse_id": int(wh),
                "batch_id": int(batch),
                "lock_qty": qty,
            })

    # 工艺单
    process_orders = []
    for idx in range(200):
        fac = form.get(f"po[{idx}][factory_id]", "").strip()
        qty = form.get(f"po[{idx}][quantity]", "").strip()
        batch_no = form.get(f"po[{idx}][batch_no]", "").strip() or None
        po_id = form.get(f"po[{idx}][process_order_id]", "").strip()
        if not fac and not qty:
            continue
        if not fac:
            raise ProductionPlanError(f"第 {idx + 1} 行工艺单未选择加工厂")
        if not qty or float(qty) <= 0:
            raise ProductionPlanError(f"第 {idx + 1} 行工艺单生产数量必须 > 0")
        process_orders.append({
            "process_order_id": int(po_id) if po_id else None,
            "factory_id": int(fac),
            "quantity": qty,
            "batch_no": batch_no,
        })

    return {
        "total_quantity": total_qty,
        "process_quantity": process_qty,
        "remark": form.get("remark", "").strip() or None,
        "reservations": reservations,
        "process_orders": process_orders,
    }


def _render_error(performance_id, msg, plan_id=None):
    plan = db.session.get(ProductionPlan, plan_id) if plan_id else None
    perf = db.session.get(SalesPerformance, performance_id)
    if not perf:
        abort(404)

    # 回显表单（简化处理：重新读取 request.form）
    fd = {
        "performance_id": performance_id,
        "total_quantity": request.form.get("total_quantity", "0"),
        "process_quantity": request.form.get("process_quantity", "0"),
        "remark": request.form.get("remark", ""),
    }
    # 库存锁定回显
    selected_reservations = {}
    for idx in range(200):
        wh = request.form.get(f"stock[{idx}][warehouse_id]", "").strip()
        batch = request.form.get(f"stock[{idx}][batch_id]", "").strip()
        qty = request.form.get(f"stock[{idx}][lock_qty]", "").strip()
        if wh and batch and qty:
            selected_reservations[(int(wh), int(batch))] = qty
    # 工艺单回显
    existing = []
    for idx in range(200):
        fac = request.form.get(f"po[{idx}][factory_id]", "").strip()
        if not fac:
            continue
        existing.append({
            "relation_id": None,
            "process_order_id": request.form.get(
                f"po[{idx}][process_order_id]", "").strip() or None,
            "process_order_no": "",
            "factory_id": fac,
            "quantity": request.form.get(f"po[{idx}][quantity]", ""),
            "batch_no": request.form.get(f"po[{idx}][batch_no]", ""),
            "status": "",
        })

    return render_template(
        "production_plan/form.html",
        mode="edit" if plan_id else "create",
        plan=plan,
        performance=perf,
        warehouses=_fg_warehouses(),
        factories=_available_factories(),
        stock_rows=_stock_rows_for_performance(performance_id),
        selected_reservations=selected_reservations,
        existing_process_orders=existing,
        form_data=fd,
        errors=[msg],
    )

PLAN_STATUS_LABELS = {
    "DRAFT": "草稿",
    "CONFIRMED": "已确认",
    "IN_PROGRESS": "进行中",
    "COMPLETED": "已完成",
    "CANCELLED": "已取消",
}

PO_STATUS_LABELS = {
    "DRAFT": "草稿",
    "ORDERED": "已下单",
    "PRODUCING": "生产中",
    "COMPLETED": "已完工",
    "RECEIVED": "已入库",
    "CANCELLED": "已取消",
}

RES_STATUS_LABELS = {
    "ACTIVE": "锁定中",
    "RELEASED": "已释放",
    "CONSUMED": "已消耗",
}


# ==================== 列表 ====================
@bp.get("/")
def list_page():
    keyword = request.args.get("keyword", "").strip() or None
    status = request.args.get("status", "").strip() or None
    perf_raw = request.args.get("performance_id", "").strip()
    performance_id = int(perf_raw) if perf_raw.isdigit() else None
    page = max(int(request.args.get("page", 1) or 1), 1)
    page_size = 20

    rows, total = ProductionPlanService.list_plans(
        keyword=keyword, status=status, performance_id=performance_id,
        page=page, page_size=page_size,
    )
    total_pages = max((total + page_size - 1) // page_size, 1)

    # 回显业绩单信息（若按业绩单过滤）
    perf_filter = None
    if performance_id:
        perf_filter = db.session.get(SalesPerformance, performance_id)

    return render_template(
        "production_plan/list.html",
        rows=rows, total=total, page=page,
        page_size=page_size, total_pages=total_pages,
        keyword=keyword or "",
        status=status or "",
        performance_id=performance_id or "",
        perf_filter=perf_filter,
        status_labels=PLAN_STATUS_LABELS,
    )


# ==================== 详情 ====================
@bp.get("/<int:plan_id>")
def detail_page(plan_id):
    detail = ProductionPlanService.get_detail(plan_id)
    if not detail:
        abort(404)

    return render_template(
        "production_plan/detail.html",
        plan=detail["plan"],
        performance=detail["performance"],
        creator=detail["creator"],
        reservations=detail["reservations"],
        process_orders=detail["process_orders"],
        locked_total=detail["locked_total"],
        status_labels=PLAN_STATUS_LABELS,
        po_status_labels=PO_STATUS_LABELS,
        res_status_labels=RES_STATUS_LABELS,
    )