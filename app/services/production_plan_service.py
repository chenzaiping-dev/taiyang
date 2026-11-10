from decimal import Decimal
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
from app.utils.datetime_utils import now_beijing, now_beijing_naive
from app.utils.decimal_utils import _q3, _q6

def gen_plan_no() -> str:
    ts = now_beijing().strftime("%Y%m%d%H%M%S")
    import random
    return f"PP{ts}{random.randint(0, 9999):04d}"

PLAN_STATUS_LABELS = {
    "DRAFT": "草稿",
    "CONFIRMED": "已确认",
    "IN_PROGRESS": "处理中",
    "COMPLETED": "已完成",
    "CANCELLED": "已取消",
}

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
            status="DRAFT",
            remark=remark,
            created_by=created_by,
            created_at=now_beijing_naive(),
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

        # ⭐ 重新写预留
        ProductionPlanService._apply_reservations(
            plan=plan, reservations=reservations, perf=perf,
        )

        # ⭐ 关联工艺单：已有行更新数量，新行创建，删除的行要处理
        ProductionPlanService._sync_process_orders(
            plan=plan,
            process_orders=process_orders,
            perf=perf,
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
                    f"批次 [{batch_no}] 可锁定数量不足："
                    f"当前可用 {available}（库存 {inv.quantity}，其他计划已锁 {other_locked}），"
                    f"本次需要 {qty}"
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
                    goods_type="STAPLE",
                    created_at=now_beijing_naive(),
                    remark=f"生产计划 {plan.plan_no} 锁定",
                )
                db.session.add(new_res)

        # 4. 剩下的 existing_map 是"本次未提交"的旧批次 → 释放
        for (wh_id, batch_id), r in existing_map.items():
            r.status = "RELEASED"
            r.remaining_quantity = Decimal("0")

    @staticmethod
    def _sync_process_orders(plan, process_orders, perf, created_by):
        """
        编辑场景下同步关联工艺单：
        - 已有 process_order_id：若数量变了 → 更新 production_plan_process_orders.planned_quantity
            和 process_orders.planned_quantity（仅允许 DRAFT 状态修改）
        - 无 process_order_id：创建新工艺单 + 关联行
        - 提交中不再包含的已有行：从关联表删除（保持 plan 关联与实际一致）
        校验：
        - Σ(planned_quantity) 不超过 plan.process_quantity（可放宽）
        - 数量 > 0
        """
        # 已有关系
        existing_rels = {
            rel.process_order_id: rel
            for rel in ProductionPlanProcessOrder.query.filter_by(
                production_plan_id=plan.id
            ).all()
        }

        submitted_ids = set()
        total_planned = Decimal("0")
        max_planned = _q6(plan.process_quantity)

        for row_idx, po in enumerate(process_orders, start=1):
            print(po)
            planned_qty = _q6(po["planned_quantity"])
            locked_qty = _q6(po["locked_quantity"])
            if planned_qty <= 0:
                raise ProductionPlanError(
                    f"第 {row_idx} 行工艺单计划生产数量必须 > 0"
                )

            po_id = po.get("process_order_id")

            # ============ 已有行：更新数量 ============
            if po_id and int(po_id) in existing_rels:
                po_id = int(po_id)
                submitted_ids.add(po_id)

                rel = existing_rels[po_id]
                order = db.session.get(ProcessOrder, po_id)
                if not order:
                    raise ProductionPlanError(
                        f"关联工艺单 #{po_id} 不存在"
                    )

                old_locked_qty = _q6(rel.locked_quantity)
                if old_locked_qty != locked_qty:
                    print(f"工艺单 {order.process_order_no} 锁定数量由 {old_locked_qty} 改为 {locked_qty}")
                    rel.locked_quantity = locked_qty
                # 只有草稿状态允许工艺单信息修改
                if order.status != "DRAFT":
                    total_planned += locked_qty
                    continue
                    raise ProductionPlanError(
                        f"工艺单 {order.process_order_no} 状态为【{order.status}】，"
                        f"仅草稿状态允许修改生产数量"
                    )

                # 若 factory_id 变了 → 同步更新工艺单加工厂
                new_factory_id = int(po["factory_id"])
                if order.factory_id != new_factory_id:
                    if not db.session.get(Factory, new_factory_id):
                        raise ProductionPlanError(
                            f"加工厂 #{new_factory_id} 不存在"
                        )
                    order.factory_id = new_factory_id

                # 数量变了 → 双表同步
                old_planned_qty = _q6(order.planned_quantity)
                print(f"工艺单 {order.process_order_no} 计划数量由 {old_planned_qty} 改为 {planned_qty}")
                if old_planned_qty != planned_qty:
                    order.planned_quantity = planned_qty
                    order.updated_at = now_beijing_naive()

                # 批次号同步（可选）
                if po.get("batch_no") and po["batch_no"] != (order.batch_no or ""):
                    order.batch_no = po["batch_no"]

                total_planned += locked_qty
                continue

            # ============ 新行：创建工艺单 ============
            ProductionPlanService._create_process_orders(
                plan=plan,
                process_orders=[po],
                perf=perf,
                created_by=created_by,
            )
            total_planned += locked_qty

        # ============ 处理被删除的行 ============
        # 提交中不再包含的已有工艺单 → 从关联表移除（工艺单本身保留）
        removed_po_ids = set(existing_rels.keys()) - submitted_ids
        for po_id in removed_po_ids:
            rel = existing_rels[po_id]
            order = db.session.get(ProcessOrder, po_id)
            if order and order.status != "DRAFT":
                raise ProductionPlanError(
                    f"工艺单 {order.process_order_no} 状态为【{order.status}】，"
                    f"无法从生产计划中移除"
                )
            db.session.delete(rel)

        # ============ 校验总计划量 ============
        if max_planned > 0 and total_planned > max_planned:
            raise ProductionPlanError(
                f"工艺单计划数量合计 {total_planned} 超过计划生产数量 {max_planned}"
        )

    @staticmethod
    def _create_process_orders(plan, process_orders, perf, created_by):
        """为每条新工艺单行创建 process_orders + 关联"""
        for po in process_orders:
            if po.get("process_order_id"):
                # 已存在的关联
                continue

            factory_id = int(po["factory_id"])
            planned_qty = _q6(po["planned_quantity"])
            locked_qty = _q6(po["locked_quantity"])
            if planned_qty <= 0:
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
                planned_quantity=planned_qty,
                performance_id=perf.id,
                order_time=now_beijing(),
                followup_employee_id=(
                    perf.main_salesperson_id or created_by or 1
                ),
                planned_cost_price=Decimal("0"),
                planned_total_cost=Decimal("0"),
                actual_total_cost=Decimal("0"),
                completed_quantity=Decimal("0"),
                status="DRAFT",
                remark=f"由生产计划 {plan.plan_no} 自动创建",
                created_by=created_by or 1,
                created_at=now_beijing_naive(),
                updated_at=now_beijing_naive(),
            )
            db.session.add(order)
            db.session.flush()

            # 关联
            rel = ProductionPlanProcessOrder(
                production_plan_id=plan.id,
                process_order_id=order.id,
                locked_quantity=locked_qty,
                created_at=now_beijing_naive(),
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
          'fg_locked_total': Decimal,
          'prod_locked_total': Decimal,
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
                FinishedGoodsReservation.goods_type == "STAPLE",
            )
            .order_by(Warehouse.warehouse_code.asc(),
                      FinishedGoodsBatch.batch_no.asc(),
                      FinishedGoodsReservation.id.asc())
            .all()
        )
        reservations = [dict(r._mapping) for r in res_rows]
        fg_locked_total = sum(
            (r["remaining_quantity"] for r in reservations
             if r["status"] == "ACTIVE"),
            Decimal("0"),
        )

        # 工艺单明细
        po_rows = (
            db.session.query(
                ProductionPlanProcessOrder.id.label("relation_id"),
                ProductionPlanProcessOrder.locked_quantity,
                ProcessOrder.id.label("process_order_id"),
                ProcessOrder.process_order_no,
                ProcessOrder.factory_id,
                Factory.factory_name,
                ProcessOrder.batch_no,
                ProcessOrder.product_name,
                ProcessOrder.product_specification,
                ProcessOrder.unit,
                ProcessOrder.planned_quantity,
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
        prod_locked_total = sum(
            (r["locked_quantity"] for r in process_orders
             if r["po_status"] != "CANCELLED"),
            Decimal("0"),
        )
        prod_planned_total = sum(
            (r["planned_quantity"] for r in process_orders
             if r["po_status"] != "CANCELLED"),
            Decimal("0"),
        )

        return {
            "plan": plan,
            "performance": performance,
            "creator": creator,
            "reservations": reservations,
            "process_orders": process_orders,
            "fg_locked_total": fg_locked_total,
            "prod_locked_total": prod_locked_total,
            "prod_planned_total": prod_planned_total,
        }

    # ==================== 状态操作 ====================

    @staticmethod
    def _get_plan_or_raise(plan_id: int) -> ProductionPlan:
        plan = db.session.get(ProductionPlan, plan_id)
        if not plan:
            raise ProductionPlanError("生产计划不存在")
        return plan

    @staticmethod
    def confirm_plan(plan_id: int) -> ProductionPlan:
        """DRAFT → CONFIRMED"""
        plan = ProductionPlanService._get_plan_or_raise(plan_id)
        if plan.status != "DRAFT":
            raise ProductionPlanError(
                f"当前状态【{PLAN_STATUS_LABELS.get(plan.status, plan.status)}】"
                f"不允许确认，仅草稿可确认"
            )
        plan.status = "CONFIRMED"
        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            raise ProductionPlanError(f"确认失败: {e}")
        return plan

    @staticmethod
    def process_plan(plan_id: int) -> ProductionPlan:
        """CONFIRMED → IN_PROGRESS"""
        plan = ProductionPlanService._get_plan_or_raise(plan_id)
        if plan.status != "CONFIRMED":
            raise ProductionPlanError(
                f"当前状态【{PLAN_STATUS_LABELS.get(plan.status, plan.status)}】"
                f"不允许处理，仅已确认可处理"
            )
        plan.status = "IN_PROGRESS"
        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            raise ProductionPlanError(f"处理失败: {e}")
        return plan

    @staticmethod
    def cancel_plan(plan_id: int) -> ProductionPlan:
        """DRAFT / CONFIRMED / IN_PROGRESS → CANCELLED"""
        plan = ProductionPlanService._get_plan_or_raise(plan_id)
        if plan.status in ("COMPLETED", "CANCELLED"):
            raise ProductionPlanError(
                f"当前状态【{PLAN_STATUS_LABELS.get(plan.status, plan.status)}】"
                f"不允许取消"
            )
        plan.status = "CANCELLED"
        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            raise ProductionPlanError(f"取消失败: {e}")
        return plan

    @staticmethod
    def complete_plan(plan_id: int, operator_id: int = None) -> ProductionPlan:
        """
        IN_PROGRESS → COMPLETED
        前置：所有关联工艺单状态必须为 RECEIVED
        动作：
          1. 检查工艺单状态
          2. 为每个已入库的成品批次写 finished_goods_reservations
          3. 更新状态
        """
        plan = ProductionPlanService._get_plan_or_raise(plan_id)
        if plan.status != "IN_PROGRESS":
            raise ProductionPlanError(
                f"当前状态【{PLAN_STATUS_LABELS.get(plan.status, plan.status)}】"
                f"不允许完成，仅处理中可完成"
            )

        # 1. 关联工艺单
        rels = ProductionPlanProcessOrder.query.filter_by(
            production_plan_id=plan.id
        ).order_by(ProductionPlanProcessOrder.id.asc()).all()
        if not rels:
            #raise ProductionPlanError("该生产计划未关联任何工艺单，无法完成")
            plan.status = "COMPLETED"
            try:
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                raise ProductionPlanError(f"完成失败: {e}")
            return plan

        po_ids = [r.process_order_id for r in rels]
        pos_map = {
            po.id: po for po in
            ProcessOrder.query.filter(ProcessOrder.id.in_(po_ids)).all()
        }

        # 2. 校验工艺单状态
        not_received = [
            pos_map[r.process_order_id]
            for r in rels
            if r.process_order_id in pos_map
            and pos_map[r.process_order_id].status != "RECEIVED"
        ]
        if not_received:
            details = "；".join(
                f"{po.process_order_no}({po.status})" for po in not_received
            )
            raise ProductionPlanError(
                f"以下工艺单尚未入库，无法完成：{details}"
            )

        # 3. 逐关联行写预留（按 locked_quantity
        try:
            for rel in rels:
                po = pos_map.get(rel.process_order_id)
                if not po:
                    raise ProductionPlanError(
                        f"关联工艺单 {rel.process_order_id} 不存在"
                    )
                ProductionPlanService._reserve_finished_goods_for_po(
                    plan=plan,
                    po=po,
                    planned_quantity=_q6(rel.locked_quantity),  # ⭐ 按锁定量
                    operator_id=operator_id,
                )
            plan.status = "COMPLETED"
            db.session.commit()
        except ProductionPlanError:
            db.session.rollback()
            raise
        except Exception as e:
            db.session.rollback()
            raise ProductionPlanError(f"完成失败: {e}")

        #4. 计算生产成本
        ProductionPlanService._calc_cost_for_production(plan)

        return plan

    @staticmethod
    def _reserve_finished_goods_for_po(
        plan, po, planned_quantity: Decimal, operator_id=None
    ):
        """
        按工艺单的 planned_quantity，锁定其成品批次库存。

        策略：
        1. 找出该工艺单所有有库存的成品批次（按 quantity 降序）
        2. 校验 Σ planned_quantity ≤ Σ 批次库存 - Σ 其他 ACTIVE 预留
        3. 按批次顺序（quantity 大者优先）分摊 planned_quantity
        4. 每分摊到的批次写一条 ACTIVE 预留，reservation_no 带序号
        """
        target = _q6(planned_quantity)
        if target <= 0:
            return

        # 1. 该工艺单所有成品批次库存（包含已被其他计划锁定的，用 available 判断）
        rows = (
            db.session.query(
                FinishedGoodsInventory.warehouse_id,
                FinishedGoodsInventory.batch_id,
                FinishedGoodsInventory.quantity,
                FinishedGoodsBatch.batch_no,
            )
            .join(FinishedGoodsBatch,
                FinishedGoodsBatch.id == FinishedGoodsInventory.batch_id)
            .filter(
                FinishedGoodsBatch.process_order_id == po.id,
                FinishedGoodsInventory.quantity > 0,
            )
            .order_by(FinishedGoodsInventory.quantity.desc(),
                    FinishedGoodsInventory.id.asc())
            .all()
        )
        if not rows:
            raise ProductionPlanError(
                f"工艺单 {po.process_order_no} 无可用成品批次库存，无法完成"
            )

        # 2. 计算每个批次的可用量（库存 - 其他 ACTIVE 预留 - 本计划已锁的）
        candidates = []
        total_available = Decimal("0")
        for wh_id, batch_id, qty, batch_no in rows:
            # 已存在的预留（本计划或其他计划）
            locked = (
                db.session.query(func.coalesce(
                    func.sum(FinishedGoodsReservation.remaining_quantity), 0))
                .filter(
                    FinishedGoodsReservation.warehouse_id == wh_id,
                    FinishedGoodsReservation.batch_id == batch_id,
                    FinishedGoodsReservation.status == "ACTIVE",
                )
                .scalar()
            ) or Decimal("0")
            available = Decimal(str(qty or 0)) - Decimal(str(locked))
            if available > 0:
                candidates.append({
                    "warehouse_id": wh_id,
                    "batch_id": batch_id,
                    "batch_no": batch_no,
                    "available": _q6(available),
                })
                total_available += available

        if total_available < target:
            raise ProductionPlanError(
                f"工艺单 {po.process_order_no} 可用成品库存不足："
                f"计划锁定 {target}，实际可用 {_q6(total_available)}"
            )

        # 3. 分摊 planned_quantity 到批次
        remaining = target
        seq = 0
        for c in candidates:
            if remaining <= 0:
                break
            take = min(c["available"], remaining)

            # 幂等：本计划 + 该批次已存在 ACTIVE 预留 → 跳过（防止重复）
            exists = FinishedGoodsReservation.query.filter_by(
                production_plan_id=plan.id,
                warehouse_id=c["warehouse_id"],
                batch_id=c["batch_id"],
                status="ACTIVE",
            ).first()
            if exists:
                # 已有预留，按其已锁数量消耗 remaining
                remaining -= exists.remaining_quantity
                continue

            seq += 1
            res = FinishedGoodsReservation(
                reservation_no=(
                    f"RES-PP-{plan.plan_no}-{po.id}-{c['warehouse_id']}-"
                    f"{c['batch_id']}-{seq}"
                ),
                reservation_type="PRODUCTION_PLAN",
                production_plan_id=plan.id,
                shipment_allocation_id=None,
                warehouse_id=c["warehouse_id"],
                batch_id=c["batch_id"],
                reserved_quantity=_q6(take),
                remaining_quantity=_q6(take),
                status="ACTIVE",
                goods_type="FRESH",
                created_at=now_beijing_naive(),
                remark=(
                    f"生产计划 {plan.plan_no} 完成时锁定 "
                    f"(工艺单 {po.process_order_no}, 计划量 {target}, "
                    f"批次 {c['batch_no']})"
                ),
            )
            db.session.add(res)
            remaining -= take

    @staticmethod
    def _calc_cost_for_production(plan):
        # ============ 生产入库：校验 + 计算成本 ============
        if not plan:
            raise ProductionPlanError(
                "没有生产计划，无法执行成本计算"
            )
        if plan.status != "COMPLETED":
            plan_label = PLAN_STATUS_LABELS.get(plan.status, plan.status)
            raise ProductionPlanError(
                f"生产计划【{plan.plan_no}】状态为【{plan_label}】，"
                f"必须为【已完成】才能执行成本计算"
            )

        rows = (
            db.session.query(
                FinishedGoodsReservation.remaining_quantity,
                FinishedGoodsBatch.unit_cost,
                FinishedGoodsBatch.batch_no,
            )
            .join(FinishedGoodsBatch,
                FinishedGoodsBatch.id == FinishedGoodsReservation.batch_id)
            .filter(
                FinishedGoodsReservation.production_plan_id == plan.id,
                FinishedGoodsReservation.reservation_type == "PRODUCTION_PLAN",
                FinishedGoodsReservation.status == "ACTIVE",
                FinishedGoodsReservation.remaining_quantity > 0,
            )
            .all()
        )

        if not rows:
            plan._cost_warning = (
                f"生产计划【{plan.plan_no}】没有可用的成品预留，"
                f"成本计算为0"
            )
            return

        perf = db.session.get(SalesPerformance, plan.performance_id)
        if not perf:
            raise SalesPerformanceError("业绩单不存在")
        total_cost = Decimal("0")
        missing_cost_batches = []
        for remaining_qty, unit_cost, batch_no in rows:
            q = _q6(remaining_qty or 0)
            if unit_cost is None:
                missing_cost_batches.append(batch_no)
                continue
            total_cost += _q6(q * _q6(unit_cost))

        total_cost = _q6(total_cost)
        amount = _q6(perf.amount or 0)
        new_profit = _q6(amount - total_cost)

        # 覆盖写入
        perf.cost = total_cost
        perf.profit = new_profit

        # 供 Controller 读取并 flash
        if missing_cost_batches:
            plan._cost_warning = (
                f"以下批次缺少 unit_cost，按 0 计入成本："
                f"{'、'.join(missing_cost_batches)}"
            )

        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            raise ProductionPlanError(f"状态更新失败: {e}")
