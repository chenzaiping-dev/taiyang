from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime
from sqlalchemy import func

from app.extensions import db
from app.generated.models.materials import Material
from app.generated.models.warehouses import Warehouse
from app.generated.models.suppliers import Supplier

from app.generated.models.inventory_documents import InventoryDocument
from app.generated.models.inventory_transactions import InventoryTransaction
from app.generated.models.inventory_cost_layers import InventoryCostLayer

from app.utils.datetime_utils import now_beijing_naive

Q2 = Decimal("0.000001")   # DECIMAL(18,6)
Q4 = Decimal("0.0001")


class InventoryError(Exception):
    pass


def _q6(v) -> Decimal:
    return Decimal(str(v)).quantize(Q2, rounding=ROUND_HALF_UP)


def _q6_amount(v) -> Decimal:
    return Decimal(str(v)).quantize(Q2, rounding=ROUND_HALF_UP)


def generate_document_no(prefix: str = "PI") -> str:
    """生成单据号：前缀 + yyyyMMddHHmmss + 4位流水"""
    now = datetime.now()
    base = now.strftime("%Y%m%d%H%M%S")
    # 简单实现：借助 database id 递增更严谨，这里用随机/时间戳足够
    import random
    suffix = f"{random.randint(0, 9999):04d}"
    return f"{prefix}{base}{suffix}"


class PurchaseInService:
    """纯材料采购入库（FIFO 成本层）"""

    @staticmethod
    def create_purchase_in(
        warehouse_id: int,
        supplier_id: int,
        lines: list,
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
        doc_no = document_no or generate_document_no("PI")
        if InventoryDocument.query.filter_by(document_no=doc_no).first():
            raise InventoryError(f"单据号重复: {doc_no}")

        doc_time = document_time or now_beijing_naive()
        doc_status = "CONFIRMED" if auto_confirm else "DRAFT"

        # 5. 创建单据
        document = InventoryDocument(
            document_no=doc_no,
            document_type="PURCHASE_IN",
            document_time=doc_time,
            status=doc_status,
            remark=remark,
            confirmed_at=now_beijing_naive() if auto_confirm else None,
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
                transaction_type="PURCHASE_IN",
                transaction_time=doc_time,
                quantity_delta=qty,          # 入库正数
                unit_cost=unit_cost,
                total_cost=total_cost,
                supplier_id=supplier_id,
                warehouse_id=warehouse_id,
            )
            db.session.add(tx)
            db.session.flush()  # 拿到 tx.id

            if auto_confirm:
                layer = InventoryCostLayer(
                    material_id=mid,
                    source_transaction_id=tx.id,
                    layer_type="PURCHASE",
                    effective_time=doc_time,
                    original_qty=qty,
                    remaining_qty=qty,
                    unit_cost=unit_cost,
                    original_cost=total_cost,
                    warehouse_id=warehouse_id,
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
        if doc.document_type != "PURCHASE_IN":
            raise InventoryError("仅采购入库单可确认")
        if doc.status != "DRAFT":
            raise InventoryError(f"单据状态不为草稿，当前: {doc.status}")

        doc.status = "CONFIRMED"
        doc.confirmed_at = now_beijing_naive()

        txs = InventoryTransaction.query.filter_by(document_id=doc.id).all()
        for tx in txs:
            if tx.quantity_delta <= 0:
                raise InventoryError("采购流水数量必须为正")
            layer = InventoryCostLayer(
                material_id=tx.material_id,
                source_transaction_id=tx.id,
                layer_type="PURCHASE",
                effective_time=tx.transaction_time,
                original_qty=tx.quantity_delta,
                remaining_qty=tx.quantity_delta,
                unit_cost=tx.unit_cost or Decimal("0"),
                original_cost=tx.total_cost or Decimal("0"),
                warehouse_id=tx.warehouse_id,
            )
            db.session.add(layer)

        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            raise InventoryError(f"确认失败: {e}")

        return {"document_id": doc.id, "status": doc.status}

from app.generated.models.inventory_cost_allocations import InventoryCostAllocation
from sqlalchemy import and_

class StockOutService:
    """原料出库（FIFO 成本层消耗）"""

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

        available = sum((l.remaining_qty for l in layers), Decimal("0"))
        if available < quantity and not allow_partial:
            raise InventoryError(
                f"库存不足：material_id={material_id} 需要 {quantity}，可用 {available}"
            )

        need = quantity
        allocations = []
        total_cost = Decimal("0")

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
        out_type: str = "PRODUCTION_OUT",
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
        out_type: PRODUCTION_OUT | STOCKTAKE_LOSS
        """
        if not lines:
            raise InventoryError("出库明细不能为空")

        if out_type not in ("PRODUCTION_OUT", "STOCKTAKE_LOSS"):
            raise InventoryError(f"不支持的出库类型: {out_type}")

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
                raise InventoryError(f"原料已禁用: {materials[mid].material_code}")
            if _q6(l["quantity"]) <= 0:
                raise InventoryError(f"原料 {materials[mid].material_code} 数量必须 > 0")

        # 3. 单据头
        doc_type_map = {
            "PRODUCTION_OUT": "PRODUCTION_OUT",
            "STOCKTAKE_LOSS": "STOCKTAKE",
        }
        doc_no = document_no or generate_document_no("PO")
        if InventoryDocument.query.filter_by(document_no=doc_no).first():
            raise InventoryError(f"单据号重复: {doc_no}")

        doc_time = document_time or now_beijing_naive()
        doc_status = "CONFIRMED" if auto_confirm else "DRAFT"

        document = InventoryDocument(
            document_no=doc_no,
            document_type=doc_type_map[out_type],
            document_time=doc_time,
            status=doc_status,
            remark=remark,
            confirmed_at=now_beijing_naive() if auto_confirm else None,
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
                transaction_type=out_type,
                transaction_time=doc_time,
                quantity_delta=-qty,        # 出库为负
                unit_cost=None,             # FIFO 后回填加权
                total_cost=None,
                process_order_id=process_order_id,
                warehouse_id=warehouse_id,
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
            "out_type": out_type,
            "line_count": len(tx_results),
            "lines": tx_results,
        }

    @staticmethod
    def confirm_document(document_id: int) -> dict:
        """确认草稿出库单：逐条 FIFO 消耗成本层"""
        doc = db.session.get(InventoryDocument, document_id)
        if not doc:
            raise InventoryError("单据不存在")
        if doc.document_type not in ("PRODUCTION_OUT", "STOCKTAKE", "ADJUSTMENT"):
            raise InventoryError("非出库类单据不可确认")
        if doc.status != "DRAFT":
            raise InventoryError(f"单据状态不为草稿，当前: {doc.status}")

        doc.status = "CONFIRMED"
        doc.confirmed_at = now_beijing_naive()

        txs = (
            InventoryTransaction.query
            .filter_by(document_id=doc.id)
            .order_by(InventoryTransaction.line_no.asc())
            .all()
        )
        for tx in txs:
            if tx.quantity_delta >= 0:
                continue  # 只处理出库行
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