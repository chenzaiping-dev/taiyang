from sqlalchemy import select, case, func, or_
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone, timedelta


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
from app.generated.models.process_orders import ProcessOrder
from app.generated.models.production_plan_process_orders import ProductionPlanProcessOrder
from app.generated.models.production_plans import ProductionPlan
from app.services.finished_goods_service import FinishedGoodsError, FinishedGoodsService

from app.utils.datetime_utils import now_beijing, now_beijing_naive

Q6 = Decimal("0.000001")


# ==================== 状态流转 ====================

STATUS_DRAFT = "DRAFT"
STATUS_PLANNED = "PLANNED"
STATUS_PARTIALLY_SHIPPED = "PARTIALLY_SHIPPED"
STATUS_SHIPPED = "SHIPPED"
STATUS_CANCELLED = "CANCELLED"


def _get_plan_or_raise(plan_id: int) -> ShipmentPlan:
    plan = db.session.get(ShipmentPlan, plan_id)
    if not plan:
        raise ShipmentPlanError("发货计划不存在")
    return plan

def _q6(v):
    return Decimal(str(v)).quantize(Q6, rounding=ROUND_HALF_UP)


def gen_shipment_no() -> str:
    ts = now_beijing().strftime("%Y%m%d%H%M%S")
    import random
    return f"SP{ts}{random.randint(0, 9999):04d}"


class ShipmentPlanError(Exception):
    pass


