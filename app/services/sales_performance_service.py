from decimal import Decimal
from datetime import date, datetime
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select, case, func, or_, exists, cast
from flask import flash

from app.extensions import db
from app.generated.models.employees import Employee
from app.generated.models.sales_performance import SalesPerformance
from app.generated.models.production_plans import ProductionPlan
from app.generated.models.shipment_plans import ShipmentPlan
from app.services.production_plan_service import PLAN_STATUS_LABELS
from app.utils.datetime_utils import now_beijing_naive, SUMMARY_EXCLUDED_STATUSES
from app.utils.decimal_utils import _q3, _q6
from app.utils.datetime_utils import util_parse_date, util_parse_date_end
from app.services.sales_performance_access_service import (
    apply_sales_performance_scope,
)
from app.services.permission_service import has_role

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
        if perf.return_of_performance_id is not None:
            raise SalesPerformanceError("退货单请使用退货单编辑流程")

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

    # ---------- 退货单 ----------
    RETURNABLE_ORIGINAL_STATUSES = {"CONFIRMED", "PRODUCED", "SHIPPED", "COMPLETED"}

    @staticmethod
    def _return_totals(original_id, lock_original=False):
        """只统计已提交退货单；草稿和取消单不占用退货额度。"""
        original = db.session.get(SalesPerformance, original_id)
        if not original:
            raise SalesPerformanceError("原销售业绩单不存在")
        rows = db.session.query(
            func.coalesce(func.sum(SalesPerformance.quantity), 0),
            func.coalesce(func.sum(SalesPerformance.amount), 0),
            func.coalesce(func.sum(SalesPerformance.cost), 0),
        ).filter(
            SalesPerformance.return_of_performance_id == original_id,
            SalesPerformance.status == "CONFIRMED",
        ).one()
        qty_used, amount_used, cost_used = (Decimal(str(v or 0)) for v in rows)
        return {
            "original": original,
            "returned_quantity": qty_used,
            "returned_amount": amount_used,
            "returned_cost": cost_used,
            "available_quantity": Decimal(str(original.quantity or 0)) + qty_used,
            "available_amount": Decimal(str(original.amount or 0)) + amount_used,
            "available_cost": Decimal(str(original.cost or 0)) + cost_used,
        }

    @staticmethod
    def get_return_context(original_id):
        """获取原销售单及退货额度、退货记录。"""
        summary = SalesPerformanceService._return_totals(original_id)
        returns = SalesPerformance.query.filter_by(
            return_of_performance_id=original_id
        ).order_by(SalesPerformance.id.desc()).all()
        return {**summary, "returns": returns}

    @staticmethod
    def _validate_return_values(original, quantity, unit_price, amount, cost,
                                exclude_return_id=None):
        try:
            qty_abs = abs(_q6(quantity))
            price = _q6(unit_price)
            amount_value = _q6(amount)
            cost_value = _q6(cost)
        except Exception:
            raise SalesPerformanceError("退货数量、单价、金额或成本格式错误")

        if qty_abs <= 0:
            raise SalesPerformanceError("退货数量必须大于 0")
        if price < 0:
            raise SalesPerformanceError("退货单价不能为负数")
        if amount_value > 0:
            raise SalesPerformanceError("退货金额必须为负数或 0")
        if cost_value > 0:
            raise SalesPerformanceError("退货成本必须为负数或 0")

        ctx = SalesPerformanceService._return_totals(original.id)
        if exclude_return_id:
            old = db.session.get(SalesPerformance, exclude_return_id)
            if old and old.status == "CONFIRMED":
                ctx["available_quantity"] -= Decimal(str(old.quantity or 0))
                ctx["available_amount"] -= Decimal(str(old.amount or 0))
                ctx["available_cost"] -= Decimal(str(old.cost or 0))

        if qty_abs > ctx["available_quantity"]:
            raise SalesPerformanceError(
                f"退货数量超过剩余可退数量 {ctx['available_quantity']}"
            )
        if abs(amount_value) > max(ctx["available_amount"], Decimal("0")):
            raise SalesPerformanceError(
                f"退款金额超过剩余可冲减金额 {ctx['available_amount']}"
            )
        if abs(cost_value) > max(ctx["available_cost"], Decimal("0")):
            raise SalesPerformanceError(
                f"退货成本超过剩余可冲减成本 {ctx['available_cost']}"
            )
        return -qty_abs, price, amount_value, cost_value

    @staticmethod
    def create_return(original_id, data, created_by=None,
                      can_edit_money=True, can_edit_cost=True):
        """创建退货草稿。数量以正数录入，落库统一转换为负数。"""
        original = SalesPerformance.query.filter_by(id=original_id).with_for_update().first()
        if not original:
            raise SalesPerformanceError("原销售业绩单不存在")
        if original.return_of_performance_id is not None:
            raise SalesPerformanceError("退货单不能再次创建退货单")
        if original.status not in SalesPerformanceService.RETURNABLE_ORIGINAL_STATUSES:
            raise SalesPerformanceError("原销售单必须已确认或已进入生产/发货/完成状态才能退货")

        try:
            qty = abs(_q6(data.get("quantity") or 0))
            unit_price_raw = data.get("unit_price")
            unit_price = _q6(
                unit_price_raw if can_edit_money and unit_price_raw not in (None, "")
                else original.unit_price
            )
            quantity = -qty
            default_amount = (
                Decimal(str(original.amount or 0)) * quantity /
                Decimal(str(original.quantity or 1))
            ) if Decimal(str(original.quantity or 0)) else Decimal("0")
            amount_raw = data.get("amount")
            amount = _q6((amount_raw if amount_raw not in (None, "") else default_amount)
                         if can_edit_money else default_amount)
            if not can_edit_money:
                amount = default_amount
            default_cost = (
                Decimal(str(original.cost or 0)) * quantity /
                Decimal(str(original.quantity or 1))
            ) if Decimal(str(original.quantity or 0)) else Decimal("0")
            cost_raw = data.get("cost")
            cost = _q6((cost_raw if cost_raw not in (None, "") else default_cost)
                       if can_edit_cost else default_cost)
            if not can_edit_cost:
                cost = _q6(default_cost)
        except Exception:
            raise SalesPerformanceError("退货数量或金额格式错误")

        quantity, unit_price, amount, cost = SalesPerformanceService._validate_return_values(
            original, quantity, unit_price, amount, cost
        )
        perf = SalesPerformance(
            performance_no=SalesPerformanceService.generate_performance_no("TH"),
            sales_order_no=original.sales_order_no,
            performance_date=data.get("performance_date") or date.today(),
            company_name=original.company_name,
            product_name=original.product_name,
            product_specification=original.product_specification,
            unit=original.unit,
            unit_price=unit_price,
            quantity=quantity,
            amount=amount,
            payment_status=original.payment_status,
            cost=cost,
            profit=amount - cost,
            main_salesperson_id=original.main_salesperson_id,
            is_changzhou_factory=original.is_changzhou_factory,
            is_joint_sales=original.is_joint_sales,
            co_salesperson_id=original.co_salesperson_id,
            expected_delivery_date=None,
            status="DRAFT",
            remark=(data.get("remark") or "").strip() or None,
            return_of_performance_id=original.id,
            created_by=created_by,
            updated_by=created_by,
            created_at=now_beijing_naive(),
            updated_at=now_beijing_naive(),
        )
        db.session.add(perf)
        try:
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            raise SalesPerformanceError(f"创建退货单失败：{exc}")
        return perf

    @staticmethod
    def update_return(return_id, data, updated_by=None,
                      can_edit_money=True, can_edit_cost=True):
        ret = SalesPerformance.query.filter_by(id=return_id).with_for_update().first()
        if not ret or ret.return_of_performance_id is None:
            raise SalesPerformanceError("退货单不存在")
        if ret.status != "DRAFT":
            raise SalesPerformanceError("只有草稿退货单可以编辑")
        original = SalesPerformance.query.filter_by(
            id=ret.return_of_performance_id
        ).with_for_update().first()
        if not original:
            raise SalesPerformanceError("原销售单不存在")
        if original.status not in SalesPerformanceService.RETURNABLE_ORIGINAL_STATUSES:
            raise SalesPerformanceError("原销售单状态已不允许编辑或提交退货")

        try:
            qty = -abs(_q6(data.get("quantity") or 0))
            unit_price_raw = data.get("unit_price")
            unit_price = _q6(
                unit_price_raw if can_edit_money and unit_price_raw not in (None, "")
                else original.unit_price
            )
            default_amount = (
                Decimal(str(original.amount or 0)) * qty /
                Decimal(str(original.quantity or 1))
            ) if Decimal(str(original.quantity or 0)) else Decimal("0")
            default_cost = (
                Decimal(str(original.cost or 0)) * qty /
                Decimal(str(original.quantity or 1))
            ) if Decimal(str(original.quantity or 0)) else Decimal("0")
            amount = _q6(data.get("amount") if can_edit_money and data.get("amount") not in (None, "")
                         else default_amount)
            cost = _q6(data.get("cost") if can_edit_cost and data.get("cost") not in (None, "")
                       else default_cost)
        except Exception:
            raise SalesPerformanceError("退货数量或金额格式错误")
        qty, unit_price, amount, cost = SalesPerformanceService._validate_return_values(
            original, qty, unit_price, amount, cost
        )
        ret.payment_status = original.payment_status
        ret.performance_date = data.get("performance_date") or ret.performance_date
        ret.quantity = qty
        ret.unit_price = unit_price
        ret.amount = amount
        ret.cost = cost
        ret.profit = amount - cost
        ret.remark = (data.get("remark") or "").strip() or None
        ret.updated_by = updated_by
        ret.updated_at = now_beijing_naive()
        try:
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            raise SalesPerformanceError(f"更新退货单失败：{exc}")
        return ret

    # ---------- 列表 ----------
    @staticmethod
    def list_performances(params=None, page: int = 1, page_size: int = 20,
                          include_summary=False):

        params = params or {}
        performance_no = (params.get("performance_no") or "").strip() or None
        company_name = (params.get("company_name") or "").strip() or None
        product_name = (params.get("product_name") or "").strip() or None
        status = (params.get("status") or "").strip() or None
        payment_status = (params.get("payment_status") or "").strip() or None
        date_from = util_parse_date(params.get("date_from"))
        date_to = util_parse_date_end(params.get("date_to"))
        doc_type = (params.get("doc_type") or "").strip() or None
        # 跟单员可能同时拥有其他角色；只要包含 order_followup 角色，
        # 就不展示业务员尚未提交的草稿业绩单。该规则同时作用于列表和 Excel。
        exclude_drafts = has_role("order_followup")
        try:
            salesperson_id = int(params.get("salesperson_id") or 0) or None
        except (TypeError, ValueError):
            salesperson_id = None

        creator = db.aliased(Employee)
        sales = db.aliased(Employee)
        co_sales = db.aliased(Employee)
        original_perf = db.aliased(SalesPerformance)

        # ⭐ 发货计划是否存在：相关子查询（避免主查询出现多行）
        has_shipment_sq = (
            exists()
            .where(ShipmentPlan.performance_id == SalesPerformance.id)
        )

        base = (
            select(
                SalesPerformance.id,
                SalesPerformance.performance_no,
                SalesPerformance.return_of_performance_id,
                case((SalesPerformance.return_of_performance_id.isnot(None), "RETURN"),
                     else_="SALE").label("document_type"),
                original_perf.performance_no.label("original_performance_no"),
                SalesPerformance.company_name,
                SalesPerformance.product_name,
                SalesPerformance.product_specification,
                SalesPerformance.unit,
                SalesPerformance.quantity,
                case(
                    (
                        SalesPerformance.is_joint_sales == 1,
                        cast(SalesPerformance.quantity / 2, db.Numeric(18, 4)),
                    ),
                    else_=SalesPerformance.quantity,
                ).label("attributed_quantity"),
                SalesPerformance.unit_price,
                SalesPerformance.amount,
                case(
                    (
                        SalesPerformance.is_joint_sales == 1,
                        cast(SalesPerformance.amount / 2, db.Numeric(18, 4)),
                    ),
                    else_=SalesPerformance.amount,
                ).label("attributed_amount"),
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
            .join(original_perf,
                original_perf.id == SalesPerformance.return_of_performance_id,
                isouter=True)
            .join(ProductionPlan,
                ProductionPlan.performance_id == SalesPerformance.id,
                isouter=True)
            .order_by(SalesPerformance.id.desc())
        )
        # 统一应用业绩单数据范围
        base = apply_sales_performance_scope(
            base,
            SalesPerformance,
            permission_code="sales_performance.view",
        )

        if doc_type == "RETURN":
            base = base.where(SalesPerformance.return_of_performance_id.isnot(None))
        elif doc_type == "SALE":
            base = base.where(SalesPerformance.return_of_performance_id.is_(None))

        if performance_no:
            base = base.where(SalesPerformance.performance_no.like(f"%{performance_no}%"))
        if company_name:
            base = base.where(SalesPerformance.company_name.like(f"%{company_name}%"))
        if product_name:
            base = base.where(SalesPerformance.product_name.like(f"%{product_name}%"))
        # 跟单员列表不显示业务员保存但尚未提交的草稿。
        # 放在查询层执行，确保分页总数和实际结果一致。
        if exclude_drafts:
            base = base.where(SalesPerformance.status != "DRAFT")
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

        summary = None        
        if include_summary:
            # 统计使用与列表相同的筛选条件和销售数据范围，排除已取消业绩单。
            summary_sq = base.where(
                SalesPerformance.status.notin_(SUMMARY_EXCLUDED_STATUSES)
            ).order_by(None).subquery()
            summary = db.session.execute(
                select(
                    func.coalesce(func.sum(summary_sq.c.amount), 0).label("amount"),
                    func.coalesce(func.sum(summary_sq.c.cost), 0).label("cost"),
                    func.coalesce(func.sum(summary_sq.c.profit), 0).label("profit"),
                    func.coalesce(func.sum(summary_sq.c.attributed_quantity), 0).label("attributed_quantity"),
                    func.coalesce(func.sum(summary_sq.c.attributed_amount), 0).label("attributed_amount"),
                )
            ).mappings().one()

        total = db.session.execute(
            select(func.count()).select_from(base.subquery())
        ).scalar() or 0

        base = base.offset((page - 1) * page_size).limit(page_size)
        rows = [dict(r) for r in db.session.execute(base).mappings().all()]
        return (rows, total, dict(summary)) if include_summary else (rows, total)

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

        perf = db.session.query(SalesPerformance).filter_by(id=perf_id).with_for_update().first()
        if not perf:
            raise SalesPerformanceError("业绩单不存在")

        if perf.return_of_performance_id is not None:
            if action not in ("confirm", "cancel"):
                raise SalesPerformanceError("退货单只能提交或取消，不能生产、发货或完成")
            if action == "confirm":
                original = SalesPerformance.query.filter_by(
                    id=perf.return_of_performance_id
                ).with_for_update().first()
                if not original or original.status not in SalesPerformanceService.RETURNABLE_ORIGINAL_STATUSES:
                    raise SalesPerformanceError("原销售单状态不允许提交退货")
                SalesPerformanceService._validate_return_values(
                    original, perf.quantity, perf.unit_price, perf.amount, perf.cost
                )
        elif action == "confirm" and perf.return_of_performance_id is None:
            pass

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
        # 原销售单存在已提交退货单时，必须先取消退货单，避免原单取消后负数退货继续冲减统计。
        if action == "cancel" and perf.return_of_performance_id is None:
            active_return = SalesPerformance.query.filter_by(
                return_of_performance_id=perf.id, status="CONFIRMED"
            ).first()
            if active_return:
                raise SalesPerformanceError(
                    f"存在已提交退货单 {active_return.performance_no}，请先取消退货单再取消原销售单"
                )

        # 取消提示，不强制拦截，因为取消业绩单本身可能先于计划清理
        if action == "cancel" and perf.return_of_performance_id is None:
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
        if perf.return_of_performance_id is not None:
            raise SalesPerformanceError("退货单取消后不能重新打开，请重新创建退货单")
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