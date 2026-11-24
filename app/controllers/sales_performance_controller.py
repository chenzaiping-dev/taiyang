from datetime import datetime
from sqlalchemy import text
from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort, session
)
from app.extensions import db
from app.services.draft_delete_service import DraftDeleteService, DraftDeleteError
from app.generated.models.employees import Employee
from app.generated.models.sales_performance import SalesPerformance
from app.generated.models.production_plans import ProductionPlan
from app.generated.models.shipment_plans import ShipmentPlan
from app.services.sales_performance_service import (
    SalesPerformanceService, SalesPerformanceError, STATUS_LABELS, PAYMENT_LABELS,
)
from app.utils.datetime_utils import util_parse_date
from app.exports.queries import run_query
from app.services.permission_service import (
    can_view_sales_performance_money, can_view_sales_performance_cost,
    can_edit_sales_performance_money, can_edit_sales_performance_cost, has_role
)
from app.services.sales_performance_access_service import can_access_sales_performance

bp = Blueprint("sales_performance", __name__, url_prefix="/sales-performances")

def _parse_form(form):
    def fnum(k):
        v = form.get(k, "").strip()
        return v if v else "0"

    def fint(k):
        v = form.get(k, "").strip()
        return int(v) if v.isdigit() else None

    return {
        # ⭐ 不再读取 performance_no
        "sales_order_no": form.get("sales_order_no", "").strip() or None,
        "performance_date": util_parse_date(form.get("performance_date", "")),
        "company_name": form.get("company_name", "").strip(),
        "product_name": form.get("product_name", "").strip(),
        "product_specification":
            form.get("product_specification", "").strip() or None,
        "unit": form.get("unit", "KG").strip() or "KG",
        "unit_price": fnum("unit_price"),
        "quantity": fnum("quantity"),
        "amount": fnum("amount"),
        "payment_status": form.get("payment_status", "UNPAID"),
        "cost": fnum("cost"),
        "profit": fnum("profit"),
        "main_salesperson_id": fint("main_salesperson_id"),
        "is_changzhou_factory": (
            form.get("is_changzhou_factory", "").strip().lower()
            in {"1", "on", "true", "yes"}
        ),
        "is_joint_sales": (
            form.get("is_joint_sales", "").strip().lower()
            in {"1", "on", "true", "yes"}
        ),
        "co_salesperson_id": fint("co_salesperson_id"),
        "expected_delivery_date":
            util_parse_date(form.get("expected_delivery_date", "")),
        "status": form.get("status", "DRAFT"),
        "remark": form.get("remark", "").strip() or None,
    }


def _employees():
    return (Employee.query.filter_by(status=1)
            .order_by(Employee.real_name.asc()).all())

def _sales_employees():
    """只返回拥有 sales 角色的在职员工。"""
    sales_ids = db.session.execute(text("""
        SELECT DISTINCT e.id
          FROM employees e
          JOIN employee_roles er ON er.employee_id = e.id
          JOIN roles r ON r.id = er.role_id
         WHERE e.status = 1
           AND r.status = 1
           AND r.role_code = 'sales'
         ORDER BY e.id
    """)).scalars().all()

    if not sales_ids:
        return []

    return (
        Employee.query
        .filter(Employee.id.in_(sales_ids))
        .order_by(Employee.real_name.asc())
        .all()
    )


def _default_form_data():
    return {
        "performance_no": "",    # 新建时为空，前端显示占位提示
        "sales_order_no": "",
        "performance_date": datetime.now().strftime("%Y-%m-%d"),
        "company_name": "",
        "product_name": "",
        "product_specification": "",
        "unit": "KG",
        "unit_price": "0",
        "quantity": "0",
        "amount": "0",
        "payment_status": "UNPAID",
        "cost": "0",
        "profit": "0",
        "main_salesperson_id": "",
        "is_changzhou_factory": False,
        "is_joint_sales": False,
        "co_salesperson_id": "",
        "expected_delivery_date": "",
        "status": "DRAFT",
        "remark": "",
    }


