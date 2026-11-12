from decimal import Decimal
from datetime import datetime
from sqlalchemy import func, and_

from app.extensions import db
from app.generated.models.materials import Material
from app.generated.models.warehouses import Warehouse
from app.generated.models.suppliers import Supplier

from app.generated.models.inventory_documents import InventoryDocument
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

class PurchaseInService:
    """纯材料采购入库（FIFO 成本层）"""

    @staticmethod
    def create_purchase_in(
        warehouse_id: int,
        supplier_id: int,
        lines: list,
        inbound_type: str = "PURCHASE_IN",   # ⭐ 新增
        document_no: str = None,
        document_time: datetime = None,
        remark: str = None,
        auto_confirm: bool = True,
    ) -> dict:
        """
        lines: [
          {"material_id": 1, "quantity": 100, "unit_cost": 12.5, "line_no": 1},
          ...
        ]
        auto_confirm: True 则直接生成 CONFIRMED 单据并写入流水 + 成本层
        """
        if not lines:
            raise InventoryError("采购明细不能为空")

        if inbound_type not in INBOUND_TYPE_LABELS:
            raise InventoryError(f"不支持的入库类型: {inbound_type}")

        # 1. 校验仓库
        wh = db.session.get(Warehouse, warehouse_id)
        if not wh or wh.status != 1:
            raise InventoryError(f"仓库不存在或已停用: {warehouse_id}")
        if wh.warehouse_type not in ("raw_material", "general"):
            raise InventoryError("原料入库必须使用原料仓或普通仓")

        # 2. 校验供应商
        sup = db.session.get(Supplier, supplier_id)
        if not sup or sup.status != 1:
            raise InventoryError(f"供应商不存在或已停用: {supplier_id}")

        # 3. 校验明细
        material_ids = [int(l["material_id"]) for l in lines]
        materials = {
            m.id: m
            for m in Material.query.filter(Material.id.in_(material_ids)).all()
        }
        for l in lines:
            mid = int(l["material_id"])
            if mid not in materials:
                raise InventoryError(f"原料不存在: {mid}")
            if materials[mid].status != 1:
                raise InventoryError(f"原料已禁用: {materials[mid].material_code}")
            q = _q6(l["quantity"])
            if q <= 0:
                raise InventoryError(f"原料 {materials[mid].material_code} 数量必须 > 0")
            if l.get("unit_cost") is None:
                raise InventoryError(f"原料 {materials[mid].material_code} 缺少 unit_cost")
            uc = _q6_amount(l["unit_cost"])
            if uc < 0:
                raise InventoryError("单价不能为负数")

        # 4. 生成单据号
        doc_no = document_no or generate_document_no(prefix_map[inbound_type])
        if InventoryDocument.query.filter_by(document_no=doc_no).first():
            raise InventoryError(f"单据号重复: {doc_no}")

        doc_time = document_time or now_beijing_naive()
        doc_status = "CONFIRMED" if auto_confirm else "DRAFT"

        # 5. 创建单据
        document = InventoryDocument(
            document_no=doc_no,
            document_type=doc_type_map[inbound_type],   # ⭐
            document_time=doc_time,
            status=doc_status,
            remark=remark,
            created_at=doc_time,
            confirmed_at=doc_time if auto_confirm else None,
        )
        db.session.add(document)
        db.session.flush()  # 拿到 document.id

        # 6. 遍历明细，创建流水 + 成本层
        transactions = []
        layers = []
        for idx, l in enumerate(lines, start=1):
            mid = int(l["material_id"])
            qty = _q6(l["quantity"])
            unit_cost = _q6_amount(l["unit_cost"])
            total_cost = _q6_amount(qty * unit_cost)
            line_no = int(l.get("line_no", idx))

            tx = InventoryTransaction(
                document_id=document.id,
                line_no=line_no,
                material_id=mid,
                transaction_type=tx_type_map[inbound_type],  # ⭐
                transaction_time=doc_time,
                quantity_delta=qty,          # 入库正数
                unit_cost=unit_cost,
                total_cost=total_cost,
                supplier_id=supplier_id,
                warehouse_id=warehouse_id,
                created_at=doc_time,
            )
            db.session.add(tx)
            db.session.flush()  # 拿到 tx.id

            if auto_confirm:
                layer = InventoryCostLayer(
                    material_id=mid,
                    source_transaction_id=tx.id,
                    layer_type=layer_type_map[inbound_type],
                    effective_time=doc_time,
                    original_qty=qty,
                    remaining_qty=qty,
                    unit_cost=unit_cost,
                    original_cost=total_cost,
                    warehouse_id=warehouse_id,
                    created_at=doc_time,
                )
                db.session.add(layer)
                layers.append(layer)

            transactions.append(tx)

        # 7. 提交
        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            raise InventoryError(f"采购入库失败: {e}")

        return {
            "document_id": document.id,
            "document_no": document.document_no,
            "status": document.status,
            "document_time": document.document_time.isoformat(),
            "warehouse_id": warehouse_id,
            "supplier_id": supplier_id,
            "line_count": len(transactions),
            "total_cost": str(sum(t.total_cost for t in transactions)),
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
    def confirm_document(document_id: int) -> dict:
        """
        确认草稿采购单：将草稿落成流水 + 成本层
        （如调用 create_purchase_in 时 auto_confirm=False，则需调用此方法）
        """
        doc = db.session.get(InventoryDocument, document_id)
        if not doc:
            raise InventoryError("单据不存在")
        if doc.document_type not in ("PURCHASE_IN", "STOCKTAKE", "RETURN"):
            raise InventoryError("仅入库单可确认")
        if doc.status != "DRAFT":
            raise InventoryError(f"单据状态不为草稿，当前: {doc.status}")

        doc.status = "CONFIRMED"
        doc_time = now_beijing_naive()
        doc.confirmed_at = doc_time
        doc.document_time = doc_time

        txs = InventoryTransaction.query.filter_by(document_id=doc.id).all()
        for tx in txs:
            if tx.quantity_delta <= 0:
                raise InventoryError("采购流水数量必须为正")
            # 只处理入库类流水
            if tx.transaction_type not in ("PURCHASE_IN", "STOCKTAKE_GAIN", "RETURN_IN"):
                raise InventoryError(
                    f"单据 {doc.document_no} 中存在非入库流水: {tx.transaction_type}"
                )
            layer = InventoryCostLayer(
                material_id=tx.material_id,
                source_transaction_id=tx.id,
                layer_type=layer_type_map[tx.transaction_type],
                effective_time=doc_time,
                original_qty=tx.quantity_delta,
                remaining_qty=tx.quantity_delta,
                unit_cost=tx.unit_cost or Decimal("0"),
                original_cost=tx.total_cost or Decimal("0"),
                warehouse_id=tx.warehouse_id,
                created_at=doc_time,
            )
            db.session.add(layer)

        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            raise InventoryError(f"确认失败: {e}")

        return {"document_id": doc.id, "status": doc.status}

    @staticmethod
    def update_draft(
        document_id: int,
        warehouse_id: int,
        supplier_id: int,
        lines: list,
        inbound_type: str = "PURCHASE_IN",
        document_time: datetime = None,
        remark: str = None,
    ) -> dict:
        doc = db.session.get(InventoryDocument, document_id)
        if not doc:
            raise InventoryError("单据不存在")
        if doc.status != "DRAFT":
            raise InventoryError(f"只有草稿可编辑，当前: {doc.status}")
        if doc.document_type not in ("PURCHASE_IN", "STOCKTAKE", "RETURN"):
            raise InventoryError("非入库类单据不可编辑")
        if inbound_type not in INBOUND_TYPE_LABELS:
            raise InventoryError(f"不支持的入库类型: {inbound_type}")

        tx_ids = [t.id for t in InventoryTransaction.query.filter_by(document_id=doc.id).all()]
        if tx_ids and db.session.query(InventoryCostLayer.id).filter(
                InventoryCostLayer.source_transaction_id.in_(tx_ids)).first():
            raise InventoryError("该草稿已存在成本层，拒绝编辑")

        # 复用创建时的仓库/供应商/明细校验
        wh = db.session.get(Warehouse, warehouse_id)
        if not wh or wh.status != 1:
            raise InventoryError(f"仓库不存在或已停用: {warehouse_id}")
        if wh.warehouse_type not in ("raw_material", "general"):
            raise InventoryError("原料入库必须使用原料仓或普通仓")

        sup = db.session.get(Supplier, supplier_id)
        if not sup or sup.status != 1:
            raise InventoryError(f"供应商不存在或已停用: {supplier_id}")

        if not lines:
            raise InventoryError("采购明细不能为空")

        material_ids = [int(l["material_id"]) for l in lines]
        materials = {
            m.id: m for m in Material.query.filter(Material.id.in_(material_ids)).all()
        }
        for l in lines:
            mid = int(l["material_id"])
            if mid not in materials:
                raise InventoryError(f"原料不存在: {mid}")
            if materials[mid].status != 1:
                raise InventoryError(f"原料已禁用: {materials[mid].material_code}")
            if _q6(l["quantity"]) <= 0:
                raise InventoryError(f"原料 {materials[mid].material_code} 数量必须 > 0")
            if l.get("unit_cost") is None:
                raise InventoryError(f"原料 {materials[mid].material_code} 缺少 unit_cost")
            if _q6_amount(l["unit_cost"]) < 0:
                raise InventoryError("单价不能为负数")

        # 更新单据头
        doc.document_type = doc_type_map[inbound_type]
        doc.document_time = document_time or doc.document_time
        doc.remark = remark

        # 重写明细：草稿未确认，直接删旧 tx 重建
        InventoryTransaction.query.filter_by(document_id=doc.id).delete(
            synchronize_session=False
        )
        db.session.flush()

        for idx, l in enumerate(lines, start=1):
            mid = int(l["material_id"])
            qty = _q6(l["quantity"])
            unit_cost = _q6_amount(l["unit_cost"])
            db.session.add(InventoryTransaction(
                document_id=doc.id,
                line_no=int(l.get("line_no", idx)),
                material_id=mid,
                transaction_type=tx_type_map[inbound_type],
                transaction_time=doc.document_time,
                quantity_delta=qty,
                unit_cost=unit_cost,
                total_cost=_q6_amount(qty * unit_cost),
                supplier_id=supplier_id,
                warehouse_id=warehouse_id,
                created_at=now_beijing_naive(),
            ))

        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            raise InventoryError(f"编辑入库草稿失败: {e}")

        return {"document_id": doc.id, "document_no": doc.document_no,
                "status": doc.status}


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
            "allocations": allocations,
            "total_cost": total_cost,
            "consumed": consumed,
            "shortage": need,
        }

    @staticmethod
    def create_stock_out(
        warehouse_id: int,
        lines: list,
        outbound_type: str = "PRODUCTION_OUT",
        document_no: str = None,
        document_time: datetime = None,
        process_order_id: int = None,
        remark: str = None,
        auto_confirm: bool = True,
        allow_partial: bool = False,
        commit: bool = True,          # ⭐ 新增
    ) -> dict:
        """
        lines: [
          {"material_id": 1, "quantity": 10, "line_no": 1},
          ...
        ]
        outbound_type: PRODUCTION_OUT | STOCKTAKE_LOSS | RETURN_OUT | ADJUSTMENT_RED | ADJUSTMENT_BLUE
        """
        if not lines:
            raise InventoryError("出库明细不能为空")

        if outbound_type not in OUTBOUND_TYPE_LABELS:
            raise InventoryError(f"不支持的出库类型: {outbound_type}")

        # 1. 校验仓库
        wh = db.session.get(Warehouse, warehouse_id)
        if not wh or wh.status != 1:
            raise InventoryError(f"仓库不存在或已停用: {warehouse_id}")
        if wh.warehouse_type not in ("raw_material", "general"):
            raise InventoryError("原料出库必须使用原料仓或普通仓")

        # 2. 校验明细
        material_ids = [int(l["material_id"]) for l in lines]
        materials = {
            m.id: m for m in Material.query.filter(Material.id.in_(material_ids)).all()
        }
        for l in lines:
            mid = int(l["material_id"])
            if mid not in materials:
                raise InventoryError(f"原料不存在: {mid}")
            if materials[mid].status != 1:
                raise InventoryError(f"原料已禁用: {materials[mid].material_code} - {materials[mid].material_name}")
            if _q6(l["quantity"]) <= 0:
                raise InventoryError(f"原料 {materials[mid].material_code} - {materials[mid].material_name} 数量必须 > 0")

        # 2.5 ⭐ 整单预检：一次性收集全部不足项
        if auto_confirm:
            StockOutService._precheck_available(
                warehouse_id, lines, allow_partial=allow_partial,
            )

        # 3. 单据头
        doc_no = document_no or generate_document_no(prefix_map[outbound_type])
        if InventoryDocument.query.filter_by(document_no=doc_no).first():
            raise InventoryError(f"单据号重复: {doc_no}")

        doc_time = document_time or now_beijing_naive()
        doc_status = "CONFIRMED" if auto_confirm else "DRAFT"

        document = InventoryDocument(
            document_no=doc_no,
            document_type=doc_type_map[outbound_type],
            document_time=doc_time,
            status=doc_status,
            remark=remark,
            created_at=doc_time,
            confirmed_at=doc_time if auto_confirm else None,
        )
        db.session.add(document)
        db.session.flush()

        # 4. 每条明细生成出库流水（负数）
        tx_results = []
        for idx, l in enumerate(lines, start=1):
            mid = int(l["material_id"])
            qty = _q6(l["quantity"])
            line_no = int(l.get("line_no", idx))

            tx = InventoryTransaction(
                document_id=document.id,
                line_no=line_no,
                material_id=mid,
                transaction_type=tx_type_map[outbound_type],  # ⭐
                transaction_time=doc_time,
                quantity_delta=-qty,        # 出库为负
                unit_cost=None,             # FIFO 后回填加权
                total_cost=None,
                process_order_id=process_order_id,
                warehouse_id=warehouse_id,
                created_at=doc_time,
            )
            db.session.add(tx)
            db.session.flush()              # 拿到 tx.id，供成本分配引用

            if auto_confirm:
                result = StockOutService._consume_fifo(
                    warehouse_id=warehouse_id,
                    material_id=mid,
                    quantity=qty,
                    out_transaction_id=tx.id,
                    allow_partial=allow_partial,
                )
                # 回填出库流水的成本（FIFO 加权平均单价）
                total_cost = result["total_cost"]
                consumed = result["consumed"]
                if consumed > 0:
                    tx.total_cost = total_cost
                    tx.unit_cost = _q6_amount(total_cost / consumed)
                tx_results.append({
                    "transaction_id": tx.id,
                    "material_id": mid,
                    "quantity": str(qty),
                    "consumed": str(consumed),
                    "shortage": str(result["shortage"]),
                    "total_cost": str(total_cost),
                    "allocations": result["allocations"],
                })
            else:
                tx_results.append({
                    "transaction_id": tx.id,
                    "material_id": mid,
                    "quantity": str(qty),
                    "allocations": [],
                })

        if commit:
            try:
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                raise InventoryError(f"出库失败: {e}")
        else:
            db.session.flush()        # ⭐ 只 flush，交给外层提交

        return {
            "document_id": document.id,
            "document_no": document.document_no,
            "document_type": document.document_type,
            "status": document.status,
            "document_time": document.document_time.isoformat(),
            "warehouse_id": warehouse_id,
            "outbound_type": outbound_type,
            "line_count": len(tx_results),
            "lines": tx_results,
        }

    @staticmethod
    def confirm_document(document_id: int) -> dict:
        """确认草稿出库单：逐条 FIFO 消耗成本层"""
        doc = db.session.get(InventoryDocument, document_id)
        if not doc:
            raise InventoryError("单据不存在")
        if doc.document_type not in ("PRODUCTION_OUT", "STOCKTAKE", "RETURN", "ADJUSTMENT_RED", "ADJUSTMENT_BLUE"):
            raise InventoryError("非出库类单据不可确认")
        if doc.status != "DRAFT":
            raise InventoryError(f"单据状态不为草稿，当前: {doc.status}")

        doc.status = "CONFIRMED"
        doc_time = now_beijing_naive()
        doc.confirmed_at = doc_time
        doc.document_time = doc_time

        txs = (
            InventoryTransaction.query
            .filter_by(document_id=doc.id)
            .order_by(InventoryTransaction.line_no.asc())
            .all()
        )

        # 确认前整单预检
        pre_lines = [
            {"material_id": tx.material_id, "quantity": str(-tx.quantity_delta)}
            for tx in txs if tx.quantity_delta < 0
        ]
        # 注意：草稿可能多仓？当前 inventory_documents 草稿是单仓（create 时一个 warehouse_id）
        # 若未来支持多仓，需要按 tx.warehouse_id 分组预检
        if pre_lines:
            wh_ids = {tx.warehouse_id for tx in txs if tx.quantity_delta < 0}
            for wh_id in wh_ids:
                group = [
                    {"material_id": tx.material_id,
                     "quantity": str(-tx.quantity_delta)}
                    for tx in txs
                    if tx.quantity_delta < 0 and tx.warehouse_id == wh_id
                ]
                StockOutService._precheck_available(wh_id, group)

        for tx in txs:
            if tx.quantity_delta >= 0:
                continue  # 只处理出库行
            # 对于草稿后才confirm的提交，更新流水时间
            tx.transaction_time = doc_time
            qty = -tx.quantity_delta
            result = StockOutService._consume_fifo(
                warehouse_id=tx.warehouse_id,
                material_id=tx.material_id,
                quantity=qty,
                out_transaction_id=tx.id,
            )
            if result["consumed"] > 0:
                tx.total_cost = result["total_cost"]
                tx.unit_cost = _q6_amount(result["total_cost"] / result["consumed"])

        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            raise InventoryError(f"确认失败: {e}")

        return {"document_id": doc.id, "status": doc.status}


    @staticmethod
    def update_draft(
        document_id: int,
        warehouse_id: int,
        lines: list,
        outbound_type: str = "PRODUCTION_OUT",
        process_order_id: int = None,
        document_time: datetime = None,
        remark: str = None,
    ) -> dict:
        doc = db.session.get(InventoryDocument, document_id)
        if not doc:
            raise InventoryError("单据不存在")
        if doc.status != "DRAFT":
            raise InventoryError(f"只有草稿可编辑，当前: {doc.status}")
        if doc.document_type not in (
            "PRODUCTION_OUT", "STOCKTAKE", "RETURN",
            "ADJUSTMENT_RED", "ADJUSTMENT_BLUE",
        ):
            raise InventoryError("非出库类单据不可编辑")
        if outbound_type not in OUTBOUND_TYPE_LABELS:
            raise InventoryError(f"不支持的出库类型: {outbound_type}")

        wh = db.session.get(Warehouse, warehouse_id)
        if not wh or wh.status != 1:
            raise InventoryError(f"仓库不存在或已停用: {warehouse_id}")
        if wh.warehouse_type not in ("raw_material", "general"):
            raise InventoryError("原料出库必须使用原料仓或普通仓")

        if not lines:
            raise InventoryError("出库明细不能为空")

        material_ids = [int(l["material_id"]) for l in lines]
        materials = {
            m.id: m for m in Material.query.filter(Material.id.in_(material_ids)).all()
        }
        for l in lines:
            mid = int(l["material_id"])
            if mid not in materials:
                raise InventoryError(f"原料不存在: {mid}")
            if materials[mid].status != 1:
                raise InventoryError(f"原料已禁用: {materials[mid].material_code}")
            if _q6(l["quantity"]) <= 0:
                raise InventoryError(f"原料 {materials[mid].material_code} 数量必须 > 0")

        doc.document_type = doc_type_map[outbound_type]
        doc.document_time = document_time or doc.document_time
        doc.remark = remark

        InventoryTransaction.query.filter_by(document_id=doc.id).delete(
            synchronize_session=False
        )
        db.session.flush()

        for idx, l in enumerate(lines, start=1):
            mid = int(l["material_id"])
            qty = _q6(l["quantity"])
            db.session.add(InventoryTransaction(
                document_id=doc.id,
                line_no=int(l.get("line_no", idx)),
                material_id=mid,
                transaction_type=tx_type_map[outbound_type],
                transaction_time=doc.document_time,
                quantity_delta=-qty,
                unit_cost=None,
                total_cost=None,
                process_order_id=process_order_id,
                warehouse_id=warehouse_id,
                created_at=now_beijing_naive(),
            ))

        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            raise InventoryError(f"编辑出库草稿失败: {e}")

        return {"document_id": doc.id, "document_no": doc.document_no,
                "status": doc.status}

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