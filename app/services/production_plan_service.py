from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone, timedelta
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select, func, or_
# 或用 with db.session.no_autoflush:

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
from app.utils.datetime_utils import now_beijing

Q6 = Decimal("0.000001")
BEIJING = timezone(timedelta(hours=8))


def _q6(v):
    return Decimal(str(v)).quantize(Q6, rounding=ROUND_HALF_UP)

def gen_plan_no() -> str:
    ts = now_beijing().strftime("%Y%m%d%H%M%S")
    import random
    return f"PP{ts}{random.randint(0, 9999):04d}"


class ProductionPlanError(Exception):
    pass


class ProductionPlanService:

    # ==================== 新建 ====================
    @staticmethod
    def create_plan(
        performance_id: int,
        total_quantity,
        process_quantity,
        reservations: list,
        process_orders: list,
        remark: str = None,
        created_by: int = None,
    ) -> ProductionPlan:
        perf = db.session.get(SalesPerformance, performance_id)
        if not perf:
            raise ProductionPlanError("业绩单不存在")

        if ProductionPlan.query.filter_by(performance_id=performance_id).first():
            raise ProductionPlanError("该业绩单已存在生产计划，请使用编辑")

        total = _q6(total_quantity)
        process = _q6(process_quantity)
        if total <= 0:
            raise ProductionPlanError("总生产数量必须 > 0")
        if process < 0 or process > total:
            raise ProductionPlanError("计划生产数量必须在 0 到 总生产数量 之间")

        # 库存锁定总量不能超过 total
        locked_sum = sum((_q6(r["lock_qty"]) for r in reservations), Decimal("0"))
        if locked_sum > total:
            raise ProductionPlanError(
                f"锁定数量合计 {locked_sum} 超过总生产数量 {total}"
            )

        plan = ProductionPlan(
            plan_no=gen_plan_no(),
            performance_id=performance_id,
            total_quantity=total,
            process_quantity=process,
            process_order_status="PLANNED" if process > 0 else "NOT_REQUIRED",
            status="DRAFT",
            remark=remark,
            created_by=created_by,
        )
        db.session.add(plan)
        db.session.flush()

        # 库存锁定
        ProductionPlanService._apply_reservations(
            plan=plan, reservations=reservations, perf=perf,
        )

        # 工艺单（自动创建）
        ProductionPlanService._create_process_orders(
            plan=plan, process_orders=process_orders, perf=perf,
            created_by=created_by,
        )

        try:
            db.session.commit()
        except IntegrityError as e:
            db.session.rollback()
            raise ProductionPlanError(f"创建失败，唯一约束冲突: {e.orig}")
        except Exception as e:
            db.session.rollback()
            raise ProductionPlanError(f"创建失败: {e}")

        return plan

    # ==================== 更新 ====================
    @staticmethod
    def update_plan(
        plan_id: int,
        total_quantity,
        process_quantity,
        reservations: list,
        process_orders: list,
        remark: str = None,
    ) -> ProductionPlan:
        plan = db.session.get(ProductionPlan, plan_id)
        if not plan:
            raise ProductionPlanError("生产计划不存在")
        if plan.status not in ("DRAFT", "CONFIRMED"):
            raise ProductionPlanError(
                f"当前状态【{plan.status}】不允许编辑"
            )

        perf = db.session.get(SalesPerformance, plan.performance_id)
        total = _q6(total_quantity)
        process = _q6(process_quantity)
        if total <= 0:
            raise ProductionPlanError("总生产数量必须 > 0")
        if process < 0 or process > total:
            raise ProductionPlanError("计划生产数量必须在 0 到 总生产数量 之间")

        locked_sum = sum((_q6(r["lock_qty"]) for r in reservations), Decimal("0"))
        if locked_sum > total:
            raise ProductionPlanError(
                f"锁定数量合计 {locked_sum} 超过总生产数量 {total}"
            )

        plan.total_quantity = total
        plan.process_quantity = process
        plan.remark = remark
        plan.process_order_status = "PLANNED" if process > 0 else "NOT_REQUIRED"

        # ⭐ 重新写预留
        ProductionPlanService._apply_reservations(
            plan=plan, reservations=reservations, perf=perf,
        )

        # ⭐ 关联工艺单：本次提交里只处理新增（已有工艺单不允许改数量）
        existing_po_ids = {
            rel.process_order_id for rel in ProductionPlanProcessOrder.query.filter_by(
                production_plan_id=plan.id).all()
        }
        new_pos = [
            po for po in process_orders
            if po.get("process_order_id") not in existing_po_ids
        ]
        ProductionPlanService._create_process_orders(
            plan=plan, process_orders=new_pos, perf=perf,
            created_by=plan.created_by,
        )

        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            raise ProductionPlanError(f"更新失败: {e}")

        return plan

    # ==================== 内部工具 ====================
    @staticmethod
    def _apply_reservations(plan, reservations, perf):
        """
        upsert 语义：
        - 已有同 (warehouse_id, batch_id) 的 ACTIVE 预留 → 直接更新数量
        - 本次未提交的旧 ACTIVE 预留 → 置为 RELEASED（释放）
        - 新批次 → 插入
        """
        # 1. 取该计划所有 ACTIVE 预留，建立 map
        existing = FinishedGoodsReservation.query.filter_by(
            production_plan_id=plan.id,
            reservation_type="PRODUCTION_PLAN",
            status="ACTIVE",
        ).all()
        existing_map = {(r.warehouse_id, r.batch_id): r for r in existing}

        # 2. 校验库存 + 汇总本次目标
        #    先按 (wh, batch) 合并前端传的同一批次多行（虽然 UI 不会，但防御）
        target = {}
        for r in reservations:
            key = (int(r["warehouse_id"]), int(r["batch_id"]))
            qty = _q6(r["lock_qty"])
            if qty <= 0:
                continue
            target[key] = target.get(key, Decimal("0")) + qty

        # 3. 逐个 upsert
        for (wh_id, batch_id), qty in target.items():
            inv = FinishedGoodsInventory.query.filter_by(
                warehouse_id=wh_id, batch_id=batch_id,
            ).with_for_update().first()
            if not inv:
                raise ProductionPlanError(
                    f"库存记录不存在（仓库 {wh_id} 批次 {batch_id}）"
                )

            # 其他计划（排除自己）已锁定的数量
            from sqlalchemy import func, and_
            other_locked = (
                db.session.query(func.coalesce(
                    func.sum(FinishedGoodsReservation.remaining_quantity), 0))
                .filter(
                    FinishedGoodsReservation.warehouse_id == wh_id,
                    FinishedGoodsReservation.batch_id == batch_id,
                    FinishedGoodsReservation.status == "ACTIVE",
                    FinishedGoodsReservation.production_plan_id != plan.id,
                )
                .scalar()
            ) or Decimal("0")

            available = (inv.quantity or Decimal("0")) - other_locked
            if qty > available:
                batch = db.session.get(FinishedGoodsBatch, batch_id)
                batch_no = batch.batch_no if batch else f"#{batch_id}"
                raise ProductionPlanError(
                    f"批次 [{batch_no}] 可锁定数量不足：可用 {available}，需要 {qty}"
                )

            existing_res = existing_map.pop((wh_id, batch_id), None)
            if existing_res:
                # 复用：只更新数量
                existing_res.reserved_quantity = qty
                existing_res.remaining_quantity = qty
                existing_res.status = "ACTIVE"
                # reservation_no 保持不变，绝不动
            else:
                # 新插入
                new_res = FinishedGoodsReservation(
                    reservation_no=f"RES-PP-{plan.plan_no}-{wh_id}-{batch_id}",
                    reservation_type="PRODUCTION_PLAN",
                    production_plan_id=plan.id,
                    shipment_allocation_id=None,
                    warehouse_id=wh_id,
                    batch_id=batch_id,
                    reserved_quantity=qty,
                    remaining_quantity=qty,
                    status="ACTIVE",
                    remark=f"生产计划 {plan.plan_no} 锁定",
                )
                db.session.add(new_res)

        # 4. 剩下的 existing_map 是"本次未提交"的旧批次 → 释放
        for (wh_id, batch_id), r in existing_map.items():
            r.status = "RELEASED"
            r.remaining_quantity = Decimal("0")

    @staticmethod
    def _create_process_orders(plan, process_orders, perf, created_by):
        """为每条新工艺单行创建 process_orders + 关联"""
        for po in process_orders:
            if po.get("process_order_id"):
                # 已存在的关联
                continue

            factory_id = int(po["factory_id"])
            qty = _q6(po["quantity"])
            if qty <= 0:
                continue
            if not db.session.get(Factory, factory_id):
                raise ProductionPlanError(f"加工厂不存在: {factory_id}")

            # 工艺单号 / 批次号
            ts = now_beijing().strftime("%Y%m%d%H%M%S")
            import random
            po_no = f"PO{ts}{random.randint(0, 9999):04d}"
            batch_no = po.get("batch_no") or f"B{ts}{random.randint(0, 99):02d}"

            order = ProcessOrder(
                process_order_no=po_no,
                factory_id=factory_id,
                batch_no=batch_no,
                product_name=perf.product_name,
                product_specification=perf.product_specification,
                unit=perf.unit or "KG",
                quantity=qty,
                performance_id=perf.id,
                order_time=now_beijing(),
                followup_employee_id=(
                    perf.main_salesperson_id or created_by or 1
                ),
                cost_price=Decimal("0"),
                total_cost=Decimal("0"),
                actual_total_cost=Decimal("0"),
                completed_quantity=Decimal("0"),
                status="DRAFT",
                remark=f"由生产计划 {plan.plan_no} 自动创建",
                created_by=created_by or 1,
            )
            db.session.add(order)
            db.session.flush()

            # 关联
            rel = ProductionPlanProcessOrder(
                production_plan_id=plan.id,
                process_order_id=order.id,
                planned_quantity=qty,
            )
            db.session.add(rel)

    @staticmethod
    def list_plans(keyword=None, status=None, performance_id=None,
                   page=1, page_size=20):
        """
        生产计划列表。
        额外聚合：锁定总数量（ACTIVE 预留 remaining_quantity 之和）
        """
        creator = db.aliased(Employee)

        # 锁定总量子查询
        locked_subq = (
            select(
                FinishedGoodsReservation.production_plan_id.label("pid"),
                func.coalesce(
                    func.sum(FinishedGoodsReservation.remaining_quantity), 0
                ).label("locked_qty"),
            )
            .where(
                FinishedGoodsReservation.reservation_type == "PRODUCTION_PLAN",
                FinishedGoodsReservation.status == "ACTIVE",
            )
            .group_by(FinishedGoodsReservation.production_plan_id)
            .subquery()
        )

        base = (
            select(
                ProductionPlan.id,
                ProductionPlan.plan_no,
                ProductionPlan.performance_id,
                ProductionPlan.total_quantity,
                ProductionPlan.process_quantity,
                ProductionPlan.process_order_status,
                ProductionPlan.status,
                ProductionPlan.created_by,
                creator.real_name.label("created_by_name"),
                ProductionPlan.created_at,
                func.coalesce(locked_subq.c.locked_qty, 0).label("locked_qty"),
            )
            .join(creator, creator.id == ProductionPlan.created_by, isouter=True)
            .join(locked_subq, locked_subq.c.pid == ProductionPlan.id, isouter=True)
            .order_by(ProductionPlan.id.desc())
        )

        if keyword:
            like = f"%{keyword}%"
            base = base.where(ProductionPlan.plan_no.like(like))
        if status:
            base = base.where(ProductionPlan.status == status)
        if performance_id:
            base = base.where(ProductionPlan.performance_id == performance_id)

        total = db.session.execute(
            select(func.count()).select_from(base.subquery())
        ).scalar() or 0

        base = base.offset((page - 1) * page_size).limit(page_size)
        rows = [dict(r) for r in db.session.execute(base).mappings().all()]
        return rows, total

    @staticmethod
    def get_detail(plan_id: int) -> dict:
        """
        返回详情页所需：
        {
          'plan': ProductionPlan,
          'performance': SalesPerformance,
          'creator': Employee,
          'reservations': [{...with 仓库/批次信息}],
          'process_orders': [{...with 加工厂/工艺单}],
          'locked_total': Decimal,
        }
        """

        plan = db.session.get(ProductionPlan, plan_id)
        if not plan:
            return None

        performance = db.session.get(SalesPerformance, plan.performance_id)
        creator = (
            db.session.get(Employee, plan.created_by)
            if plan.created_by else None
        )

        # 锁定明细：ACTIVE + RELEASED 都展示，方便审计，但标出状态
        res_rows = (
            db.session.query(
                FinishedGoodsReservation.id,
                FinishedGoodsReservation.reservation_no,
                FinishedGoodsReservation.warehouse_id,
                Warehouse.warehouse_code,
                Warehouse.warehouse_name,
                FinishedGoodsReservation.batch_id,
                FinishedGoodsBatch.batch_no,
                FinishedGoodsBatch.product_name,
                FinishedGoodsBatch.product_specification,
                FinishedGoodsBatch.unit,
                FinishedGoodsReservation.reserved_quantity,
                FinishedGoodsReservation.remaining_quantity,
                FinishedGoodsReservation.status,
                FinishedGoodsReservation.created_at,
            )
            .join(Warehouse,
                  Warehouse.id == FinishedGoodsReservation.warehouse_id)
            .join(FinishedGoodsBatch,
                  FinishedGoodsBatch.id == FinishedGoodsReservation.batch_id)
            .filter(
                FinishedGoodsReservation.production_plan_id == plan.id,
                FinishedGoodsReservation.reservation_type == "PRODUCTION_PLAN",
            )
            .order_by(Warehouse.warehouse_code.asc(),
                      FinishedGoodsBatch.batch_no.asc(),
                      FinishedGoodsReservation.id.asc())
            .all()
        )
        reservations = [dict(r._mapping) for r in res_rows]
        locked_total = sum(
            (r["remaining_quantity"] for r in reservations
             if r["status"] == "ACTIVE"),
            Decimal("0"),
        )

        # 工艺单明细
        po_rows = (
            db.session.query(
                ProductionPlanProcessOrder.id.label("relation_id"),
                ProductionPlanProcessOrder.planned_quantity,
                ProcessOrder.id.label("process_order_id"),
                ProcessOrder.process_order_no,
                ProcessOrder.factory_id,
                Factory.factory_name,
                ProcessOrder.batch_no,
                ProcessOrder.product_name,
                ProcessOrder.product_specification,
                ProcessOrder.unit,
                ProcessOrder.quantity,
                ProcessOrder.status.label("po_status"),
                ProcessOrder.completed_quantity,
                ProcessOrder.order_time,
            )
            .join(ProcessOrder,
                  ProcessOrder.id == ProductionPlanProcessOrder.process_order_id)
            .join(Factory, Factory.id == ProcessOrder.factory_id, isouter=True)
            .filter(ProductionPlanProcessOrder.production_plan_id == plan.id)
            .order_by(ProductionPlanProcessOrder.id.asc())
            .all()
        )
        process_orders = [dict(r._mapping) for r in po_rows]

        return {
            "plan": plan,
            "performance": performance,
            "creator": creator,
            "reservations": reservations,
            "process_orders": process_orders,
            "locked_total": locked_total,
        }