# ==================== 列表 ====================
@bp.get("/")
def list_page():
    performance_no = request.args.get("performance_no", "").strip() or None
    company_name = request.args.get("company_name", "").strip() or None
    product_name = request.args.get("product_name", "").strip() or None
    status = request.args.get("status", "").strip() or None
    payment_status = request.args.get("payment_status", "").strip() or None
    date_from_raw = request.args.get("date_from", "").strip() or None
    date_to_raw = request.args.get("date_to", "").strip() or None
    sp_raw = request.args.get("salesperson_id", "").strip()
    salesperson_id = int(sp_raw) if sp_raw.isdigit() else None
    doc_type = request.args.get("doc_type", "").strip() or None

    page = max(int(request.args.get("page", 1) or 1), 1)
    page_size = 20

    rows, total, summary = SalesPerformanceService.list_performances(
        params=request.args, page=page, page_size=page_size, include_summary=True
    )
    can_view_money = can_view_sales_performance_money()
    can_view_cost = can_view_sales_performance_cost()
    if not can_view_money:
        summary["amount"] = None
        summary["attributed_amount"] = None
    if not can_view_cost:
        summary["cost"] = None
        summary["profit"] = None
    total_pages = max((total + page_size - 1) // page_size, 1)

    return render_template(
        "sales_performance/list.html",
        rows=rows, total=total, page=page,
        page_size=page_size, total_pages=total_pages,
        performance_no=performance_no or "",
        company_name=company_name or "",
        product_name=product_name or "",
        status=status or "",
        payment_status=payment_status or "",
        date_from=date_from_raw or "",
        date_to=date_to_raw or "",
        salesperson_id=salesperson_id or "",
        doc_type=doc_type or "",
        employees=_sales_employees(),          # ⭐ 销售员下拉用
        status_labels=STATUS_LABELS,
        payment_labels=PAYMENT_LABELS,
        is_sales_role=has_role("sales"),
        summary=summary,
        can_view_summary_money=can_view_money,
        can_view_summary_cost=can_view_cost,
    )


# ==================== 新建 ====================
@bp.get("/new")
def new_page():
    return render_template(
        "sales_performance/form.html",
        mode="create",
        perf=None,
        employees=_employees(),
        sales_employees=_sales_employees(),
        form_data=_default_form_data(),
        status_labels=STATUS_LABELS,
        payment_labels=PAYMENT_LABELS,
        errors=[],
        employee_id=session.get("employee_id"),
    )


@bp.post("/new")
def create_submit():
    try:
        data = _parse_form(request.form)
        if not can_edit_sales_performance_money():
            data.update({"unit_price": "0", "amount": "0", "cost": "0", "profit": "0"})
        perf = SalesPerformanceService.create(data, created_by=session.get("employee_id"))
        flash(f"业绩单创建成功: {perf.performance_no}", "success")
        return redirect(url_for("sales_performance.detail_page",
                                perf_id=perf.id))
    except SalesPerformanceError as e:
        return render_template(
            "sales_performance/form.html",
            mode="create",
            perf=None,
            employees=_employees(),
            sales_employees=_sales_employees(),
            form_data={**_default_form_data(), **{
                k: request.form.get(k, "")
                for k in _default_form_data()
            }},
            status_labels=STATUS_LABELS,
            payment_labels=PAYMENT_LABELS,
            errors=[str(e)],
        ), 400


# ==================== 编辑 ====================
@bp.get("/<int:perf_id>/edit")
def edit_page(perf_id):
    perf = db.session.get(SalesPerformance, perf_id)
    if not perf:
        abort(404)
    if perf.return_of_performance_id is not None:
        return redirect(url_for("sales_performance.edit_return_page", return_id=perf_id))
    if not can_access_sales_performance(perf_id):
        abort(404)

    form_data = {
        "performance_no": perf.performance_no,
        "sales_order_no": perf.sales_order_no or "",
        "performance_date": (
            perf.performance_date.strftime("%Y-%m-%d")
            if perf.performance_date else ""
        ),
        "company_name": perf.company_name or "",
        "product_name": perf.product_name or "",
        "product_specification": perf.product_specification or "",
        "unit": perf.unit or "KG",
        "unit_price": str(perf.unit_price or 0),
        "quantity": str(perf.quantity or 0),
        "amount": str(perf.amount or 0),
        "payment_status": perf.payment_status or "UNPAID",
        "cost": str(perf.cost or 0),
        "profit": str(perf.profit or 0),
        "main_salesperson_id": perf.main_salesperson_id or "",
        "is_changzhou_factory": bool(perf.is_changzhou_factory),
        "is_joint_sales": bool(perf.is_joint_sales),
        "co_salesperson_id": perf.co_salesperson_id or "",
        "expected_delivery_date": (
            perf.expected_delivery_date.strftime("%Y-%m-%d")
            if perf.expected_delivery_date else ""
        ),
        "status": perf.status or "DRAFT",
        "remark": perf.remark or "",
    }

    return render_template(
        "sales_performance/form.html",
        mode="edit",
        perf=perf,
        employees=_employees(),
        sales_employees=_sales_employees(),
        form_data=form_data,
        status_labels=STATUS_LABELS,
        payment_labels=PAYMENT_LABELS,
        errors=[],
        employee_id=session.get("employee_id"),
    )


@bp.post("/<int:perf_id>/edit")
def update_submit(perf_id):
    perf = db.session.get(SalesPerformance, perf_id)
    if not perf:
        abort(404)
    if perf.return_of_performance_id is not None:
        abort(400)
    if not can_access_sales_performance(perf_id):
        abort(404)

    try:
        data = _parse_form(request.form)
        if not can_edit_sales_performance_money():
            # 服务端保留原金额，绝不信任无权限用户提交的隐藏/伪造字段。
            data.update({
                "unit_price": str(perf.unit_price or 0),
                "amount": str(perf.amount or 0),
                "cost": str(perf.cost or 0),
                "profit": str(perf.profit or 0),
            })
        SalesPerformanceService.update(perf_id, data, updated_by=session.get("employee_id"))
        flash("业绩单已更新", "success")
        return redirect(url_for("sales_performance.detail_page",
                                perf_id=perf_id))
    except SalesPerformanceError as e:
        return render_template(
            "sales_performance/form.html",
            mode="edit",
            perf=perf,
            employees=_employees(),
            sales_employees=_sales_employees(),
            form_data={**_default_form_data(), **{
                k: request.form.get(k, "")
                for k in _default_form_data()
            }},
            status_labels=STATUS_LABELS,
            payment_labels=PAYMENT_LABELS,
            errors=[str(e)],
        ), 400


# ==================== 详情（简版，可后续扩展） ====================
@bp.get("/<int:perf_id>")
def detail_page(perf_id):
    perf = db.session.get(SalesPerformance, perf_id)
    if not perf:
        abort(404)
    if not can_access_sales_performance(perf_id):
        abort(404)
    main_sales = (db.session.get(Employee, perf.main_salesperson_id)
                  if perf.main_salesperson_id else None)
    co_sales = (db.session.get(Employee, perf.co_salesperson_id)
                if perf.co_salesperson_id else None)

    production_plan = ProductionPlan.query.filter_by(performance_id=perf_id).first()
    shipment_plan = ShipmentPlan.query.filter_by(performance_id=perf_id).first()
    return_context = None
    original_performance = None
    if perf.return_of_performance_id:
        original_performance = db.session.get(SalesPerformance, perf.return_of_performance_id)
    else:
        return_context = SalesPerformanceService.get_return_context(perf_id)

    return render_template(
        "sales_performance/detail.html",
        perf=perf,
        main_sales=main_sales,
        co_sales=co_sales,
        status_labels=STATUS_LABELS,
        payment_labels=PAYMENT_LABELS,
        production_plan=production_plan,
        shipment_plan=shipment_plan,
        return_context=return_context,
        original_performance=original_performance,
        can_view_money=can_view_sales_performance_money(),
        can_view_cost=can_view_sales_performance_cost(),
    )

@bp.get("/<int:perf_id>/returns/new")
def new_return_page(perf_id):
    original = db.session.get(SalesPerformance, perf_id)
    if not original:
        abort(404)
    if not can_access_sales_performance(perf_id):
        abort(404)
    if original.return_of_performance_id is not None:
        abort(400)
    if original.status not in SalesPerformanceService.RETURNABLE_ORIGINAL_STATUSES:
        flash("原销售单必须已确认或已进入生产/发货/完成状态才能退货", "error")
        return redirect(url_for("sales_performance.detail_page", perf_id=perf_id))
    context = SalesPerformanceService.get_return_context(perf_id)
    return render_template(
        "sales_performance/return_form.html", mode="create", ret=None,
        original=original, context=context,
        form_data={"performance_date": datetime.now().strftime("%Y-%m-%d"),
                   "quantity": "", "unit_price": str(original.unit_price or 0),
                   "amount": "", "cost": "", "remark": ""},
        can_edit_money=can_edit_sales_performance_money(),
        can_edit_cost=can_edit_sales_performance_cost(),
        can_view_money=can_view_sales_performance_money(),
        can_view_cost=can_view_sales_performance_cost(), errors=[],
    )


@bp.post("/<int:perf_id>/returns/new")
def create_return_submit(perf_id):
    original = db.session.get(SalesPerformance, perf_id)
    if not original or not can_access_sales_performance(perf_id):
        abort(404)
    data = {
        "performance_date": util_parse_date(request.form.get("performance_date", "")),
        "quantity": request.form.get("quantity", "0"),
        "unit_price": request.form.get("unit_price", "0"),
        "amount": request.form.get("amount", "0"),
        "cost": request.form.get("cost", "0"),
        "remark": request.form.get("remark", ""),
    }
    try:
        ret = SalesPerformanceService.create_return(
            perf_id, data, created_by=session.get("employee_id"),
            can_edit_money=can_edit_sales_performance_money(),
            can_edit_cost=can_edit_sales_performance_cost(),
        )
        flash(f"退货单创建成功：{ret.performance_no}", "success")
        return redirect(url_for("sales_performance.detail_page", perf_id=ret.id))
    except SalesPerformanceError as exc:
        context = SalesPerformanceService.get_return_context(perf_id)
        return render_template(
            "sales_performance/return_form.html", mode="create", ret=None,
            original=original, context=context, form_data=data,
            can_edit_money=can_edit_sales_performance_money(),
            can_edit_cost=can_edit_sales_performance_cost(),
            can_view_money=can_view_sales_performance_money(),
            can_view_cost=can_view_sales_performance_cost(), errors=[str(exc)],
        ), 400


@bp.get("/returns/<int:return_id>/edit")
def edit_return_page(return_id):
    ret = db.session.get(SalesPerformance, return_id)
    if not ret or ret.return_of_performance_id is None:
        abort(404)
    if not can_access_sales_performance(return_id):
        abort(404)
    if ret.status != "DRAFT":
        flash("只有草稿退货单可以编辑", "error")
        return redirect(url_for("sales_performance.detail_page", perf_id=return_id))
    original = db.session.get(SalesPerformance, ret.return_of_performance_id)
    context = SalesPerformanceService.get_return_context(original.id)
    return render_template(
        "sales_performance/return_form.html", mode="edit", ret=ret,
        original=original, context=context,
        form_data={"performance_date": ret.performance_date.strftime("%Y-%m-%d"),
                   "quantity": str(abs(ret.quantity)), "unit_price": str(ret.unit_price),
                   "amount": str(ret.amount), "cost": str(ret.cost),
                   "remark": ret.remark or ""},
        can_edit_money=can_edit_sales_performance_money(),
        can_edit_cost=can_edit_sales_performance_cost(),
        can_view_money=can_view_sales_performance_money(),
        can_view_cost=can_view_sales_performance_cost(), errors=[],
    )


@bp.post("/returns/<int:return_id>/edit")
def update_return_submit(return_id):
    ret = db.session.get(SalesPerformance, return_id)
    if not ret or ret.return_of_performance_id is None:
        abort(404)
    if not can_access_sales_performance(return_id):
        abort(404)
    data = {
        "performance_date": util_parse_date(request.form.get("performance_date", "")),
        "quantity": request.form.get("quantity", "0"),
        "unit_price": request.form.get("unit_price", "0"),
        "amount": request.form.get("amount", "0"),
        "cost": request.form.get("cost", "0"),
        "remark": request.form.get("remark", ""),
    }
    try:
        SalesPerformanceService.update_return(
            return_id, data, updated_by=session.get("employee_id"),
            can_edit_money=can_edit_sales_performance_money(),
            can_edit_cost=can_edit_sales_performance_cost(),
        )
        flash("退货单已更新", "success")
        return redirect(url_for("sales_performance.detail_page", perf_id=return_id))
    except SalesPerformanceError as exc:
        original = db.session.get(SalesPerformance, ret.return_of_performance_id)
        context = SalesPerformanceService.get_return_context(original.id)
        return render_template(
            "sales_performance/return_form.html", mode="edit", ret=ret,
            original=original, context=context, form_data=data,
            can_edit_money=can_edit_sales_performance_money(),
            can_edit_cost=can_edit_sales_performance_cost(),
            can_view_money=can_view_sales_performance_money(),
            can_view_cost=can_view_sales_performance_cost(), errors=[str(exc)],
        ), 400


@bp.post("/<int:perf_id>/<action>")
def change_status(perf_id, action):
    # {'confirm', 'produce', 'ship', 'complete', 'cancel'}
    if action not in ("confirm", "produce", "ship", "complete", "cancel"):
        abort(404)
    if not can_access_sales_performance(perf_id):
        abort(404)
    try:
        perf = SalesPerformanceService.change_status(
            perf_id, action, operator_id=session.get("employee_id"),
        )
        labels = {
            "confirm": "已确认",
            "produce": "已生产",
            "ship": "已发货",
            "complete": "已完成",
            "cancel": "已取消",
        }
        flash(labels.get(action, "操作成功"), "success")

        # ⭐ 成本缺失提示
        if getattr(perf, "_cost_warning", None):
            flash(perf._cost_warning, "error")
    except SalesPerformanceError as e:
        flash(str(e), "error")
    return redirect(request.referrer or url_for("sales_performance.list_page"))

@bp.post("/<int:perf_id>/delete")
def delete_draft(perf_id):
    if not can_access_sales_performance(perf_id):
        abort(404)
    try:
        DraftDeleteService.delete_sales_performance(perf_id)
        flash("业绩表草稿及其关联的草稿计划已删除", "success")
    except DraftDeleteError as e:
        db.session.rollback()
        flash(str(e), "error")
    except Exception:
        db.session.rollback()
        flash("删除失败：存在未满足的关联约束或数据库异常，请检查关联业务单据", "error")
    return redirect(url_for("sales_performance.list_page"))


@bp.post("/<int:perf_id>/reopen")
def reopen(perf_id):
    if not can_access_sales_performance(perf_id):
        abort(404)
    try:
        SalesPerformanceService.reopen(perf_id, operator_id=session.get("employee_id"))
        flash("业绩单已重新打开为草稿，可继续编辑", "success")
    except SalesPerformanceError as e:
        flash(str(e), "error")
    return redirect(request.referrer or url_for("sales_performance.list_page"))