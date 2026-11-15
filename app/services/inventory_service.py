from decimal import Decimal
from datetime import datetime
from sqlalchemy import func, and_

from app.extensions import db
from app.generated.models.materials import Material
from app.generated.models.warehouses import Warehouse
from app.generated.models.suppliers import Supplier

from app.generated.models.inventory_documents import InventoryDocument
from app.generated.models.inventory_document_lines import InventoryDocumentLine
from app.generated.models.inventory_transactions import InventoryTransaction
from app.generated.models.inventory_cost_layers import InventoryCostLayer
from app.generated.models.inventory_cost_allocations import InventoryCostAllocation

from app.utils.datetime_utils import now_beijing_naive
from app.utils.decimal_utils import _q3, _q6, _q6_amount

class InventoryError(Exception):
    pass

# 单据类型映射
INBOUND_TYPE_LABELS = {
    "PURCHASE_IN":   "采购入库",
    "STOCKTAKE_GAIN": "盘点盘盈",
    "RETURN_IN":     "退货回仓",
}

OUTBOUND_TYPE_LABELS = {
    "PRODUCTION_OUT":  "生产领料",
    "STOCKTAKE_LOSS":  "盘亏出库",
    "RETURN_OUT":      "退货出库",
    "ADJUSTMENT_RED":  "配比红冲出库",
    "ADJUSTMENT_BLUE": "配比蓝补出库",
}

doc_type_map = {
    "PURCHASE_IN":      "PURCHASE_IN",
    "STOCKTAKE_GAIN":   "STOCKTAKE",
    "RETURN_IN":        "RETURN",
    "PRODUCTION_OUT":   "PRODUCTION_OUT",
    "STOCKTAKE_LOSS":   "STOCKTAKE",
    "RETURN_OUT":       "RETURN",
    "ADJUSTMENT_RED":   "ADJUSTMENT_RED",
    "ADJUSTMENT_BLUE":  "ADJUSTMENT_BLUE",
}

tx_type_map = {
    "PURCHASE_IN":      "PURCHASE_IN",
    "STOCKTAKE_GAIN":   "STOCKTAKE_GAIN",
    "RETURN_IN":        "RETURN_IN",
    "PRODUCTION_OUT":   "PRODUCTION_OUT",
    "STOCKTAKE_LOSS":   "STOCKTAKE_LOSS",
    "RETURN_OUT":       "RETURN_OUT",
    "ADJUSTMENT_RED":   "ADJUSTMENT_RED",
    "ADJUSTMENT_BLUE":  "ADJUSTMENT_BLUE",
}

prefix_map = {
    "PURCHASE_IN":      "PI",
    "STOCKTAKE_GAIN":   "SG",
    "RETURN_IN":        "RI",
    "PRODUCTION_OUT":   "PO",
    "STOCKTAKE_LOSS":   "SL",
    "RETURN_OUT":       "RO",
    "ADJUSTMENT_RED":   "AR",
    "ADJUSTMENT_BLUE":  "AB",
}
layer_type_map = {
    "PURCHASE_IN": "PURCHASE",
    "STOCKTAKE_GAIN": "STOCKTAKE_RESET",
    "RETURN_IN": "RETURN",
}

def _rm_warehouses():
    return (
        Warehouse.query
        .filter(
            Warehouse.status == 1,
            Warehouse.warehouse_type.in_(["raw_material", "general"]),
        )
        .order_by(Warehouse.id.asc())
        .all()
    )

def generate_document_no(prefix: str = "PI") -> str:
    """生成单据号：前缀 + yyyyMMddHHmmss + 4位流水"""
    now = datetime.now()
    base = now.strftime("%Y%m%d%H%M%S")
    # 简单实现：借助 database id 递增更严谨，这里用随机/时间戳足够
    import random
    suffix = f"{random.randint(0, 9999):04d}"
    return f"{prefix}{base}{suffix}"

def generate_document_no_new(prefix: str = "PI") -> str:
    """生成单据号：前缀 + yyyyMMddHHmmss + 4位流水"""
    return generate_document_no(prefix_map[prefix])


