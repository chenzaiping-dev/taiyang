# app/services/process_order_adjustment_service.py
from decimal import Decimal
from sqlalchemy import func
from app.extensions import db
from app.utils.datetime_utils import now_beijing_naive
from app.utils.decimal_utils import _q6
from app.generated.models.process_orders import ProcessOrder
from app.generated.models.process_order_materials import ProcessOrderMaterial
from app.generated.models.materials import Material
from app.generated.models.inventory_documents import InventoryDocument
from app.generated.models.inventory_transactions import InventoryTransaction
from app.generated.models.inventory_cost_layers import InventoryCostLayer
from app.generated.models.inventory_cost_allocations import InventoryCostAllocation
from app.generated.models.process_order_material_adjustments import (
    ProcessOrderMaterialAdjustment,
)
from app.services.inventory_service import (
    StockOutService, InventoryError, generate_document_no_new
)

class ProcessOrderAdjustmentError(Exception):
    pass


class ProcessOrderAdjustmentService:

    @staticmethod
    def adjust_materials(
        order_id: int,
        new_materials: list,
        reason: str = None,
        operator_id: int = None,
    ) -> dict:
        """
        修订原料配比（仅 ORDERED / PRODUCING 状态允许）。
        new_materials: [{'material_id', 'ratio_value', 'quantity', 'remark'}]
        流程：
          1. 校验状态
          2. 计算差异（每原料）：delta_qty = new_qty - old_qty
          3. 对 delta < 0 的原料做"红冲出库"（退回成本层）
          4. 对 delta > 0 的原料做"追加出库"（新消耗成本层）
          5. 更新 process_order_materials
          6. 写 process_order_material_adjustments 记录调整快照
          7. 一次 commit
        """
        order = db.session.get(ProcessOrder, order_id)
        if not order:
            raise ProcessOrderAdjustmentError("工艺单不存在")
        if order.status not in ("ORDERED", "PRODUCING", "COMPLETED", "RECEIVED"):
            raise ProcessOrderAdjustmentError(
                f"当前状态【{order.status}】不允许调整配比，仅已下单/生产中/完成/入库可调整"
            )

        # 原配比
        old_rows = ProcessOrderMaterial.query.filter_by(
            process_order_id=order_id
        ).all()
        old_map = {m.material_id: m for m in old_rows}

        # 归一化新配比
        normalized = ProcessOrderAdjustmentService._normalize(new_materials, order)

        # 差异计算
        new_ids = {m["material_id"] for m in normalized}
        old_ids = set(old_map.keys())
        all_ids = new_ids | old_ids

        to_reverse = []   # (material_id, qty_to_reverse, old_material_row)
        to_consume = []   # (material_id, qty_to_consume, new_material_dict)
        meta_only = []    # (material_id, old_row, new_dict)  仅配比/备注变化

        for mid in all_ids:
            old_row = old_map.get(mid)
            new_item = next((m for m in normalized if m["material_id"] == mid), None)

            old_qty = _q6(old_row.quantity) if old_row else Decimal("0")
            new_qty = _q6(new_item["quantity"]) if new_item else Decimal("0")

            if new_qty > old_qty:
                to_consume.append((mid, new_qty - old_qty, new_item))
                continue
            elif new_qty < old_qty:
                to_reverse.append((mid, old_qty - new_qty, old_row))
                continue

            # 数量相同：检查 ratio / remark 是否变了
            if old_row and new_item:
                old_ratio = old_row.ratio_value
                new_ratio = new_item["ratio_value"]
                old_remark = (old_row.remark or "").strip()
                new_remark = ((new_item.get("remark") or "").strip())
                if old_ratio != new_ratio or old_remark != new_remark:
                    meta_only.append((mid, old_row, new_item))

        if not to_reverse and not to_consume and not meta_only:
            raise ProcessOrderAdjustmentError("配比及备注均无变化，无需调整")

        # 找原原料出库单（place_order 创建的）
        #orig_doc_no = f"PROD-OUT-{order.process_order_no}"
        #orig_doc = InventoryDocument.query.filter_by(document_no=orig_doc_no).first()
        #if not orig_doc and to_reverse:
        #    raise ProcessOrderAdjustmentError(
        #        "找不到原领料单，无法回溯成本层"
        #    )

        adjust_no = f"ADJ-{order.process_order_no}-{now_beijing_naive().strftime('%Y%m%d%H%M%S')}"
        red_doc = blue_doc = None

        try:
            # ---------- 1. 红冲（退回）----------
            if to_reverse:
                red_doc = ProcessOrderAdjustmentService._create_reverse_out(
                    order=order,
                    #orig_doc=orig_doc,
                    reverse_items=to_reverse,
                    adjust_no=adjust_no,
                    operator_id=operator_id,
                    reason=reason,
                )

            # ---------- 2. 追加（新消耗）----------
            if to_consume:
                blue_doc = ProcessOrderAdjustmentService._create_consume_in(
                    order=order,
                    consume_items=to_consume,
                    adjust_no=adjust_no,
                    operator_id=operator_id,
                    reason=reason,
                )

            # ---------- 3. 更新 process_order_materials ----------
            old_snapshot = [
                {"material_id": m.material_id,
                 "ratio_value": str(m.ratio_value),
                 "quantity": str(m.quantity),
                 "cost": str(m.cost)}
                for m in old_rows
            ]

            # 全删重插（保留 schema 上的关系）
            ProcessOrderMaterial.query.filter_by(
                process_order_id=order_id
            ).delete(synchronize_session=False)
            for item in normalized:
                db.session.add(ProcessOrderMaterial(
                    process_order_id=order_id,
                    material_id=item["material_id"],
                    ratio_value=item["ratio_value"],
                    quantity=item["quantity"],
                    cost=Decimal("0"),     # 稍后重算
                    remark=item.get("remark"),
                ))
            db.session.flush()

            # ---------- 4. 重算每材料累计成本 ----------
            cost_rows = (
                db.session.query(
                    InventoryTransaction.material_id,
                    func.sum(InventoryTransaction.total_cost),
                )
                .filter(InventoryTransaction.process_order_id == order.id)
                .group_by(InventoryTransaction.material_id)
                .all()
            )
            cost_map = {mid: _q6(c or 0) for mid, c in cost_rows}

            new_rows = ProcessOrderMaterial.query.filter_by(
                process_order_id=order_id
            ).all()
            total_cost = Decimal("0")
            for row in new_rows:
                row.cost = cost_map.get(row.material_id, Decimal("0"))
                total_cost += row.cost
            order.actual_total_cost = _q6(total_cost)

            # ---------- 4.5 同步成品批次单价 ----------
            # 配比调整改变了 actual_total_cost，若该工艺单已入库且批次
            # 尚未发生出库，则按新的 actual_total_cost / completed_quantity
            # 回写 finished_goods_batches.unit_cost。
            # 已发生出库的批次不改，避免历史成本不可复现。
            ProcessOrderAdjustmentService._sync_batch_unit_cost(order)

            # ---------- 5. 记录调整单 ----------
            new_snapshot = [
                {"material_id": r.material_id,
                 "ratio_value": str(r.ratio_value),
                 "quantity": str(r.quantity),
                 "cost": str(r.cost)}
                for r in new_rows
            ]
            adjust = ProcessOrderMaterialAdjustment(
                adjustment_no=adjust_no,
                process_order_id=order_id,
                adjust_time=now_beijing_naive(),
                adjust_reason=reason,
                old_snapshot=old_snapshot,
                new_snapshot=new_snapshot,
                red_document_id=red_doc.id if red_doc else None,
                blue_document_id=blue_doc.id if blue_doc else None,
                created_by=operator_id,
                created_at=now_beijing_naive(),
            )
            db.session.add(adjust)

            db.session.commit()
        except Exception as e:
            db.session.rollback()
            raise ProcessOrderAdjustmentError(f"配比调整失败: {e}")

        return {
            "adjustment_no": adjust_no,
            "process_order_id": order_id,
            "reverse_doc_no": red_doc.document_no if red_doc else None,
            "consume_doc_no": blue_doc.document_no if blue_doc else None,
            "new_actual_total_cost": str(order.actual_total_cost),
        }

    @staticmethod
    def _normalize(materials: list, order) -> list:
        """复用 place_order 里的校验逻辑"""
        # 至少要有一条，ratio 合计 ≤ 1，quantity = planned_quantity * ratio
        from decimal import Decimal
        material_ids = [int(m["material_id"]) for m in materials]
        if len(material_ids) != len(set(material_ids)):
            raise ProcessOrderAdjustmentError("同一原料只能出现一次")
        exist = {
            m.id: m for m in Material.query.filter(
                Material.id.in_(material_ids)).all()
        }
        total_qty = _q6(order.planned_quantity)
        ratio_sum = Decimal("0")
        result = []
        for idx, m in enumerate(materials, start=1):
            mid = int(m["material_id"])
            if mid not in exist:
                raise ProcessOrderAdjustmentError(f"原料不存在: {mid}")
            ratio = Decimal(str(m["ratio_value"]))
            if ratio < 0 or ratio > 1:
                raise ProcessOrderAdjustmentError(
                    f"第 {idx} 行配比必须在 0-1 之间")
            ratio_sum += ratio
            result.append({
                "material_id": mid,
                "ratio_value": ratio,
                "quantity": _q6(total_qty * ratio),
                "remark": m.get("remark"),
            })
        if ratio_sum > Decimal("1") + Decimal("0.000001"):
            raise ProcessOrderAdjustmentError(f"配比合计不能超过 1，当前 {ratio_sum}")
        return result

    @staticmethod
    def _create_reverse_out(order, reverse_items, adjust_no,
                            operator_id, reason):
        """
        红冲：把某些原料的部分消耗退回成本层。
        按 (process_order_id, material_id) 从所有正向消耗流水里逆序归还。
        """
        adjust_type = "ADJUSTMENT_RED"
        doc_no = generate_document_no_new(adjust_type)
        doc = InventoryDocument(
            document_no=doc_no,
            document_type=adjust_type,
            document_time=now_beijing_naive(),
            status="CONFIRMED",
            remark=f"[MATERIAL_ADJUST_RED] {order.process_order_no} 配比减少红冲",
            created_at=now_beijing_naive(),
            confirmed_at=now_beijing_naive(),
        )
        db.session.add(doc)
        db.session.flush()

        # ⭐ 单据内全局递增的 line_no（跨原料、跨 orig_tx 都唯一）
        next_line_no = 1

        for mid, qty, old_material in reverse_items:
            need = _q6(qty)
            if need <= 0:
                continue

            # 找该工艺单该原料所有"正向消耗"流水
            orig_txs_q = (
                InventoryTransaction.query
                .filter(
                    InventoryTransaction.process_order_id == order.id,
                    InventoryTransaction.material_id == mid,
                    InventoryTransaction.quantity_delta < 0,
                    InventoryTransaction.transaction_type.in_(
                        ["PRODUCTION_OUT", "ADJUSTMENT_BLUE"]
                    ),
                )
            )
            if order.material_warehouse_id:
                orig_txs_q = orig_txs_q.filter(
                    InventoryTransaction.warehouse_id
                    == order.material_warehouse_id
                )
            orig_txs = (
                orig_txs_q
                .order_by(
                    InventoryTransaction.transaction_time.desc(),
                    InventoryTransaction.id.desc(),
                )
                .with_for_update()
                .all()
            )
            if not orig_txs:
                raise ProcessOrderAdjustmentError(
                    f"找不到原料 {mid} 在原领料单或调整单中的消耗流水"
                )

            for orig_tx in orig_txs:
                if need <= 0:
                    break

                reversible = (
                    db.session.query(func.coalesce(
                        func.sum(InventoryCostAllocation.quantity), 0))
                    .filter(
                        InventoryCostAllocation.out_transaction_id == orig_tx.id
                    )
                    .scalar()
                ) or Decimal("0")
                if reversible <= 0:
                    continue

                take_qty = min(reversible, need)

                new_tx = InventoryTransaction(
                    document_id=doc.id,
                    line_no=next_line_no,          # ⭐ 全局递增，避免重复
                    material_id=mid,
                    transaction_type=adjust_type,
                    transaction_time=now_beijing_naive(),
                    quantity_delta=take_qty,
                    process_order_id=order.id,
                    warehouse_id=orig_tx.warehouse_id,
                    created_at=now_beijing_naive(),
                )
                next_line_no += 1                  # ⭐ 每插一条 +1
                db.session.add(new_tx)
                db.session.flush()

                result = ProcessOrderAdjustmentService._reverse_allocation(
                    orig_tx.id, take_qty, new_tx.id
                )
                recovered_cost = result["total_cost"]
                new_tx.total_cost = -_q6(recovered_cost)
                new_tx.unit_cost = (
                    _q6(recovered_cost / take_qty) if take_qty > 0 else None
                )

                need -= take_qty

            if need > 0:
                raise ProcessOrderAdjustmentError(
                    f"原料 {mid} 可红冲数量不足，还差 {need}（可能超出已消耗量）"
                )

        return doc

    @staticmethod
    def _reverse_allocation(orig_tx_id: int, reverse_qty: Decimal, new_tx_id: int):
        """按原 allocation 逆序（LIFO）归还"""
        allocs = (
            InventoryCostAllocation.query
            .filter_by(out_transaction_id=orig_tx_id)
            .order_by(InventoryCostAllocation.id.desc())
            .with_for_update()
            .all()
        )
        need = reverse_qty
        total_cost = Decimal("0")
        for alloc in allocs:
            if need <= 0:
                break
            layer = db.session.get(InventoryCostLayer, alloc.cost_layer_id)
            if not layer:
                continue
            give = min(alloc.quantity, need)
            cost = _q6(give * alloc.unit_cost)
            # 成本层归还
            layer.remaining_qty = layer.remaining_qty + give
            # 记录红冲 allocation（负数）
            db.session.add(InventoryCostAllocation(
                out_transaction_id=new_tx_id,
                cost_layer_id=layer.id,
                quantity=-give,          # ⭐ 负数（需放宽 CHECK）
                unit_cost=alloc.unit_cost,
                cost_amount=-cost,       # ⭐ 负数（需放宽 CHECK）
                created_at=now_beijing_naive(),
            ))
            # 原 allocation 数量也同步减少（保持净额准确）
            alloc.quantity -= give
            alloc.cost_amount = _q6(alloc.quantity * alloc.unit_cost)
            if alloc.quantity <= 0:
                db.session.delete(alloc)

            need -= give
            total_cost += cost
        if need > 0:
            raise ProcessOrderAdjustmentError(
                f"无法完整归还 {reverse_qty}，还差 {need}")
        return {"total_cost": total_cost}

    @staticmethod
    def _create_consume_in(order, consume_items, adjust_no, operator_id, reason):
        """
        追加：按新配比出库（沿用 FIFO）。
        复用 StockOutService.create_stock_out，用 commit=False 合并事务。
        """
        #doc_no = f"ADJ-BLUE-{order.process_order_no}-{adjust_no[-6:]}"
        adjust_type = "ADJUSTMENT_BLUE"
        doc_no = generate_document_no_new(adjust_type)

        lines = [
            {"material_id": mid, "quantity": str(qty), "line_no": idx + 1}
            for idx, (mid, qty, _) in enumerate(consume_items)
        ]
        warehouse_id = (
            order.material_warehouse_id
            or ProcessOrderAdjustmentService._default_rm_warehouse_id()
        )
        result = StockOutService.create_stock_out(
            warehouse_id=warehouse_id,
            lines=lines,
            outbound_type=adjust_type,
            document_no=doc_no,
            document_time=now_beijing_naive(),
            process_order_id=order.id,
            remark=f"[MATERIAL_ADJUST_ADD] {order.process_order_no} 配比增加补充出库",
            auto_confirm=True,
            allow_partial=False,
            commit=False,
        )
        doc = db.session.get(InventoryDocument, result["document_id"])
        return doc

    @staticmethod
    def _default_rm_warehouse_id():
        from app.generated.models.warehouses import Warehouse
        wh = Warehouse.query.filter_by(
            warehouse_type="raw_material", status=1
        ).order_by(Warehouse.id.asc()).first()
        if not wh:
            raise ProcessOrderAdjustmentError("没有可用的原料仓")
        return wh.id

    @staticmethod
    def _sync_batch_unit_cost(order):
        """
        配比调整后，按最新 actual_total_cost 回写成品批次单价。
        - 仅处理来源为本工艺单的批次（finished_goods_batches.process_order_id）
        - 批次已发生出库（finished_goods_transactions 有 quantity_delta < 0）
          则不自动改，避免历史成本被覆盖
        - completed_quantity <= 0 时无法计算单价，跳过
        """
        from app.generated.models.finished_goods_batches import (
            FinishedGoodsBatch,
        )
        from app.generated.models.finished_goods_transactions import (
            FinishedGoodsTransaction,
        )

        qty = _q6(order.completed_quantity or 0)
        if qty <= 0:
            qty = _q6(order.planned_quantity)
        actual_cost = _q6(order.actual_total_cost or 0)
        if qty <= 0 or actual_cost <= 0:
            return

        new_unit_cost = _q6(actual_cost / qty)

        batches = FinishedGoodsBatch.query.filter_by(
            process_order_id=order.id
        ).all()
        for batch in batches:
            #has_outbound = (
            #    db.session.query(FinishedGoodsTransaction.id)
            #    .filter(
            #        FinishedGoodsTransaction.batch_id == batch.id,
            #        FinishedGoodsTransaction.quantity_delta < 0,
            #    )
            #    .first()
            #)
            #if has_outbound:
            #    # 已出库，旧单价已参与成本计算，不自动改. to be continued
            #    continue
            if batch.unit_cost != new_unit_cost:
                batch.unit_cost = new_unit_cost