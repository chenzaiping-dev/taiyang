"""草稿单据删除服务。

只允许删除 DRAFT 单据；凡是已经产生库存流水/预留/发货事实的记录均拒绝删除。
所有删除操作在单个数据库事务中完成。
"""
from app.extensions import db

from app.generated.models.inventory_documents import InventoryDocument
from app.generated.models.inventory_document_lines import InventoryDocumentLine
from app.generated.models.inventory_transactions import InventoryTransaction
from app.generated.models.finished_goods_documents import FinishedGoodsDocument
from app.generated.models.finished_goods_document_lines import FinishedGoodsDocumentLine
from app.generated.models.finished_goods_transactions import FinishedGoodsTransaction
from app.generated.models.process_orders import ProcessOrder
from app.generated.models.process_order_materials import ProcessOrderMaterial
from app.generated.models.process_order_material_adjustments import ProcessOrderMaterialAdjustment
from app.generated.models.production_plans import ProductionPlan
from app.generated.models.production_plan_process_orders import ProductionPlanProcessOrder
from app.generated.models.sales_performance import SalesPerformance
from app.generated.models.shipment_plans import ShipmentPlan
from app.generated.models.shipment_plan_lines import ShipmentPlanLine
from app.generated.models.shipment_plan_allocations import ShipmentPlanAllocation
from app.generated.models.finished_goods_reservations import FinishedGoodsReservation
from app.generated.models.finished_goods_batches import FinishedGoodsBatch


class DraftDeleteError(Exception):
    pass


def _delete_process_order_children(order: ProcessOrder):
    """删除一个无任何已生效业务引用的 DRAFT 工艺单。"""
    if order.status != "DRAFT":
        raise DraftDeleteError(f"工艺单 {order.process_order_no} 不是草稿状态，不能删除")

    # 工艺单一旦参与原料/成品流水、批次档案或调整记录，就不能物理删除。
    refs = [
        (InventoryTransaction, InventoryTransaction.process_order_id, "原料库存流水"),
        (InventoryDocumentLine, InventoryDocumentLine.process_order_id, "原料库存单据明细"),
        (FinishedGoodsDocumentLine, FinishedGoodsDocumentLine.process_order_id, "成品库存单据明细"),
        (FinishedGoodsBatch, FinishedGoodsBatch.process_order_id, "成品批次档案"),
        (ProcessOrderMaterialAdjustment, ProcessOrderMaterialAdjustment.process_order_id, "原料配比调整记录"),
    ]
    for model, column, label in refs:
        if db.session.query(model.id).filter(column == order.id).first():
            raise DraftDeleteError(
                f"工艺单 {order.process_order_no} 已关联{label}，为保护业务记录不能删除"
            )

    # 只允许从草稿生产计划解除关联；已确认/执行中的计划不能被破坏。
    links = ProductionPlanProcessOrder.query.filter_by(process_order_id=order.id).all()
    for link in links:
        plan = db.session.get(ProductionPlan, link.production_plan_id)
        if plan and plan.status != "DRAFT":
            raise DraftDeleteError(
                f"工艺单 {order.process_order_no} 关联的生产计划 {plan.plan_no} "
                f"状态 {plan.status}，不能删除"
            )
    ProductionPlanProcessOrder.query.filter_by(process_order_id=order.id).delete(
        synchronize_session=False
    )

    ProcessOrderMaterial.query.filter_by(process_order_id=order.id).delete(
        synchronize_session=False
    )
    db.session.delete(order)


