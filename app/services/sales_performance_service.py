from decimal import Decimal
from datetime import date, datetime
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select, case, func, or_, exists
from flask import flash

from app.extensions import db
from app.generated.models.employees import Employee
from app.generated.models.sales_performance import SalesPerformance
from app.generated.models.production_plans import ProductionPlan
from app.generated.models.shipment_plans import ShipmentPlan
from app.services.production_plan_service import PLAN_STATUS_LABELS
from app.utils.datetime_utils import now_beijing_naive
from app.utils.decimal_utils import _q3, _q6

PAYMENT_LABELS = {"UNPAID": "未收款", "PAID": "已收款"}

STATUS_ACTIONS = {
    "confirm": {
        "from": {"DRAFT"},
        "to": "CONFIRMED",
        "label": "确认",
    },
    "produce": {
        "from": {"CONFIRMED"},
        "to": "PRODUCED",
        "label": "生产",
    },
    "ship": {
        "from": {"PRODUCED", "CONFIRMED"},
        "to": "SHIPPED",
        "label": "发货",
    },
    "complete": {
        "from": {"SHIPPED"},
        "to": "COMPLETED",
        "label": "完成",
    },
    "cancel": {
        "from": {"DRAFT", "CONFIRMED", "PRODUCED", "SHIPPED", "COMPLETED"},
        "to": "CANCELLED",
        "label": "取消",
    },
}

STATUS_LABELS = {
    "DRAFT": "草稿",
    "CONFIRMED": "已确认",
    "PRODUCED": "已生产",
    "SHIPPED": "已发货",
    "COMPLETED": "已完成",
    "CANCELLED": "已取消",
}

class SalesPerformanceError(Exception):
    pass


