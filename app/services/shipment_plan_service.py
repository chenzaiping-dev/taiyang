from sqlalchemy import select, case, func, or_
from decimal import Decimal
from datetime import datetime, timezone, timedelta

from app.extensions import db
from app.generated.models.employees import Employee
from app.generated.models.warehouses import Warehouse
from app.generated.models.sales_performance import SalesPerformance
from app.generated.models.finished_goods_documents import FinishedGoodsDocument

from app.generated.models.finished_goods_batches import FinishedGoodsBatch
from app.generated.models.finished_goods_inventory import FinishedGoodsInventory
from app.generated.models.finished_goods_reservations import FinishedGoodsReservation
from app.generated.models.shipment_plans import ShipmentPlan
from app.generated.models.shipment_plan_lines import ShipmentPlanLine
from app.generated.models.shipment_plan_allocations import ShipmentPlanAllocation
from app.generated.models.process_orders import ProcessOrder
from app.generated.models.production_plans import ProductionPlan
from app.services.finished_goods_service import FinishedGoodsError, FinishedGoodsService

from app.utils.datetime_utils import now_beijing, now_beijing_naive
from app.utils.decimal_utils import _q3, _q6

# ==================== 状态流转 ====================

STATUS_DRAFT = "DRAFT"
STATUS_CONFIRMED = "CONFIRMED"
STATUS_PARTIALLY_SHIPPED = "PARTIALLY_SHIPPED"
STATUS_SHIPPED = "SHIPPED"
STATUS_CANCELLED = "CANCELLED"


