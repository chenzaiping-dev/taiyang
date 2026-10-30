from decimal import Decimal, ROUND_HALF_UP
from sqlalchemy import select, case, func, or_
from app.extensions import db
from app.generated.models.warehouses import Warehouse
from app.generated.models.employees import Employee
from app.generated.models.process_orders import ProcessOrder

from app.generated.models.finished_goods_batches import FinishedGoodsBatch
from app.generated.models.finished_goods_documents import FinishedGoodsDocument
from app.generated.models.finished_goods_document_lines import FinishedGoodsDocumentLine
from app.generated.models.finished_goods_inventory import FinishedGoodsInventory
from app.generated.models.finished_goods_transactions import FinishedGoodsTransaction
from app.generated.models.finished_goods_reservations import FinishedGoodsReservation


def _infer_source_entry(row: dict) -> str:
    doc_no = row.get("document_no") or ""
    doc_type = row.get("document_type") or ""
    remark = row.get("document_remark") or ""

    if "[PROCESS_ORDER_RECEIVE]" in remark:
        return "工艺单入库"
    if doc_type == "PRODUCTION_IN":
        return "成品入库页"
    if doc_type == "CUSTOMER_RETURN":
        return "成品入库页-退货"
    if doc_type == "ADJUSTMENT":
        return "成品入库页-盘点"
    if doc_type == "OUTBOUND":
        return "成品出库页"
    if doc_type == "TRANSFER":
        return "成品调拨"
    return "未知入口"

def _infer_source_entry_old(row: dict) -> str:
    """
    推断这张成品流水是从哪个入口生成的：
    - document_no 前缀 + 批次是否有 process_order 决定
    """
    doc_no = row.get("document_no") or ""
    doc_type = row.get("document_type") or ""
    batch_has_process = row.get("process_order_id") is not None

    if doc_type == "PRODUCTION_IN":
        # 工艺单入库：document_no 约定为 FG-IN-<工艺单号>，且批次带 process_order_id
        if batch_has_process and doc_no.startswith("FG-IN-"):
            # 进一步判断是否"工艺单页面直接入库"
            # 工艺单入库时，document_no 是 FG-IN-POxxx；而成品入库页面是 FG-IN<时间戳>
            if doc_no.startswith("FG-IN-PO") or doc_no.startswith("FG-IN-"):
                # 通过工艺单号判断：若后缀等于工艺单号，则为工艺单入库
                po_no = row.get("process_order_no")
                if po_no and doc_no.endswith(po_no):
                    return "工艺单入库"
        # 成品入库页面（手工录入）
        if doc_no.startswith("FG-IN"):
            return "成品入库页"
    if doc_type == "CUSTOMER_RETURN":
        return "成品入库页-退货"
    if doc_type == "ADJUSTMENT":
        return "成品入库页-盘点"
    if doc_type == "OUTBOUND":
        return "成品出库页"
    if doc_type == "TRANSFER":
        return "成品调拨"
    return "未知入口"

