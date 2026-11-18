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
    "IN_REVERSE":    "入库撤销",   # ⭐ delta 负
}

OUTBOUND_TYPE_LABELS = {
    "PRODUCTION_OUT":  "生产领料",
    "STOCKTAKE_LOSS":  "盘亏出库",
    "RETURN_OUT":      "退货出库",
    "ADJUSTMENT_RED":  "配比红冲出库", # ⭐ delta 正
    "ADJUSTMENT_BLUE": "配比蓝补出库",
    "OUT_REVERSE":     "出库撤销",   # ⭐ delta 正
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
    "OUT_REVERSE": "REVERSE",     # ⭐ 新增
    "IN_REVERSE": "REVERSE",   # ⭐ 新增
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
    "IN_REVERSE":       "IN_REVERSE",
    "OUT_REVERSE":      "OUT_REVERSE",
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


class FifoAllocationReversalService:
    """
    FIFO 出库分配的统一回补器。

    所有“退回原料成本层”的业务都必须经过这里：
    - 普通出库单撤销
    - 工艺单取消
    - 工艺单配比减少（红冲）

    重要约束：
    1. 只回补源出库流水上仍为正数的 allocation。历史红冲已扣减的数量不会重复回补。
    2. 按调用方提供的源流水顺序、以及每条流水 allocation.id 倒序（LIFO）回补。
    3. 直接恢复原 cost_layer，不重新执行 FIFO。
    4. 源 allocation 减少，反向流水写负 allocation，保证后续多次配比调整可计算“净未撤销消耗量”。
    """

    @staticmethod
    def reverse_allocations(
        source_transactions,
        reverse_transaction,
        quantity=None,
        now=None,
        error_cls=InventoryError,
    ) -> dict:
        if not reverse_transaction or not reverse_transaction.id:
            raise error_cls("回补成本层前必须先创建并 flush 反向库存流水")

        now = now or now_beijing_naive()
        remaining = None if quantity is None else _q6(quantity)
        if remaining is not None and remaining <= 0:
            raise error_cls("成本层回补数量必须大于 0")

        restored_qty = Decimal("0")
        restored_cost = Decimal("0")

        for source_tx in source_transactions:
            if remaining is not None and remaining <= 0:
                break

            allocs = (
                InventoryCostAllocation.query
                .filter(
                    InventoryCostAllocation.out_transaction_id == source_tx.id,
                    InventoryCostAllocation.quantity > 0,
                )
                .order_by(InventoryCostAllocation.id.desc())
                .with_for_update()
                .all()
            )

            for alloc in allocs:
                if remaining is not None and remaining <= 0:
                    break

                available = _q6(alloc.quantity)
                if available <= 0:
                    continue
                give = available if remaining is None else min(available, remaining)
                if give <= 0:
                    continue

                layer = (
                    db.session.query(InventoryCostLayer)
                    .filter_by(id=alloc.cost_layer_id)
                    .with_for_update()
                    .first()
                )
                if not layer:
                    raise error_cls(
                        f"源出库流水 #{source_tx.id} 的 FIFO 成本层 "
                        f"#{alloc.cost_layer_id} 不存在，无法回补"
                    )

                new_remaining = _q6(layer.remaining_qty) + give
                if new_remaining > _q6(layer.original_qty):
                    raise error_cls(
                        f"成本层 #{layer.id} 回补后数量将超过初始数量，"
                        "可能存在重复回补或库存成本数据不一致"
                    )

                unit_cost = _q6_amount(alloc.unit_cost)
                cost = _q6_amount(give * unit_cost)

                # 恢复原始成本层，而不是重新按 FIFO 选择成本层。
                layer.remaining_qty = new_remaining

                # 源 allocation 只保留尚未被红冲/撤销的净消耗量。
                new_source_qty = _q6(alloc.quantity) - give
                alloc.quantity = new_source_qty
                alloc.cost_amount = _q6_amount(new_source_qty * unit_cost)
                if new_source_qty <= 0:
                    db.session.delete(alloc)

                # 反向流水使用负 allocation，建立“回补到哪个成本层”的审计关系。
                reverse_alloc = (
                    InventoryCostAllocation.query
                    .filter_by(
                        out_transaction_id=reverse_transaction.id,
                        cost_layer_id=layer.id,
                    )
                    .with_for_update()
                    .first()
                )
                if reverse_alloc:
                    reverse_alloc.quantity = _q6(reverse_alloc.quantity) - give
                    reverse_alloc.cost_amount = _q6_amount(
                        reverse_alloc.cost_amount - cost
                    )
                else:
                    db.session.add(InventoryCostAllocation(
                        out_transaction_id=reverse_transaction.id,
                        cost_layer_id=layer.id,
                        quantity=-give,
                        unit_cost=unit_cost,
                        cost_amount=-cost,
                        created_at=now,
                    ))

                restored_qty += give
                restored_cost += cost
                if remaining is not None:
                    remaining -= give

        if remaining is not None and remaining > 0:
            raise error_cls(f"原出库可回补数量不足，还差 {remaining}")

        return {
            "quantity": _q6(restored_qty),
            "total_cost": _q6_amount(restored_cost),
            "unit_cost": (
                _q6_amount(restored_cost / restored_qty)
                if restored_qty > 0 else None
            ),
        }



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
        """兼容旧调用：按单一仓库预检库存。"""
        normalized = [dict(line, warehouse_id=int(line.get("warehouse_id") or warehouse_id)) for line in lines]
        return StockOutService._precheck_available_multi(normalized, allow_partial=allow_partial)

    @staticmethod
    def _precheck_available_multi(lines, allow_partial=False):
        """按每行 (warehouse_id, material_id) 汇总需求，整单校验，不允许超库存。"""
        if allow_partial:
            return
        need_map = {}
        for line in lines:
            mid = int(line["material_id"])
            wh_id = int(line["warehouse_id"])
            qty = _q6(line["quantity"])
            if qty <= 0:
                continue
            key = (wh_id, mid)
            need_map[key] = need_map.get(key, Decimal("0")) + qty
        if not need_map:
            return

        mids = {mid for _, mid in need_map}
        materials = {m.id: m for m in Material.query.filter(Material.id.in_(mids)).all()}
        wh_ids = {wh_id for wh_id, _ in need_map}
        avail_rows = (
            db.session.query(
                InventoryCostLayer.warehouse_id.label("wh_id"),
                InventoryCostLayer.material_id.label("mid"),
                func.coalesce(func.sum(InventoryCostLayer.remaining_qty), 0).label("available"),
            )
            .filter(
                InventoryCostLayer.warehouse_id.in_(wh_ids),
                InventoryCostLayer.material_id.in_(mids),
                InventoryCostLayer.remaining_qty > 0,
            )
            .group_by(InventoryCostLayer.warehouse_id, InventoryCostLayer.material_id)
            .all()
        )
        available_map = {
            (int(r.wh_id), int(r.mid)): _q6(r.available or 0)
            for r in avail_rows
        }
        warehouses = {w.id: w for w in Warehouse.query.filter(Warehouse.id.in_(wh_ids)).all()}
        shortages = []
        for (wh_id, mid), need in sorted(need_map.items()):
            available = available_map.get((wh_id, mid), Decimal("0"))
            if need > available:
                material = materials.get(mid)
                warehouse = warehouses.get(wh_id)
                material_label = (
                    f"{material.material_code} - {material.material_name}"
                    if material else f"原料#{mid}"
                )
                warehouse_label = (
                    f"{warehouse.warehouse_code} - {warehouse.warehouse_name}"
                    if warehouse else f"仓库#{wh_id}"
                )
                shortage = _q6(need - available)
                shortages.append(
                    f"{warehouse_label} / {material_label}：需要 {need}，可用 {available}，不足 {shortage}"
                )
        if shortages:
            raise InventoryError("以下库存不足，无法出库：\n" + "\n".join(shortages))

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
        warehouse_id: int = None,  # 兼容旧调用；新调用必须在每行提供 warehouse_id
        document_no: str = None,
        document_time: datetime = None,
        process_order_id: int = None,
        created_by: int = None,
        remark: str = None,
        auto_confirm: bool = True,
        allow_partial: bool = False,
        commit: bool = True,
    ) -> dict:
        """创建原料出库单；每行独立指定仓库，确认时按行仓库消耗 FIFO。"""
        if not lines:
            raise InventoryError("出库明细不能为空")
        if outbound_type not in OUTBOUND_TYPE_LABELS:
            raise InventoryError(f"不支持的出库类型: {outbound_type}")

        normalized_lines = []
        for idx, source in enumerate(lines, 1):
            line = dict(source)
            line_wh_id = line.get("warehouse_id") or warehouse_id
            if not line_wh_id:
                raise InventoryError(f"第 {idx} 行未指定仓库")
            try:
                line["warehouse_id"] = int(line_wh_id)
                line["material_id"] = int(line["material_id"])
                line["quantity"] = _q6(line["quantity"])
            except (KeyError, TypeError, ValueError):
                raise InventoryError(f"第 {idx} 行出库明细格式错误")
            if line["quantity"] <= 0:
                raise InventoryError(f"第 {idx} 行出库数量必须大于 0")
            normalized_lines.append(line)

        # 同一仓库、同一原料只能有一行，避免重复行绕过前端校验。
        keys = [(x["warehouse_id"], x["material_id"]) for x in normalized_lines]
        if len(keys) != len(set(keys)):
            raise InventoryError("同一仓库的同一种原料只能出现一次")

        warehouse_ids = {x["warehouse_id"] for x in normalized_lines}
        warehouses = {w.id: w for w in Warehouse.query.filter(Warehouse.id.in_(warehouse_ids)).all()}
        for wh_id in warehouse_ids:
            wh = warehouses.get(wh_id)
            if not wh or wh.status != 1 or wh.warehouse_type not in ("raw_material", "general"):
                raise InventoryError(f"仓库无效或不是原料仓/普通仓：{wh_id}")

        mids = {x["material_id"] for x in normalized_lines}
        materials = {m.id: m for m in Material.query.filter(Material.id.in_(mids)).all()}
        for line in normalized_lines:
            material = materials.get(line["material_id"])
            if not material or material.status != 1:
                raise InventoryError(f"原料不存在或已停用: {line['material_id']}")

        # 草稿保存也检查当前可用库存；确认时 confirm_document 会在锁定成本层后再次检查。
        StockOutService._precheck_available_multi(normalized_lines, allow_partial=allow_partial)

        doc_time = document_time or now_beijing_naive()
        doc_no = document_no or generate_document_no(prefix_map[outbound_type])
        if InventoryDocument.query.filter_by(document_no=doc_no).first():
            raise InventoryError(f"单据号重复: {doc_no}")

        doc = InventoryDocument(
            document_no=doc_no, document_type=doc_type_map[outbound_type],
            document_time=doc_time, status="DRAFT", remark=remark,
            created_by=created_by, created_at=doc_time,
            confirmed_by=created_by if auto_confirm else None,
            confirmed_at=now_beijing_naive() if auto_confirm else None,
        )
        db.session.add(doc)
        db.session.flush()

        for idx, line in enumerate(normalized_lines, 1):
            _save_document_line(
                doc.id, int(line.get("line_no", idx)), line["material_id"],
                line["quantity"], tx_type_map[outbound_type], line["warehouse_id"],
                process_order_id=process_order_id, remark=line.get("remark"),
            )

        try:
            if auto_confirm:
                result = StockOutService.confirm_document(
                    doc.id, allow_partial=allow_partial,
                    confirmed_by=created_by, commit=False,
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
            "warehouse_id": None if len(warehouse_ids) > 1 else next(iter(warehouse_ids)),
            "outbound_type": outbound_type,
            "line_count": len(normalized_lines), "lines": result,
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
        # 这里不需要判断撤销单，因为生产工艺单可能调整过配比，如果取消工艺单触发的撤销，走ADJUSTMENT_RED
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
        lines: list,
        warehouse_id: int = None,  # 兼容旧调用
        outbound_type: str = "PRODUCTION_OUT",
        process_order_id: int = None,
        document_time: datetime = None,
        remark: str = None,
    ) -> dict:
        doc = db.session.query(InventoryDocument).filter_by(id=document_id).with_for_update().first()
        if not doc or doc.status != "DRAFT":
            raise InventoryError("单据不存在或不是草稿")
        if outbound_type not in OUTBOUND_TYPE_LABELS:
            raise InventoryError(f"不支持的出库类型: {outbound_type}")
        if not lines:
            raise InventoryError("出库明细不能为空")

        normalized_lines = []
        for idx, source in enumerate(lines, 1):
            line = dict(source)
            line_wh_id = line.get("warehouse_id") or warehouse_id
            if not line_wh_id:
                raise InventoryError(f"第 {idx} 行未指定仓库")
            try:
                line["warehouse_id"] = int(line_wh_id)
                line["material_id"] = int(line["material_id"])
                line["quantity"] = _q6(line["quantity"])
            except (KeyError, TypeError, ValueError):
                raise InventoryError(f"第 {idx} 行出库明细格式错误")
            if line["quantity"] <= 0:
                raise InventoryError(f"第 {idx} 行出库数量必须大于 0")
            normalized_lines.append(line)

        keys = [(x["warehouse_id"], x["material_id"]) for x in normalized_lines]
        if len(keys) != len(set(keys)):
            raise InventoryError("同一仓库的同一种原料只能出现一次")
        warehouse_ids = {x["warehouse_id"] for x in normalized_lines}
        warehouses = {w.id: w for w in Warehouse.query.filter(Warehouse.id.in_(warehouse_ids)).all()}
        for wh_id in warehouse_ids:
            wh = warehouses.get(wh_id)
            if not wh or wh.status != 1 or wh.warehouse_type not in ("raw_material", "general"):
                raise InventoryError(f"仓库无效或不是原料仓/普通仓：{wh_id}")
        mids = {x["material_id"] for x in normalized_lines}
        materials = {m.id: m for m in Material.query.filter(Material.id.in_(mids)).all()}
        for line in normalized_lines:
            material = materials.get(line["material_id"])
            if not material or material.status != 1:
                raise InventoryError(f"原料不存在或已停用: {line['material_id']}")

        # 编辑草稿时重新读取实时库存并校验，不依赖上次打开页面时的库存快照。
        StockOutService._precheck_available_multi(normalized_lines, allow_partial=False)

        InventoryDocumentLine.query.filter_by(document_id=doc.id).delete(synchronize_session=False)
        doc.document_type = doc_type_map[outbound_type]
        doc.document_time = document_time or doc.document_time
        doc.remark = remark
        for idx, line in enumerate(normalized_lines, 1):
            _save_document_line(
                doc.id, int(line.get("line_no", idx)), line["material_id"],
                line["quantity"], tx_type_map[outbound_type], line["warehouse_id"],
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
        口径：按正向消耗流水上【现存正数】的 InventoryCostAllocation 汇总需归还量，
        而不是按 quantity_delta 累计。配比调整可能多次红冲/蓝补，源 allocation 已被逐次扣减；
        这里只冲销仍未被历史红冲抵消的净消耗量，避免重复回补成本层。
        无任何正向出库流水返回 None；有历史流水但 allocation 已全部红冲时返回
        already_reversed=True（不生成零数量单据），让上层可安全完成工艺单取消。
        返回：{document_id, document_no, reversed_qty, already_reversed?}。
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
        print("reverse_by_process_order, 定位allocation的tx_ids:", tx_ids)

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
            # 有历史正向出库流水，但所有有效 allocation 已被配比调整红冲。
            # 这不是“找不到原领料流水”，取消工艺单时无需再生成一张零数量红冲单。
            # 返回非 None 结果，允许上层继续取消工艺单。
            return {
                "document_id": None,
                "document_no": None,
                "reversed_qty": "0",
                "already_reversed": True,
                "source_transaction_count": len(consume_txs),
            }

        doc_no = generate_document_no_new("ADJUSTMENT_RED")
        doc_time = now_beijing_naive()
        doc = InventoryDocument(
            document_no=doc_no,
            document_type="ADJUSTMENT_RED",
            document_time=doc_time,
            status="CONFIRMED",
            remark=(
                f"[PROCESS_ORDER_CANCEL] {order.process_order_no} 取消工艺单"
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
        print("reverse_by_process_order, need_map:", need_map)

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

            result = FifoAllocationReversalService.reverse_allocations(
                source_transactions=src_txs,
                reverse_transaction=new_tx,
                quantity=need,
                now=doc_time,
                error_cls=InventoryError,
            )
            recovered = result["total_cost"]
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

class InventoryDocumentReverseService:
    """原料 FIFO 单据撤销：通过反向流水保留审计轨迹，不物理删除原单。"""

    INBOUND_LINE_TYPES = {"PURCHASE_IN", "STOCKTAKE_GAIN", "RETURN_IN"}
    OUTBOUND_LINE_TYPES = {"PRODUCTION_OUT", "STOCKTAKE_LOSS", "RETURN_OUT"}

    @staticmethod
    def _load_original(document_id: int, lock: bool = False):
        q = db.session.query(InventoryDocument).filter_by(id=document_id)
        if lock:
            q = q.with_for_update()
        doc = q.first()
        if not doc:
            raise InventoryError("原料库存单据不存在")
        return doc

    @staticmethod
    def _validate_reverseable(doc, lock: bool = False):
        if doc.status != "CONFIRMED":
            raise InventoryError("只有已确认的原料库存单据才允许撤销")
        if doc.document_type == "REVERSE":
            raise InventoryError("撤销单不能再次撤销")
        remark_text = (doc.remark or "")
        if (
            doc.document_type in ("ADJUSTMENT_RED", "ADJUSTMENT_BLUE")
            or "[MATERIAL_ADJUST_RED]" in remark_text
            or "[MATERIAL_ADJUST_BLUE]" in remark_text
            or "[PROCESS_ORDER_CANCEL]" in remark_text
        ):
            raise InventoryError("工艺单配比红冲/蓝补单及工艺单取消产生的红冲单不允许撤销")
        if "[PROCESS_ORDER_PLACE]" in remark_text:
            raise InventoryError("工艺单领料下单不允许单独撤销, 请走工艺单取消触发")
        if doc.document_type not in ("PURCHASE_IN", "STOCKTAKE", "RETURN", "PRODUCTION_OUT"):
            raise InventoryError("该类型原料单据不支持撤销")

        if getattr(doc, "related_document_id", None):
            raise InventoryError("该单据本身是撤销单，不能再次撤销")

        existing = InventoryDocument.query.filter_by(
            related_document_id=doc.id
        ).first()
        if existing:
            raise InventoryError(
                f"该单据已撤销，撤销单号：{existing.document_no}"
            )

        tx_q = InventoryTransaction.query.filter_by(document_id=doc.id)
        if lock:
            tx_q = tx_q.with_for_update()
        txs = tx_q.order_by(InventoryTransaction.line_no.asc()).all()
        if not txs:
            raise InventoryError("该单据没有已生效的库存流水，不能撤销")

        types = {tx.transaction_type for tx in txs}
        if types & {"ADJUSTMENT_RED", "ADJUSTMENT_BLUE"}:
            raise InventoryError("工艺单配比红冲/蓝补流水不允许撤销")

        if types.issubset(InventoryDocumentReverseService.INBOUND_LINE_TYPES):
            direction = "IN"
            for tx in txs:
                layer_q = InventoryCostLayer.query.filter_by(
                    source_transaction_id=tx.id
                )
                if lock:
                    layer_q = layer_q.with_for_update()
                layer = layer_q.first()
                if not layer:
                    raise InventoryError(
                        f"入库流水 #{tx.id} 未找到对应 FIFO 成本层，不能撤销"
                    )
                original_qty = _q6(layer.original_qty)
                remaining_qty = _q6(layer.remaining_qty)
                if remaining_qty != original_qty:
                    consumed = original_qty - remaining_qty
                    raise InventoryError(
                        f"入库单已生成的成本层 #{layer.id} 已被消耗（初始 {original_qty}，"
                        f"剩余 {remaining_qty}，已消耗 {consumed}），不允许撤销入库单"
                    )
            return direction, txs

        if types.issubset(InventoryDocumentReverseService.OUTBOUND_LINE_TYPES):
            direction = "OUT"
            for tx in txs:
                alloc_q = InventoryCostAllocation.query.filter_by(
                    out_transaction_id=tx.id
                )
                if lock:
                    alloc_q = alloc_q.with_for_update()
                allocs = alloc_q.all()
                if not any(_q6(a.quantity) > 0 for a in allocs):
                    raise InventoryError(
                        f"出库流水 #{tx.id} 没有可回补的 FIFO 成本分配，不能撤销"
                    )
            return direction, txs

        raise InventoryError(
            "该单据包含不支持撤销的业务类型；工艺单调整红冲/蓝补及上游冲销单不可撤销"
        )

    @staticmethod
    def can_reverse(document_id: int):
        try:
            doc = InventoryDocumentReverseService._load_original(document_id)
            direction, _ = InventoryDocumentReverseService._validate_reverseable(
                doc, lock=False
            )
            return True, None, direction
        except InventoryError as exc:
            return False, str(exc), None

    @staticmethod
    def reverse_document(
        document_id: int,
        reason: str = None,
        operator_id: int = None,
        commit: bool = True,
    ) -> dict:
        """整单撤销。出库按原 allocation 回补原成本层；入库要求成本层完全未消耗。"""
        try:
            doc = InventoryDocumentReverseService._load_original(
                document_id, lock=True
            )
            direction, txs = InventoryDocumentReverseService._validate_reverseable(
                doc, lock=True
            )

            now = now_beijing_naive()
            if direction == "IN":
                reverse_no=f"INREV-{doc.document_no}"
            else:
                reverse_no=f"OUTREV-{doc.document_no}"
            #reverse_no = generate_document_no("IR")
            reverse_doc = InventoryDocument(
                document_no=reverse_no,
                document_type="REVERSE",
                related_document_id=doc.id,
                document_time=now,
                status="CONFIRMED",
                remark=(
                    f"[INVENTORY_REVERSE] {doc.document_no} 撤销原单"
                    + (f"；原因：{reason.strip()}" if reason and reason.strip() else "")
                ),
                created_by=operator_id,
                confirmed_by=operator_id,
                created_at=now,
                confirmed_at=now,
            )
            db.session.add(reverse_doc)
            db.session.flush()

            total_reversed_qty = Decimal("0")
            total_reversed_cost = Decimal("0")
            next_line_no = 1

            if direction == "IN":
                # 撤销原入库：成本层必须完全未被消耗；将剩余量归零，保留层及原流水。
                for source_tx in txs:
                    layer = (
                        InventoryCostLayer.query
                        .filter_by(source_transaction_id=source_tx.id)
                        .with_for_update()
                        .first()
                    )
                    if not layer:
                        raise InventoryError(
                            f"入库流水 #{source_tx.id} 对应成本层不存在，不能撤销"
                        )
                    if _q6(layer.remaining_qty) != _q6(layer.original_qty):
                        raise InventoryError(
                            f"成本层 #{layer.id} 已被消耗，不能撤销入库单"
                        )

                    qty = _q6(source_tx.quantity_delta)
                    unit_cost = _q6_amount(layer.unit_cost)
                    cost = _q6_amount(qty * unit_cost)

                    reverse_tx = InventoryTransaction(
                        document_id=reverse_doc.id,
                        line_no=next_line_no,
                        material_id=source_tx.material_id,
                        transaction_type="IN_REVERSE",
                        transaction_time=now,
                        quantity_delta=-qty,
                        unit_cost=unit_cost,
                        total_cost=-cost,
                        supplier_id=source_tx.supplier_id,
                        process_order_id=source_tx.process_order_id,
                        warehouse_id=source_tx.warehouse_id,
                        created_at=now,
                    )
                    db.session.add(reverse_tx)
                    db.session.flush()
                    _save_document_line(
                        reverse_doc.id, next_line_no, source_tx.material_id,
                        qty, "IN_REVERSE", source_tx.warehouse_id,
                        supplier_id=source_tx.supplier_id,
                        process_order_id=source_tx.process_order_id,
                        unit_cost=unit_cost,
                        remark=f"撤销入库流水 #{source_tx.id}",
                    )
                    next_line_no += 1

                    # 不删除成本层/源流水，避免破坏历史关联；剩余量归零表示该层已被撤销。
                    layer.remaining_qty = Decimal("0")
                    total_reversed_qty += qty
                    total_reversed_cost += cost

            else:
                # 撤销原出库：只回补该原单尚未被历史红冲抵消的 allocation。
                # 工艺单领料单本身会被上层规则禁止单独撤销；这里处理普通出库单。
                for source_tx in txs:
                    positive_qty = (
                        db.session.query(
                            func.coalesce(func.sum(InventoryCostAllocation.quantity), 0)
                        )
                        .filter(
                            InventoryCostAllocation.out_transaction_id == source_tx.id,
                            InventoryCostAllocation.quantity > 0,
                        )
                        .scalar()
                    ) or Decimal("0")
                    qty = _q6(positive_qty)
                    if qty <= 0:
                        raise InventoryError(
                            f"出库流水 #{source_tx.id} 没有可回补的成本层数量"
                        )

                    reverse_tx = InventoryTransaction(
                        document_id=reverse_doc.id,
                        line_no=next_line_no,
                        material_id=source_tx.material_id,
                        transaction_type="OUT_REVERSE",
                        transaction_time=now,
                        quantity_delta=qty,
                        unit_cost=None,
                        total_cost=None,
                        supplier_id=source_tx.supplier_id,
                        process_order_id=source_tx.process_order_id,
                        warehouse_id=source_tx.warehouse_id,
                        created_at=now,
                    )
                    db.session.add(reverse_tx)
                    db.session.flush()
                    _save_document_line(
                        reverse_doc.id, next_line_no, source_tx.material_id,
                        qty, "OUT_REVERSE", source_tx.warehouse_id,
                        supplier_id=source_tx.supplier_id,
                        process_order_id=source_tx.process_order_id,
                        remark=f"撤销出库流水 #{source_tx.id}，按原成本层回补",
                    )
                    next_line_no += 1

                    result = FifoAllocationReversalService.reverse_allocations(
                        source_transactions=[source_tx],
                        reverse_transaction=reverse_tx,
                        quantity=qty,
                        now=now,
                        error_cls=InventoryError,
                    )
                    reverse_tx.unit_cost = result["unit_cost"]
                    reverse_tx.total_cost = -result["total_cost"]
                    total_reversed_qty += result["quantity"]
                    total_reversed_cost += result["total_cost"]

            if commit:
                db.session.commit()
            else:
                db.session.flush()

            return {
                "document_id": reverse_doc.id,
                "document_no": reverse_doc.document_no,
                "original_document_id": doc.id,
                "original_document_no": doc.document_no,
                "direction": direction,
                "reversed_qty": str(total_reversed_qty),
                "reversed_cost": str(_q6_amount(total_reversed_cost)),
            }
        except Exception as exc:
            if commit:
                db.session.rollback()
            if isinstance(exc, InventoryError):
                raise
            raise InventoryError(f"撤销原料库存单据失败：{exc}")
