from decimal import Decimal
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
from app.services.finished_goods_service import (
    FG_INBOUND_TYPE_LABELS, FG_OUTBOUND_TYPE_LABELS, FG_TX_TYPE_LABELS
)

def _infer_source_entry(row: dict) -> str:
    doc_no = row.get("document_no") or ""
    doc_type = row.get("document_type") or ""
    remark = row.get("document_remark") or ""
    print(remark)

    if "[PROCESS_ORDER_RECEIVE]" in remark:
        return "工艺单入库"
    if "[SHIP_PLAN_OUTBOUND]" in remark:
        return "发货计划出库"
    if "[CANCEL_PROCESS_ORDER]" in remark:
        return "取消工艺单"
    if "[CANCEL_SHIP_PLAN]" in remark:
        return "取消发货计划"
    if doc_type == "PRODUCTION_IN":
        return "成品入库页"
    if doc_type == "RETURN":
        return "成品入库页-退货"
    if doc_type == "STOCKTAKE":
        return "成品入库页-盘点"
    if doc_type == "OUTBOUND":
        return "成品出库页"
    if doc_type == "TRANSFER":
        return "成品调拨"
    if doc_type == "OUT_REVERSE":
        return "出库撤销"
    if doc_type == "IN_REVERSE":
        return "入库撤销"
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
                FinishedGoodsBatch.unit_cost,
                func.round(FinishedGoodsBatch.unit_cost * FinishedGoodsInventory.quantity, 6).label("total_cost"),
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
                      FinishedGoodsBatch.id.desc())
                      #FinishedGoodsBatch.batch_no.asc())
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
    def list_transactions(warehouse_id=None, batch_keyword=None, tx_type=None,
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
        if batch_keyword:
            like = f"%{batch_keyword}%"
            stmt = stmt.where(or_(
                FinishedGoodsBatch.batch_no.like(like),
                FinishedGoodsBatch.product_name.like(like),
            ))
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
            (FinishedGoodsTransaction.transaction_type == "RETURN_IN",
             FinishedGoodsTransaction.quantity_delta),
            else_=0,
        )
        in_stocktake = case(
            (FinishedGoodsTransaction.transaction_type == "STOCKTAKE_GAIN",
             FinishedGoodsTransaction.quantity_delta),
            else_=0,
        )
        in_transfer = case(
            (FinishedGoodsTransaction.transaction_type == "TRANSFER_IN",
             FinishedGoodsTransaction.quantity_delta),
            else_=0,
        )
        out_reverse = case(
            (FinishedGoodsTransaction.transaction_type == "OUT_REVERSE",
             -FinishedGoodsTransaction.quantity_delta),
            else_=0,
        )
        out_sale = case(
            (FinishedGoodsTransaction.transaction_type == "OUTBOUND",
             -FinishedGoodsTransaction.quantity_delta),
            else_=0,
        )
        out_return = case(
            (FinishedGoodsTransaction.transaction_type == "RETURN_OUT",
             -FinishedGoodsTransaction.quantity_delta),
            else_=0,
        )
        out_stocktake = case(
            (FinishedGoodsTransaction.transaction_type == "STOCKTAKE_LOSS",
             -FinishedGoodsTransaction.quantity_delta),
            else_=0,
        )
        out_transfer = case(
            (FinishedGoodsTransaction.transaction_type == "TRANSFER_OUT",
             -FinishedGoodsTransaction.quantity_delta),
            else_=0,
        )
        in_reverse = case(
            (FinishedGoodsTransaction.transaction_type == "IN_REVERSE",
             FinishedGoodsTransaction.quantity_delta),
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
            func.sum(in_stocktake).label("in_stocktake"),
            func.sum(in_transfer).label("in_transfer"),
            func.sum(out_reverse).label("out_reverse"),
            func.sum(out_sale).label("out_outbound"),
            func.sum(out_return).label("out_return"),
            func.sum(out_stocktake).label("out_stocktake"),
            func.sum(out_transfer).label("out_transfer"),
            func.sum(in_reverse).label("in_reverse"),
            # total_in = 生产 + 退货回 + 盘盈 + 调拨入 - 撤销入
            (
                func.sum(in_prod)
                + func.sum(in_return)
                + func.sum(in_stocktake)
                + func.sum(in_transfer)
                + func.sum(in_reverse)
            ).label("total_in"),

            # total_out = 销售 + 退货出 + 盘亏 + 调拨出 - 撤销出
            (
                func.sum(out_sale)
                + func.sum(out_return)
                + func.sum(out_stocktake)
                + func.sum(out_transfer)
                + func.sum(out_reverse)
            ).label("total_out"),
            #func.sum(in_qty).label("total_in"),
            #func.sum(out_qty).label("total_out"),
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
            #stmt = stmt.order_by(FinishedGoodsBatch.batch_no.asc())
            stmt = stmt.order_by(FinishedGoodsBatch.id.desc())

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

    # app/services/finished_goods_query_service.py
    @staticmethod
    def _locked_subquery_inventory():
        """库存可用量计算用的锁定汇总：所有 ACTIVE 预留（不分 goods_type / reservation_type）"""
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

    @staticmethod
    def list_all_stock_for_outbound(batch_keyword=None, warehouse_id=None,
                                    page=1, page_size=200):
        """
        出库页一次性列出全部库存（可按仓库/关键字过滤）。
        每行带 warehouse_id，方便提交。
        """
        locked_subq = FinishedGoodsQueryService._locked_subquery_inventory()
        locked = func.coalesce(locked_subq.c.locked_qty, 0)
        available = FinishedGoodsInventory.quantity - locked

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
                locked.label("locked_qty"),
                available.label("available_qty"),
            )
            .join(Warehouse, Warehouse.id == FinishedGoodsInventory.warehouse_id)
            .join(FinishedGoodsBatch,
                FinishedGoodsBatch.id == FinishedGoodsInventory.batch_id)
            .join(locked_subq,
                (locked_subq.c.wh_id == FinishedGoodsInventory.warehouse_id)
                & (locked_subq.c.bt_id == FinishedGoodsInventory.batch_id),
                isouter=True)
            .where(FinishedGoodsInventory.quantity > 0, available > 0)
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
    def list_fg_documents(
        direction: str,           # 'IN' | 'OUT'
        keyword=None, status=None,
        inbound_type=None,        # 仅 IN 方向过滤
        outbound_type=None,       # 仅 OUT 方向过滤
        warehouse_id=None,
        date_from=None, date_to=None,
        page=1, page_size=50,
    ):
        """
        成品库入库/出库单列表（方向维度）。

        方向判定：优先 FinishedGoodsDocument.business_type
          - business_type ∈ FG_INBOUND_TYPE_LABELS  → 入库
          - business_type ∈ FG_OUTBOUND_TYPE_LABELS → 出库
          - business_type IS NULL（历史数据）→ 回退由 document_type 推导
        数量合计来自 lines.quantity，对 DRAFT / CONFIRMED 都有效。
        """
        from sqlalchemy import exists
        from sqlalchemy.orm import aliased

        reverse_doc = aliased(FinishedGoodsDocument)
        reversed_exists = (
            exists()
            .where(
                reverse_doc.related_document_id == FinishedGoodsDocument.id,
                reverse_doc.status == "CONFIRMED",
            )
        )

        # ---- 方向判定 ----
        inbound_biz = list(FG_INBOUND_TYPE_LABELS.keys())
        outbound_biz = list(FG_OUTBOUND_TYPE_LABELS.keys())

        # 历史兜底：business_type 为 NULL 时按 document_type 归类
        legacy_inbound_doc_types = [
            "PRODUCTION_IN",
        ]
        legacy_outbound_doc_types = [
            "OUTBOUND",
        ]

        if direction == "IN":
            dir_condition = db.or_(
                FinishedGoodsDocument.business_type.in_(inbound_biz),
                db.and_(
                    FinishedGoodsDocument.business_type.is_(None),
                    FinishedGoodsDocument.document_type.in_(
                        legacy_inbound_doc_types
                    ),
                ),
            )
        else:
            dir_condition = db.or_(
                FinishedGoodsDocument.business_type.in_(outbound_biz),
                db.and_(
                    FinishedGoodsDocument.business_type.is_(None),
                    FinishedGoodsDocument.document_type.in_(
                        legacy_outbound_doc_types
                    ),
                ),
            )

        # ---- 数量/行数聚合（与方向无关，按行统计） ----
        qty_expr = func.coalesce(
            func.sum(FinishedGoodsDocumentLine.quantity), 0
        ).label("total_qty")

        stmt = (
            select(
                FinishedGoodsDocument.id,
                FinishedGoodsDocument.document_no,
                FinishedGoodsDocument.document_type,
                FinishedGoodsDocument.business_type,
                FinishedGoodsDocument.document_time,
                FinishedGoodsDocument.status,
                FinishedGoodsDocument.remark,
                FinishedGoodsDocument.created_at,
                FinishedGoodsDocument.confirmed_at,
                reversed_exists.label("is_reversed"),
                func.count(
                    func.distinct(FinishedGoodsDocumentLine.id)
                ).label("line_count"),
                qty_expr,
            )
            .join(
                FinishedGoodsDocumentLine,
                FinishedGoodsDocumentLine.document_id
                == FinishedGoodsDocument.id,
            )
            .where(dir_condition)
            .group_by(
                FinishedGoodsDocument.id,
                FinishedGoodsDocument.document_no,
                FinishedGoodsDocument.document_type,
                FinishedGoodsDocument.business_type,
                FinishedGoodsDocument.document_time,
                FinishedGoodsDocument.status,
                FinishedGoodsDocument.remark,
                FinishedGoodsDocument.created_at,
                FinishedGoodsDocument.confirmed_at,
            )
            .order_by(FinishedGoodsDocument.id.desc())
        )

        # ---------------- 基本过滤 ----------------
        if keyword:
            stmt = stmt.where(
                FinishedGoodsDocument.document_no.like(f"%{keyword}%")
            )
        if status:
            stmt = stmt.where(FinishedGoodsDocument.status == status)
        if date_from:
            stmt = stmt.where(FinishedGoodsDocument.document_time >= date_from)
        if date_to:
            stmt = stmt.where(FinishedGoodsDocument.document_time < date_to)

        # ---------------- 仓库过滤 ----------------
        # 行级字段：IN 看 to_warehouse_id，OUT 看 from_warehouse_id
        if warehouse_id:
            stmt = stmt.where(
                db.or_(
                    FinishedGoodsDocumentLine.from_warehouse_id == warehouse_id,
                    FinishedGoodsDocumentLine.to_warehouse_id == warehouse_id,
                )
            )

        # ---------------- 业务类型过滤 ----------------
        # 直接用 business_type，DRAFT / CONFIRMED 都生效
        biz_type = inbound_type if direction == "IN" else outbound_type
        if biz_type:
            stmt = stmt.where(FinishedGoodsDocument.business_type == biz_type)

        total = db.session.execute(
            select(func.count()).select_from(stmt.subquery())
        ).scalar() or 0

        stmt = stmt.offset((page - 1) * page_size).limit(page_size)
        rows = [dict(r) for r in db.session.execute(stmt).mappings().all()]
        return rows, total

    @staticmethod
    def get_fg_document_detail(document_id: int):
        """
        成品库通用单据详情。
        返回：
        {
        'document': {...},
        'lines': [{
            line_id, line_no, batch_id, batch_no, product_name,
            product_specification, unit, quality_status,
            from_warehouse_id, from_warehouse_code, from_warehouse_name,
            to_warehouse_id, to_warehouse_code, to_warehouse_name,
            quantity, unit_cost, remark,
            transaction_id, transaction_type, transaction_time, quantity_delta
        }],
        'total_in_qty', 'total_out_qty'
        }
        """
        doc = db.session.get(FinishedGoodsDocument, document_id)
        if not doc:
            return None
        if doc.document_type not in (
            "PRODUCTION_IN", "OUTBOUND", "STOCKTAKE",
            "RETURN", "TRANSFER", "REVERSE",
        ):
            return None

        from_wh = db.aliased(Warehouse)
        to_wh = db.aliased(Warehouse)

        # 方向：入库行 to_warehouse_id 非空 → 正；出库行 from_warehouse_id 非空 → 负
        direction_delta = case(
            (FinishedGoodsDocumentLine.to_warehouse_id.isnot(None),
            FinishedGoodsDocumentLine.quantity),
            (FinishedGoodsDocumentLine.from_warehouse_id.isnot(None),
            -FinishedGoodsDocumentLine.quantity),
            else_=None,
        )

        line_stmt = (
            select(
                FinishedGoodsDocumentLine.id.label("line_id"),
                FinishedGoodsDocumentLine.line_no,
                FinishedGoodsDocumentLine.batch_id,
                FinishedGoodsBatch.batch_no,
                FinishedGoodsBatch.product_name,
                FinishedGoodsBatch.product_specification,
                FinishedGoodsBatch.unit,
                FinishedGoodsBatch.quality_status,

                FinishedGoodsDocumentLine.from_warehouse_id,
                from_wh.warehouse_code.label("from_warehouse_code"),
                from_wh.warehouse_name.label("from_warehouse_name"),

                FinishedGoodsDocumentLine.to_warehouse_id,
                to_wh.warehouse_code.label("to_warehouse_code"),
                to_wh.warehouse_name.label("to_warehouse_name"),

                FinishedGoodsDocumentLine.quantity,
                FinishedGoodsDocumentLine.unit_cost,
                FinishedGoodsDocumentLine.remark,

                FinishedGoodsTransaction.id.label("transaction_id"),
                FinishedGoodsTransaction.transaction_type,
                FinishedGoodsTransaction.transaction_time,
                func.coalesce(
                    FinishedGoodsTransaction.quantity_delta,
                    direction_delta,
                ).label("quantity_delta"),
                FinishedGoodsTransaction.warehouse_id.label("tx_warehouse_id"),
                Warehouse.warehouse_code.label("tx_warehouse_code"),
                Warehouse.warehouse_name.label("tx_warehouse_name"),
            )
            .join(FinishedGoodsBatch,
                FinishedGoodsBatch.id == FinishedGoodsDocumentLine.batch_id)
            .outerjoin(                                       # ⭐ 改为 outerjoin
                FinishedGoodsTransaction,
                FinishedGoodsTransaction.document_line_id
                == FinishedGoodsDocumentLine.id,
            )
            .outerjoin(                                       # ⭐ 这里也要 outerjoin
                Warehouse,
                Warehouse.id == FinishedGoodsTransaction.warehouse_id,
            )
            .outerjoin(from_wh, from_wh.id == FinishedGoodsDocumentLine.from_warehouse_id)
            .outerjoin(to_wh, to_wh.id == FinishedGoodsDocumentLine.to_warehouse_id)
            .where(FinishedGoodsDocumentLine.document_id == document_id)
            .order_by(FinishedGoodsDocumentLine.line_no.asc())
        )
        lines = [dict(r) for r in db.session.execute(line_stmt).mappings().all()]

        total_in_qty = sum(
            (l["quantity_delta"] for l in lines
            if l["quantity_delta"] is not None and l["quantity_delta"] > 0),
            Decimal("0"),
        )
        total_out_qty = sum(
            (-l["quantity_delta"] for l in lines
            if l["quantity_delta"] is not None and l["quantity_delta"] < 0),
            Decimal("0"),
        )

        return {
            "document": {
                "id": doc.id,
                "document_no": doc.document_no,
                "document_type": doc.document_type,
                "document_time": doc.document_time,
                "status": doc.status,
                "related_document_id": doc.related_document_id,
                "remark": doc.remark,
                "created_by": doc.created_by,
                "confirmed_by": doc.confirmed_by,
                "created_at": doc.created_at,
                "confirmed_at": doc.confirmed_at,
            },
            "lines": lines,
            "total_in_qty": total_in_qty,
            "total_out_qty": total_out_qty,
        }