def _delete_shipment_plan_children(plan: ShipmentPlan):
    if plan.status != "DRAFT":
        raise DraftDeleteError(
            f"发货计划 {plan.shipment_plan_no} 不是草稿状态，不能删除"
        )

    if ShipmentPlanLine.query.filter(
        ShipmentPlanLine.shipment_plan_id == plan.id,
        ShipmentPlanLine.shipped_quantity > 0,
    ).first():
        raise DraftDeleteError(
            f"发货计划 {plan.shipment_plan_no} 已存在实际发货数量，不能直接删除"
        )

    line_ids = [r[0] for r in db.session.query(ShipmentPlanLine.id).filter_by(
        shipment_plan_id=plan.id
    ).all()]
    allocation_ids = []
    if line_ids:
        allocation_ids = [r[0] for r in db.session.query(
            ShipmentPlanAllocation.id
        ).filter(ShipmentPlanAllocation.shipment_line_id.in_(line_ids)).all()]

    if allocation_ids and ShipmentPlanAllocation.query.filter(
        ShipmentPlanAllocation.id.in_(allocation_ids),
        ShipmentPlanAllocation.shipped_quantity > 0,
    ).first():
        raise DraftDeleteError(
            f"发货计划 {plan.shipment_plan_no} 的批次分配已存在实际发货数量，不能删除"
        )

    if allocation_ids and FinishedGoodsReservation.query.filter(
        FinishedGoodsReservation.shipment_allocation_id.in_(allocation_ids)
    ).first():
        raise DraftDeleteError(
            f"发货计划 {plan.shipment_plan_no} 已有关联库存预留记录，不能直接删除"
        )

    if allocation_ids:
        ShipmentPlanAllocation.query.filter(
            ShipmentPlanAllocation.id.in_(allocation_ids)
        ).delete(synchronize_session=False)
    if line_ids:
        ShipmentPlanLine.query.filter(
            ShipmentPlanLine.id.in_(line_ids)
        ).delete(synchronize_session=False)
    db.session.delete(plan)


