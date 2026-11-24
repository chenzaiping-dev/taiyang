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

from app.utils.datetime_utils import now_beijing, now_beijing_naive, SUMMARY_EXCLUDED_STATUSES
from app.utils.decimal_utils import _q3, _q6
from app.utils.datetime_utils import util_parse_date, util_parse_date_end

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
    def get_plan(plan_id: int):
        """按 ID 读取发货计划；未找到时返回 None。"""
        return db.session.get(ShipmentPlan, plan_id)

    @staticmethod
    def get_plan_edit_context(plan_id: int):
        """读取编辑页所需数据，控制器不直接查询计划/业绩/分配表。"""
        plan = db.session.get(ShipmentPlan, plan_id)
        if not plan:
            return None
        performance = db.session.get(SalesPerformance, plan.performance_id)
        reservations = ShipmentPlanService.get_available_reservations(
            plan.performance_id
        )
        existing_rows = (
            db.session.query(
                ShipmentPlanAllocation.warehouse_id,
                ShipmentPlanAllocation.batch_id,
                ShipmentPlanAllocation.planned_quantity,
                ShipmentPlanLine.product_name,
                ShipmentPlanLine.product_specification,
                ShipmentPlanLine.unit,
                ShipmentPlanLine.unit_price,
            )
            .join(
                ShipmentPlanLine,
                ShipmentPlanLine.id == ShipmentPlanAllocation.shipment_line_id,
            )
            .filter(ShipmentPlanLine.shipment_plan_id == plan.id)
            .all()
        )
        existing_map = {
            (row.warehouse_id, row.batch_id): dict(row._mapping)
            for row in existing_rows
        }
        return {
            "plan": plan,
            "performance": performance,
            "reservations": reservations,
            "existing_map": existing_map,
        }

    @staticmethod
    def get_existing_allocation_prices(plan_id: int):
        """返回编辑计划已有分配的单价映射，供无金额编辑权限时保留原值。"""
        rows = (
            db.session.query(
                ShipmentPlanAllocation.warehouse_id,
                ShipmentPlanAllocation.batch_id,
                ShipmentPlanLine.unit_price,
            )
            .join(
                ShipmentPlanLine,
                ShipmentPlanLine.id == ShipmentPlanAllocation.shipment_line_id,
            )
            .filter(ShipmentPlanLine.shipment_plan_id == plan_id)
            .all()
        )
        return {
            (row.warehouse_id, row.batch_id): row.unit_price
            for row in rows
        }

    @staticmethod
    def get_update_error_context(plan_id: int):
        """更新失败后重新取得模板所需的持久化上下文。"""
        plan = db.session.get(ShipmentPlan, plan_id)
        if not plan:
            return None
        return {
            "plan": plan,
            "performance": db.session.get(SalesPerformance, plan.performance_id),
            "reservations": ShipmentPlanService.get_available_reservations(
                plan.performance_id
            ),
        }

    @staticmethod
    def get_detail_context(plan_id: int):
        """读取发货计划详情页全部数据。"""
        plan = db.session.get(ShipmentPlan, plan_id)
        if not plan:
            return None
        performance = db.session.get(SalesPerformance, plan.performance_id)
        creator = (
            db.session.get(Employee, plan.created_by)
            if plan.created_by else None
        )
        lines = (
            ShipmentPlanLine.query
            .filter_by(shipment_plan_id=plan.id)
            .order_by(ShipmentPlanLine.id.asc())
            .all()
        )
        document = FinishedGoodsDocument.query.filter_by(
            document_no=f"FG-OUT-{plan.shipment_plan_no}"
        ).first()

        allocations_map = {}
        for line in lines:
            allocations_map[line.id] = (
                db.session.query(
                    ShipmentPlanAllocation.id,
                    ShipmentPlanAllocation.warehouse_id,
                    Warehouse.warehouse_code,
                    Warehouse.warehouse_name,
                    ShipmentPlanAllocation.batch_id,
                    FinishedGoodsBatch.batch_no,
                    FinishedGoodsBatch.product_name.label("fg_product_name"),
                    FinishedGoodsBatch.product_specification.label("fg_product_specification"),
                    ShipmentPlanAllocation.planned_quantity,
                    ShipmentPlanAllocation.shipped_quantity,
                )
                .join(Warehouse, Warehouse.id == ShipmentPlanAllocation.warehouse_id)
                .join(
                    FinishedGoodsBatch,
                    FinishedGoodsBatch.id == ShipmentPlanAllocation.batch_id,
                )
                .filter(ShipmentPlanAllocation.shipment_line_id == line.id)
                .all()
            )
        return {
            "plan": plan,
            "performance": performance,
            "creator": creator,
            "document": document,
            "lines": lines,
            "allocations_map": allocations_map,
        }

    @staticmethod
    def list_plans_query(params=None, page: int = 1, page_size: int = 20,
                         include_summary: bool = False):
        """发货计划列表查询；HTML 列表和 Excel 导出共用此查询入口。"""
        params = params or {}
        creator = db.aliased(Employee)
        keyword = (params.get("keyword") or "").strip() or None
        status = (params.get("status") or "").strip() or None
        performance_id = (params.get("performance_id") or "").strip() or None
        performance_no = (params.get("performance_no") or "").strip() or None
        company_name = (params.get("company_name") or "").strip() or None
        product_name = (params.get("product_name") or "").strip() or None

        date_from = util_parse_date(params.get("ship_time_from"))
        date_to = util_parse_date_end(params.get("ship_time_to"))
        shipped_subq = (
            select(
                ShipmentPlanLine.shipment_plan_id.label("pid"),
                func.coalesce(
                    func.sum(ShipmentPlanLine.shipped_quantity), 0
                ).label("total_shipped"),
                func.coalesce(
                    func.sum(ShipmentPlanLine.quantity), 0
                ).label("total_planned"),
            )
            .group_by(ShipmentPlanLine.shipment_plan_id)
            .subquery()
        )
        stmt = (
            select(
                ShipmentPlan.id,
                ShipmentPlan.shipment_plan_no,
                ShipmentPlan.performance_id,
                SalesPerformance.performance_no,
                SalesPerformance.company_name,
                SalesPerformance.product_name.label("product_name"),
                SalesPerformance.product_specification,
                ShipmentPlan.destination,
                ShipmentPlan.planned_ship_time,
                ShipmentPlan.status,
                ShipmentPlan.created_by,
                creator.real_name.label("created_by_name"),
                ShipmentPlan.created_at,
                func.coalesce(shipped_subq.c.total_planned, 0).label("planned_qty"),
                func.coalesce(shipped_subq.c.total_shipped, 0).label("shipped_qty"),
            )
            .join(creator, creator.id == ShipmentPlan.created_by, isouter=True)
            .join(
                SalesPerformance,
                SalesPerformance.id == ShipmentPlan.performance_id,
                isouter=True,
            )
            .join(shipped_subq, shipped_subq.c.pid == ShipmentPlan.id, isouter=True)
            .order_by(ShipmentPlan.id.desc())
        )
        if keyword:
            stmt = stmt.where(
                ShipmentPlan.shipment_plan_no.like(f"%{keyword}%")
            )
        if status:
            stmt = stmt.where(ShipmentPlan.status == status)
        if performance_id:
            stmt = stmt.where(ShipmentPlan.performance_id == performance_id)
        if performance_no:
            stmt = stmt.where(SalesPerformance.performance_no.like(f"%{performance_no}%"))
        if company_name:
            stmt = stmt.where(SalesPerformance.company_name.like(f"%{company_name}%"))
        if product_name:
            stmt = stmt.where(SalesPerformance.product_name.like(f"%{product_name}%"))
        if date_from:
            stmt = stmt.where(ShipmentPlan.planned_ship_time >= date_from)
        if date_to:
            stmt = stmt.where(ShipmentPlan.planned_ship_time < date_to)

        summary = None
        if include_summary:
            summary_sq = stmt.where(
                ShipmentPlan.status.notin_(SUMMARY_EXCLUDED_STATUSES)
            ).order_by(None).subquery()
            summary = db.session.execute(
                select(
                    func.coalesce(func.sum(summary_sq.c.planned_qty), 0).label("planned_qty")
                )
            ).mappings().one()

        page = max(int(page or 1), 1)
        page_size = max(int(page_size or 20), 1)
        total = db.session.execute(
            select(func.count()).select_from(stmt.subquery())
        ).scalar() or 0
        stmt = stmt.offset((page - 1) * page_size).limit(page_size)
        rows = [
            dict(row)
            for row in db.session.execute(stmt).mappings().all()
        ]
        return (rows, total, dict(summary)) if include_summary else (rows, total)

    @staticmethod
    def list_plan_details_query(params=None, page: int = 1, page_size: int = 20,
                                include_summary: bool = False):
        """发货计划明细列表查询：每条仓库/批号分配记录一行，排除草稿。"""
        params = params or {}
        creator = db.aliased(Employee)
        keyword = (params.get("keyword") or "").strip() or None
        status = (params.get("status") or "").strip() or None
        performance_no = (params.get("performance_no") or "").strip() or None
        company_name = (params.get("company_name") or "").strip() or None
        product_name = (params.get("product_name") or "").strip() or None
        try:
            warehouse_id = int(params.get("warehouse_id") or 0) or None
        except (TypeError, ValueError):
            warehouse_id = None
        batch_keyword = (params.get("batch_keyword") or "").strip() or None
        date_from = util_parse_date(params.get("ship_time_from"))
        date_to = util_parse_date_end(params.get("ship_time_to"))

        totals = (
            select(
                ShipmentPlanLine.shipment_plan_id.label("pid"),
                func.coalesce(func.sum(ShipmentPlanLine.quantity), 0).label("total_planned"),
                func.coalesce(func.sum(ShipmentPlanLine.shipped_quantity), 0).label("total_shipped"),
            )
            .group_by(ShipmentPlanLine.shipment_plan_id)
            .subquery()
        )
        stmt = (
            select(
                ShipmentPlan.id,
                ShipmentPlan.shipment_plan_no,
                ShipmentPlan.performance_id,
                SalesPerformance.performance_no,
                SalesPerformance.company_name,
                ShipmentPlanLine.product_name.label("product_name"),
                ShipmentPlanLine.product_specification,
                ShipmentPlan.destination,
                ShipmentPlan.planned_ship_time,
                ShipmentPlan.status,
                ShipmentPlan.created_by,
                creator.real_name.label("created_by_name"),
                ShipmentPlan.created_at,
                func.coalesce(totals.c.total_planned, 0).label("planned_qty"),
                func.coalesce(totals.c.total_shipped, 0).label("shipped_qty"),
                Warehouse.id.label("warehouse_id"),
                Warehouse.warehouse_code,
                Warehouse.warehouse_name,
                FinishedGoodsBatch.id.label("batch_id"),
                FinishedGoodsBatch.batch_no,
                FinishedGoodsBatch.process_order_id,
                FinishedGoodsBatch.product_name.label("fg_product_name"),
                FinishedGoodsBatch.product_specification.label("fg_product_specification"),
                ProcessOrder.process_order_no.label("process_order_no"),
                ShipmentPlanAllocation.planned_quantity.label("shipment_quantity"),
            )
            .join(ShipmentPlanLine, ShipmentPlanLine.shipment_plan_id == ShipmentPlan.id)
            .join(ShipmentPlanAllocation,
                  ShipmentPlanAllocation.shipment_line_id == ShipmentPlanLine.id)
            .join(Warehouse, Warehouse.id == ShipmentPlanAllocation.warehouse_id)
            .join(FinishedGoodsBatch,
                  FinishedGoodsBatch.id == ShipmentPlanAllocation.batch_id)
            .join(ProcessOrder, ProcessOrder.id == FinishedGoodsBatch.process_order_id, isouter=True)
            .join(creator, creator.id == ShipmentPlan.created_by, isouter=True)
            .join(SalesPerformance,
                  SalesPerformance.id == ShipmentPlan.performance_id, isouter=True)
            .join(totals, totals.c.pid == ShipmentPlan.id, isouter=True)
            .where(ShipmentPlan.status != "DRAFT")
            .order_by(ShipmentPlan.id.desc(), ShipmentPlanLine.id.asc(),
                      ShipmentPlanAllocation.id.asc())
        )
        if keyword:
            stmt = stmt.where(ShipmentPlan.shipment_plan_no.like(f"%{keyword}%"))
        if status:
            stmt = stmt.where(ShipmentPlan.status == status)
        if performance_no:
            stmt = stmt.where(SalesPerformance.performance_no.like(f"%{performance_no}%"))
        if company_name:
            stmt = stmt.where(SalesPerformance.company_name.like(f"%{company_name}%"))
        if product_name:
            stmt = stmt.where(ShipmentPlanLine.product_name.like(f"%{product_name}%"))
        if warehouse_id:
            stmt = stmt.where(Warehouse.id == warehouse_id)
        if batch_keyword:
            stmt = stmt.where(FinishedGoodsBatch.batch_no.like(f"%{batch_keyword}%"))
        if date_from:
            stmt = stmt.where(ShipmentPlan.planned_ship_time >= date_from)
        if date_to:
            stmt = stmt.where(ShipmentPlan.planned_ship_time < date_to)

        summary = None
        if include_summary:
            summary_sq = stmt.where(
                ShipmentPlan.status.notin_(SUMMARY_EXCLUDED_STATUSES)                
            ).order_by(None).subquery()
            summary = db.session.execute(
                select(
                    func.coalesce(func.sum(summary_sq.c.shipment_quantity), 0)
                    .label("shipment_quantity")
                )
            ).mappings().one()
            summary = dict(summary)

        page = max(int(page or 1), 1)
        page_size = max(int(page_size or 20), 1)
        total = db.session.execute(
            select(func.count()).select_from(stmt.order_by(None).subquery())
        ).scalar() or 0
        stmt = stmt.offset((page - 1) * page_size).limit(page_size)
        rows = [dict(row) for row in db.session.execute(stmt).mappings().all()]
        return (rows, total, summary) if include_summary else (rows, total)

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
        if perf.return_of_performance_id is not None:
            raise ShipmentPlanError("退货业绩单不能创建发货计划")
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
        if not perf or perf.return_of_performance_id is not None:
            raise ShipmentPlanError("退货业绩单不能创建或编辑发货计划")

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
        """
        确认发货计划时完成预留转移：
        1. 锁定发货计划、生产计划、明细和分配行；
        2. 从本生产计划的 PRODUCTION_PLAN 预留中扣减本计划全部分配数量；
        3. 为每条 allocation 建立 SHIPMENT 预留，继续锁住待发库存；
        4. 最后才把计划状态改为 CONFIRMED。

        整个过程同一事务。任一批次预留不足，所有扣减/新增预留/状态修改一起回滚。
        """
        plan = (
            ShipmentPlan.query.filter_by(id=plan_id)
            .with_for_update()
            .first()
        )
        if not plan:
            raise ShipmentPlanError("发货计划不存在")
        if plan.status != STATUS_DRAFT:
            raise ShipmentPlanError(
                f"只有草稿状态可执行【确认】，当前：{plan.status}"
            )

        try:
            production_plan = (
                ProductionPlan.query
                .filter_by(performance_id=plan.performance_id)
                .with_for_update()
                .first()
            )
            if not production_plan:
                raise ShipmentPlanError(
                    f"业绩单 #{plan.performance_id} 没有关联的生产计划，"
                    "无法确认发货计划"
                )

            lines = (
                ShipmentPlanLine.query
                .filter_by(shipment_plan_id=plan.id)
                .order_by(ShipmentPlanLine.id.asc())
                .with_for_update()
                .all()
            )
            if not lines:
                raise ShipmentPlanError("发货计划无明细，不能确认")

            allocs = (
                ShipmentPlanAllocation.query
                .filter(
                    ShipmentPlanAllocation.shipment_line_id.in_(
                        [line.id for line in lines]
                    )
                )
                .order_by(ShipmentPlanAllocation.id.asc())
                .with_for_update()
                .all()
            )
            if not allocs:
                raise ShipmentPlanError("发货计划无批次分配，不能确认")

            # 先汇总同仓库、同批次的需求，再统一扣减生产计划预留。
            # 避免同一批次拆成多条 allocation 时重复读取/竞争同一预留行。
            demand = {}
            for alloc in allocs:
                qty = _q6(alloc.planned_quantity or 0)
                if qty <= 0:
                    raise ShipmentPlanError(
                        f"批次分配 #{alloc.id} 的计划数量必须大于 0"
                    )
                key = (int(alloc.warehouse_id), int(alloc.batch_id))
                demand[key] = demand.get(key, Decimal("0")) + qty

            for (warehouse_id, batch_id), qty in demand.items():
                ShipmentPlanService._consume_production_reservation(
                    production_plan_id=production_plan.id,
                    warehouse_id=warehouse_id,
                    batch_id=batch_id,
                    quantity=qty,
                )

            # 生产计划预留转成发货计划预留。
            # 总 ACTIVE 预留量不变，避免其他生产计划在“已确认但未出库”期间
            # 再次占用同一批实物库存。
            for alloc in allocs:
                existing = (
                    FinishedGoodsReservation.query
                    .filter_by(
                        reservation_type="SHIPMENT",
                        shipment_allocation_id=alloc.id,
                    )
                    .with_for_update()
                    .first()
                )
                if existing:
                    raise ShipmentPlanError(
                        f"分配记录 #{alloc.id} 已存在发货预留，"
                        "请检查是否重复确认或数据异常"
                    )

                qty = _q6(alloc.planned_quantity)
                db.session.add(FinishedGoodsReservation(
                    reservation_no=f"RES-SP-{plan.shipment_plan_no}-{alloc.id}",
                    reservation_type="SHIPMENT",
                    production_plan_id=None,
                    shipment_allocation_id=alloc.id,
                    warehouse_id=alloc.warehouse_id,
                    batch_id=alloc.batch_id,
                    reserved_quantity=qty,
                    remaining_quantity=qty,
                    status="ACTIVE",
                    goods_type="STAPLE",
                    remark=f"发货计划 {plan.shipment_plan_no} 确认占用",
                    created_at=now_beijing_naive(),
                ))

            plan.status = STATUS_CONFIRMED
            plan.updated_at = now_beijing_naive()
            db.session.commit()
        except ShipmentPlanError:
            db.session.rollback()
            raise
        except Exception as e:
            db.session.rollback()
            raise ShipmentPlanError(f"确认发货计划失败: {e}")

        return plan

    # ---------- 取消 ----------
    @staticmethod
    def cancel_plan(plan_id: int, operator_id: int = None) -> ShipmentPlan:
        """
        取消发货计划：
        - DRAFT：未占用预留，直接取消；
        - 新流程 CONFIRMED：释放 SHIPMENT 预留，并恢复确认时转出的全部生产计划预留；
        - 新流程已发货：先冲销成品出库，再释放 SHIPMENT 预留，并恢复全部生产计划预留；
        - 旧版本已确认/已发货单据：兼容旧逻辑（旧确认阶段没有 SHIPMENT 预留）。

        整体单事务：任一环节失败全部回滚。
        """
        plan = (
            ShipmentPlan.query.filter_by(id=plan_id)
            .with_for_update()
            .first()
        )
        if not plan:
            raise ShipmentPlanError("发货计划不存在")
        if plan.status == STATUS_CANCELLED:
            raise ShipmentPlanError("发货计划已取消，无需重复操作")

        lines = (
            ShipmentPlanLine.query
            .filter_by(shipment_plan_id=plan.id)
            .order_by(ShipmentPlanLine.id.asc())
            .with_for_update()
            .all()
        )
        if not lines:
            plan.status = STATUS_CANCELLED
            try:
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                raise ShipmentPlanError(f"取消失败: {e}")
            return plan

        line_ids = [line.id for line in lines]
        allocs = (
            ShipmentPlanAllocation.query
            .filter(ShipmentPlanAllocation.shipment_line_id.in_(line_ids))
            .order_by(ShipmentPlanAllocation.id.asc())
            .with_for_update()
            .all()
        )
        allocation_ids = [a.id for a in allocs]

        shipped_snapshot = [
            (a.warehouse_id, a.batch_id, _q6(a.shipped_quantity or 0))
            for a in allocs
            if _q6(a.shipped_quantity or 0) > 0
        ]
        has_shipped = bool(shipped_snapshot)

        # 是否存在 SHIPMENT 类型的预留记录，是区分新旧流程的持久化标记。
        # 即使发货时该预留已 RELEASED，记录仍保留，可用于取消时恢复完整计划量。
        shipment_reservations = []
        if allocation_ids:
            shipment_reservations = (
                FinishedGoodsReservation.query
                .filter(
                    FinishedGoodsReservation.reservation_type == "SHIPMENT",
                    FinishedGoodsReservation.shipment_allocation_id.in_(allocation_ids),
                )
                .order_by(FinishedGoodsReservation.id.asc())
                .with_for_update()
                .all()
            )
        uses_reservation_transfer = bool(shipment_reservations)

        try:
            # 1. 如果已经发生实际出库，先冲销出库单，恢复成品实物库存。
            if has_shipped:
                doc_no = f"FG-OUT-{plan.shipment_plan_no}"
                out_doc = (
                    FinishedGoodsDocument.query
                    .filter_by(document_no=doc_no)
                    .with_for_update()
                    .first()
                )
                if not out_doc:
                    raise ShipmentPlanError(
                        f"发货计划 {plan.shipment_plan_no} 存在已发数量，"
                        f"但找不到对应出库单 {doc_no}，无法安全取消"
                    )

                FinishedGoodsService.reverse_document(
                    document_id=out_doc.id,
                    reason=f"[SHIP_PLAN_CANCEL] 取消发货计划 {plan.shipment_plan_no}",
                    created_by=operator_id,
                    commit=False,
                )

            # 2. 释放本发货计划仍 ACTIVE 的 SHIPMENT 预留。
            #    已发货时通常已在 ship_plan 中释放；这里兼容未发货取消。
            for reservation in shipment_reservations:
                if reservation.status == "ACTIVE":
                    reservation.remaining_quantity = Decimal("0")
                    reservation.status = "RELEASED"

            # 3. 恢复生产计划预留。
            production_plan = None
            if uses_reservation_transfer:
                production_plan = (
                    ProductionPlan.query
                    .filter_by(performance_id=plan.performance_id)
                    .with_for_update()
                    .first()
                )
                if not production_plan:
                    raise ShipmentPlanError(
                        f"业绩单 #{plan.performance_id} 没有关联的生产计划，"
                        "无法恢复生产计划预留"
                    )

                # 确认时转出的是全部 planned_quantity，不是实际 shipped_quantity。
                # 即使确认后尚未发货，也必须完整恢复。
                for alloc in allocs:
                    ShipmentPlanService._restore_production_reservation(
                        production_plan_id=production_plan.id,
                        warehouse_id=alloc.warehouse_id,
                        batch_id=alloc.batch_id,
                        quantity=_q6(alloc.planned_quantity or 0),
                    )
            elif has_shipped:
                # 兼容旧版本：旧代码在 ship_plan() 时才消耗生产计划预留，
                # 所以旧单据只恢复实际已消耗的 shipped_quantity。
                production_plan = (
                    ProductionPlan.query
                    .filter_by(performance_id=plan.performance_id)
                    .with_for_update()
                    .first()
                )
                if not production_plan:
                    raise ShipmentPlanError(
                        f"业绩单 #{plan.performance_id} 没有关联的生产计划，"
                        "无法恢复旧流程已消耗的预留"
                    )
                for wh_id, batch_id, qty in shipped_snapshot:
                    ShipmentPlanService._restore_production_reservation(
                        production_plan_id=production_plan.id,
                        warehouse_id=wh_id,
                        batch_id=batch_id,
                        quantity=qty,
                    )

            # 4. 已出库单据被冲销后，清零计划已发数量。
            if has_shipped:
                for alloc in allocs:
                    alloc.shipped_quantity = Decimal("0")
                for line in lines:
                    line.shipped_quantity = Decimal("0")

            plan.status = STATUS_CANCELLED
            plan.actual_ship_time = None
            plan.updated_at = now_beijing_naive()
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
        """
        执行已确认发货计划：
        - 新流程：确认阶段已把生产计划预留转为 SHIPMENT 预留；
          发货时只释放本计划 SHIPMENT 预留，再执行成品出库。
        - 兼容旧流程：若完全不存在 SHIPMENT 预留，则沿用旧的发货时扣减
          PRODUCTION_PLAN 预留逻辑。
        """
        plan = (
            ShipmentPlan.query.filter_by(id=plan_id)
            .with_for_update()
            .first()
        )
        if not plan:
            raise ShipmentPlanError("发货计划不存在")
        if plan.status != STATUS_CONFIRMED:
            raise ShipmentPlanError(
                f"只有【已确认】状态可执行发货，当前：{plan.status}"
            )

        lines = (
            ShipmentPlanLine.query
            .filter_by(shipment_plan_id=plan.id)
            .order_by(ShipmentPlanLine.id.asc())
            .with_for_update()
            .all()
        )
        if not lines:
            raise ShipmentPlanError("发货计划无明细，不能发货")

        allocs = (
            ShipmentPlanAllocation.query
            .filter(ShipmentPlanAllocation.shipment_line_id.in_(
                [line.id for line in lines]
            ))
            .order_by(ShipmentPlanAllocation.id.asc())
            .with_for_update()
            .all()
        )
        if not allocs:
            raise ShipmentPlanError("发货计划无批次分配，不能发货")

        pending = []
        for alloc in allocs:
            qty = _q6(
                (alloc.planned_quantity or Decimal("0"))
                - (alloc.shipped_quantity or Decimal("0"))
            )
            if qty > 0:
                pending.append((alloc, qty))
        if not pending:
            raise ShipmentPlanError("没有待发货数量")

        production_plan = (
            ProductionPlan.query
            .filter_by(performance_id=plan.performance_id)
            .with_for_update()
            .first()
        )
        if not production_plan:
            raise ShipmentPlanError(
                f"业绩单 #{plan.performance_id} 没有关联的生产计划，"
                "无法确定生产计划预留"
            )

        allocation_ids = [alloc.id for alloc, _ in pending]
        try:
            shipment_reservations = (
                FinishedGoodsReservation.query
                .filter(
                    FinishedGoodsReservation.reservation_type == "SHIPMENT",
                    FinishedGoodsReservation.shipment_allocation_id.in_(allocation_ids),
                )
                .order_by(FinishedGoodsReservation.id.asc())
                .with_for_update()
                .all()
            )

            if shipment_reservations:
                # 新流程：每个 allocation 的发货预留必须覆盖其待发数量。
                by_allocation = {}
                for reservation in shipment_reservations:
                    by_allocation.setdefault(
                        reservation.shipment_allocation_id, []
                    ).append(reservation)

                for alloc, qty in pending:
                    rows = by_allocation.get(alloc.id, [])
                    active_qty = sum(
                        (
                            _q6(r.remaining_quantity or 0)
                            for r in rows
                            if r.status == "ACTIVE"
                        ),
                        Decimal("0"),
                    )
                    if active_qty != qty:
                        raise ShipmentPlanError(
                            f"批次分配 #{alloc.id} 的发货预留不匹配："
                            f"当前有效预留 {active_qty}，待发数量 {qty}。"
                            "请检查预留记录，暂不能出库"
                        )

                # 在本事务内释放本计划自己的发货预留，随后 create_outbound
                # 的“库存 - ACTIVE 预留”校验才能使用这部分库存。
                for reservation in shipment_reservations:
                    if reservation.status == "ACTIVE":
                        reservation.remaining_quantity = Decimal("0")
                        reservation.status = "RELEASED"
            else:
                # 兼容部署前已经确认、但尚无 SHIPMENT 预留的老计划。
                # 新确认的计划不会走到这里。
                for alloc, qty in pending:
                    ShipmentPlanService._consume_production_reservation(
                        production_plan_id=production_plan.id,
                        warehouse_id=alloc.warehouse_id,
                        batch_id=alloc.batch_id,
                        quantity=qty,
                    )

            fg_lines = [
                {
                    "warehouse_id": alloc.warehouse_id,
                    "batch_id": alloc.batch_id,
                    "quantity": qty,
                    "remark": (
                        f"发货计划 {plan.shipment_plan_no} "
                        f"批次分配#{alloc.id}"
                    ),
                }
                for alloc, qty in pending
            ]

            FinishedGoodsService.create_outbound(
                lines=fg_lines,
                document_no=f"FG-OUT-{plan.shipment_plan_no}",
                document_time=now_beijing(),
                created_by=operator_id,
                remark=f"[SHIP_PLAN_OUTBOUND] 出库 {plan.shipment_plan_no}",
                commit=False,
            )

            for alloc, qty in pending:
                alloc.shipped_quantity = (
                    (alloc.shipped_quantity or Decimal("0")) + qty
                )

            for line in lines:
                total = db.session.query(
                    func.coalesce(
                        func.sum(ShipmentPlanAllocation.shipped_quantity), 0
                    )
                ).filter_by(shipment_line_id=line.id).scalar() or Decimal("0")
                line.shipped_quantity = _q6(total)

            all_done = all(
                line.shipped_quantity >= line.quantity for line in lines
            )
            plan.status = (
                STATUS_SHIPPED if all_done else STATUS_PARTIALLY_SHIPPED
            )
            if all_done and not plan.actual_ship_time:
                plan.actual_ship_time = now_beijing()
            plan.updated_at = now_beijing_naive()

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
        production_plan_id: int,       # ⭐ 新增，必传
        warehouse_id: int,
        batch_id: int,
        quantity: Decimal
    ):
        """
        按 FIFO（created_at, id）顺序消耗指定仓库+批次上 ACTIVE 的
        PRODUCTION_PLAN 预留。剩余量变 0 的预留置为 CONSUMED；
        转移去向由对应的 SHIPMENT 预留记录表示。不足则抛异常，由外层事务回滚。
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
                # 现有数据库 CHECK 约束允许 CONSUMED/RELEASED 的 remaining=0；
                # SHIPMENT 预留记录负责标识转移去向。
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
        将确认发货计划时转出的生产计划预留补回。
        优先复用同一 (plan, warehouse, batch) 下最近的预留行（TRANSFERRED/CONSUMED/ACTIVE），
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

        # 优先复用已存在的预留（TRANSFERRED / CONSUMED / ACTIVE）
        res = (
            FinishedGoodsReservation.query
            .filter(
                FinishedGoodsReservation.production_plan_id == production_plan_id,
                FinishedGoodsReservation.warehouse_id == warehouse_id,
                FinishedGoodsReservation.batch_id == batch_id,
                FinishedGoodsReservation.reservation_type == "PRODUCTION_PLAN",
                FinishedGoodsReservation.status.in_(["TRANSFERRED", "CONSUMED", "ACTIVE"]),
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