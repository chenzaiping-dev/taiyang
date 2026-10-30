from decimal import Decimal, ROUND_HALF_UP
from sqlalchemy import select
from datetime import datetime

from app.extensions import db
from app.utils.datetime_utils import now_beijing_naive

from app.generated.models.process_orders import ProcessOrder
from app.generated.models.process_order_materials import ProcessOrderMaterial
from app.generated.models.materials import Material
from app.generated.models.factories import Factory
from app.generated.models.sales_performance import SalesPerformance
from app.generated.models.employees import Employee
from app.generated.models.inventory_documents import InventoryDocument
from app.generated.models.inventory_transactions import InventoryTransaction

from app.services.finished_goods_service import FinishedGoodsError, FinishedGoodsService
from app.generated.models.finished_goods_batches import FinishedGoodsBatch



Q6 = Decimal("0.000001")
RATIO_TOLERANCE = Decimal("0.000001")   # 配比合计容差

# ---------- 状态机 ----------
# 允许的动作：当前状态 -> 目标状态
STATUS_ACTIONS = {
    "place_order": {  # 下单
        "from": {"DRAFT"},
        "to": "ORDERED",
        "label": "下单",
    },
    "produce": {  # 生产
        "from": {"ORDERED"},
        "to": "PRODUCING",
        "label": "生产",
    },    
    "receive": {     # 完工入库（映射到 RECEIVED）
        "from": {"PRODUCING"},
        "to": "RECEIVED",
        "label": "完工入库",
    },
    "cancel": {       # 取消
        "from": {"DRAFT", "ORDERED"},
        "to": "CANCELLED",
        "label": "取消",
    },
}

STATUS_LABELS = {
    "DRAFT": "草稿",
    "ORDERED": "已下单",
    "PRODUCING": "生产中",
    "COMPLETED": "已完工",
    "RECEIVED": "已入库",
    "CANCELLED": "已取消",
}

from app.services.inventory_service import (
    StockOutService, InventoryError,
)


class PlaceOrderResult(dict):
    """结构化返回，方便 controller 用"""

class ProcessOrderError(Exception):
    pass


def _q6(v) -> Decimal:
    return Decimal(str(v)).quantize(Q6, rounding=ROUND_HALF_UP)


def generate_process_order_no() -> str:
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    import random
    return f"PO{ts}{random.randint(0, 9999):04d}"


