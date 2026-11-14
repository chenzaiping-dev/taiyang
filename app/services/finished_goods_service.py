from decimal import Decimal
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
from app.utils.datetime_utils import now_beijing, now_beijing_naive
from app.utils.decimal_utils import _q3, _q6


def gen_doc_no(prefix: str) -> str:
    ts = now_beijing().strftime("%Y%m%d%H%M%S")
    import random
    return f"{prefix}{ts}{random.randint(0, 9999):04d}"

fg_doc_type_map = {
    "PRODUCTION_IN": "PRODUCTION_IN",
    "OUTBOUND": "OUTBOUND",
    "STOCKTAKE_GAIN": "STOCKTAKE",
    "STOCKTAKE_LOSS": "STOCKTAKE",
    "RETURN_IN": "RETURN",
    "RETURN_OUT": "RETURN",
    "TRANSFER_IN": "TRANSFER",
    "TRANSFER_OUT": "TRANSFER",
    "OUT_REVERSE": "REVERSE",     # ⭐ 新增
    "IN_REVERSE": "REVERSE",   # ⭐ 新增    
}

batch_source_map = {
    "PRODUCTION_IN": "PRODUCTION",
    "STOCKTAKE_GAIN": "STOCKTAKE",
    "RETURN_IN": "RETURN",
    "TRANSFER_IN": "OTHER",
}

fg_prefix_map = {
    "PRODUCTION_IN": "PI",
    "OUTBOUND": "OB",
    "STOCKTAKE_GAIN": "SG",
    "STOCKTAKE_LOSS": "SL",
    "RETURN_IN": "RI",
    "RETURN_OUT": "RO",
    "TRANSFER_IN": "TI",
    "TRANSFER_OUT": "TO",
    "OUT_REVERSE": "OUTREV",
    "IN_REVERSE": "INREV",
}

FG_TRANSACTION_TYPE_DIRECTION = {
    "PRODUCTION_IN":    "in",
    "OUTBOUND":         "out",
    "STOCKTAKE_GAIN":   "in",
    "STOCKTAKE_LOSS":   "out",
    "RETURN_IN":        "in",
    "RETURN_OUT":       "out",
    "TRANSFER_IN":      "in",
    "TRANSFER_OUT":     "out",
    "OUT_REVERSE":      "out",        # ⭐ 库存增加
    "IN_REVERSE":       "in",       # ⭐ 库存减少    
}

FG_INBOUND_TYPE_LABELS = {
    "PRODUCTION_IN": "生产入库",
    "STOCKTAKE_GAIN": "盘盈入库",
    "RETURN_IN": "退货入库",
    "TRANSFER_IN": "调拨入库",
    "IN_REVERSE": "入库撤销",   # ⭐ delta 负
}
FG_OUTBOUND_TYPE_LABELS = {
    "OUTBOUND": "销售出库",
    "STOCKTAKE_LOSS": "盘亏出库",
    "RETURN_OUT": "退货出库",
    "TRANSFER_OUT": "调拨出库",
    "OUT_REVERSE":  "出库撤销",   # ⭐ delta 正
}

FG_TX_TYPE_LABELS = {
    "PRODUCTION_IN": "生产入库",
    "OUTBOUND": "销售出库",
    "STOCKTAKE_GAIN": "盘盈入库",
    "STOCKTAKE_LOSS": "盘亏出库",
    "RETURN_IN": "退货入库",
    "RETURN_OUT": "退货出库",
    "TRANSFER_IN": "调拨入库",
    "TRANSFER_OUT": "调拨出库",
    "OUT_REVERSE":  "出库撤销",   # ⭐ delta 正
    "IN_REVERSE": "入库撤销",   # ⭐ delta 负
}


class FinishedGoodsError(Exception):
    pass


