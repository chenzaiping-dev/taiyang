from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone, timedelta
from sqlalchemy import select, case, func, or_

from app.extensions import db
from app.generated.models.warehouses import Warehouse
from app.generated.models.employees import Employee

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


def gen_doc_no(prefix: str) -> str:
    ts = now_beijing().strftime("%Y%m%d%H%M%S")
    import random
    return f"{prefix}{ts}{random.randint(0, 9999):04d}"


class FinishedGoodsError(Exception):
    pass


class FinishedGoodsService:

    # ---------- 成品入库 ----------
    @staticmethod
    def create_inbound(
        warehouse_id: int,
        lines: list,
        inbound_type: str = "PRODUCTION_IN",
        document_no: str = None,
        document_time: datetime = None,
        created_by: int = None,
        remark: str = None,
        auto_confirm: bool = True,
        commit: bool = True,          # ⭐ 新增
    ) -> dict:
        """
        inbound_type ∈ {'PRODUCTION_IN', 'CUSTOMER_RETURN', 'ADJUSTMENT'}
        lines: [{
            'batch_id'(可选，用于选择已有批次),
            'batch_no'(新批次时提供),
            'product_name', 'product_specification', 'unit',
            'process_order_id'(可选),
            'quality_status'(可选，默认 QUALIFIED),
            'quantity', 'unit_cost'(可选), 'remark'
        }]
        """
        if inbound_type not in ("PRODUCTION_IN", "CUSTOMER_RETURN", "ADJUSTMENT"):
            raise FinishedGoodsError("不支持的入库类型")

        wh = db.session.get(Warehouse, warehouse_id)
        if not wh or wh.status != 1:
            raise FinishedGoodsError("仓库不存在或停用")
        if wh.warehouse_type not in ("finished_goods", "general"):
            raise FinishedGoodsError("成品入库必须选择成品仓或普通仓")

        if not lines:
            raise FinishedGoodsError("入库明细不能为空")

        doc_no = document_no or gen_doc_no("FG-IN")
        if FinishedGoodsDocument.query.filter_by(document_no=doc_no).first():
            raise FinishedGoodsError(f"单据号重复: {doc_no}")

        doc_time = document_time or now_beijing()

        document = FinishedGoodsDocument(
            document_no=doc_no,
            document_type=inbound_type,
            document_time=doc_time,
            status="CONFIRMED" if auto_confirm else "DRAFT",
            remark=remark,
            created_by=created_by,
            confirmed_by=created_by if auto_confirm else None,
            confirmed_at=now_beijing() if auto_confirm else None,
        )
        db.session.add(document)
        db.session.flush()

        line_results = []
        for idx, l in enumerate(lines, start=1):
            qty = _q6(l["quantity"])
            if qty <= 0:
                raise FinishedGoodsError(f"第 {idx} 行数量必须 > 0")

            # 找/建批次
            batch = None
            if l.get("batch_id"):
                batch = db.session.get(FinishedGoodsBatch, int(l["batch_id"]))
                if not batch:
                    raise FinishedGoodsError(f"第 {idx} 行批次不存在")

                # ⭐ 批次已存在时校验来源工艺单一致性
                incoming_po_id = l.get("process_order_id")
                if incoming_po_id is not None:
                    if (batch.process_order_id is not None
                            and int(batch.process_order_id) != int(incoming_po_id)):
                        raise FinishedGoodsError(
                            f"第 {idx} 行批次 [{batch.batch_no}] 已有来源工艺单，"
                            f"不能改为其他工艺单"
                        )
                    # 原本为空则允许补写（例如先前手工入库未关联，现在补上）
                    if batch.process_order_id is None:
                        batch.process_order_id = int(incoming_po_id)
            else:
                batch_no = (l.get("batch_no") or "").strip()
                if not batch_no:
                    raise FinishedGoodsError(f"第 {idx} 行缺少批次号")
                batch = FinishedGoodsBatch.query.filter_by(batch_no=batch_no).first()
                if not batch:
                    batch = FinishedGoodsBatch(
                        batch_no=batch_no,
                        process_order_id=l.get("process_order_id"),   # ⭐ 新建时写入
                        product_name=l.get("product_name") or "未命名",
                        product_specification=l.get("product_specification"),
                        unit=l.get("unit") or "KG",
                        batch_source=(
                            "RETURN" if inbound_type == "CUSTOMER_RETURN"
                            else ("PRODUCTION" if inbound_type == "PRODUCTION_IN"
                                else "OTHER")
                        ),
                        quality_status=l.get("quality_status") or "QUALIFIED",
                        remark=l.get("remark"),
                    )
                    db.session.add(batch)
                    db.session.flush()
                else:
                    # 批次已存在（按批次号找的），同样要做一致性校验
                    incoming_po_id = l.get("process_order_id")
                    if incoming_po_id is not None:
                        if (batch.process_order_id is not None
                                and int(batch.process_order_id) != int(incoming_po_id)):
                            raise FinishedGoodsError(
                                f"第 {idx} 行批次 [{batch.batch_no}] 已有来源工艺单，"
                                f"不能改为其他工艺单"
                            )
                        if batch.process_order_id is None:
                            batch.process_order_id = int(incoming_po_id)

            line = FinishedGoodsDocumentLine(
                document_id=document.id,
                line_no=idx,
                batch_id=batch.id,
                from_warehouse_id=None,
                to_warehouse_id=warehouse_id,
                quantity=qty,
                unit_cost=_q6(l.get("unit_cost") or 0) if l.get("unit_cost") else None,
                remark=l.get("remark"),
            )
            db.session.add(line)
            db.session.flush()

            if auto_confirm:
                FinishedGoodsService._apply_delta(
                    warehouse_id=warehouse_id, batch_id=batch.id,
                    delta=qty, document=document, line=line,
                    tx_type=inbound_type, tx_time=doc_time,
                )

            line_results.append({
                "line_id": line.id,
                "batch_id": batch.id,
                "batch_no": batch.batch_no,
                "quantity": str(qty),
            })

        # ---- 收尾提交逻辑改为条件执行 ----
        if commit:
            try:
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                raise FinishedGoodsError(f"成品入库失败: {e}")
        else:
            try:
                db.session.flush()    # ⭐ 只 flush，交给外层事务提交
            except Exception as e:
                raise FinishedGoodsError(f"成品入库 flush 失败: {e}")

        return {
            "document_id": document.id,
            "document_no": document.document_no,
            "status": document.status,
            "document_type": document.document_type,
            "document_time": document.document_time.isoformat(),
            "lines": line_results,
        }

    # ---------- 成品出库（人工选批次） ----------
    @staticmethod
    def create_outbound(
        warehouse_id: int,
        lines: list,
        document_no: str = None,
        document_time: datetime = None,
        created_by: int = None,
        remark: str = None,
    ) -> dict:
        """
        人工选批次出库。
        lines: [{'batch_id', 'quantity', 'unit_cost'(可选), 'remark'}]
        """
        wh = db.session.get(Warehouse, warehouse_id)
        if not wh or wh.status != 1:
            raise FinishedGoodsError("仓库不存在或停用")
        if wh.warehouse_type not in ("finished_goods", "general"):
            raise FinishedGoodsError("成品出库必须选择成品仓或普通仓")
        if not lines:
            raise FinishedGoodsError("出库明细不能为空")

        doc_no = document_no or gen_doc_no("FG-OUT")
        if FinishedGoodsDocument.query.filter_by(document_no=doc_no).first():
            raise FinishedGoodsError(f"单据号重复: {doc_no}")
        doc_time = document_time or now_beijing()

        document = FinishedGoodsDocument(
            document_no=doc_no,
            document_type="OUTBOUND",
            document_time=doc_time,
            status="CONFIRMED",
            remark=remark,
            created_by=created_by,
            confirmed_by=created_by,
            confirmed_at=now_beijing(),
        )
        db.session.add(document)
        db.session.flush()

        line_results = []
        for idx, l in enumerate(lines, start=1):
            qty = _q6(l["quantity"])
            if qty <= 0:
                raise FinishedGoodsError(f"第 {idx} 行数量必须 > 0")
            batch_id = int(l["batch_id"])
            batch = db.session.get(FinishedGoodsBatch, batch_id)
            if not batch:
                raise FinishedGoodsError(f"第 {idx} 行批次不存在")

            # 校验批次在该仓库的库存
            inv = FinishedGoodsInventory.query.filter_by(
                warehouse_id=warehouse_id, batch_id=batch_id
            ).with_for_update().first()

            locked = (
                db.session.query(func.coalesce(
                    func.sum(FinishedGoodsReservation.remaining_quantity), 0))
                .filter(
                    FinishedGoodsReservation.warehouse_id == warehouse_id,
                    FinishedGoodsReservation.batch_id == batch_id,
                    FinishedGoodsReservation.status == "ACTIVE",
                )
                .scalar()
            ) or Decimal("0")

            available = (inv.quantity or Decimal("0")) - locked
            if qty > available:
                raise FinishedGoodsError(
                    f"批次 [{batch.batch_no}] 可用量不足：可用 {available}，需要 {qty}"
                )

            line = FinishedGoodsDocumentLine(
                document_id=document.id,
                line_no=idx,
                batch_id=batch_id,
                from_warehouse_id=warehouse_id,
                to_warehouse_id=None,
                quantity=qty,
                unit_cost=_q6(l.get("unit_cost") or 0) if l.get("unit_cost") else None,
                remark=l.get("remark"),
            )
            db.session.add(line)
            db.session.flush()

            FinishedGoodsService._apply_delta(
                warehouse_id=warehouse_id, batch_id=batch_id,
                delta=-qty, document=document, line=line,
                tx_type="OUTBOUND", tx_time=doc_time,
            )

            line_results.append({
                "line_id": line.id,
                "batch_id": batch_id,
                "batch_no": batch.batch_no,
                "quantity": str(qty),
            })

        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            raise FinishedGoodsError(f"成品出库失败: {e}")

        return {
            "document_id": document.id,
            "document_no": document.document_no,
            "status": document.status,
            "document_type": document.document_type,
            "document_time": document.document_time.isoformat(),
            "lines": line_results,
        }

    # ---------- 库存变动应用 ----------
    @staticmethod
    def _apply_delta(warehouse_id, batch_id, delta, document, line, tx_type, tx_time):
        inv = FinishedGoodsInventory.query.filter_by(
            warehouse_id=warehouse_id, batch_id=batch_id
        ).with_for_update().first()
        if not inv:
            inv = FinishedGoodsInventory(
                warehouse_id=warehouse_id,
                batch_id=batch_id,
                quantity=Decimal("0"),
            )
            db.session.add(inv)
        new_qty = (inv.quantity or Decimal("0")) + delta
        if new_qty < 0:
            raise FinishedGoodsError(f"批次 {batch_id} 库存不足以出库")
        inv.quantity = new_qty

        tx = FinishedGoodsTransaction(
            document_id=document.id,
            document_line_id=line.id,
            batch_id=batch_id,
            warehouse_id=warehouse_id,
            transaction_type=tx_type,
            quantity_delta=delta,
            transaction_time=tx_time,
        )
        db.session.add(tx)
        return tx