class ShipmentPlanService:

    @staticmethod
    def get_available_reservations(performance_id: int):
        """
        返回该业绩单对应生产计划下所有 ACTIVE 的生产计划预留：
        [{
            reservation_id, warehouse_id, warehouse_code, warehouse_name,
            batch_id, batch_no, product_name, product_specification, unit,
            remaining_quantity
        }]
        """
        plan = ProductionPlan.query.filter_by(
            performance_id=performance_id
        ).first()
        if not plan:
            return []

        q = (
            db.session.query(
                FinishedGoodsReservation.id.label("reservation_id"),
                FinishedGoodsReservation.warehouse_id,
                Warehouse.warehouse_code,
                Warehouse.warehouse_name,
                FinishedGoodsReservation.batch_id,
                FinishedGoodsBatch.batch_no,
                FinishedGoodsBatch.product_name,
                FinishedGoodsBatch.product_specification,
                FinishedGoodsBatch.unit,
                FinishedGoodsReservation.remaining_quantity,
            )
            .join(Warehouse, Warehouse.id == FinishedGoodsReservation.warehouse_id)
            .join(FinishedGoodsBatch,
                  FinishedGoodsBatch.id == FinishedGoodsReservation.batch_id)
            .filter(
                FinishedGoodsReservation.production_plan_id == plan.id,
                FinishedGoodsReservation.reservation_type == "PRODUCTION_PLAN",
                FinishedGoodsReservation.status == "ACTIVE",
                FinishedGoodsReservation.remaining_quantity > 0,
            )
            .order_by(Warehouse.warehouse_code.asc(),
                      FinishedGoodsBatch.batch_no.asc())
        )
        return [dict(r._mapping) for r in q.all()]

    # ==================== 新建 ====================
    @staticmethod
    def create_plan(
        performance_id: int,
        destination: str,
        transport_method: str = None,
        planned_ship_time=None,
        remark: str = None,
        lines: list = None,
        created_by: int = None,
    ) -> ShipmentPlan:
        """
        lines: [{
            reservation_id, warehouse_id, batch_id, quantity,
            product_name, product_specification, unit,
            unit_price(可选)
        }]
        """
        perf = db.session.get(SalesPerformance, performance_id)
        if not perf:
            raise ShipmentPlanError("业绩单不存在")
        if not destination:
            raise ShipmentPlanError("目的地必填")
        if not lines:
            raise ShipmentPlanError("至少需要一条发货明细")

        plan = ShipmentPlan(
            shipment_plan_no=gen_shipment_no(),
            performance_id=performance_id,
            destination=destination,
            transport_method=transport_method,
            planned_ship_time=planned_ship_time,
            status="DRAFT",
            remark=remark,
            created_by=created_by,
            created_at=now_beijing_naive(),
            updated_at=now_beijing_naive(),
        )
        db.session.add(plan)
        db.session.flush()

        ShipmentPlanService._upsert_lines(plan=plan, lines=lines, perf=perf)

        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            raise ShipmentPlanError(f"创建发货计划失败: {e}")

        return plan

    # ==================== 更新 ====================
    @staticmethod
    def update_plan(
        plan_id: int,
        destination: str,
        transport_method: str = None,
        planned_ship_time=None,
        remark: str = None,
        lines: list = None,
    ) -> ShipmentPlan:
        plan = db.session.get(ShipmentPlan, plan_id)
        if not plan:
            raise ShipmentPlanError("发货计划不存在")
        if plan.status != "DRAFT":
            raise ShipmentPlanError(
                f"当前状态【{plan.status}】不允许编辑"
            )
        if not destination:
            raise ShipmentPlanError("目的地必填")
        if not lines:
            raise ShipmentPlanError("至少需要一条发货明细")

        perf = db.session.get(SalesPerformance, plan.performance_id)
        if not perf:
            raise ShipmentPlanError("业绩单丢失")

        plan.destination = destination
        plan.transport_method = transport_method
        plan.planned_ship_time = planned_ship_time
        plan.remark = remark

        # 明细全删重插（简单且一致）
        ShipmentPlanAllocation.query.filter(
            ShipmentPlanAllocation.shipment_line_id.in_(
                db.session.query(ShipmentPlanLine.id).filter_by(
                    shipment_plan_id=plan.id)
            )
        ).delete(synchronize_session=False)
        ShipmentPlanLine.query.filter_by(shipment_plan_id=plan.id).delete(
            synchronize_session=False
        )
        db.session.flush()

        ShipmentPlanService._upsert_lines(plan=plan, lines=lines, perf=perf)

        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            raise ShipmentPlanError(f"更新发货计划失败: {e}")

        return plan

    # ==================== 内部：写明细 + 分配 ====================
    @staticmethod
    def _upsert_lines(plan, lines, perf):
        """
        lines: 前端提交的明细。每条对应一个预留（reservation_id），
        并写 shipment_plan_lines + shipment_plan_allocations。
        """
        # 同产品合并到同一 line（同一 (product_name, spec, unit) → 同一 line）
        # 每行的多个 reservation 生成 allocations
        grouped = {}
        for l in lines:
            qty = _q6(l["quantity"])
            if qty <= 0:
                continue
            key = (
                l.get("product_name") or perf.product_name,
                l.get("product_specification") or perf.product_specification,
                l.get("unit") or perf.unit or "KG",
                _q6(l.get("unit_price") or 0),
            )
            grouped.setdefault(key, []).append(l)

        for (pname, pspec, punit, uprice), items in grouped.items():
            total_qty = sum((_q6(i["quantity"]) for i in items), Decimal("0"))
            line = ShipmentPlanLine(
                shipment_plan_id=plan.id,
                product_name=pname,
                product_specification=pspec,
                unit=punit,
                unit_price=uprice,
                quantity=total_qty,
                shipped_quantity=Decimal("0"),
            )
            db.session.add(line)
            db.session.flush()

            for it in items:
                # 校验：reservation 剩余量是否足够
                res = db.session.get(
                    FinishedGoodsReservation, int(it["reservation_id"])
                ) if it.get("reservation_id") else None
                qty = _q6(it["quantity"])

                if res:
                    if res.status != "ACTIVE":
                        raise ShipmentPlanError(
                            f"预留已失效（id={res.id}）"
                        )
                    if qty > res.remaining_quantity:
                        raise ShipmentPlanError(
                            f"预留 {res.id} 可用量 {res.remaining_quantity} "
                            f"不足 {qty}"
                        )

                alloc = ShipmentPlanAllocation(
                    shipment_line_id=line.id,
                    warehouse_id=int(it["warehouse_id"]),
                    batch_id=int(it["batch_id"]),
                    planned_quantity=qty,
                    shipped_quantity=Decimal("0"),
                )
                db.session.add(alloc)

    # ==================== 记录发货 ====================
    @staticmethod
    def record_shipment(plan_id: int, allocations: list):
        """
        allocations: [{allocation_id, quantity}]  —— 每条对应 shipment_plan_allocations
        发货后累加到 shipped_quantity，若全部发完则把 plan 状态改成 SHIPPED，
        部分发货改成 PARTIALLY_SHIPPED。
        """
        plan = db.session.get(ShipmentPlan, plan_id)
        if not plan:
            raise ShipmentPlanError("发货计划不存在")
        if plan.status in ("SHIPPED", "CANCELLED"):
            raise ShipmentPlanError(f"当前状态【{plan.status}】不允许再发货")

        line_ids = set()
        for a in allocations:
            alloc = db.session.get(
                ShipmentPlanAllocation, int(a["allocation_id"])
            )
            if not alloc:
                raise ShipmentPlanError(f"分配记录 {a['allocation_id']} 不存在")

            qty = _q6(a["quantity"])
            if qty <= 0:
                continue
            remaining = alloc.planned_quantity - alloc.shipped_quantity
            if qty > remaining:
                raise ShipmentPlanError(
                    f"分配 {alloc.id} 剩余可发 {remaining}，本次 {qty} 超额"
                )

            alloc.shipped_quantity += qty
            line_ids.add(alloc.shipment_line_id)

        # 回写 line.shipped_quantity
        for lid in line_ids:
            line = db.session.get(ShipmentPlanLine, lid)
            total = db.session.query(
                func.coalesce(func.sum(ShipmentPlanAllocation.shipped_quantity), 0)
            ).filter_by(shipment_line_id=lid).scalar()
            line.shipped_quantity = _q6(total)

        # 判断计划整体状态
        all_lines = ShipmentPlanLine.query.filter_by(
            shipment_plan_id=plan.id).all()
        all_done = all(l.shipped_quantity >= l.quantity for l in all_lines)
        any_done = any(l.shipped_quantity > 0 for l in all_lines)

        plan.status = (
            "SHIPPED" if all_done
            else "PARTIALLY_SHIPPED" if any_done
            else plan.status
        )
        if all_done and not plan.actual_ship_time:
            plan.actual_ship_time = now_beijing()

        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            raise ShipmentPlanError(f"记录发货失败: {e}")

        return plan

    # ---------- 计划 ----------
    @staticmethod
    def mark_planned(plan_id: int) -> ShipmentPlan:
        plan = _get_plan_or_raise(plan_id)
        if plan.status != STATUS_DRAFT:
            raise ShipmentPlanError(
                f"只有草稿状态可执行【计划】，当前：{plan.status}")
        plan.status = STATUS_PLANNED
        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            raise ShipmentPlanError(f"标记计划失败: {e}")
        return plan

    # ---------- 取消 ----------
    @staticmethod
    def cancel_plan(plan_id: int) -> ShipmentPlan:
        plan = _get_plan_or_raise(plan_id)
        if plan.status in (STATUS_SHIPPED, STATUS_CANCELLED):
            raise ShipmentPlanError(
                f"当前状态【{plan.status}】不可取消")
        # 已有实际发货的数量不允许取消（业务要求整体已发货不可撤）
        has_shipped = (
            db.session.query(func.coalesce(
                func.sum(ShipmentPlanLine.shipped_quantity), 0))
            .filter_by(shipment_plan_id=plan.id)
            .scalar()
        ) or 0
        if Decimal(str(has_shipped)) > 0:
            raise ShipmentPlanError(
                "存在已发货明细，无法取消；请走退货/冲销流程")
        plan.status = STATUS_CANCELLED
        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            raise ShipmentPlanError(f"取消失败: {e}")
        return plan

    # ---------- 发货（关联出库） ----------
    @staticmethod
    def old_ship_plan(plan_id: int, operator_id: int = None) -> ShipmentPlan:
        """
        将 PLANNED 状态的发货计划整体发货：
        1. 校验状态为 PLANNED
        2. 按 shipments_plan_allocations 逐条调用 FinishedGoodsService 出库
           （扣减锁定库存，按 batch 出库，出库数量 = planned_quantity - shipped_quantity）
        3. 出库成功后回写 line.shipped_quantity、allocation.shipped_quantity
        4. plan.status = SHIPPED, actual_ship_time = now
        出库和状态更新同一事务。
        """
        plan = _get_plan_or_raise(plan_id)
        if plan.status != STATUS_PLANNED:
            raise ShipmentPlanError(
                f"只有【已计划】状态可执行发货，当前：{plan.status}")

        lines = ShipmentPlanLine.query.filter_by(
            shipment_plan_id=plan.id).all()
        if not lines:
            raise ShipmentPlanError("发货计划无明细，不能发货")

        # 收集待出库的 allocation
        allocs = (
            ShipmentPlanAllocation.query
            .filter(ShipmentPlanAllocation.shipment_line_id.in_(
                [l.id for l in lines]))
            .all()
        )
        if not allocs:
            raise ShipmentPlanError("发货计划无批次分配，不能发货")

        try:
            for alloc in allocs:
                ship_qty = alloc.planned_quantity - alloc.shipped_quantity
                if ship_qty <= 0:
                    continue

                # 调用既有出库服务：按仓库+批次出库
                FinishedGoodsService.outbound(
                    warehouse_id=alloc.warehouse_id,
                    batch_id=alloc.batch_id,
                    quantity=_q6(ship_qty),
                    business_type="SHIPMENT_PLAN",
                    business_id=plan.id,
                    remark=f"发货计划 {plan.shipment_plan_no} 出库",
                    operator_id=operator_id,
                )

                alloc.shipped_quantity += ship_qty

            # 回写 line 已发数量
            for line in lines:
                total = db.session.query(
                    func.coalesce(
                        func.sum(ShipmentPlanAllocation.shipped_quantity), 0)
                ).filter_by(shipment_line_id=line.id).scalar()
                line.shipped_quantity = _q6(total)

            # 判断整体是否发完（本方法只做整体发货，若全部发完→SHIPPED）
            all_done = all(
                l.shipped_quantity >= l.quantity for l in lines
            )
            plan.status = (
                STATUS_SHIPPED if all_done
                else STATUS_PARTIALLY_SHIPPED
            )
            if all_done and not plan.actual_ship_time:
                plan.actual_ship_time = now_beijing()

            db.session.commit()
        except ShipmentPlanError:
            db.session.rollback()
            raise
        except Exception as e:
            db.session.rollback()
            raise ShipmentPlanError(f"发货出库失败: {e}")

        return plan

    @staticmethod
    def ship_plan(plan_id: int, operator_id: int = None) -> ShipmentPlan:
        plan = db.session.get(ShipmentPlan, plan_id)
        print(plan)
        if not plan:
            raise ShipmentPlanError("发货计划不存在")
        if plan.status != "PLANNED":
            raise ShipmentPlanError(
                f"只有【已计划】状态可执行发货，当前：{plan.status}")

        lines = ShipmentPlanLine.query.filter_by(
            shipment_plan_id=plan.id).all()
        if not lines:
            raise ShipmentPlanError("发货计划无明细，不能发货")

        allocs = (
            ShipmentPlanAllocation.query
            .filter(ShipmentPlanAllocation.shipment_line_id.in_(
                [l.id for l in lines]))
            .all()
        )
        if not allocs:
            raise ShipmentPlanError("发货计划无批次分配，不能发货")

        # 1. 过滤出本次真正要发的 allocation，并按仓库分组
        pending = []
        for a in allocs:
            qty = (a.planned_quantity or Decimal("0")) - (a.shipped_quantity or Decimal("0"))
            if qty > 0:
                pending.append((a, _q6(qty)))
        if not pending:
            raise ShipmentPlanError("没有待发货数量")

        group_by_wh = {}
        for a, qty in pending:
            group_by_wh.setdefault(a.warehouse_id, []).append((a, qty))

        try:
            # 2. 先释放对应预留（否则出库可用量 = 库存 - ACTIVE预留 会不够）
            for a, qty in pending:
                ShipmentPlanService._consume_production_reservation(
                    warehouse_id=a.warehouse_id,
                    batch_id=a.batch_id,
                    quantity=qty,
                )

            # 3. 逐仓库调用 FinishedGoodsService.create_outbound(commit=False)
            for wh_id, items in group_by_wh.items():
                fg_lines = [
                    {
                        "batch_id": a.batch_id,
                        "quantity": qty,
                        "remark": f"发货计划 {plan.shipment_plan_no} 批次分配#{a.id}",
                    }
                    for a, qty in items
                ]
                FinishedGoodsService.create_outbound(
                    warehouse_id=wh_id,
                    lines=fg_lines,
                    document_no=f"FG-OUT-{plan.shipment_plan_no}-{wh_id}",  # ⭐ 可追溯
                    document_time=now_beijing(),
                    created_by=operator_id,
                    remark=f"发货计划 {plan.shipment_plan_no} 出库",
                    commit=False,          # ⭐ 交给本方法统一提交
                )

            # 4. 回写 allocation / line 已发数量
            for a, qty in pending:
                a.shipped_quantity = (a.shipped_quantity or Decimal("0")) + qty

            for line in lines:
                total = db.session.query(
                    func.coalesce(
                        func.sum(ShipmentPlanAllocation.shipped_quantity), 0)
                ).filter_by(shipment_line_id=line.id).scalar() or Decimal("0")
                line.shipped_quantity = _q6(total)

            # 5. 更新计划状态
            all_done = all(line.shipped_quantity >= line.quantity for line in lines)
            plan.status = "SHIPPED" if all_done else "PARTIALLY_SHIPPED"
            if all_done and not plan.actual_ship_time:
                plan.actual_ship_time = now_beijing()

            db.session.commit()
        except FinishedGoodsError as e:
            db.session.rollback()
            raise ShipmentPlanError(f"发货出库失败: {e}")
        except ShipmentPlanError:
            db.session.rollback()
            raise
        except Exception as e:
            db.session.rollback()
            raise ShipmentPlanError(f"发货失败: {e}")

        return plan

    # ---------- 私有：扣减/消耗生产计划预留 ----------
    @staticmethod
    def _consume_production_reservation(
        warehouse_id: int, batch_id: int, quantity: Decimal
    ):
        """
        按 FIFO（created_at, id）顺序消耗指定仓库+批次上 ACTIVE 的
        PRODUCTION_PLAN 预留。剩余量变 0 的预留置为 CONSUMED。
        不足则抛异常（由 ship_plan 统一回滚）。
        """
        need = _q6(quantity)
        reservations = (
            FinishedGoodsReservation.query
            .filter(
                FinishedGoodsReservation.warehouse_id == warehouse_id,
                FinishedGoodsReservation.batch_id == batch_id,
                FinishedGoodsReservation.reservation_type == "PRODUCTION_PLAN",
                FinishedGoodsReservation.status == "ACTIVE",
                FinishedGoodsReservation.remaining_quantity > 0,
            )
            .order_by(FinishedGoodsReservation.created_at.asc(),
                      FinishedGoodsReservation.id.asc())
            .with_for_update()
            .all()
        )
        for res in reservations:
            if need <= 0:
                break
            take = min(res.remaining_quantity, need)
            res.remaining_quantity -= take
            need -= take
            if res.remaining_quantity <= 0:
                res.remaining_quantity = Decimal("0")
                res.status = "CONSUMED"

        if need > 0:
            raise ShipmentPlanError(
                f"仓库#{warehouse_id} 批次#{batch_id} 预留量不足，"
                f"尚缺 {need}"
            )