class FinishedGoodsService:

    # ---------- 成品入库 ----------
    @staticmethod
    def create_inbound(
        lines: list,
        inbound_type: str = "PRODUCTION_IN",
        warehouse_id: int = None,       # 兼容旧的单仓调用；若 lines 里都带 warehouse_id 可省略
        document_no: str = None,
        document_time: datetime = None,
        created_by: int = None,
        remark: str = None,
        auto_confirm: bool = True,
        commit: bool = True,          # ⭐ 新增
    ) -> dict:
        """
        inbound_type ∈ {'PRODUCTION_IN', 'RETURN_IN', 'STOCKTAKE_GAIN', 'TRANSFER_IN'}
        lines: [{
            'batch_id'(可选，用于选择已有批次),
            'batch_no'(新批次时提供),
            'product_name', 'product_specification', 'unit',
            'process_order_id'(可选),
            'quality_status'(可选，默认 QUALIFIED),
            'quantity', 'unit_cost'(可选), 'remark'
        }]
        """
        if inbound_type == 'OUT_REVERSE':
            pass
        else:
            if inbound_type not in FG_INBOUND_TYPE_LABELS:
                raise FinishedGoodsError(f"不支持的入库类型: {inbound_type}")

        if not lines:
            raise FinishedGoodsError("入库明细不能为空")

        doc_no = document_no or gen_doc_no("FG-" + fg_prefix_map[inbound_type])
        if FinishedGoodsDocument.query.filter_by(document_no=doc_no).first():
            raise FinishedGoodsError(f"单据号重复: {doc_no}")

        doc_time = document_time or now_beijing()

        document = FinishedGoodsDocument(
            document_no=doc_no,
            document_type=fg_doc_type_map[inbound_type],
            document_time=doc_time,
            status="CONFIRMED" if auto_confirm else "DRAFT",
            business_type=inbound_type,
            remark=remark,
            created_by=created_by,
            created_at=now_beijing_naive(),
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

            wh_id = int(l.get("warehouse_id") or warehouse_id or 0)
            if not wh_id:
                raise FinishedGoodsError(f"第 {idx} 行缺少仓库ID")

            wh = db.session.get(Warehouse, wh_id)
            if not wh or wh.status != 1:
                raise FinishedGoodsError(f"第 {idx} 行仓库不存在或停用")
            if wh.warehouse_type not in ("finished_goods", "general"):
                raise FinishedGoodsError(f"第 {idx} 行仓库不是成品仓")

            #print(l)

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
                        unit_cost=_q6(l.get("unit_cost") or 0) if l.get("unit_cost") else 0,
                        batch_source=(batch_source_map[inbound_type]
                            if inbound_type in batch_source_map else "OTHER"),
                        quality_status=l.get("quality_status") or "QUALIFIED",
                        remark=l.get("remark"),
                        created_at=now_beijing_naive(),
                    )
                    db.session.add(batch)
                    db.session.flush()
                else:
                    # 批次已存在（按批次号找的），同样要做一致性校验
                    incoming_po_id = l.get("process_order_id")
                    b_old_has_order_id = True
                    if batch.process_order_id is None:
                        b_old_has_order_id = False
                    if incoming_po_id is not None:
                        if (batch.process_order_id is not None
                                and int(batch.process_order_id) != int(incoming_po_id)):
                            raise FinishedGoodsError(
                                f"第 {idx} 行批次 [{batch.batch_no}] 已有来源工艺单，"
                                f"不能改为其他工艺单"
                            )
                        if batch.process_order_id is None:
                            batch.process_order_id = int(incoming_po_id)
                    # 做价格一致性检测, to be continued
                    input_unit_cost_str = l.get("unit_cost")
                    if input_unit_cost_str is not None:
                        input_unit_cost = _q6(input_unit_cost_str)
                        # 如果旧的产品批号有工艺单，则需要检查价格是否一致
                        if (b_old_has_order_id and input_unit_cost != batch.unit_cost):
                            raise FinishedGoodsError(
                                f"第 {idx} 行批次 [{batch.batch_no}] 价格不一致，"
                                f"当前: {batch.unit_cost}, 新输入: {input_unit_cost}"
                            )
                        # 如果旧的产品批号没有工艺单，则价格可以覆盖
                        if b_old_has_order_id is False:
                            batch.unit_cost = input_unit_cost

            line = FinishedGoodsDocumentLine(
                document_id=document.id,
                line_no=idx,
                batch_id=batch.id,
                from_warehouse_id=None,       # ⭐ 每行的仓库
                to_warehouse_id=wh_id,
                quantity=qty,
                unit_cost=_q6(l.get("unit_cost") or 0) if l.get("unit_cost") else None,
                remark=l.get("remark"),
                created_at=now_beijing_naive(),
            )
            db.session.add(line)
            db.session.flush()

            if auto_confirm:
                FinishedGoodsService._apply_delta(
                    warehouse_id=wh_id, batch_id=batch.id,
                    delta=qty, document=document, line=line,
                    tx_type=inbound_type, tx_time=doc_time,
                )

            line_results.append({
                "line_id": line.id,
                "batch_id": batch.id,
                "batch_no": batch.batch_no,
                "warehouse_id": wh_id,
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
            created_at=now_beijing_naive()
        )
        db.session.add(tx)
        return tx

    @staticmethod
    def create_outbound(
        lines: list,
        outbound_type: str = "OUTBOUND",
        warehouse_id: int = None,       # 兼容旧的单仓调用；若 lines 里都带 warehouse_id 可省略
        document_no: str = None,
        document_time: datetime = None,
        created_by: int = None,
        remark: str = None,
        auto_confirm: bool = True,          # ⭐ 新增
        commit: bool = True,
    ) -> dict:
        """
        lines 每行需包含：
        - warehouse_id（可选，缺省时用形参）
        - batch_id
        - quantity
        - unit_cost（可选）
        - remark（可选）
        """
        # 顶部仓库校验改为"按行取仓库后校验"
        # （删除原来的"必须先传 warehouse_id"的强校验，改到循环里做）
        #print(outbound_type, remark)
        if outbound_type == 'IN_REVERSE':
            pass
        else:
            if outbound_type not in FG_OUTBOUND_TYPE_LABELS:
                raise FinishedGoodsError(f"不支持的出库类型: {outbound_type}")
        if not lines:
            raise FinishedGoodsError("出库明细不能为空")

        doc_no = document_no or gen_doc_no("FG-" + fg_prefix_map[outbound_type])
        if FinishedGoodsDocument.query.filter_by(document_no=doc_no).first():
            raise FinishedGoodsError(f"单据号重复: {doc_no}")
        doc_time = document_time or now_beijing()

        document = FinishedGoodsDocument(
            document_no=doc_no,
            document_type=fg_doc_type_map[outbound_type],
            document_time=doc_time,
            status="CONFIRMED" if auto_confirm else "DRAFT",           # ⭐
            business_type=outbound_type,                               # ⭐
            remark=remark,
            created_by=created_by,
            confirmed_by=created_by if auto_confirm else None,        # ⭐
            confirmed_at=now_beijing() if auto_confirm else None,     # ⭐
            created_at=now_beijing_naive(),
        )
        db.session.add(document)
        db.session.flush()

        line_results = []
        for idx, l in enumerate(lines, start=1):
            qty = _q6(l["quantity"])
            if qty <= 0:
                raise FinishedGoodsError(f"第 {idx} 行数量必须 > 0")

            wh_id = int(l.get("warehouse_id") or warehouse_id or 0)
            if not wh_id:
                raise FinishedGoodsError(f"第 {idx} 行缺少仓库ID")

            wh = db.session.get(Warehouse, wh_id)
            if not wh or wh.status != 1:
                raise FinishedGoodsError(f"第 {idx} 行仓库不存在或停用")
            if wh.warehouse_type not in ("finished_goods", "general"):
                raise FinishedGoodsError(f"第 {idx} 行仓库不是成品仓")

            batch_id = int(l["batch_id"])
            batch = db.session.get(FinishedGoodsBatch, batch_id)
            if not batch:
                raise FinishedGoodsError(f"第 {idx} 行批次不存在")

            # ⭐ 只有 auto_confirm 才校验库存可用量；
            #    DRAFT 阶段允许先落单，等确认时再校验
            if auto_confirm:
                inv = FinishedGoodsInventory.query.filter_by(
                    warehouse_id=wh_id, batch_id=batch_id
                ).with_for_update().first()

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
                available = (inv.quantity or Decimal("0")) - locked if inv else Decimal("0")
                if qty > available:
                    raise FinishedGoodsError(
                        f"批次 [{batch.batch_no}] 在仓库 [{wh.warehouse_name}] 可用量不足："
                        f"可用 {available}，需要 {qty}"
                    )

            line = FinishedGoodsDocumentLine(
                document_id=document.id,
                line_no=idx,
                batch_id=batch_id,
                from_warehouse_id=wh_id,       # ⭐ 每行的仓库
                to_warehouse_id=None,
                quantity=qty,
                unit_cost=(
                    _q6(l.get("unit_cost"))
                    if l.get("unit_cost") is not None
                    else (batch.unit_cost if batch.unit_cost is not None else None)
                ),
                remark=l.get("remark"),
                created_at=now_beijing_naive(),
            )
            db.session.add(line)
            db.session.flush()

            if auto_confirm:
                FinishedGoodsService._apply_delta(
                    warehouse_id=wh_id, batch_id=batch_id,
                    delta=-qty, document=document, line=line,
                    tx_type=outbound_type, tx_time=doc_time,
                )

            line_results.append({
                "line_id": line.id,
                "warehouse_id": wh_id,
                "batch_id": batch_id,
                "batch_no": batch.batch_no,
                "quantity": str(qty),
            })

        # ---- 收尾：与原入库一致的提交方式 ----
        if commit:
            try:
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                raise FinishedGoodsError(f"成品出库失败: {e}")
        else:
            try:
                db.session.flush()
            except Exception as e:
                raise FinishedGoodsError(f"成品出库 flush 失败: {e}")

        return {
            "document_id": document.id,
            "document_no": document.document_no,
            "status": document.status,
            "document_type": document.document_type,
            "document_time": document.document_time.isoformat(),
            "lines": line_results,
        }


    @staticmethod
    def confirm_outbound_document(document_id: int, confirmed_by: int = None) -> dict:
        """
        确认草稿出库单：
        1. 校验状态 = DRAFT
        2. 逐行校验库存可用量
        3. 应用 _apply_delta（扣库存、写流水）
        4. 状态置 CONFIRMED
        """
        doc = db.session.get(FinishedGoodsDocument, document_id)
        if not doc:
            raise FinishedGoodsError("单据不存在")
        if doc.document_type not in (
            "OUTBOUND", "STOCKTAKE", "RETURN", "TRANSFER", "REVERSE",
        ):
            raise FinishedGoodsError("非出库类单据不可确认")
        if doc.status != "DRAFT":
            raise FinishedGoodsError(f"单据状态不为草稿，当前: {doc.status}")

        # ⭐ 从business_type来
        tx_type = doc.business_type or "OUTBOUND"

        lines = FinishedGoodsDocumentLine.query.filter_by(
            document_id=doc.id
        ).order_by(FinishedGoodsDocumentLine.line_no.asc()).all()
        if not lines:
            raise FinishedGoodsError("单据无明细，无法确认")

        doc_time = now_beijing()

        for line in lines:
            qty = _q6(line.quantity)
            if qty <= 0:
                raise FinishedGoodsError(f"行 {line.line_no}: 数量必须 > 0")

            batch_id = line.batch_id
            wh_id = line.from_warehouse_id
            if not wh_id:
                raise FinishedGoodsError(f"行 {line.line_no}: 缺少调出仓库")

            batch = db.session.get(FinishedGoodsBatch, batch_id)
            wh = db.session.get(Warehouse, wh_id)

            if line.unit_cost is None and batch and batch.unit_cost is not None:
                line.unit_cost = batch.unit_cost

            inv = FinishedGoodsInventory.query.filter_by(
                warehouse_id=wh_id, batch_id=batch_id
            ).with_for_update().first()

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

            available = (inv.quantity or Decimal("0")) - locked if inv else Decimal("0")
            if qty > available:
                raise FinishedGoodsError(
                    f"批次 [{batch.batch_no if batch else batch_id}] 在仓库 "
                    f"[{wh.warehouse_name if wh else wh_id}] 可用量不足："
                    f"可用 {available}，需要 {qty}"
                )

            FinishedGoodsService._apply_delta(
                warehouse_id=wh_id, batch_id=batch_id,
                delta=-qty, document=doc, line=line,
                tx_type=tx_type, tx_time=doc_time,     # ⭐ 用解析出来的类型
            )

        doc.status = "CONFIRMED"
        doc.confirmed_by = confirmed_by
        doc.confirmed_at = doc_time
        doc.document_time = doc_time

        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            raise FinishedGoodsError(f"确认出库单失败: {e}")

        return {"document_id": doc.id, "document_no": doc.document_no,
                "status": doc.status}

    @staticmethod
    def confirm_inbound_document(document_id: int, confirmed_by: int = None) -> dict:
        """
        确认草稿入库单：校验批次、逐行写 _apply_delta（加库存、写流水）。
        """
        doc = db.session.get(FinishedGoodsDocument, document_id)
        if not doc:
            raise FinishedGoodsError("单据不存在")
        if doc.document_type not in (
            "PRODUCTION_IN", "STOCKTAKE", "RETURN", "TRANSFER",
            "REVERSE",              # ⭐ 新增
        ):
            raise FinishedGoodsError("非入库类单据不可确认")
        if doc.status != "DRAFT":
            raise FinishedGoodsError(f"单据状态不为草稿，当前: {doc.status}")

        lines = FinishedGoodsDocumentLine.query.filter_by(
            document_id=doc.id
        ).order_by(FinishedGoodsDocumentLine.line_no.asc()).all()
        if not lines:
            raise FinishedGoodsError("单据无明细，无法确认")

        # 从 document_type 反推 tx_type
        type_to_tx = {
            "PRODUCTION_IN": "PRODUCTION_IN",
            "STOCKTAKE": "STOCKTAKE_GAIN",
            "RETURN": "RETURN_IN",
            "TRANSFER": "TRANSFER_IN",
            "REVERSE": "OUT_REVERSE",   # ⭐ 红冲入库
        }
        tx_type = doc.business_type or type_to_tx.get(doc.document_type, "PRODUCTION_IN")

        doc_time = now_beijing()

        for line in lines:
            qty = _q6(line.quantity)
            if qty <= 0:
                raise FinishedGoodsError(f"行 {line.line_no}: 数量必须 > 0")
            wh_id = line.to_warehouse_id
            if not wh_id:
                raise FinishedGoodsError(f"行 {line.line_no}: 缺少调入仓库")

            FinishedGoodsService._apply_delta(
                warehouse_id=wh_id, batch_id=line.batch_id,
                delta=qty, document=doc, line=line,
                tx_type=tx_type, tx_time=doc_time,
            )

        doc.status = "CONFIRMED"
        doc.confirmed_by = confirmed_by
        doc.confirmed_at = doc_time
        doc.document_time = doc_time

        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            raise FinishedGoodsError(f"确认入库单失败: {e}")

        return {"document_id": doc.id, "document_no": doc.document_no,
                "status": doc.status}

    @staticmethod
    def update_inbound_draft(
        document_id: int,
        warehouse_id: int,
        lines: list,
        document_time: datetime = None,
        business_type: str = "PRODUCTION_IN",
        remark: str = None,
    ) -> dict:
        """编辑草稿入库单（DRAFT）。重写明细，不产生库存变动。"""
        doc = db.session.get(FinishedGoodsDocument, document_id)
        if not doc:
            raise FinishedGoodsError("单据不存在")
        if doc.status != "DRAFT":
            raise FinishedGoodsError(f"单据状态不为草稿，当前: {doc.status}")
        if doc.document_type not in (
            "PRODUCTION_IN", "STOCKTAKE", "RETURN", "TRANSFER",
        ):
            raise FinishedGoodsError("非入库类单据不可编辑")

        if not lines:
            raise FinishedGoodsError("入库明细不能为空")

        doc.document_time = document_time or doc.document_time
        doc.remark = remark
        doc.business_type = business_type

        # 删旧明细（DRAFT 未产生流水，可以整批删）
        FinishedGoodsDocumentLine.query.filter_by(document_id=doc.id).delete(
            synchronize_session=False
        )
        db.session.flush()

        for idx, l in enumerate(lines, start=1):
            qty = _q6(l["quantity"])
            if qty <= 0:
                raise FinishedGoodsError(f"第 {idx} 行数量必须 > 0")

            # 找/建批次（与 create_inbound 相同的批次逻辑）
            batch = None
            if l.get("batch_id"):
                batch = db.session.get(FinishedGoodsBatch, int(l["batch_id"]))
                if not batch:
                    raise FinishedGoodsError(f"第 {idx} 行批次不存在")
            else:
                batch_no = (l.get("batch_no") or "").strip()
                if not batch_no:
                    raise FinishedGoodsError(f"第 {idx} 行缺少批次号")
                batch = FinishedGoodsBatch.query.filter_by(batch_no=batch_no).first()
                if not batch:
                    batch = FinishedGoodsBatch(
                        batch_no=batch_no,
                        process_order_id=l.get("process_order_id"),
                        product_name=l.get("product_name") or "未命名",
                        product_specification=l.get("product_specification"),
                        unit=l.get("unit") or "KG",
                        unit_cost=_q6(l.get("unit_cost") or 0) if l.get("unit_cost") else None,
                        batch_source="OTHER",
                        quality_status=l.get("quality_status") or "QUALIFIED",
                        remark=l.get("remark"),
                        created_at=now_beijing_naive(),
                    )
                    db.session.add(batch)
                    db.session.flush()

            line = FinishedGoodsDocumentLine(
                document_id=doc.id,
                line_no=idx,
                batch_id=batch.id,
                from_warehouse_id=None,
                to_warehouse_id=warehouse_id,
                quantity=qty,
                unit_cost=_q6(l.get("unit_cost") or 0) if l.get("unit_cost") else None,
                remark=l.get("remark"),
                created_at=now_beijing_naive(),
            )
            db.session.add(line)

        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            raise FinishedGoodsError(f"编辑入库草稿失败: {e}")

        return {"document_id": doc.id, "document_no": doc.document_no,
                "status": doc.status}


    @staticmethod
    def update_outbound_draft(
        document_id: int,
        lines: list,
        document_time: datetime = None,
        business_type: str = "OUTBOUND",
        remark: str = None,
    ) -> dict:
        """编辑草稿出库单（DRAFT）。重写明细，不产生库存变动。"""
        doc = db.session.get(FinishedGoodsDocument, document_id)
        if not doc:
            raise FinishedGoodsError("单据不存在")
        if doc.status != "DRAFT":
            raise FinishedGoodsError(f"单据状态不为草稿，当前: {doc.status}")
        if doc.document_type not in (
            "OUTBOUND", "STOCKTAKE", "RETURN", "TRANSFER", "REVERSE",
        ):
            raise FinishedGoodsError("非出库类单据不可编辑")

        if not lines:
            raise FinishedGoodsError("出库明细不能为空")

        doc.document_time = document_time or doc.document_time
        doc.remark = remark
        doc.business_type = business_type

        FinishedGoodsDocumentLine.query.filter_by(document_id=doc.id).delete(
            synchronize_session=False
        )
        db.session.flush()

        for idx, l in enumerate(lines, start=1):
            qty = _q6(l["quantity"])
            if qty <= 0:
                raise FinishedGoodsError(f"第 {idx} 行数量必须 > 0")

            wh_id = int(l.get("warehouse_id") or 0)
            if not wh_id:
                raise FinishedGoodsError(f"第 {idx} 行缺少仓库ID")
            batch_id = int(l["batch_id"])
            if not db.session.get(FinishedGoodsBatch, batch_id):
                raise FinishedGoodsError(f"第 {idx} 行批次不存在")

            line = FinishedGoodsDocumentLine(
                document_id=doc.id,
                line_no=idx,
                batch_id=batch_id,
                from_warehouse_id=wh_id,
                to_warehouse_id=None,
                quantity=qty,
                unit_cost=_q6(l.get("unit_cost") or 0) if l.get("unit_cost") else None,
                remark=l.get("remark"),
                created_at=now_beijing_naive(),
            )
            db.session.add(line)

        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            raise FinishedGoodsError(f"编辑出库草稿失败: {e}")

        return {"document_id": doc.id, "document_no": doc.document_no,
                "status": doc.status}

    @staticmethod
    def reverse_document(
        document_id: int,
        reason: str = None,
        created_by: int = None,
        commit: bool = True,          # ⭐ 新增
    ) -> dict:
        """
        撤销一张已确认（CONFIRMED）的入库/出库单：
        - 入库单（PRODUCTION_IN/STOCKTAKE/RETURN/TRANSFER 且 to_warehouse）→ 生成一张出库冲销
        - 出库单（OUTBOUND/STOCKTAKE/RETURN/TRANSFER/ADJUSTMENT 且 from_warehouse）→ 生成一张入库冲销
        新单据的 related_document_id = 原单 id，status = CONFIRMED（直接落地）。
        """
        doc = (
            FinishedGoodsDocument.query
            .filter_by(id=document_id)
            .with_for_update()
            .first()
        )
        if not doc:
            raise FinishedGoodsError("单据不存在")
        if doc.status != "CONFIRMED":
            raise FinishedGoodsError(f"只有已确认单据可撤销，当前: {doc.status}")
        if doc.document_type == "REVERSE":
            raise FinishedGoodsError("调整类单据（撤销单）不可再次撤销")

        if reason and "[CANCEL_" in reason:
            pass
        else:
            if doc.remark:
                if "[PROCESS_ORDER_RECEIVE]" in doc.remark:
                    raise FinishedGoodsError("工艺单入库, 请走生产计划/工艺单取消")
                if "[SHIP_PLAN_OUTBOUND]" in doc.remark:
                    raise FinishedGoodsError("发货计划出库, 请走发货计划取消")

        # ⭐ 核心防护：反查是否已存在撤销单
        existing_reverse = FinishedGoodsDocument.query.filter(
            FinishedGoodsDocument.related_document_id == doc.id,
            FinishedGoodsDocument.status == "CONFIRMED",
        ).first()
        if existing_reverse:
            raise FinishedGoodsError(
                f"该单据已撤销过，撤销单号 {existing_reverse.document_no}，"
                f"不能重复撤销"
            )

        lines = FinishedGoodsDocumentLine.query.filter_by(
            document_id=doc.id
        ).order_by(FinishedGoodsDocumentLine.line_no.asc()).all()
        if not lines:
            raise FinishedGoodsError("单据无明细")

        # 判断方向：看第一行的 to_warehouse 是否为空
        first = lines[0]
        is_inbound = first.to_warehouse_id is not None
        reverse_remark = f"[REVERSE] 撤销原单 {doc.document_no}" + (f" · {reason}" if reason else "")

        if is_inbound:
            # 入库单的废弃 → 生成一张出库单
            out_lines = [
                {
                    "warehouse_id": l.to_warehouse_id,
                    "batch_id": l.batch_id,
                    "quantity": str(l.quantity),
                    "remark": reverse_remark,
                }
                for l in lines
            ]
            result = FinishedGoodsService.create_outbound(
                lines=out_lines,
                document_no=f"INREV-{doc.document_no}",
                outbound_type="IN_REVERSE",
                document_time=now_beijing(),
                created_by=created_by,
                remark=reverse_remark,
                auto_confirm=True,
                commit=False,                # ⭐
            )
            new_doc = db.session.get(FinishedGoodsDocument, result["document_id"])
        else:
            # 出库单的废弃 → 生成一张入库
            in_lines = [
                {
                    "warehouse_id": l.from_warehouse_id,
                    "batch_id": l.batch_id,
                    "quantity": str(l.quantity),
                    "remark": reverse_remark,
                }
                for l in lines
            ]
            result = FinishedGoodsService.create_inbound(
                lines=in_lines,
                document_no=f"OUTREV-{doc.document_no}",
                inbound_type="OUT_REVERSE",       # ⭐ 出库撤销
                document_time=now_beijing(),
                created_by=created_by,
                remark=reverse_remark,
                auto_confirm=True,
                commit=False,                # ⭐
            )
            new_doc = db.session.get(FinishedGoodsDocument, result["document_id"])
            # ⭐ 若是发货计划出库，同步回滚发货计划
            #if doc.remark and "[SHIP_PLAN_OUTBOUND]" in doc.remark:
            #    try:
            #        FinishedGoodsService._rollback_shipment_plan_on_reverse(doc)
            #    except Exception as e:
            #        # 回滚发货计划失败 → 整体回滚
            #        db.session.rollback()
            #        raise FinishedGoodsError(f"撤销发货计划联动失败: {e}")

        # 关联原单
        new_doc.related_document_id = doc.id
        if commit:
            try:
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                raise FinishedGoodsError(f"撤销提交失败: {e}")
        else:
            try:
                db.session.flush()
            except Exception as e:
                raise FinishedGoodsError(f"撤销 flush 失败: {e}")

        return {
            "reverse_document_id": new_doc.id,
            "reverse_document_no": new_doc.document_no,
            "original_document_id": doc.id,
            "original_document_no": doc.document_no,
        }

    @staticmethod
    def _rollback_shipment_plan_on_reverse(orig_doc: FinishedGoodsDocument):
        """
        发货计划出库被撤销时，反向冲减 shipment_plan_allocations.shipped_quantity，
        并重算 shipment_plan_lines.shipped_quantity 与 shipment_plans.status。

        原发货计划出库单 document_no = FG-OUT-{plan.shipment_plan_no}
        通过 document_no 反查发货计划。
        """
        from app.generated.models.shipment_plans import ShipmentPlan
        from app.generated.models.shipment_plan_lines import ShipmentPlanLine
        from app.generated.models.shipment_plan_allocations import ShipmentPlanAllocation

        # 从 remark 里兜底识别；也可以直接用 document_no 前缀
        if not orig_doc.remark or "[SHIP_PLAN_OUTBOUND]" not in orig_doc.remark:
            return

        # 通过 document_no = FG-OUT-{shipment_plan_no} 反查
        prefix = "FG-OUT-"
        if not orig_doc.document_no.startswith(prefix):
            return
        plan_no = orig_doc.document_no[len(prefix):]
        plan = ShipmentPlan.query.filter_by(shipment_plan_no=plan_no).first()
        if not plan:
            raise FinishedGoodsError(
                f"找不到发货计划 {plan_no}，无法回滚"
            )

        # 1. 撤销本次出库对应的 allocation.shipped_quantity
        #    原出库单的每一行对应一个 allocation；通过 (wh, batch, qty) 匹配回滚
        orig_lines = FinishedGoodsDocumentLine.query.filter_by(
            document_id=orig_doc.id
        ).all()

        for ol in orig_lines:
            alloc = (
                ShipmentPlanAllocation.query
                .join(ShipmentPlanLine,
                    ShipmentPlanLine.id == ShipmentPlanAllocation.shipment_line_id)
                .filter(
                    ShipmentPlanLine.shipment_plan_id == plan.id,
                    ShipmentPlanAllocation.warehouse_id == ol.from_warehouse_id,
                    ShipmentPlanAllocation.batch_id == ol.batch_id,
                    ShipmentPlanAllocation.shipped_quantity >= ol.quantity,
                )
                .order_by(ShipmentPlanAllocation.id.desc())
                .with_for_update()
                .first()
            )
            if alloc:
                alloc.shipped_quantity = (alloc.shipped_quantity or Decimal("0")) - _q6(ol.quantity)

        # 2. 重算 line.shipped_quantity
        lines = ShipmentPlanLine.query.filter_by(
            shipment_plan_id=plan.id
        ).all()
        for line in lines:
            total = (
                db.session.query(func.coalesce(
                    func.sum(ShipmentPlanAllocation.shipped_quantity), 0))
                .filter(ShipmentPlanAllocation.shipment_line_id == line.id)
                .scalar()
            ) or Decimal("0")
            line.shipped_quantity = _q6(total)

        # 3. 重算 plan.status
        has_any = any(l.shipped_quantity > 0 for l in lines)
        all_done = all(l.shipped_quantity >= l.quantity for l in lines)

        if not has_any:
            plan.status = "CONFIRMED"
            plan.actual_ship_time = None
        elif all_done:
            plan.status = "SHIPPED"
        else:
            plan.status = "PARTIALLY_SHIPPED"

        # 注意：这里不提交，由外层 reverse_document 统一 commit

    @staticmethod
    def reverse_inbound_by_process_order(
        order,
        reason: str = None,
        created_by: int = None,
        commit=True,
    ):
        """
        撤销工艺单对应的成品入库单（FG-IN-{process_order_no}）。
        返回 reverse_document 的结果；若无入库单返回 None。
        """
        doc_no = f"FG-IN-{order.process_order_no}"
        doc = FinishedGoodsDocument.query.filter_by(document_no=doc_no).first()
        if not doc:
            return None
        return FinishedGoodsService.reverse_document(
            document_id=doc.id, reason=reason, 
            created_by=created_by, commit=commit,
        )