def _save_document_line(document_id, line_no, material_id, quantity,
                        transaction_type, warehouse_id, supplier_id=None,
                        process_order_id=None, unit_cost=None, remark=None):
    """保存业务单据明细；这里绝不创建库存流水。"""
    line = InventoryDocumentLine(
        document_id=document_id,
        line_no=line_no,
        material_id=material_id,
        transaction_type=transaction_type,
        quantity=quantity,                 # 明细数量统一为正数
        unit_cost=unit_cost,
        supplier_id=supplier_id,
        process_order_id=process_order_id,
        warehouse_id=warehouse_id,
        remark=remark,
        created_at=now_beijing_naive(),
    )
    db.session.add(line)
    return line


class PurchaseInService:
    """原料入库单：草稿只写 inventory_document_lines，确认后生成流水和 FIFO 成本层。"""

    @staticmethod
    def create_purchase_in(
        lines: list,
        inbound_type: str = "PURCHASE_IN",
        warehouse_id: int = None,
        supplier_id: int = None,
        document_no: str = None,
        document_time: datetime = None,
        created_by: int = None,
        remark: str = None,
        auto_confirm: bool = True,
    ) -> dict:
        if not lines:
            raise InventoryError("入库明细不能为空")
        if inbound_type not in INBOUND_TYPE_LABELS:
            raise InventoryError(f"不支持的入库类型: {inbound_type}")

        # 1. 校验仓库
        wh = db.session.get(Warehouse, warehouse_id)
        if not wh or wh.status != 1 or wh.warehouse_type not in ("raw_material", "general"):
            raise InventoryError("原料入库必须使用有效的原料仓或普通仓")

        # 2. 校验供应商
        sup = db.session.get(Supplier, supplier_id)
        if not sup or sup.status != 1:
            raise InventoryError(f"供应商不存在或已停用: {supplier_id}")

        # 3. 校验明细
        mids = [int(x["material_id"]) for x in lines]
        materials = {m.id: m for m in Material.query.filter(Material.id.in_(mids)).all()}
        doc_time = document_time or now_beijing_naive()
        for line in lines:
            mid = int(line["material_id"])
            if mid not in materials or materials[mid].status != 1:
                raise InventoryError(f"原料不存在或已停用: {mid}")
            if _q6(line["quantity"]) <= 0:
                raise InventoryError("入库数量必须大于 0")
            if line.get("unit_cost") is None or _q6_amount(line["unit_cost"]) < 0:
                raise InventoryError("入库单价不能为空且不能为负数")

        # 4. 生成单据号
        doc_no = document_no or generate_document_no(prefix_map[inbound_type])
        if InventoryDocument.query.filter_by(document_no=doc_no).first():
            raise InventoryError(f"单据号重复: {doc_no}")

        # 5. 创建单据
        doc = InventoryDocument(
            document_no=doc_no,
            document_type=doc_type_map[inbound_type],
            document_time=doc_time,
            status="DRAFT",
            remark=remark,
            created_by=created_by,
            created_at=doc_time,
            confirmed_by=created_by if auto_confirm else None,
            confirmed_at=now_beijing_naive() if auto_confirm else None,
        )
        db.session.add(doc)
        db.session.flush()

        # 6. 保存单据明细
        for idx, line in enumerate(lines, 1):
            _save_document_line(
                doc.id, int(line.get("line_no", idx)), int(line["material_id"]),
                _q6(line["quantity"]), tx_type_map[inbound_type], warehouse_id,
                supplier_id=supplier_id, unit_cost=_q6_amount(line["unit_cost"]),
                remark=line.get("remark"),
            )

        # 7. 确认提交
        try:
            db.session.flush()
            if auto_confirm:
                result = PurchaseInService.confirm_document(doc.id, confirmed_by=created_by, commit=False)
            else:
                result = {"document_id": doc.id, "status": "DRAFT"}
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            if isinstance(e, InventoryError):
                raise
            raise InventoryError(f"入库单创建失败: {e}")
        return {
            "document_id": doc.id,
            "document_no": doc.document_no,
            "status": doc.status,
            "document_time": doc.document_time.isoformat(),
            "warehouse_id": warehouse_id,
            "supplier_id": supplier_id,
            "line_count": len(lines),
            "lines_result": result,
            "total_cost": str(sum((_q6(line["quantity"]) * _q6_amount(line["unit_cost"])
                                  for line in lines), Decimal("0"))) if auto_confirm else None,
        }

    @staticmethod
    def confirm_document(
        document_id: int,
        confirmed_by: int = None,
        commit: bool = True
    ) -> dict:
        doc = db.session.query(InventoryDocument).filter_by(id=document_id).with_for_update().first()
        if not doc:
            raise InventoryError("单据不存在")
        if doc.document_type not in ("PURCHASE_IN", "STOCKTAKE", "RETURN"):
            raise InventoryError("仅入库单可确认")
        if doc.status != "DRAFT":
            raise InventoryError(f"单据状态不为草稿，当前: {doc.status}")

        lines = (InventoryDocumentLine.query.filter_by(document_id=doc.id)
                 .order_by(InventoryDocumentLine.line_no.asc()).all())
        if not lines:
            raise InventoryError("单据没有明细，不能确认")
        if InventoryTransaction.query.filter_by(document_id=doc.id).first():
            raise InventoryError("该单据已生成流水，禁止重复确认")

        now = now_beijing_naive()
        layers = []
        for line in lines:
            if line.quantity <= 0:
                raise InventoryError("入库明细数量必须大于 0")
            if line.transaction_type not in ("PURCHASE_IN", "STOCKTAKE_GAIN", "RETURN_IN"):
                raise InventoryError(f"非法入库类型: {line.transaction_type}")
            unit_cost = _q6_amount(line.unit_cost or 0)
            qty = _q6(line.quantity)
            tx = InventoryTransaction(
                document_id=doc.id, line_no=line.line_no,
                material_id=line.material_id,
                transaction_type=line.transaction_type,
                transaction_time=now,
                quantity_delta=qty, unit_cost=unit_cost,
                total_cost=_q6_amount(qty * unit_cost),
                supplier_id=line.supplier_id, process_order_id=line.process_order_id,
                warehouse_id=line.warehouse_id, created_at=now,
            )
            db.session.add(tx)
            db.session.flush()
            layer = InventoryCostLayer(
                material_id=line.material_id, source_transaction_id=tx.id,
                layer_type=layer_type_map[line.transaction_type],
                effective_time=doc.document_time, original_qty=qty,
                remaining_qty=qty, unit_cost=unit_cost,
                original_cost=_q6_amount(qty * unit_cost),
                warehouse_id=line.warehouse_id, created_at=now,
            )
            db.session.add(layer)
            layers.append(layer)

        doc.status = "CONFIRMED"
        doc.document_time = now
        doc.confirmed_at = now
        doc.confirmed_by = confirmed_by

        if commit:
            try:
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                raise InventoryError(f"确认入库失败: {e}")
        else:
            db.session.flush()

        return {"document_id": doc.id,
                "status": doc.status,
                "layers": [
                    {
                        "layer_id": layer.id,
                        "transaction_id": layer.source_transaction_id,
                        "material_id": layer.material_id,
                        "original_qty": str(layer.original_qty),
                        "remaining_qty": str(layer.remaining_qty),
                        "unit_cost": str(layer.unit_cost),
                    }
                    for layer in layers
                ],
                }

    @staticmethod
    def update_draft(
        document_id: int,
        lines: list,
        warehouse_id: int,
        supplier_id: int,
        inbound_type: str = "PURCHASE_IN",
        document_time: datetime = None,
        remark: str = None
    ) -> dict:
        doc = db.session.query(InventoryDocument).filter_by(id=document_id).with_for_update().first()
        if not doc or doc.status != "DRAFT":
            raise InventoryError("单据不存在或不是草稿")
        if not lines:
            raise InventoryError("入库明细不能为空")
        wh = db.session.get(Warehouse, warehouse_id)
        sup = db.session.get(Supplier, supplier_id)
        if not wh or wh.status != 1 or wh.warehouse_type not in ("raw_material", "general"):
            raise InventoryError("原料入库必须使用有效的原料仓或普通仓")
        if not sup or sup.status != 1:
            raise InventoryError("供应商不存在或已停用")
        mids = [int(x["material_id"]) for x in lines]
        materials = {m.id: m for m in Material.query.filter(Material.id.in_(mids)).all()}
        for line in lines:
            mid = int(line["material_id"])
            if mid not in materials or materials[mid].status != 1:
                raise InventoryError(f"原料不存在或已停用: {mid}")
            if _q6(line["quantity"]) <= 0 or line.get("unit_cost") is None or _q6_amount(line["unit_cost"]) < 0:
                raise InventoryError("入库数量必须大于 0，单价不能为空且不能为负数")
        InventoryDocumentLine.query.filter_by(document_id=doc.id).delete(synchronize_session=False)
        doc.document_type = doc_type_map[inbound_type]
        doc.document_time = document_time or doc.document_time
        doc.remark = remark
        for idx, line in enumerate(lines, 1):
            _save_document_line(
                doc.id, int(line.get("line_no", idx)), int(line["material_id"]),
                _q6(line["quantity"]), tx_type_map[inbound_type], warehouse_id,
                supplier_id=supplier_id, unit_cost=_q6_amount(line["unit_cost"]),
                remark=line.get("remark"),
            )
        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            raise InventoryError(f"编辑入库草稿失败: {e}")
        return {"document_id": doc.id, "document_no": doc.document_no, "status": doc.status}