def _get_plan_or_raise(plan_id: int) -> ShipmentPlan:
    plan = db.session.get(ShipmentPlan, plan_id)
    if not plan:
        raise ShipmentPlanError("发货计划不存在")
    return plan

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
                created_at=now_beijing_naive(),
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
                    created_at=now_beijing_naive(),
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
                f"只有草稿状态可执行【确认】，当前：{plan.status}")
        plan.status = STATUS_CONFIRMED
        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            raise ShipmentPlanError(f"标记确认失败: {e}")
        return plan

    # ---------- 取消 ----------
    @staticmethod
    def cancel_plan(plan_id: int, operator_id: int = None) -> ShipmentPlan:
        """
        取消发货计划：
        - DRAFT / CONFIRMED            → 只置 CANCELLED
        - PARTIALLY_SHIPPED / SHIPPED  → 撤销已发生的成品出库单（冲销库存），
                                         还原被消耗的生产计划预留，再置 CANCELLED
        整体单事务：任一环节失败全部回滚。
        """
        plan = _get_plan_or_raise(plan_id)
        if plan.status == STATUS_CANCELLED:
            raise ShipmentPlanError("发货计划已取消，无需重复操作")

        lines = ShipmentPlanLine.query.filter_by(
            shipment_plan_id=plan.id
        ).order_by(ShipmentPlanLine.id.asc()).all()
        if not lines:
            # 无明细，直接取消
            plan.status = STATUS_CANCELLED
            try:
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                raise ShipmentPlanError(f"取消失败: {e}")
            return plan

        line_ids = [l.id for l in lines]
        allocs = (
            ShipmentPlanAllocation.query
            .filter(ShipmentPlanAllocation.shipment_line_id.in_(line_ids))
            .order_by(ShipmentPlanAllocation.id.asc())
            .all()
        )

        # 快照"每个 allocation 已发数量"，必须在撤销出库单之前取，
        # 因为 reverse_document 联动会把它冲减为 0
        shipped_snapshot = [
            (a.warehouse_id, a.batch_id, _q6(a.shipped_quantity or 0))
            for a in allocs
            if _q6(a.shipped_quantity or 0) > 0
        ]

        has_shipped = any(qty > 0 for _, _, qty in shipped_snapshot)
        print("has shipped:", has_shipped, shipped_snapshot)

        try:
            if has_shipped:
                # 1. 撤销成品出库单（内部会冲销库存、回滚 allocation.shipped_quantity 与 plan.status）
                doc_no = f"FG-OUT-{plan.shipment_plan_no}"
                out_doc = FinishedGoodsDocument.query.filter_by(
                    document_no=doc_no
                ).first()
                if not out_doc:
                    raise ShipmentPlanError(
                        f"发货计划 {plan.shipment_plan_no} 存在已发数量，"
                        f"但找不到对应出库单 {doc_no}，数据异常，无法安全取消"
                    )

                FinishedGoodsService.reverse_document(
                    document_id=out_doc.id,
                    reason=f"[CANCEL_SHIP_PLAN] 取消发货计划 {plan.shipment_plan_no}",
                    created_by=operator_id,
                    commit=False,          # 合并事务
                )

                # 2. 还原被消耗的生产计划预留
                production_plan = ProductionPlan.query.filter_by(
                    performance_id=plan.performance_id
                ).first()
                if not production_plan:
                    raise ShipmentPlanError(
                        f"业绩单 #{plan.performance_id} 没有关联的生产计划，"
                        f"无法还原预留"
                    )

                for wh_id, batch_id, qty in shipped_snapshot:
                    ShipmentPlanService._restore_production_reservation(
                        production_plan_id=production_plan.id,
                        warehouse_id=wh_id,
                        batch_id=batch_id,
                        quantity=qty,
                    )

                # 3. 清空 allocation / line 已发数量
                for a in allocs:
                    a.shipped_quantity = Decimal("0")
                for line in lines:
                    line.shipped_quantity = Decimal("0")

            plan.status = STATUS_CANCELLED
            plan.actual_ship_time = None
            db.session.commit()
        except ShipmentPlanError:
            db.session.rollback()
            raise
        except FinishedGoodsError as e:
            db.session.rollback()
            raise ShipmentPlanError(f"取消失败，撤销出库异常: {e}")
        except Exception as e:
            db.session.rollback()
            raise ShipmentPlanError(f"取消失败: {e}")

        return plan

    @staticmethod
    def ship_plan(plan_id: int, operator_id: int = None) -> ShipmentPlan:
        plan = db.session.get(ShipmentPlan, plan_id)
        if not plan:
            raise ShipmentPlanError("发货计划不存在")
        if plan.status != "CONFIRMED":
            raise ShipmentPlanError(
                f"只有【已确认】状态可执行发货，当前：{plan.status}")

        # ⭐ 找到该发货计划对应的生产计划（1:1）
        production_plan = ProductionPlan.query.filter_by(
            performance_id=plan.performance_id
        ).first()
        if not production_plan:
            raise ShipmentPlanError(
                f"业绩单 #{plan.performance_id} 没有关联的生产计划，"
                f"无法确定要消耗的成品预留"
            )
        production_plan_id = production_plan.id

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

        # 1. 过滤出本次真正要发的 allocation
        pending = []
        for a in allocs:
            qty = (a.planned_quantity or Decimal("0")) - (a.shipped_quantity or Decimal("0"))
            if qty > 0:
                pending.append((a, _q6(qty)))
        if not pending:
            raise ShipmentPlanError("没有待发货数量")

        try:
            # 2. 先释放对应生产计划预留（否则可用量 = 库存 - ACTIVE 预留 会不够）
            for a, qty in pending:
                ShipmentPlanService._consume_production_reservation(
                    production_plan_id=production_plan_id,   # ⭐ 强校验：只消耗本计划
                    warehouse_id=a.warehouse_id,
                    batch_id=a.batch_id,
                    quantity=qty,
                )

            # 3. ⭐ 一次性构建全部出库行（每行自带 warehouse_id），一张单多仓
            fg_lines = [
                {
                    "warehouse_id": a.warehouse_id,        # ⭐ 行内自带仓库
                    "batch_id": a.batch_id,
                    "quantity": qty,
                    "remark": (
                        f"发货计划 {plan.shipment_plan_no} "
                        f"批次分配#{a.id}"
                    ),
                }
                for a, qty in pending
            ]

            FinishedGoodsService.create_outbound(
                lines=fg_lines,                            # ⭐ 新签名：lines 第一位
                document_no=f"FG-OUT-{plan.shipment_plan_no}",  # ⭐ 不再带仓后缀
                document_time=now_beijing(),
                created_by=operator_id,
                remark=f"[SHIP_PLAN_OUTBOUND] 出库 {plan.shipment_plan_no}",
                commit=False,                              # ⭐ 交给本方法统一提交
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
            #print("[AFTER COMMIT]")
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
        production_plan_id: int,       # ⭐ 新增，必传
        warehouse_id: int,
        batch_id: int,
        quantity: Decimal
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
                FinishedGoodsReservation.production_plan_id == production_plan_id,  # ⭐ 关键
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
            print(f"[CONSUME] res.id={res.id}, before={res.remaining_quantity}, take={take}")
            res.remaining_quantity -= take
            need -= take
            if res.remaining_quantity <= 0:
                res.remaining_quantity = Decimal("0")
                res.status = "CONSUMED"

        if need > 0:
            print(f"[SHORT] wh={warehouse_id}, batch={batch_id}, need={need}")
            raise ShipmentPlanError(
                f"仓库#{warehouse_id} 批次#{batch_id} 预留量不足，"
                f"尚缺 {need}"
            )
        print(f"[CONSUME DONE] need={need}")

    @staticmethod
    def _restore_production_reservation(
        production_plan_id: int,
        warehouse_id: int,
        batch_id: int,
        quantity: Decimal,
    ):
        """
        将发货时消耗掉的生产计划预留补回。
        优先复用同一 (plan, warehouse, batch) 下最近的预留行（CONSUMED/ACTIVE），
        把它 remaining_quantity 加回、状态置回 ACTIVE；
        没有现存行则新建一条 ACTIVE。
        受 finished_goods_reservations 的 CHECK 约束：
          - ACTIVE 必须 remaining_quantity > 0
          - remaining_quantity <= reserved_quantity
        因此复用行时若 reserved_quantity < 补回量，需要同步抬升 reserved_quantity。
        """
        qty = _q6(quantity)
        if qty <= 0:
            return

        # 优先复用已存在的预留（CONSUMED 或 ACTIVE）
        res = (
            FinishedGoodsReservation.query
            .filter(
                FinishedGoodsReservation.production_plan_id == production_plan_id,
                FinishedGoodsReservation.warehouse_id == warehouse_id,
                FinishedGoodsReservation.batch_id == batch_id,
                FinishedGoodsReservation.reservation_type == "PRODUCTION_PLAN",
                FinishedGoodsReservation.status.in_(["CONSUMED", "ACTIVE"]),
            )
            .order_by(FinishedGoodsReservation.id.desc())
            .with_for_update()
            .first()
        )

        if res:
            new_remaining = _q6(res.remaining_quantity or 0) + qty
            # 保证 remaining <= reserved
            if new_remaining > _q6(res.reserved_quantity or 0):
                res.reserved_quantity = new_remaining
            res.remaining_quantity = new_remaining
            res.status = "ACTIVE"
        else:
            # 没有现存行：新建
            db.session.add(FinishedGoodsReservation(
                reservation_no=(
                    f"RES-SP-RESTORE-{production_plan_id}-"
                    f"{warehouse_id}-{batch_id}-{int(now_beijing_naive().timestamp())}"
                ),
                reservation_type="PRODUCTION_PLAN",
                production_plan_id=production_plan_id,
                shipment_allocation_id=None,
                warehouse_id=warehouse_id,
                batch_id=batch_id,
                reserved_quantity=qty,
                remaining_quantity=qty,
                status="ACTIVE",
                goods_type="STAPLE",
                remark="取消发货计划,还原预留",
                created_at=now_beijing_naive(),
            ))