class FinishedGoodsQueryService:

    @staticmethod
    def list_warehouse_stock(warehouse_id=None, batch_keyword=None,
                             page=1, page_size=50):
        """成品库仓库列表：按 (warehouse, batch) 展示现存 + 锁定 + 可用"""
        # ⭐ 锁定汇总子查询
        locked_subq = (
            select(
                FinishedGoodsReservation.warehouse_id.label("wh_id"),
                FinishedGoodsReservation.batch_id.label("bt_id"),
                func.coalesce(
                    func.sum(FinishedGoodsReservation.remaining_quantity), 0
                ).label("locked_qty"),
            )
            .where(
                FinishedGoodsReservation.status == "ACTIVE",
            )
            .group_by(
                FinishedGoodsReservation.warehouse_id,
                FinishedGoodsReservation.batch_id,
            )
            .subquery()
        )

        locked_qty_col = func.coalesce(locked_subq.c.locked_qty, 0)

        stmt = (
            select(
                FinishedGoodsInventory.id,
                FinishedGoodsInventory.warehouse_id,
                Warehouse.warehouse_code,
                Warehouse.warehouse_name,
                FinishedGoodsInventory.batch_id,
                FinishedGoodsBatch.batch_no,
                FinishedGoodsBatch.product_name,
                FinishedGoodsBatch.product_specification,
                FinishedGoodsBatch.unit,
                FinishedGoodsBatch.quality_status,
                FinishedGoodsInventory.quantity.label("total_qty"),
                # ⭐ 锁定数量 & 可用数量
                locked_qty_col.label("locked_qty"),
                (FinishedGoodsInventory.quantity - locked_qty_col).label("available_qty"),
                FinishedGoodsInventory.updated_at,
            )
            .join(Warehouse, Warehouse.id == FinishedGoodsInventory.warehouse_id)
            .join(FinishedGoodsBatch,
                  FinishedGoodsBatch.id == FinishedGoodsInventory.batch_id)
            .join(locked_subq,
                  (locked_subq.c.wh_id == FinishedGoodsInventory.warehouse_id)
                  & (locked_subq.c.bt_id == FinishedGoodsInventory.batch_id),
                  isouter=True)
            .where(FinishedGoodsInventory.quantity > 0)
            .order_by(Warehouse.warehouse_code.asc(),
                      FinishedGoodsBatch.batch_no.asc())
        )
        if warehouse_id:
            stmt = stmt.where(FinishedGoodsInventory.warehouse_id == warehouse_id)
        if batch_keyword:
            like = f"%{batch_keyword}%"
            stmt = stmt.where(or_(
                FinishedGoodsBatch.batch_no.like(like),
                FinishedGoodsBatch.product_name.like(like),
            ))

        total = db.session.execute(
            select(func.count()).select_from(stmt.subquery())
        ).scalar() or 0
        stmt = stmt.offset((page - 1) * page_size).limit(page_size)

        rows = [dict(r) for r in db.session.execute(stmt).mappings().all()]
        return rows, total

    @staticmethod
    def list_transactions(warehouse_id=None, batch_id=None, tx_type=None,
                          document_no=None, date_from=None, date_to=None,
                          page=1, page_size=50):
        creator = db.aliased(Employee)   # ⭐ 单据制单人

        stmt = (
            select(
                FinishedGoodsTransaction.id,
                FinishedGoodsTransaction.transaction_time,
                FinishedGoodsTransaction.transaction_type,
                FinishedGoodsTransaction.quantity_delta,
                FinishedGoodsTransaction.warehouse_id,
                Warehouse.warehouse_code,
                Warehouse.warehouse_name,
                FinishedGoodsTransaction.batch_id,
                FinishedGoodsBatch.batch_no,
                FinishedGoodsBatch.product_name,
                FinishedGoodsBatch.unit,

                # ⭐ 批次来源工艺单
                FinishedGoodsBatch.process_order_id,
                ProcessOrder.process_order_no,

                # ⭐ 单据（含制单人）
                FinishedGoodsDocument.id.label("document_id"),
                FinishedGoodsDocument.document_no,
                FinishedGoodsDocument.document_type,
                FinishedGoodsDocument.status.label("document_status"),
                FinishedGoodsDocument.remark.label("document_remark"),
                creator.real_name.label("creator_name"),
            )
            .join(Warehouse, Warehouse.id == FinishedGoodsTransaction.warehouse_id)
            .join(FinishedGoodsBatch,
                  FinishedGoodsBatch.id == FinishedGoodsTransaction.batch_id)
            .join(FinishedGoodsDocument,
                  FinishedGoodsDocument.id == FinishedGoodsTransaction.document_id)
            .join(ProcessOrder,
                  ProcessOrder.id == FinishedGoodsBatch.process_order_id,
                  isouter=True)                     # 批次可能非工艺单来源
            .join(creator,
                  creator.id == FinishedGoodsDocument.created_by,
                  isouter=True)                     # 制单人为空允许
            .order_by(FinishedGoodsTransaction.transaction_time.desc(),
                      FinishedGoodsTransaction.id.desc())
        )

        if warehouse_id:
            stmt = stmt.where(FinishedGoodsTransaction.warehouse_id == warehouse_id)
        if batch_id:
            stmt = stmt.where(FinishedGoodsTransaction.batch_id == batch_id)
        if tx_type:
            stmt = stmt.where(FinishedGoodsTransaction.transaction_type == tx_type)
        if document_no:
            stmt = stmt.where(FinishedGoodsDocument.document_no.like(f"%{document_no}%"))
        if date_from:
            stmt = stmt.where(FinishedGoodsTransaction.transaction_time >= date_from)
        if date_to:
            stmt = stmt.where(FinishedGoodsTransaction.transaction_time < date_to)

        total = db.session.execute(
            select(func.count()).select_from(stmt.subquery())
        ).scalar() or 0
        stmt = stmt.offset((page - 1) * page_size).limit(page_size)

        rows = [dict(r) for r in db.session.execute(stmt).mappings().all()]

        # ⭐ 在 Python 里推断"操作入口"
        for r in rows:
            r["source_entry"] = _infer_source_entry(r)
        return rows, total

    @staticmethod
    def list_available_batches(warehouse_id):
        """供人工选批次出库使用：该仓库可用数量 > 0 的批次"""
        locked_subq = (
            select(
                FinishedGoodsReservation.batch_id.label("bt_id"),
                func.coalesce(
                    func.sum(FinishedGoodsReservation.remaining_quantity), 0
                ).label("locked_qty"),
            )
            .where(
                FinishedGoodsReservation.warehouse_id == warehouse_id,
                FinishedGoodsReservation.status == "ACTIVE",
            )
            .group_by(FinishedGoodsReservation.batch_id)
            .subquery()
        )

        locked = func.coalesce(locked_subq.c.locked_qty, 0)
        available = FinishedGoodsInventory.quantity - locked

        rows = (
            db.session.query(
                FinishedGoodsBatch.id,
                FinishedGoodsBatch.batch_no,
                FinishedGoodsBatch.product_name,
                FinishedGoodsBatch.product_specification,
                FinishedGoodsBatch.unit,
                FinishedGoodsBatch.quality_status,
                FinishedGoodsInventory.quantity.label("total_qty"),
                locked.label("locked_qty"),
                available.label("available_qty"),
            )
            .join(FinishedGoodsInventory,
                FinishedGoodsInventory.batch_id == FinishedGoodsBatch.id)
            .join(locked_subq,
                locked_subq.c.bt_id == FinishedGoodsBatch.id,
                isouter=True)
            .filter(
                FinishedGoodsInventory.warehouse_id == warehouse_id,
                available > 0,           # ⭐ 只列可用的
            )
            .order_by(FinishedGoodsBatch.batch_no.asc())
            .all()
        )
        return [dict(r._mapping) for r in rows]


    @staticmethod
    def query_io_summary(
        warehouse_id=None,
        batch_keyword=None,
        date_from=None,
        date_to=None,
        group_by: str = "batch",     # 'batch' | 'product'
        page: int = 1,
        page_size: int = 50,
    ):
        """
        成品库累计出入库汇总。
        group_by='batch'    → 按 (批次, 仓库) 汇总
        group_by='product'  → 按 (产品名称, 规格, 仓库) 汇总
        """
        in_qty = case(
            (FinishedGoodsTransaction.quantity_delta > 0,
             FinishedGoodsTransaction.quantity_delta),
            else_=0,
        )
        out_qty = case(
            (FinishedGoodsTransaction.quantity_delta < 0,
             -FinishedGoodsTransaction.quantity_delta),
            else_=0,
        )
        in_prod = case(
            (FinishedGoodsTransaction.transaction_type == "PRODUCTION_IN",
             FinishedGoodsTransaction.quantity_delta),
            else_=0,
        )
        in_return = case(
            (FinishedGoodsTransaction.transaction_type == "CUSTOMER_RETURN",
             FinishedGoodsTransaction.quantity_delta),
            else_=0,
        )
        in_adjust = case(
            (FinishedGoodsTransaction.transaction_type == "ADJUSTMENT",
             FinishedGoodsTransaction.quantity_delta),
            else_=0,
        )
        out_sale = case(
            (FinishedGoodsTransaction.transaction_type == "OUTBOUND",
             -FinishedGoodsTransaction.quantity_delta),
            else_=0,
        )

        filters = []
        if warehouse_id:
            filters.append(FinishedGoodsTransaction.warehouse_id == warehouse_id)
        if batch_keyword:
            like = f"%{batch_keyword}%"
            filters.append(or_(
                FinishedGoodsBatch.batch_no.like(like),
                FinishedGoodsBatch.product_name.like(like),
            ))
        if date_from:
            filters.append(FinishedGoodsTransaction.transaction_time >= date_from)
        if date_to:
            filters.append(FinishedGoodsTransaction.transaction_time < date_to)

        if group_by == "product":
            group_cols = [
                FinishedGoodsTransaction.warehouse_id,
                FinishedGoodsBatch.product_name,
                FinishedGoodsBatch.product_specification,
                FinishedGoodsBatch.unit,
            ]
            cols = [
                FinishedGoodsTransaction.warehouse_id,
                Warehouse.warehouse_code,
                Warehouse.warehouse_name,
                FinishedGoodsBatch.product_name,
                FinishedGoodsBatch.product_specification,
                FinishedGoodsBatch.unit,
            ]
        else:  # batch
            group_cols = [
                FinishedGoodsTransaction.batch_id,
                FinishedGoodsTransaction.warehouse_id,
            ]
            cols = [
                FinishedGoodsTransaction.batch_id,
                FinishedGoodsTransaction.warehouse_id,
                Warehouse.warehouse_code,
                Warehouse.warehouse_name,
                FinishedGoodsBatch.batch_no,
                FinishedGoodsBatch.product_name,
                FinishedGoodsBatch.product_specification,
                FinishedGoodsBatch.unit,
                FinishedGoodsBatch.quality_status,
            ]

        cols += [
            func.sum(in_prod).label("in_production"),
            func.sum(in_return).label("in_return"),
            func.sum(in_adjust).label("in_adjustment"),
            func.sum(out_sale).label("out_outbound"),
            func.sum(in_qty).label("total_in"),
            func.sum(out_qty).label("total_out"),
            func.sum(FinishedGoodsTransaction.quantity_delta).label("net_qty"),
        ]

        stmt = (
            select(*cols)
            .join(FinishedGoodsBatch,
                  FinishedGoodsBatch.id == FinishedGoodsTransaction.batch_id)
            .join(Warehouse,
                  Warehouse.id == FinishedGoodsTransaction.warehouse_id)
            .where(*filters)
            .group_by(*group_cols)
        )

        if group_by == "product":
            stmt = stmt.order_by(FinishedGoodsBatch.product_name.asc())
        else:
            stmt = stmt.order_by(FinishedGoodsBatch.batch_no.asc())

        total = db.session.execute(
            select(func.count()).select_from(stmt.subquery())
        ).scalar() or 0

        stmt = stmt.offset((page - 1) * page_size).limit(page_size)
        rows = [dict(r) for r in db.session.execute(stmt).mappings().all()]

        # 当前库存：额外查出作为对账列
        for r in rows:
            if group_by == "product":
                current = (
                    db.session.query(func.coalesce(
                        func.sum(FinishedGoodsInventory.quantity), 0))
                    .join(FinishedGoodsBatch,
                          FinishedGoodsBatch.id == FinishedGoodsInventory.batch_id)
                    .filter(
                        FinishedGoodsInventory.warehouse_id == r["warehouse_id"],
                        FinishedGoodsBatch.product_name == r["product_name"],
                    )
                    .scalar()
                )
            else:
                current = (
                    db.session.query(func.coalesce(
                        func.sum(FinishedGoodsInventory.quantity), 0))
                    .filter(
                        FinishedGoodsInventory.warehouse_id == r["warehouse_id"],
                        FinishedGoodsInventory.batch_id == r["batch_id"],
                    )
                    .scalar()
                )
            r["current_qty"] = current or 0
            r["diff"] = (Decimal(str(r["net_qty"] or 0))
                         - Decimal(str(r["current_qty"] or 0)))

        return rows, total

    @staticmethod
    def get_batch_available_qty(warehouse_id: int, batch_id: int):
        """返回 (库存总量, 锁定数量, 可用数量)"""
        inv = FinishedGoodsInventory.query.filter_by(
            warehouse_id=warehouse_id, batch_id=batch_id
        ).first()
        total_qty = inv.quantity if inv else 0

        locked = (
            db.session.query(func.coalesce(
                func.sum(FinishedGoodsReservation.remaining_quantity), 0))
            .filter(
                FinishedGoodsReservation.warehouse_id == warehouse_id,
                FinishedGoodsReservation.batch_id == batch_id,
                FinishedGoodsReservation.status == "ACTIVE",
            )
            .scalar()
        ) or 0

        return total_qty, locked, (total_qty - locked)

    # ⭐ 新增：可复用的"可用量"表达式
    @staticmethod
    def _locked_subquery():
        return (
            select(
                FinishedGoodsReservation.warehouse_id.label("wh_id"),
                FinishedGoodsReservation.batch_id.label("bt_id"),
                func.coalesce(
                    func.sum(FinishedGoodsReservation.remaining_quantity), 0
                ).label("locked_qty"),
            )
            .where(FinishedGoodsReservation.status == "ACTIVE")
            .group_by(
                FinishedGoodsReservation.warehouse_id,
                FinishedGoodsReservation.batch_id,
            )
            .subquery()
        )