class DraftDeleteService:
    @staticmethod
    def delete_inventory_document(document_id: int, direction: str = None):
        doc = db.session.get(InventoryDocument, document_id)
        if not doc:
            raise DraftDeleteError("原料库存单据不存在")
        if doc.status != "DRAFT":
            raise DraftDeleteError("只有草稿状态的原料库存单据可以删除")
        inbound_types = {"PURCHASE_IN", "STOCKTAKE", "RETURN"}
        outbound_types = {
            "PRODUCTION_OUT", "STOCKTAKE", "RETURN",
            "ADJUSTMENT_RED", "ADJUSTMENT_BLUE",
        }
        if direction == "in" and doc.document_type not in inbound_types:
            raise DraftDeleteError("单据类型与入库列表不匹配")
        if direction == "out" and doc.document_type not in outbound_types:
            raise DraftDeleteError("单据类型与出库列表不匹配")
        if InventoryTransaction.query.filter_by(document_id=doc.id).first():
            raise DraftDeleteError("该单据已产生原料库存流水，不能直接删除")
        InventoryDocumentLine.query.filter_by(document_id=doc.id).delete(
            synchronize_session=False
        )
        db.session.delete(doc)
        db.session.commit()

    @staticmethod
    def delete_finished_goods_document(document_id: int, direction: str = None):
        doc = db.session.get(FinishedGoodsDocument, document_id)
        if not doc:
            raise DraftDeleteError("成品库存单据不存在")
        if doc.status != "DRAFT":
            raise DraftDeleteError("只有草稿状态的成品库存单据可以删除")
        inbound_business_types = {
            "PRODUCTION_IN", "STOCKTAKE_GAIN", "RETURN_IN",
            "TRANSFER_IN", "IN_REVERSE",
        }
        outbound_business_types = {
            "OUTBOUND", "STOCKTAKE_LOSS", "RETURN_OUT",
            "TRANSFER_OUT", "OUT_REVERSE",
        }
        if doc.business_type:
            if direction == "in" and doc.business_type not in inbound_business_types:
                raise DraftDeleteError("单据类型与入库列表不匹配")
            if direction == "out" and doc.business_type not in outbound_business_types:
                raise DraftDeleteError("单据类型与出库列表不匹配")
        else:
            # 兼容旧数据：business_type 为空时，沿用列表查询的 document_type 兜底规则。
            legacy_types = {"in": {"PRODUCTION_IN"}, "out": {"OUTBOUND"}}
            if direction in legacy_types and doc.document_type not in legacy_types[direction]:
                raise DraftDeleteError("单据类型与列表方向不匹配")
        if FinishedGoodsTransaction.query.filter_by(document_id=doc.id).first():
            raise DraftDeleteError("该单据已产生成品库存流水，不能直接删除")
        FinishedGoodsDocumentLine.query.filter_by(document_id=doc.id).delete(
            synchronize_session=False
        )
        db.session.delete(doc)
        db.session.commit()

    @staticmethod
    def delete_process_order(order_id: int):
        order = db.session.get(ProcessOrder, order_id)
        if not order:
            raise DraftDeleteError("工艺单不存在")
        _delete_process_order_children(order)
        db.session.commit()

    @staticmethod
    def delete_shipment_plan(plan_id: int):
        plan = db.session.get(ShipmentPlan, plan_id)
        if not plan:
            raise DraftDeleteError("发货计划不存在")
        _delete_shipment_plan_children(plan)
        db.session.commit()

    @staticmethod
    def delete_sales_performance(perf_id: int):
        perf = db.session.get(SalesPerformance, perf_id)
        if not perf:
            raise DraftDeleteError("业绩单不存在")
        if perf.status != "DRAFT":
            raise DraftDeleteError("只有草稿状态的业绩单可以删除")
        if perf.return_of_performance_id is None:
            linked_returns = SalesPerformance.query.filter_by(
                return_of_performance_id=perf.id
            ).first()
            if linked_returns:
                raise DraftDeleteError("该销售单已有退货记录，不能直接删除原销售单")

        plans = ProductionPlan.query.filter_by(performance_id=perf.id).all()
        shipments = ShipmentPlan.query.filter_by(performance_id=perf.id).all()
        orders = ProcessOrder.query.filter_by(performance_id=perf.id).all()

        # 业绩单删除会影响下游单据，因此仅在所有下游记录均为草稿且没有实际预留时执行。
        non_draft_plans = [p for p in plans if p.status != "DRAFT"]
        non_draft_shipments = [p for p in shipments if p.status != "DRAFT"]
        non_draft_orders = [o for o in orders if o.status != "DRAFT"]
        if non_draft_plans or non_draft_shipments or non_draft_orders:
            raise DraftDeleteError(
                "该业绩单已关联非草稿的生产计划、工艺单或发货计划，请先处理下游单据"
            )

        plan_ids = {p.id for p in plans}
        for order in orders:
            for link in ProductionPlanProcessOrder.query.filter_by(
                process_order_id=order.id
            ).all():
                if link.production_plan_id not in plan_ids:
                    other_plan = db.session.get(ProductionPlan, link.production_plan_id)
                    other_no = other_plan.plan_no if other_plan else str(link.production_plan_id)
                    raise DraftDeleteError(
                        f"工艺单 {order.process_order_no} 还关联其他生产计划 {other_no}，"
                        "请先解除关联后再删除业绩单"
                    )

        # 先删除发货草稿及其明细。
        for shipment in shipments:
            _delete_shipment_plan_children(shipment)

        # 删除生产计划草稿及关联行。生产计划下挂的工艺单在下面统一删除，
        # 避免同一工艺单被重复处理；所有这些工艺单都已在上面校验为 DRAFT。
        for plan in plans:
            if FinishedGoodsReservation.query.filter_by(
                production_plan_id=plan.id
            ).first():
                raise DraftDeleteError(
                    f"生产计划 {plan.plan_no} 已有库存预留记录，不能随业绩单删除"
                )
            ProductionPlanProcessOrder.query.filter_by(
                production_plan_id=plan.id
            ).delete(synchronize_session=False)
            db.session.delete(plan)

        # 业绩单下所有工艺单都为草稿，统一清理其配比和计划关联。
        for order in orders:
            _delete_process_order_children(order)

        # 先落库删除子表，避免 FK 检查时父业绩单先于关联单据删除。
        db.session.flush()
        db.session.delete(perf)
        db.session.commit()