class ProcessOrderService:

    # ---------- 新建（已有，稍作抽取） ----------
    @staticmethod
    def _validate_and_normalize(
        factory_id, performance_id, followup_employee_id, created_by,
        batch_no, product_name, quantity, materials,
    ):
        """
        抽取公共校验：主表字段 + 明细配比。
        返回 (q, normalized_materials)
        """
        if not batch_no:
            raise ProcessOrderError("生产批号必填")
        if not product_name:
            raise ProcessOrderError("产品名称必填")
        q = _q6(quantity)
        if q <= 0:
            raise ProcessOrderError("计划生产数量必须 > 0")

        if db.session.get(Factory, factory_id) is None:
            raise ProcessOrderError(f"加工厂不存在: {factory_id}")
        if db.session.get(SalesPerformance, performance_id) is None:
            raise ProcessOrderError(f"业绩单不存在: {performance_id}")
        if db.session.get(Employee, followup_employee_id) is None:
            raise ProcessOrderError(f"跟单员不存在: {followup_employee_id}")
        if db.session.get(Employee, created_by) is None:
            raise ProcessOrderError(f"创建人不存在: {created_by}")

        if not materials:
            raise ProcessOrderError("至少需要一条原料配比")

        material_ids = [int(m["material_id"]) for m in materials]
        if len(material_ids) != len(set(material_ids)):
            raise ProcessOrderError("同一原料只能出现一次")

        exist = {
            m.id: m for m in Material.query.filter(Material.id.in_(material_ids)).all()
        }
        for mid in material_ids:
            if mid not in exist:
                raise ProcessOrderError(f"原料不存在: {mid}")
            if exist[mid].status != 1:
                raise ProcessOrderError(f"原料已禁用: {exist[mid].material_code}")

        ratio_sum = Decimal("0")
        normalized = []
        for idx, m in enumerate(materials, start=1):
            mid = int(m["material_id"])
            ratio = Decimal(str(m["ratio_value"]))
            if ratio < 0 or ratio > 1:
                raise ProcessOrderError(
                    f"第 {idx} 行原料配比必须在 0 到 1 之间（当前 {ratio}）"
                )
            qty = _q6(q * ratio)
            cost = _q6(m.get("cost", 0))
            if cost < 0:
                raise ProcessOrderError(f"第 {idx} 行成本不能为负数")
            ratio_sum += ratio
            normalized.append({
                "material_id": mid, 
                "ratio_value": ratio,
                "quantity": qty,
                "cost": cost,
                "remark": m.get("remark"),
            })

        if abs(ratio_sum - Decimal("1")) > RATIO_TOLERANCE:
            raise ProcessOrderError(f"原料配比合计必须为 1，当前为 {ratio_sum}")

        # 数量补齐
        total_qty = sum((it["quantity"] for it in normalized), Decimal("0"))
        if normalized and abs(total_qty - q) > Decimal("0.000001"):
            diff = q - total_qty
            normalized[-1]["quantity"] = _q6(normalized[-1]["quantity"] + diff)

        return q, normalized

    # ---------- 新建 ----------
    @staticmethod
    def create_process_order(
        factory_id, performance_id, followup_employee_id, created_by,
        batch_no, product_name, quantity, order_time, materials,
        product_specification=None, unit="KG", cost_price=0, total_cost=0,
        remark=None, process_order_no=None, status="DRAFT",
    ) -> dict:
        q, normalized = ProcessOrderService._validate_and_normalize(
            factory_id, performance_id, followup_employee_id, created_by,
            batch_no, product_name, quantity, materials,
        )

        doc_no = process_order_no or generate_process_order_no()
        if ProcessOrder.query.filter_by(process_order_no=doc_no).first():
            raise ProcessOrderError(f"工艺单号重复: {doc_no}")

        order = ProcessOrder(
            process_order_no=doc_no,
            factory_id=factory_id, batch_no=batch_no,
            product_name=product_name,
            product_specification=product_specification,
            unit=unit or "KG", quantity=q,
            performance_id=performance_id,
            order_time=order_time or now_beijing_naive(),
            followup_employee_id=followup_employee_id,
            cost_price=_q6(cost_price), total_cost=_q6(total_cost),
            status=status, remark=remark, created_by=created_by,
            created_at=now_beijing_naive(), updated_at=now_beijing_naive(),
        )
        db.session.add(order)
        db.session.flush()

        rows = []
        for it in normalized:
            row = ProcessOrderMaterial(
                process_order_id=order.id,
                material_id=it["material_id"],
                ratio_value=it["ratio_value"],
                quantity=it["quantity"],
                cost=it["cost"],
                remark=it["remark"],
            )
            db.session.add(row)
            rows.append(row)

        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            if "uk_order_material" in str(e):
                raise ProcessOrderError("原料明细重复")
            raise ProcessOrderError(f"创建工艺单失败: {e}")

        return {
            "process_order_id": order.id,
            "process_order_no": order.process_order_no,
            "status": order.status,
            "order_time": order.order_time.isoformat(),
            "material_count": len(rows),
            "materials": [
                {"id": r.id, "material_id": r.material_id,
                 "ratio_value": str(r.ratio_value),
                 "quantity": str(r.quantity), "cost": str(r.cost)}
                for r in rows
            ],
        }

    # ---------- 更新（新增） ----------
    @staticmethod
    def update_process_order(
        order_id: int,
        factory_id, performance_id, followup_employee_id,
        batch_no, product_name, quantity, order_time, materials,
        product_specification=None, unit="KG", cost_price=0, total_cost=0,
        remark=None, status=None,
        operator_id: int = None,           # 记录最后修改人（可选）
        allow_status_edit: bool = True,
    ) -> dict:
        """
        更新工艺单：主表 UPDATE + 明细 DELETE/INSERT。
        - created_by / process_order_no 不可修改
        - 若工艺单已进入下游阶段（如已入库），可拒绝编辑（由调用方判断）
        """
        order = db.session.get(ProcessOrder, order_id)
        if not order:
            raise ProcessOrderError("工艺单不存在")

        # 状态限制（可选）
        if not allow_status_edit and order.status not in ("DRAFT", "ORDERED"):
            raise ProcessOrderError(f"当前状态 {order.status} 不允许编辑")

        q, normalized = ProcessOrderService._validate_and_normalize(
            factory_id, performance_id, followup_employee_id,
            order.created_by,            # 用原创建人，防止被篡改
            batch_no, product_name, quantity, materials,
        )

        # ---- 主表字段 ----
        order.factory_id = factory_id
        order.performance_id = performance_id
        order.followup_employee_id = followup_employee_id
        order.batch_no = batch_no
        order.product_name = product_name
        order.product_specification = product_specification
        order.unit = unit or "KG"
        order.quantity = q
        order.order_time = order_time or order.order_time
        order.cost_price = _q6(cost_price)
        order.total_cost = _q6(total_cost)
        order.remark = remark
        if status:
            order.status = status

        # ---- 明细：全删重插 ----
        ProcessOrderMaterial.query.filter_by(process_order_id=order.id).delete(
            synchronize_session=False
        )
        rows = []
        for it in normalized:
            row = ProcessOrderMaterial(
                process_order_id=order.id,
                material_id=it["material_id"],
                ratio_value=it["ratio_value"],
                quantity=it["quantity"],
                cost=it["cost"],
                remark=it["remark"],
            )
            db.session.add(row)
            rows.append(row)

        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            if "uk_order_material" in str(e):
                raise ProcessOrderError("原料明细重复")
            raise ProcessOrderError(f"更新工艺单失败: {e}")

        return {
            "process_order_id": order.id,
            "process_order_no": order.process_order_no,
            "status": order.status,
            "material_count": len(rows),
            "materials": [
                {"id": r.id, "material_id": r.material_id,
                 "ratio_value": str(r.ratio_value),
                 "quantity": str(r.quantity), "cost": str(r.cost)}
                for r in rows
            ],
        }

    @staticmethod
    def list_orders(
        keyword: str = None,
        status: str = None,
        factory_id: int = None,
        page: int = 1,
        page_size: int = 20,
    ):
        """
        列表查询：一次 JOIN 出加工厂/业绩单/跟单人/创建人名称。
        返回 (items, total)。
        items: [dict]
        """
        creator = db.aliased(Employee)   # 创建人别名
        follow  = db.aliased(Employee)   # 跟单人别名

        stmt = (
            select(
                ProcessOrder.id,
                ProcessOrder.process_order_no,
                ProcessOrder.factory_id,
                Factory.factory_name.label("factory_name"),
                ProcessOrder.product_name,
                ProcessOrder.quantity,
                ProcessOrder.unit,
                ProcessOrder.performance_id,
                SalesPerformance.performance_no.label("performance_no"),
                ProcessOrder.order_time,
                follow.real_name.label("followup_employee_name"),
                creator.real_name.label("created_by_name"),
                ProcessOrder.cost_price,
                ProcessOrder.status,
            )
            .join(Factory, Factory.id == ProcessOrder.factory_id, isouter=True)
            .join(SalesPerformance,
                  SalesPerformance.id == ProcessOrder.performance_id, isouter=True)
            .join(follow, follow.id == ProcessOrder.followup_employee_id, isouter=True)
            .join(creator, creator.id == ProcessOrder.created_by, isouter=True)
            .order_by(ProcessOrder.id.desc())
        )

        if keyword:
            like = f"%{keyword}%"
            stmt = stmt.where(
                db.or_(
                    ProcessOrder.process_order_no.like(like),
                    ProcessOrder.product_name.like(like),
                    ProcessOrder.batch_no.like(like),
                )
            )
        if status:
            stmt = stmt.where(ProcessOrder.status == status)
        if factory_id:
            stmt = stmt.where(ProcessOrder.factory_id == factory_id)

        # 总数（复用过滤条件）
        from sqlalchemy import func
        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = db.session.execute(count_stmt).scalar() or 0

        # 分页
        stmt = stmt.offset((page - 1) * page_size).limit(page_size)
        rows = db.session.execute(stmt).mappings().all()

        items = [dict(r) for r in rows]
        return items, total

    @staticmethod
    def change_status(order_id: int, action: str) -> dict:
        """
        统一状态流转入口。
        action ∈ {'place_order', 'ship_out', 'cancel'}
        """
        rule = STATUS_ACTIONS.get(action)
        if not rule:
            raise ProcessOrderError(f"不支持的操作: {action}")

        order = db.session.get(ProcessOrder, order_id)
        if not order:
            raise ProcessOrderError("工艺单不存在")

        if order.status not in rule["from"]:
            raise ProcessOrderError(
                f"当前状态【{STATUS_LABELS.get(order.status, order.status)}】"
                f"不允许执行【{rule['label']}】"
            )

        order.status = rule["to"]
        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            raise ProcessOrderError(f"状态更新失败: {e}")

        return {
            "process_order_id": order.id,
            "process_order_no": order.process_order_no,
            "old_status": None,
            "new_status": order.status,
            "action": action,
        }

    @staticmethod
    def place_order(
        order_id: int,
        out_warehouse_id: int,
        operator_id: int = None,
        remark: str = None,
        allow_partial: bool = False,
    ) -> dict:
        """
        下单 + 自动生产领料出库。
        - 状态：DRAFT -> ORDERED
        - 出库：按 process_order_materials 明细，FIFO 消耗
        - 事务：任一失败整体回滚
        """
        from app.services.inventory_service import (
            StockOutService, InventoryError,
        )        
        # 1. 幂等/状态校验
        order = db.session.get(ProcessOrder, order_id)
        if not order:
            raise ProcessOrderError("工艺单不存在")
        if order.status != "DRAFT":
            raise ProcessOrderError(
                f"当前状态【{STATUS_LABELS.get(order.status, order.status)}】"
                f"不允许下单"
            )

        # 2. 拿到明细
        materials = (
            ProcessOrderMaterial.query
            .filter_by(process_order_id=order_id)
            .order_by(ProcessOrderMaterial.id.asc())
            .all()
        )
        if not materials:
            raise ProcessOrderError("工艺单没有原料明细，无法下单")

        # 3. 组装出库明细（数量用 Decimal）
        lines = [
            {
                "line_no": idx + 1,
                "material_id": m.material_id,
                "quantity": str(m.quantity),
            }
            for idx, m in enumerate(materials)
        ]

        # 4. 幂等出库单号（重复调用不会产生第二张单）
        document_no = f"PROD-OUT-{order.process_order_no}"

        # 5. 先改状态，同事务里出库（commit=False）
        old_status = order.status
        order.status = "ORDERED"
        db.session.flush()   # 让 order.status 变更先写到 session

        # ---- 出库（不提交，交给外层事务） ----
        stock_out_result = None
        try:
            stock_out_result = StockOutService.create_stock_out(
                warehouse_id=out_warehouse_id,
                lines=lines,
                out_type="PRODUCTION_OUT",
                document_no=document_no,
                document_time=now_beijing_naive(),
                process_order_id=order.id,
                remark=remark or f"下单自动领料 - {order.process_order_no}",
                auto_confirm=True,
                allow_partial=allow_partial,
                commit=False,          # ⭐ 交给外层
            )
        except InventoryError as e:
            # 出库失败 → 整体回滚（status 变更也不会保存）
            db.session.rollback()
            raise ProcessOrderError(f"下单失败，原料出库异常: {e}")

        # ================================================================
        # ⭐ 回写 FIFO 实际成本到 process_order_materials.cost
        # ================================================================
        # 从本次出库的流水里，按 material_id 汇总 total_cost
        # （一般同一工艺单同一原料只有一条流水，但允许部分出库/多次领用
        #  时可能有多条，统一 SUM 汇总更稳）
        from sqlalchemy import func

        rows = (
            db.session.query(
                InventoryTransaction.material_id,
                func.sum(InventoryTransaction.total_cost).label("cost_sum"),
            )
            .filter(
                InventoryTransaction.document_id == stock_out_result["document_id"],
                InventoryTransaction.process_order_id == order.id,
                InventoryTransaction.transaction_type == "PRODUCTION_OUT",
            )
            .group_by(InventoryTransaction.material_id)
            .all()
        )
        cost_by_material = {
            mid: _q6(cost_sum or 0)
            for mid, cost_sum in rows
        }

        actual_total_cost = Decimal("0")
        for m in materials:
            real_cost = cost_by_material.get(m.material_id, Decimal("0"))
            m.cost = real_cost
            actual_total_cost += real_cost

        # 顺手把汇总回写到主表（如果你不需要，可以删掉这两行）
        order.actual_total_cost = _q6(actual_total_cost)

        # 6. 一次提交
        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            raise ProcessOrderError(f"下单提交失败: {e}")

        return {
            "process_order_id": order.id,
            "process_order_no": order.process_order_no,
            "old_status": old_status,
            "new_status": order.status,
            "materials_cost": [
                {"material_id": m.material_id, "cost": str(m.cost)}
                for m in materials
            ],            
            "stock_out": {
                "document_id": stock_out_result["document_id"],
                "document_no": stock_out_result["document_no"],
                "document_type": stock_out_result["document_type"],
                "line_count": stock_out_result["line_count"],
                "lines": stock_out_result["lines"],
            },
        }

    @staticmethod
    def receive_to_finished_goods(
        order_id: int,
        warehouse_id: int,
        operator_id: int = None,
        remark: str = None,
    ) -> dict:
        """
        工艺单入库到成品库：
        - 状态：PRODUCING -> RECEIVED
        - 批次号 = order.batch_no
        - 成本单价 = actual_total_cost / completed_quantity
        - 入库数量 = completed_quantity（若为 0 则用 quantity）
        - 事务与状态更新合并
        """
        order = db.session.get(ProcessOrder, order_id)
        if not order:
            raise ProcessOrderError("工艺单不存在")

        if order.status != "PRODUCING":
            raise ProcessOrderError(
                f"当前状态【{STATUS_LABELS.get(order.status, order.status)}】"
                f"不允许入库，仅【已下单】状态可入库"
            )

        # 数量：优先实际完工数，否则回退到计划数
        qty = _q6(order.completed_quantity or 0)
        if qty <= 0:
            qty = _q6(order.quantity)
        if qty <= 0:
            raise ProcessOrderError("入库数量必须 > 0")

        # 成本单价：实际总成本 / 完工数量（至少避免除 0）
        actual_cost = _q6(order.actual_total_cost or 0)
        unit_cost = (
            _q6(actual_cost / qty) if qty > 0 and actual_cost > 0
            else Decimal("0")
        )

        # 幂等：批次号已存在时拒绝（防止重复点击 / 并发）
        if FinishedGoodsBatch.query.filter_by(batch_no=order.batch_no).first():
            raise ProcessOrderError(
                f"批次号 [{order.batch_no}] 已存在，无法重复入库"
            )

        # 组装入库明细
        lines = [{
            "batch_no": order.batch_no,                      # ⭐ 用工艺单批次号
            "product_name": order.product_name,
            "product_specification": order.product_specification,
            "unit": order.unit or "KG",
            "process_order_id": order.id,
            "quality_status": "QUALIFIED",
            "quantity": str(qty),
            "unit_cost": str(unit_cost),                     # ⭐ 计算出的单位成本
            "remark": remark or f"工艺单入库 - {order.process_order_no}",
        }]

        old_status = order.status
        order.status = "RECEIVED"
        db.session.flush()   # 状态变更先写入 session，若出库失败会 rollback

        try:
            result = FinishedGoodsService.create_inbound(
                warehouse_id=warehouse_id,
                lines=lines,
                inbound_type="PRODUCTION_IN",
                document_no=f"FG-IN-{order.process_order_no}",  # 幂等单号
                document_time=now_beijing_naive(),
                created_by=operator_id,
                remark=f"[PROCESS_ORDER_RECEIVE] 工艺单入库 {order.process_order_no}",
                auto_confirm=True,
                commit=False,          # ⭐ 交给外层提交
            )
        except FinishedGoodsError as e:
            db.session.rollback()
            raise ProcessOrderError(f"成品入库失败: {e}")

        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            raise ProcessOrderError(f"入库提交失败: {e}")

        return {
            "process_order_id": order.id,
            "process_order_no": order.process_order_no,
            "old_status": old_status,
            "new_status": order.status,
            "batch_no": order.batch_no,
            "quantity": str(qty),
            "unit_cost": str(unit_cost),
            "actual_total_cost": str(actual_cost),
            "finished_goods": {
                "document_id": result["document_id"],
                "document_no": result["document_no"],
                "batch_id": result["lines"][0]["batch_id"],
                "batch_no": result["lines"][0]["batch_no"],
            },
        }