class SalesPerformanceService:

    @staticmethod
    def _normalize_and_validate(data: dict, is_create: bool = True) -> dict:
        # 必填项（去掉 performance_no）
        if not data.get("company_name"):
            raise SalesPerformanceError("公司名称必填")
        if not data.get("product_name"):
            raise SalesPerformanceError("产品名称必填")
        if not data.get("performance_date"):
            raise SalesPerformanceError("日期必填")

        status = data.get("status") or "DRAFT"
        if status not in STATUS_LABELS:
            raise SalesPerformanceError(f"非法状态: {status}")

        payment = data.get("payment_status") or "UNPAID"
        if payment not in PAYMENT_LABELS:
            raise SalesPerformanceError(f"非法收款状态: {payment}")

        try:
            unit_price = _q6(data.get("unit_price") or 0)
            quantity   = _q6(data.get("quantity") or 0)
            amount     = _q6(data.get("amount") or 0)
            cost       = _q6(data.get("cost") or 0)
            profit     = _q6(data.get("profit") or 0)
        except Exception:
            raise SalesPerformanceError("金额/数量字段格式错误")

        if unit_price < 0:
            raise SalesPerformanceError("销售单价不能为负")
        if quantity <= 0:
            raise SalesPerformanceError("销售数量必须 > 0")
        if amount < 0 or cost < 0:
            raise SalesPerformanceError("金额/成本不能为负")

        if data.get("main_salesperson_id") and not db.session.get(
                Employee, int(data["main_salesperson_id"])):
            raise SalesPerformanceError("主销售员不存在")
        if data.get("co_salesperson_id") and not db.session.get(
                Employee, int(data["co_salesperson_id"])):
            raise SalesPerformanceError("共同销售员不存在")

        is_joint = 1 if data.get("is_joint_sales") else 0
        co_id = data.get("co_salesperson_id")
        if is_joint and not co_id:
            raise SalesPerformanceError("共同销售时必选共同销售员")
        if not is_joint:
            co_id = None

        result = {
            "sales_order_no": (data.get("sales_order_no") or "").strip() or None,
            "performance_date": data["performance_date"],
            "company_name": data["company_name"].strip(),
            "product_name": data["product_name"].strip(),
            "product_specification":
                (data.get("product_specification") or "").strip() or None,
            "unit": data.get("unit") or "KG",
            "unit_price": unit_price,
            "quantity": quantity,
            "amount": amount,
            "payment_status": payment,
            "cost": cost,
            "profit": profit,
            "main_salesperson_id": (int(data["main_salesperson_id"])
                                    if data.get("main_salesperson_id") else None),
            "is_changzhou_factory": 1 if data.get("is_changzhou_factory") else 0,
            "is_joint_sales": is_joint,
            "co_salesperson_id": (int(co_id) if co_id else None),
            "expected_delivery_date": data.get("expected_delivery_date"),
            "status": status,
            "remark": (data.get("remark") or "").strip() or None,
        }

        # ⭐ 仅编辑时（is_create=False）保留原编号校验；新建不接收编号
        if not is_create:
            # 可选：仍保留 performance_no 只读校验，避免误改
            pass

        return result

    # ---------- 编号自动生成 ----------
    @staticmethod
    def generate_performance_no(prefix: str = "YJB", when: date = None) -> str:
        """
        生成格式：PREFIX + YYYYMMDD + NNNN（无分隔符），例如 YJB202610300001
        取当日已有最大流水号 + 1
        """
        d = when or date.today()
        day = d.strftime("%Y%m%d")
        head = f"{prefix}{day}"          # 例如 "YJB20261030"
        like_pattern = f"{head}%"        # 与真实格式一致，无分隔符

        # 查询当日最大编号
        row = db.session.execute(
            select(func.max(SalesPerformance.performance_no))
            .where(SalesPerformance.performance_no.like(like_pattern))
        ).scalar()

        seq = 1
        if row:
            try:
                # ⭐ 关键改动：截掉前缀+日期，剩下的就是流水号
                tail = row[len(head):] if row.startswith(head) else ""
                seq = int(tail) + 1 if tail.isdigit() else 1
            except (ValueError, IndexError):
                seq = 1

        return f"{head}{seq:04d}"

    # ---------- 新建 ----------
    @staticmethod
    def create(data: dict, created_by: int = None) -> SalesPerformance:
        n = SalesPerformanceService._normalize_and_validate(data)

        # ⭐ 忽略表单传入的 performance_no，一律后端生成
        n.pop("performance_no", None)

        # 生成编号，若并发冲突则重试（最多 5 次）
        last_err = None
        for _ in range(5):
            n["performance_no"] = (
                SalesPerformanceService.generate_performance_no()
            )
            perf = SalesPerformance(
                **n,
                created_by=created_by,
                updated_by=created_by,
                created_at=now_beijing_naive(),
                updated_at=now_beijing_naive(),
            )
            db.session.add(perf)
            try:
                db.session.commit()
                return perf
            except IntegrityError as e:
                db.session.rollback()
                last_err = e
                # 唯一约束冲突 → 重新生成编号再试
                continue

        raise SalesPerformanceError(
            f"生成业绩单号失败，请重试：{last_err.orig if last_err else ''}"
        )

        if SalesPerformance.query.filter_by(
                performance_no=n["performance_no"]).first():
            raise SalesPerformanceError(
                f"业绩单编号已存在: {n['performance_no']}")
        doc_time = now_beijing_naive()
        perf = SalesPerformance(
            **n,
            created_by=created_by,
            updated_by=created_by,
            created_at=doc_time,
            updated_at=doc_time,
        )
        db.session.add(perf)
        try:
            db.session.commit()
        except IntegrityError as e:
            db.session.rollback()
            raise SalesPerformanceError(f"保存失败，唯一约束冲突: {e.orig}")
        except Exception as e:
            db.session.rollback()
            raise SalesPerformanceError(f"保存失败: {e}")
        return perf

    # ---------- 更新 ----------
    @staticmethod
    def update(perf_id: int, data: dict, updated_by: int = None) -> SalesPerformance:
        perf = db.session.get(SalesPerformance, perf_id)
        if not perf:
            raise SalesPerformanceError("业绩单不存在")

        n = SalesPerformanceService._normalize_and_validate(
            data, is_create=False
        )

        # ⭐ 编辑时强制保留原编号
        n.pop("performance_no", None)

        for k, v in n.items():
            setattr(perf, k, v)
        perf.updated_by = updated_by
        perf.updated_at = now_beijing_naive()
        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            raise SalesPerformanceError(f"更新失败: {e}")
        return perf

    # ---------- 列表 ----------
    @staticmethod
    def list_performances(keyword=None, status=None, payment_status=None,
                          date_from=None, date_to=None, salesperson_id=None,
                          page=1, page_size=20):
        creator = db.aliased(Employee)
        sales = db.aliased(Employee)
        co_sales = db.aliased(Employee)

        # ⭐ 发货计划是否存在：相关子查询（避免主查询出现多行）
        has_shipment_sq = (
            exists()
            .where(ShipmentPlan.performance_id == SalesPerformance.id)
        )

        base = (
            select(
                SalesPerformance.id,
                SalesPerformance.performance_no,
                SalesPerformance.company_name,
                SalesPerformance.product_name,
                SalesPerformance.product_specification,
                SalesPerformance.unit,
                SalesPerformance.quantity,
                SalesPerformance.unit_price,
                SalesPerformance.amount,
                SalesPerformance.payment_status,
                SalesPerformance.cost,
                SalesPerformance.profit,
                SalesPerformance.is_joint_sales,
                SalesPerformance.is_changzhou_factory,
                SalesPerformance.expected_delivery_date,
                SalesPerformance.status,
                SalesPerformance.created_at,
                sales.real_name.label("salesperson_name"),
                co_sales.real_name.label("co_salesperson_name"),
                # ⭐ 生产计划 ID（因 performance_id 唯一，最多一行）
                ProductionPlan.id.label("production_plan_id"),
                # ⭐ 发货计划是否存在
                has_shipment_sq.label("has_shipment_plan"),
            )
            .join(sales, sales.id == SalesPerformance.main_salesperson_id,
                isouter=True)
            .join(co_sales, co_sales.id == SalesPerformance.co_salesperson_id,
                isouter=True)
            .join(ProductionPlan,
                ProductionPlan.performance_id == SalesPerformance.id,
                isouter=True)
            .order_by(SalesPerformance.id.desc())
        )
        if keyword:
            like = f"%{keyword}%"
            base = base.where(or_(
                SalesPerformance.performance_no.like(like),
                SalesPerformance.company_name.like(like),
                SalesPerformance.product_name.like(like),
            ))
        if status:
            base = base.where(SalesPerformance.status == status)
        if payment_status:
            base = base.where(SalesPerformance.payment_status == payment_status)

        # ⭐ 时间跨度（按业绩日期）
        if date_from:
            base = base.where(SalesPerformance.performance_date >= date_from)
        if date_to:
            base = base.where(SalesPerformance.performance_date < date_to)

        # ⭐ 销售员：主/副任一命中
        if salesperson_id:
            base = base.where(or_(
                SalesPerformance.main_salesperson_id == salesperson_id,
                SalesPerformance.co_salesperson_id == salesperson_id,
            ))

        total = db.session.execute(
            select(func.count()).select_from(base.subquery())
        ).scalar() or 0

        base = base.offset((page - 1) * page_size).limit(page_size)
        rows = [dict(r) for r in db.session.execute(base).mappings().all()]
        return rows, total

    @staticmethod
    def change_status(perf_id: int, action: str,
                      operator_id: int = None) -> SalesPerformance:
        """
        统一状态流转入口。
        action ∈ {'confirm', 'produce', 'ship', 'complete', 'cancel'}
        """
        rule = STATUS_ACTIONS.get(action)
        if not rule:
            raise SalesPerformanceError(f"不支持的操作: {action}")

        perf = db.session.get(SalesPerformance, perf_id)
        if not perf:
            raise SalesPerformanceError("业绩单不存在")

        if perf.status not in rule["from"]:
            cur = STATUS_LABELS.get(perf.status, perf.status)
            raise SalesPerformanceError(
                f"当前状态【{cur}】不允许执行【{rule['label']}】"
            )

        # ⭐ 生产入库：额外校验生产计划状态
        if action == "ship" and perf.is_changzhou_factory:
            plan = ProductionPlan.query.filter_by(
                performance_id=perf.id
            ).first()
            if not plan:
                raise SalesPerformanceError(
                    "该业绩单没有关联的生产计划，无法执行发货"
                )
            if plan.status != "COMPLETED":
                plan_label = PLAN_STATUS_LABELS.get(plan.status, plan.status)
                raise SalesPerformanceError(
                    f"生产计划【{plan.plan_no}】状态为【{plan_label}】，"
                    f"必须为【已完成】才能发货"
                )
        # 取消提示，不强制拦截，因为取消业绩单本身可能先于计划清理
        if action == "cancel":
            active_plan = ProductionPlan.query.filter(
                ProductionPlan.performance_id == perf.id,
                ProductionPlan.status.in_(["CONFIRMED", "IN_PROGRESS", "COMPLETED"]),
            ).first()
            if active_plan:
                #raise SalesPerformanceError(
                flash(
                    f"关联生产计划【{active_plan.plan_no}】状态为"
                    f"【{PLAN_STATUS_LABELS.get(active_plan.status, active_plan.status)}】，"
                    f"请先取消生产计划再取消业绩单"
                )
            active_ship = ShipmentPlan.query.filter(
                ShipmentPlan.performance_id == perf.id,
                ShipmentPlan.status.in_(["CONFIRMED", "PARTIALLY_SHIPPED", "SHIPPED"]),
            ).first()
            if active_ship:
                #raise SalesPerformanceError(
                flash(
                    f"存在未取消的发货计划【{active_ship.shipment_plan_no}】，"
                    f"请先取消发货计划再取消业绩单"
                )

        # ============ 状态切换 ============
        old_status = perf.status
        perf.status = rule["to"]
        perf.updated_by = operator_id
        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            raise SalesPerformanceError(f"状态更新失败: {e}")

        return perf

    @staticmethod
    def reopen(perf_id: int, operator_id: int = None) -> SalesPerformance:
        """
        CANCELLED → DRAFT，允许重新编辑并重走流程。
        前置：关联的生产计划/发货计划必须已处于可复用状态，
        否则会造成状态与库存不一致。不新建任何计划。
        """
        perf = db.session.get(SalesPerformance, perf_id)
        if not perf:
            raise SalesPerformanceError("业绩单不存在")
        if perf.status != "CANCELLED":
            raise SalesPerformanceError(
                f"当前状态【{STATUS_LABELS.get(perf.status, perf.status)}】"
                f"不允许重新编辑，仅已取消可重新编辑"
            )

        # 1. 生产计划：只允许不存在，或状态为 CANCELLED / DRAFT
        plan = ProductionPlan.query.filter_by(performance_id=perf.id).first()
        if plan and plan.status not in ("DRAFT", "CANCELLED"):
            raise SalesPerformanceError(
                f"关联生产计划【{plan.plan_no}】状态为"
                f"【{PLAN_STATUS_LABELS.get(plan.status, plan.status)}】，"
                f"请先在计划页取消/处理后再重新编辑业绩单"
            )

        # 2. 发货计划：允许不存在，或全部为 CANCELLED
        active_shipments = (
            ShipmentPlan.query
            .filter(
                ShipmentPlan.performance_id == perf.id,
                ShipmentPlan.status != "CANCELLED",
            )
            .all()
        )
        if active_shipments:
            nos = "、".join(p.shipment_plan_no for p in active_shipments)
            raise SalesPerformanceError(
                f"存在未取消的发货计划：{nos}，"
                f"请先取消发货计划后再重新编辑业绩单"
            )

        perf.status = "DRAFT"
        perf.updated_by = operator_id
        perf.updated_at = now_beijing_naive()
        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            raise SalesPerformanceError(f"重新编辑失败: {e}")
        return perf