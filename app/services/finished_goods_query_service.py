from decimal import Decimal, ROUND_HALF_UP
from sqlalchemy import select, case, func, or_
from app.extensions import db
from app.generated.models.warehouses import Warehouse

from app.generated.models.finished_goods_batches import FinishedGoodsBatch
from app.generated.models.finished_goods_documents import FinishedGoodsDocument
from app.generated.models.finished_goods_document_lines import FinishedGoodsDocumentLine
from app.generated.models.finished_goods_inventory import FinishedGoodsInventory
from app.generated.models.finished_goods_transactions import FinishedGoodsTransaction


class FinishedGoodsQueryService:

    @staticmethod
    def list_warehouse_stock(warehouse_id=None, batch_keyword=None,
                             page=1, page_size=50):
        """成品库仓库列表：按 (warehouse, batch) 展示现存"""
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
                FinishedGoodsInventory.quantity,
                FinishedGoodsInventory.updated_at,
            )
            .join(Warehouse, Warehouse.id == FinishedGoodsInventory.warehouse_id)
            .join(FinishedGoodsBatch,
                  FinishedGoodsBatch.id == FinishedGoodsInventory.batch_id)
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
        return [dict(r) for r in db.session.execute(stmt).mappings().all()], total

    @staticmethod
    def list_transactions(warehouse_id=None, batch_id=None, tx_type=None,
                          document_no=None, date_from=None, date_to=None,
                          page=1, page_size=50):
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
                FinishedGoodsDocument.id.label("document_id"),
                FinishedGoodsDocument.document_no,
                FinishedGoodsDocument.document_type,
                FinishedGoodsDocument.status.label("document_status"),
                FinishedGoodsDocument.remark.label("document_remark"),
            )
            .join(Warehouse, Warehouse.id == FinishedGoodsTransaction.warehouse_id)
            .join(FinishedGoodsBatch,
                  FinishedGoodsBatch.id == FinishedGoodsTransaction.batch_id)
            .join(FinishedGoodsDocument,
                  FinishedGoodsDocument.id == FinishedGoodsTransaction.document_id)
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
        return [dict(r) for r in db.session.execute(stmt).mappings().all()], total

    @staticmethod
    def list_available_batches(warehouse_id):
        """供人工选批次出库使用：该仓库当前有库存的批次"""
        rows = (
            db.session.query(
                FinishedGoodsBatch.id,
                FinishedGoodsBatch.batch_no,
                FinishedGoodsBatch.product_name,
                FinishedGoodsBatch.product_specification,
                FinishedGoodsBatch.unit,
                FinishedGoodsBatch.quality_status,
                FinishedGoodsInventory.quantity,
            )
            .join(FinishedGoodsInventory,
                  FinishedGoodsInventory.batch_id == FinishedGoodsBatch.id)
            .filter(
                FinishedGoodsInventory.warehouse_id == warehouse_id,
                FinishedGoodsInventory.quantity > 0,
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