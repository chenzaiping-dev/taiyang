from datetime import datetime
from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort
)
from app.extensions import db
from app.generated.models.employees import Employee
from app.generated.models.sales_performance import SalesPerformance


from app.services.sales_performance_service import (
    SalesPerformanceService, SalesPerformanceError,
)

bp = Blueprint("sales_performance", __name__,
               url_prefix="/sales-performances")

STATUS_LABELS = {
    "DRAFT": "草稿",
    "SUBMITTED": "已提交",
    "PRODUCTION_PLANNED": "生产计划中",
    "FINISHED_GOODS_STOCKED": "成品已入库",
    "SHIPMENT_SCHEDULED": "发货已排程",
    "COMPLETED": "已完成",
    "CANCELLED": "已取消",
}

PAYMENT_LABELS = {"UNPAID": "未收款", "PAID": "已收款"}


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


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
        "performance_date": _parse_date(form.get("performance_date", "")),
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
        "is_changzhou_factory": form.get("is_changzhou_factory") == "on",
        "is_joint_sales": form.get("is_joint_sales") == "on",
        "co_salesperson_id": fint("co_salesperson_id"),
        "expected_delivery_date":
            _parse_date(form.get("expected_delivery_date", "")),
        "status": form.get("status", "DRAFT"),
        "remark": form.get("remark", "").strip() or None,
    }


def _employees():
    return (Employee.query.filter_by(status=1)
            .order_by(Employee.real_name.asc()).all())


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
    keyword = request.args.get("keyword", "").strip() or None
    status = request.args.get("status", "").strip() or None
    payment_status = request.args.get("payment_status", "").strip() or None
    page = max(int(request.args.get("page", 1) or 1), 1)
    page_size = 20

    rows, total = SalesPerformanceService.list_performances(
        keyword=keyword, status=status,
        payment_status=payment_status,
        page=page, page_size=page_size,
    )
    total_pages = max((total + page_size - 1) // page_size, 1)

    return render_template(
        "sales_performance/list.html",
        rows=rows, total=total, page=page,
        page_size=page_size, total_pages=total_pages,
        keyword=keyword or "",
        status=status or "",
        payment_status=payment_status or "",
        status_labels=STATUS_LABELS,
        payment_labels=PAYMENT_LABELS,
    )


# ==================== 新建 ====================
@bp.get("/new")
def new_page():
    return render_template(
        "sales_performance/form.html",
        mode="create",
        perf=None,
        employees=_employees(),
        form_data=_default_form_data(),
        status_labels=STATUS_LABELS,
        payment_labels=PAYMENT_LABELS,
        errors=[],
    )


@bp.post("/new")
def create_submit():
    try:
        data = _parse_form(request.form)
        perf = SalesPerformanceService.create(data, created_by=1)
        flash(f"业绩单创建成功: {perf.performance_no}", "success")
        return redirect(url_for("sales_performance.detail_page",
                                perf_id=perf.id))
    except SalesPerformanceError as e:
        return render_template(
            "sales_performance/form.html",
            mode="create",
            perf=None,
            employees=_employees(),
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
        form_data=form_data,
        status_labels=STATUS_LABELS,
        payment_labels=PAYMENT_LABELS,
        errors=[],
    )


@bp.post("/<int:perf_id>/edit")
def update_submit(perf_id):
    perf = db.session.get(SalesPerformance, perf_id)
    if not perf:
        abort(404)

    try:
        data = _parse_form(request.form)
        SalesPerformanceService.update(perf_id, data, updated_by=1)
        flash("业绩单已更新", "success")
        return redirect(url_for("sales_performance.detail_page",
                                perf_id=perf_id))
    except SalesPerformanceError as e:
        return render_template(
            "sales_performance/form.html",
            mode="edit",
            perf=perf,
            employees=_employees(),
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
    main_sales = (db.session.get(Employee, perf.main_salesperson_id)
                  if perf.main_salesperson_id else None)
    co_sales = (db.session.get(Employee, perf.co_salesperson_id)
                if perf.co_salesperson_id else None)

    return render_template(
        "sales_performance/detail.html",
        perf=perf,
        main_sales=main_sales,
        co_sales=co_sales,
        status_labels=STATUS_LABELS,
        payment_labels=PAYMENT_LABELS,
    )