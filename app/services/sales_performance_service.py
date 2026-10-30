from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select, case, func, or_

from app.extensions import db
from app.generated.models.employees import Employee
from app.generated.models.sales_performance import SalesPerformance
from app.utils.datetime_utils import now_beijing_naive

Q6 = Decimal("0.000001")
ALLOWED_STATUS = {"DRAFT", "SUBMITTED", "PRODUCTION_PLANNED",
                  "FINISHED_GOODS_STOCKED", "SHIPMENT_SCHEDULED",
                  "COMPLETED", "CANCELLED"}
ALLOWED_PAYMENT = {"UNPAID", "PAID"}


def _q6(v):
    return Decimal(str(v)).quantize(Q6, rounding=ROUND_HALF_UP)


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
        if status not in ALLOWED_STATUS:
            raise SalesPerformanceError(f"非法状态: {status}")

        payment = data.get("payment_status") or "UNPAID"
        if payment not in ALLOWED_PAYMENT:
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
                          page=1, page_size=20):
        from sqlalchemy import select, func, or_
        creator = db.aliased(Employee)
        sales = db.aliased(Employee)

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
                SalesPerformance.expected_delivery_date,
                SalesPerformance.status,
                SalesPerformance.created_at,
                sales.real_name.label("salesperson_name"),
            )
            .join(sales, sales.id == SalesPerformance.main_salesperson_id,
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

        total = db.session.execute(
            select(func.count()).select_from(base.subquery())
        ).scalar() or 0

        base = base.offset((page - 1) * page_size).limit(page_size)
        rows = [dict(r) for r in db.session.execute(base).mappings().all()]
        return rows, total