class StockOutService:
    """原料出库（FIFO 成本层消耗）"""

    @staticmethod
    def _precheck_available(warehouse_id, lines, allow_partial=False):
        """
        整单出库前预检：按 (warehouse_id, material_id) 汇总需求，
        与当前成本层剩余量比较，收集全部不足项后一次性抛出。
        allow_partial=True 时不做"不足"拦截（与 _consume_fifo 语义一致）。
        """
        if allow_partial:
            return

        # 1. 聚合本单需求
        need_map = {}
        for l in lines:
            mid = int(l["material_id"])
            qty = _q6(l["quantity"])
            if qty <= 0:
                continue
            key = (int(warehouse_id), mid)
            need_map[key] = need_map.get(key, Decimal("0")) + qty

        if not need_map:
            return

        # 2. 一次性查出涉及的原料
        mids = {mid for _, mid in need_map.keys()}
        materials = {
            m.id: m for m in Material.query.filter(Material.id.in_(mids)).all()
        }

        # 3. 一次性按 (warehouse, material) 汇总可用量
        avail_rows = (
            db.session.query(
                InventoryCostLayer.material_id.label("mid"),
                func.coalesce(func.sum(InventoryCostLayer.remaining_qty), 0)
                    .label("available"),
            )
            .filter(
                InventoryCostLayer.warehouse_id == warehouse_id,
                InventoryCostLayer.material_id.in_(mids),
                InventoryCostLayer.remaining_qty > 0,
            )
            .group_by(InventoryCostLayer.material_id)
            .all()
        )
        available_map = {int(r.mid): _q6(r.available or 0) for r in avail_rows}

        # 4. 收集不足项
        shortages = []
        for (wh_id, mid), need in need_map.items():
            available = available_map.get(mid, Decimal("0"))
            if need > available:
                m = materials.get(mid)
                label = (
                    f"{m.material_code} - {m.material_name}"
                    if m else f"#{mid}"
                )
                shortages.append(
                    f"{label}：需要 {need}，可用 {available}"
                )

        if shortages:
            raise InventoryError(
                "以下原料库存不足，无法出库：\n" + "\n".join(shortages)
            )

    @staticmethod
    def _consume_fifo(
        warehouse_id: int,
        material_id: int,
        quantity: Decimal,
        out_transaction_id: int,
        allow_partial: bool = False,
    ) -> dict:
        """
        对单个原料按 FIFO 消耗成本层，写入成本分配明细。
        返回：{allocations: [...], total_cost: Decimal, consumed: Decimal, shortage: Decimal}
        """
        layers = (
            InventoryCostLayer.query
            .filter(
                InventoryCostLayer.warehouse_id == warehouse_id,
                InventoryCostLayer.material_id == material_id,
                InventoryCostLayer.remaining_qty > 0,
            )
            .order_by(
                InventoryCostLayer.effective_time.asc(),
                InventoryCostLayer.id.asc(),
            )
            .with_for_update()  # 行锁，防止并发超卖
            .all()
        )

        #material = db.session.get(Material, material_id)
        #if not material:
        #    raise InventoryError(f"原料不存在: {material_id}")

        available = sum((l.remaining_qty for l in layers), Decimal("0"))
        if available < quantity and not allow_partial:
            raise InventoryError(
                f"库存不足：material_id={material_id} 需要 {quantity}，可用 {available}"
                #f"原料库存不足, {material.material_code} - {material.material_name} 需要 {quantity}，可用 {available}"
            )

        need = quantity
        allocations = []
        total_cost = Decimal("0")
        doc_time = now_beijing_naive()

        for layer in layers:
            if need <= 0:
                break
            take = min(layer.remaining_qty, need)
            if take <= 0:
                continue

            cost_amount = _q6_amount(take * layer.unit_cost)

            alloc = InventoryCostAllocation(
                out_transaction_id=out_transaction_id,
                cost_layer_id=layer.id,
                quantity=take,
                unit_cost=layer.unit_cost,
                cost_amount=cost_amount,
                created_at=doc_time,
            )
            db.session.add(alloc)

            layer.remaining_qty = layer.remaining_qty - take
            need = need - take
            total_cost += cost_amount

            allocations.append({
                "cost_layer_id": layer.id,
                "quantity": str(take),
                "unit_cost": str(layer.unit_cost),
                "cost_amount": str(cost_amount),
            })

        consumed = quantity - need
        return {
            "material_id": material_id,
            "quantity": quantity,
            "allocations": allocations,
            "total_cost": total_cost,
            "consumed": consumed,
            "shortage": need,
        }


    @staticmethod
    def create_stock_out(
        lines: list,
        outbound_type: str = "PRODUCTION_OUT",
        warehouse_id: int = None,
        document_no: str = None,
        document_time: datetime = None,
        process_order_id: int = None,
        created_by: int = None,
        remark: str = None,
        auto_confirm: bool = True,
        allow_partial: bool = False,
        commit: bool = True,
    ) -> dict:
        """出库单草稿只写单据明细；确认时才生成负向库存流水并扣减 FIFO。"""
        if not lines:
            raise InventoryError("出库明细不能为空")
        if outbound_type not in OUTBOUND_TYPE_LABELS:
            raise InventoryError(f"不支持的出库类型: {outbound_type}")

        # 1. 校验仓库
        wh = db.session.get(Warehouse, warehouse_id)
        if not wh or wh.status != 1 or wh.warehouse_type not in ("raw_material", "general"):
            raise InventoryError("原料出库必须使用有效的原料仓或普通仓")

        # 2. 校验明细
        mids = [int(x["material_id"]) for x in lines]
        materials = {m.id: m for m in Material.query.filter(Material.id.in_(mids)).all()}
        for line in lines:
            mid = int(line["material_id"])
            if mid not in materials or materials[mid].status != 1:
                raise InventoryError(f"原料不存在或已停用: {mid}")
            if _q6(line["quantity"]) <= 0:
                raise InventoryError("出库数量必须大于 0")

        # 3. 生成单据号
        doc_time = document_time or now_beijing_naive()
        doc_no = document_no or generate_document_no(prefix_map[outbound_type])
        if InventoryDocument.query.filter_by(document_no=doc_no).first():
            raise InventoryError(f"单据号重复: {doc_no}")

        # 4. 创建单据
        doc = InventoryDocument(
            document_no=doc_no, document_type=doc_type_map[outbound_type],
            document_time=doc_time, status="DRAFT", remark=remark,
            created_by=created_by,
            created_at=doc_time,
            confirmed_by=created_by if auto_confirm else None,
            confirmed_at=now_beijing_naive() if auto_confirm else None,
        )
        db.session.add(doc)
        db.session.flush()

        # 5. 保存单据明细
        for idx, line in enumerate(lines, 1):
            _save_document_line(
                doc.id, int(line.get("line_no", idx)), int(line["material_id"]),
                _q6(line["quantity"]), tx_type_map[outbound_type], warehouse_id,
                process_order_id=process_order_id, remark=line.get("remark"),
            )

        # 7. 确认提交
        try:
            if auto_confirm:
                result = StockOutService.confirm_document(
                    doc.id,
                    allow_partial=allow_partial,
                    confirmed_by=created_by,
                    commit=False
                )
            else:
                result = {"document_id": doc.id, "status": "DRAFT"}
            if commit:
                db.session.commit()
            else:
                db.session.flush()
        except Exception as e:
            db.session.rollback() if commit else None
            if isinstance(e, InventoryError):
                raise
            raise InventoryError(f"出库单创建失败: {e}")
        return {
            "document_id": doc.id, "document_no": doc.document_no,
            "document_type": doc.document_type, "status": doc.status,
            "document_time": doc.document_time.isoformat(),
            "warehouse_id": warehouse_id, "outbound_type": outbound_type,
            "line_count": len(lines), "lines": result,
        }

    @staticmethod
    def confirm_document(
        document_id: int,
        allow_partial: bool = False,
        confirmed_by: int = None,
        commit: bool = True
    ) -> dict:
        doc = db.session.query(InventoryDocument).filter_by(id=document_id).with_for_update().first()
        if not doc:
            raise InventoryError("单据不存在")
        if doc.document_type not in (
            "PRODUCTION_OUT", "STOCKTAKE", "RETURN", "ADJUSTMENT_RED", "ADJUSTMENT_BLUE"
        ):
            raise InventoryError("非出库类单据不可确认")
        if doc.status != "DRAFT":
            raise InventoryError(f"单据状态不为草稿，当前: {doc.status}")
        lines = (InventoryDocumentLine.query.filter_by(document_id=doc.id)
                 .order_by(InventoryDocumentLine.line_no.asc()).all())
        if not lines:
            raise InventoryError("单据没有明细，不能确认")
        if InventoryTransaction.query.filter_by(document_id=doc.id).first():
            raise InventoryError("该单据已生成流水，禁止重复确认")
        grouped = {}
        for line in lines:
            if line.quantity <= 0 or line.transaction_type not in (
                "PRODUCTION_OUT", "STOCKTAKE_LOSS", "RETURN_OUT",
                "ADJUSTMENT_RED", "ADJUSTMENT_BLUE"
            ):
                raise InventoryError("出库明细数量或业务类型不合法")
            grouped.setdefault(line.warehouse_id, []).append({
                "material_id": line.material_id, "quantity": line.quantity
            })
        if not allow_partial:
            for wh_id, group in grouped.items():
                StockOutService._precheck_available(wh_id, group)

        now = now_beijing_naive()
        lines_result = []
        for line in lines:
            qty = _q6(line.quantity)
            tx = InventoryTransaction(
                document_id=doc.id, line_no=line.line_no,
                material_id=line.material_id,
                transaction_type=line.transaction_type,
                transaction_time=now,
                quantity_delta=-qty, unit_cost=None, total_cost=None,
                supplier_id=line.supplier_id, process_order_id=line.process_order_id,
                warehouse_id=line.warehouse_id, created_at=now,
            )
            db.session.add(tx)
            db.session.flush()
            result = StockOutService._consume_fifo(
                warehouse_id=line.warehouse_id, material_id=line.material_id,
                quantity=qty, out_transaction_id=tx.id,
                allow_partial=allow_partial,
            )
            lines_result.append(result)

            if result["consumed"] > 0:
                tx.total_cost = result["total_cost"]
                tx.unit_cost = _q6_amount(result["total_cost"] / result["consumed"])

        doc.status = "CONFIRMED"
        doc.document_time = now
        doc.confirmed_at = now
        doc.confirmed_by = confirmed_by

        if commit:
            try:
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                raise InventoryError(f"确认出库失败: {e}")
        else:
            db.session.flush()
        return {"document_id": doc.id, "status": doc.status, "lines_result":lines_result}

    @staticmethod
    def update_draft(
        document_id: int,
        warehouse_id: int,
        lines: list,
        outbound_type: str = "PRODUCTION_OUT",
        process_order_id: int = None,
        document_time: datetime = None,
        remark: str = None
    ) -> dict:
        doc = db.session.query(InventoryDocument).filter_by(id=document_id).with_for_update().first()
        if not doc or doc.status != "DRAFT":
            raise InventoryError("单据不存在或不是草稿")
        if outbound_type not in OUTBOUND_TYPE_LABELS:
            raise InventoryError(f"不支持的出库类型: {outbound_type}")
        wh = db.session.get(Warehouse, warehouse_id)
        if not wh or wh.status != 1 or wh.warehouse_type not in ("raw_material", "general"):
            raise InventoryError("原料出库必须使用有效的原料仓或普通仓")
        if not lines:
            raise InventoryError("出库明细不能为空")
        mids = [int(x["material_id"]) for x in lines]
        materials = {m.id: m for m in Material.query.filter(Material.id.in_(mids)).all()}
        for line in lines:
            mid = int(line["material_id"])
            if mid not in materials or materials[mid].status != 1 or _q6(line["quantity"]) <= 0:
                raise InventoryError(f"原料不存在、已停用或数量不合法: {mid}")
        InventoryDocumentLine.query.filter_by(document_id=doc.id).delete(synchronize_session=False)
        doc.document_type = doc_type_map[outbound_type]
        doc.document_time = document_time or doc.document_time
        doc.remark = remark
        for idx, line in enumerate(lines, 1):
            _save_document_line(
                doc.id, int(line.get("line_no", idx)), int(line["material_id"]),
                _q6(line["quantity"]), tx_type_map[outbound_type], warehouse_id,
                process_order_id=process_order_id, remark=line.get("remark"),
            )
        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            raise InventoryError(f"编辑出库草稿失败: {e}")
        return {"document_id": doc.id, "document_no": doc.document_no, "status": doc.status}

    @staticmethod
    def reverse_by_process_order(
        order,
        reason: str = None,
        operator_id: int = None,
        commit: bool = True,
    ) -> dict | None:
        """
        冲销某工艺单的全部原料出库消耗（用于取消工艺单）。
        口径：按正向消耗流水上【现存】的 InventoryCostAllocation 汇总需归还量，
        而不是按流水 quantity_delta 累计——已红冲的部分已被 ADJUSTMENT_RED 抵消，
        其 allocation 也已在 _reverse_allocation 中删除/减少，不应再次计入。
        返回：{document_id, document_no, reversed_qty}；无可冲销量返回 None。
        """
        wh_filter = []
        if getattr(order, "material_warehouse_id", None):
            wh_filter.append(
                InventoryTransaction.warehouse_id == order.material_warehouse_id
            )

        # 该工艺单全部正向消耗流水（含调增补出），仅用于定位 allocation
        consume_txs = (
            InventoryTransaction.query
            .filter(
                InventoryTransaction.process_order_id == order.id,
                InventoryTransaction.quantity_delta < 0,
                InventoryTransaction.transaction_type.in_(
                    ["PRODUCTION_OUT", "ADJUSTMENT_BLUE"]
                ),
                *wh_filter,
            )
            .order_by(
                InventoryTransaction.transaction_time.desc(),
                InventoryTransaction.id.desc(),
            )
            .with_for_update()
            .all()
        )
        if not consume_txs:
            return None

        tx_ids = [tx.id for tx in consume_txs]

        # ⭐ 按 (material, warehouse) 汇总现存 allocation（quantity > 0）作为需归还量
        alloc_rows = (
            db.session.query(
                InventoryTransaction.material_id.label("mid"),
                InventoryTransaction.warehouse_id.label("wh_id"),
                func.coalesce(
                    func.sum(InventoryCostAllocation.quantity), 0
                ).label("need_qty"),
            )
            .join(
                InventoryTransaction,
                InventoryTransaction.id
                == InventoryCostAllocation.out_transaction_id,
            )
            .filter(
                InventoryCostAllocation.out_transaction_id.in_(tx_ids),
                InventoryCostAllocation.quantity > 0,
            )
            .group_by(
                InventoryTransaction.material_id,
                InventoryTransaction.warehouse_id,
            )
            .all()
        )

        need_map = {
            (int(r.mid), int(r.wh_id)): _q6(r.need_qty or 0)
            for r in alloc_rows
            if (r.need_qty or 0) > 0
        }
        if not need_map:
            return None

        doc_no = generate_document_no_new("ADJUSTMENT_RED")
        doc_time = now_beijing_naive()
        doc = InventoryDocument(
            document_no=doc_no,
            document_type="ADJUSTMENT_RED",
            document_time=doc_time,
            status="CONFIRMED",
            remark=(
                f"[PROCESS_ORDER_CANCEL] 取消工艺单 {order.process_order_no} "
                f"冲销原料出库" + (f" · {reason}" if reason else "")
            ),
            created_at=doc_time,
            confirmed_at=doc_time,
            created_by=operator_id,
            confirmed_by=operator_id,
        )
        db.session.add(doc)
        db.session.flush()

        next_line_no = 1
        reversed_qty = Decimal("0")

        for (mid, wh_id), need in need_map.items():
            # 该 (material, warehouse) 下的源流水，从新到旧归还
            src_txs = (
                InventoryTransaction.query
                .filter(
                    InventoryTransaction.id.in_(tx_ids),
                    InventoryTransaction.material_id == mid,
                    InventoryTransaction.warehouse_id == wh_id,
                    InventoryTransaction.quantity_delta < 0,
                    InventoryTransaction.transaction_type.in_(
                        ["PRODUCTION_OUT", "ADJUSTMENT_BLUE"]
                    ),
                )
                .order_by(
                    InventoryTransaction.transaction_time.desc(),
                    InventoryTransaction.id.desc(),
                )
                .with_for_update()
                .all()
            )

            new_tx = InventoryTransaction(
                document_id=doc.id,
                line_no=next_line_no,
                material_id=mid,
                transaction_type="ADJUSTMENT_RED",
                transaction_time=doc_time,
                quantity_delta=need,
                unit_cost=None,
                total_cost=None,
                process_order_id=order.id,
                warehouse_id=wh_id,
                created_at=doc_time,
            )
            next_line_no += 1
            db.session.add(new_tx)
            db.session.flush()
            _save_document_line(
                doc.id, new_tx.line_no, mid, need, "ADJUSTMENT_RED",
                wh_id, process_order_id=order.id,
                unit_cost=None, remark="工艺单取消红冲",
            )

            remain = need
            recovered = Decimal("0")

            for src in src_txs:
                if remain <= 0:
                    break
                allocs = (
                    InventoryCostAllocation.query
                    .filter_by(out_transaction_id=src.id)
                    .order_by(InventoryCostAllocation.id.desc())
                    .with_for_update()
                    .all()
                )
                for alloc in allocs:
                    if remain <= 0:
                        break
                    if alloc.quantity <= 0:
                        continue
                    layer = db.session.get(InventoryCostLayer, alloc.cost_layer_id)
                    if not layer:
                        continue
                    give = min(alloc.quantity, remain)
                    if give <= 0:
                        continue

                    cost = _q6_amount(give * alloc.unit_cost)
                    layer.remaining_qty = layer.remaining_qty + give

                    existing = InventoryCostAllocation.query.filter_by(
                        out_transaction_id=new_tx.id,
                        cost_layer_id=layer.id,
                    ).first()
                    if existing:
                        existing.quantity = existing.quantity - give
                        existing.cost_amount = _q6_amount(
                            existing.cost_amount - cost
                        )
                    else:
                        db.session.add(InventoryCostAllocation(
                            out_transaction_id=new_tx.id,
                            cost_layer_id=layer.id,
                            quantity=-give,
                            unit_cost=alloc.unit_cost,
                            cost_amount=-cost,
                            created_at=doc_time,
                        ))

                    alloc.quantity -= give
                    alloc.cost_amount = _q6_amount(alloc.quantity * alloc.unit_cost)
                    if alloc.quantity <= 0:
                        db.session.delete(alloc)

                    remain -= give
                    recovered += cost

            if remain > 0:
                # 理论上不会发生：need 由现存 allocation 汇总而来
                raise InventoryError(
                    f"原料 {mid} 在仓库 {wh_id} 可红冲数量不足，还差 {remain}"
                )

            new_tx.total_cost = -_q6_amount(recovered)
            new_tx.unit_cost = (
                _q6_amount(recovered / need) if need > 0 else None
            )
            reversed_qty += need

        if commit:
            try:
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                raise InventoryError(f"冲销原料出库失败: {e}")
        else:
            db.session.flush()

        return {
            "document_id": doc.id,
            "document_no": doc.document_no,
            "reversed_qty": str(reversed_qty),
        }