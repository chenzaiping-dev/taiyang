from sqlalchemy import select, case, func, or_
from decimal import Decimal
from app.extensions import db
from app.generated.models.employees import Employee
from app.generated.models.materials import Material
from app.generated.models.warehouses import Warehouse
from app.generated.models.suppliers import Supplier

from app.generated.models.inventory_documents import InventoryDocument
from app.generated.models.inventory_transactions import InventoryTransaction
from app.generated.models.inventory_cost_layers import InventoryCostLayer
from app.generated.models.inventory_cost_allocations import InventoryCostAllocation

from app.generated.models.process_orders import ProcessOrder
from app.utils.decimal_utils import _q3, _q6

class InventoryQueryService:

    @staticmethod
    def query_material_stock(
        material_code: str = None,
        warehouse_id: int = None,
        group_by_warehouse: bool = False,
        page: int = 1,
        page_size: int = 50,
    ):
        """
        查询原料库存。
        - material_code: 模糊匹配原料编码/名称
        - warehouse_id:  精确指定仓库
        - group_by_warehouse: True 则按 (material, warehouse) 明细展示；
                              False 则只按 material 汇总
        返回 (rows, total)
        """
        # 只统计 remaining_qty > 0 的成本层
        base_filter = [InventoryCostLayer.remaining_qty > 0]

        if material_code:
            like = f"%{material_code}%"
            base_filter.append(
                or_(
                    Material.material_code.like(like),
                    Material.material_name.like(like),
                )
            )
        if warehouse_id:
            base_filter.append(InventoryCostLayer.warehouse_id == warehouse_id)

        # 公共列
        qty_sum = func.sum(InventoryCostLayer.remaining_qty).label("total_qty")
        cost_sum = func.sum(
            InventoryCostLayer.remaining_qty * InventoryCostLayer.unit_cost
        ).label("total_cost")

        if group_by_warehouse:
            stmt = (
                select(
                    InventoryCostLayer.material_id,
                    Material.material_code,
                    Material.material_name,
                    Material.unit,
                    InventoryCostLayer.warehouse_id,
                    Warehouse.warehouse_code,
                    Warehouse.warehouse_name,
                    qty_sum,
                    cost_sum,
                )
                .join(Material, Material.id == InventoryCostLayer.material_id)
                .join(Warehouse, Warehouse.id == InventoryCostLayer.warehouse_id)
                .where(*base_filter)
                .group_by(
                    InventoryCostLayer.material_id,
                    Material.material_code,
                    Material.material_name,
                    Material.unit,
                    InventoryCostLayer.warehouse_id,
                    Warehouse.warehouse_code,
                    Warehouse.warehouse_name,
                )
                .order_by(Material.material_code.asc(),
                          Warehouse.warehouse_code.asc())
            )
        else:
            stmt = (
                select(
                    InventoryCostLayer.material_id,
                    Material.material_code,
                    Material.material_name,
                    Material.unit,
                    qty_sum,
                    cost_sum,
                )
                .join(Material, Material.id == InventoryCostLayer.material_id)
                .where(*base_filter)
                .group_by(
                    InventoryCostLayer.material_id,
                    Material.material_code,
                    Material.material_name,
                    Material.unit,
                )
                #.order_by(Material.id.desc())
                .order_by(Material.material_code.asc())
            )

        # 总数
        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = db.session.execute(count_stmt).scalar() or 0

        # 分页
        stmt = stmt.offset((page - 1) * page_size).limit(page_size)
        raw_rows = db.session.execute(stmt).mappings().all()

        rows = []
        for r in raw_rows:
            d = dict(r)
            qty = d.get("total_qty") or Decimal("0")
            cost = d.get("total_cost") or Decimal("0")
            d["avg_unit_cost"] = (
                (cost / qty).quantize(Decimal("0.0001"))
                if qty > 0 else Decimal("0")
            )
            d["total_cost"] = _q6(d.get("total_cost"))
            rows.append(d)

        return rows, total


    @staticmethod
    def query_material_io_summary(
        material_code: str = None,
        warehouse_id: int = None,
        date_from=None,
        date_to=None,
        page: int = 1,
        page_size: int = 50,
    ):
        """
        按原料（可选按仓库）汇总累计入库/出库数量。
        返回 (rows, total)
        rows 每行含:
            material_id, material_code, material_name, unit,
            warehouse_id, warehouse_code, warehouse_name,
            in_purchase, in_return, in_gain,
            out_production, out_loss,
            total_in, total_out, net_qty
        """
        # 归一化方向：入库正、出库也显示正数
        in_qty = case(
            (InventoryTransaction.quantity_delta > 0,
             InventoryTransaction.quantity_delta),
            else_=0,
        )
        out_qty = case(
            (InventoryTransaction.quantity_delta < 0,
             -InventoryTransaction.quantity_delta),
            else_=0,
        )

        # 按业务类型分桶
        in_purchase = case(
            (InventoryTransaction.transaction_type.in_(
                ["PURCHASE_IN", "RETURN_IN"]),
             InventoryTransaction.quantity_delta),
            else_=0,
        )
        in_gain = case(
            (InventoryTransaction.transaction_type == "STOCKTAKE_GAIN",
             InventoryTransaction.quantity_delta),
            else_=0,
        )
        out_production = case(
            (InventoryTransaction.transaction_type == "PRODUCTION_OUT",
             -InventoryTransaction.quantity_delta),
            else_=0,
        )
        out_loss = case(
            (InventoryTransaction.transaction_type == "STOCKTAKE_LOSS",
             -InventoryTransaction.quantity_delta),
            else_=0,
        )

        filters = []
        if material_code:
            like = f"%{material_code}%"
            filters.append(or_(
                Material.material_code.like(like),
                Material.material_name.like(like),
            ))
        if warehouse_id:
            filters.append(InventoryTransaction.warehouse_id == warehouse_id)
        if date_from:
            filters.append(InventoryTransaction.transaction_time >= date_from)
        if date_to:
            filters.append(InventoryTransaction.transaction_time < date_to)

        # 汇总列
        cols = [
            InventoryTransaction.material_id,
            Material.material_code,
            Material.material_name,
            Material.unit,
            func.sum(in_purchase).label("in_purchase"),
            func.sum(in_gain).label("in_gain"),
            func.sum(out_production).label("out_production"),
            func.sum(out_loss).label("out_loss"),
            func.sum(in_qty).label("total_in"),
            func.sum(out_qty).label("total_out"),
            func.sum(InventoryTransaction.quantity_delta).label("net_qty"),
        ]

        group_cols = [
            InventoryTransaction.material_id,
            Material.material_code,
            Material.material_name,
            Material.unit,
        ]

        # 若指定仓库，加仓库列
        if warehouse_id:
            cols += [
                InventoryTransaction.warehouse_id,
                Warehouse.warehouse_code,
                Warehouse.warehouse_name,
            ]
            group_cols += [
                InventoryTransaction.warehouse_id,
                Warehouse.warehouse_code,
                Warehouse.warehouse_name,
            ]

        stmt = (
            select(*cols)
            .join(Material, Material.id == InventoryTransaction.material_id)
        )
        if warehouse_id:
            stmt = stmt.join(
                Warehouse, Warehouse.id == InventoryTransaction.warehouse_id
            )
        stmt = stmt.where(*filters).group_by(*group_cols) \
                   .order_by(Material.material_code.asc())

        # 总数
        total = db.session.execute(
            select(func.count()).select_from(stmt.subquery())
        ).scalar() or 0

        stmt = stmt.offset((page - 1) * page_size).limit(page_size)
        raw = db.session.execute(stmt).mappings().all()

        rows = []
        for r in raw:
            d = dict(r)
            for k in ("in_purchase", "in_gain", "out_production",
                      "out_loss", "total_in", "total_out", "net_qty"):
                d[k] = d.get(k) or 0
            rows.append(d)
        return rows, total

    @staticmethod
    def query_material_transactions(
        material_code: str = None,
        warehouse_id: int = None,
        transaction_type: str = None,
        document_no: str = None,
        date_from=None,
        date_to=None,
        page: int = 1,
        page_size: int = 50,
    ):
        """
        原料出入库流水查询（含单据头信息）。
        返回 (rows, total)
        """
        creator = db.aliased(Employee)

        # 关联：原料 / 仓库 / 单据 / 供应商 / 工艺单
        stmt = (
            select(
                InventoryTransaction.id,
                InventoryTransaction.transaction_time,
                InventoryTransaction.transaction_type,
                InventoryTransaction.quantity_delta,
                InventoryTransaction.unit_cost,
                InventoryTransaction.total_cost,
                Material.id.label("material_id"),
                Material.material_code,
                Material.material_name,
                Material.unit,
                Warehouse.id.label("warehouse_id"),
                Warehouse.warehouse_code,
                Warehouse.warehouse_name,
                InventoryDocument.id.label("document_id"),
                InventoryDocument.document_no,
                InventoryDocument.document_type,
                InventoryDocument.status.label("document_status"),
                InventoryDocument.remark.label("document_remark"),
                Supplier.id.label("supplier_id"),
                Supplier.name.label("supplier_name"),
                ProcessOrder.id.label("process_order_id"),
                ProcessOrder.process_order_no,
            )
            .join(Material, Material.id == InventoryTransaction.material_id)
            .join(Warehouse, Warehouse.id == InventoryTransaction.warehouse_id)
            .join(InventoryDocument,
                  InventoryDocument.id == InventoryTransaction.document_id)
            .join(Supplier, Supplier.id == InventoryTransaction.supplier_id,
                  isouter=True)
            .join(ProcessOrder,
                  ProcessOrder.id == InventoryTransaction.process_order_id,
                  isouter=True)
            .order_by(InventoryTransaction.transaction_time.desc(),
                      InventoryTransaction.id.desc())
        )

        filters = []
        if material_code:
            like = f"%{material_code}%"
            filters.append(or_(
                Material.material_code.like(like),
                Material.material_name.like(like),
            ))
        if warehouse_id:
            filters.append(InventoryTransaction.warehouse_id == warehouse_id)
        if transaction_type:
            filters.append(
                InventoryTransaction.transaction_type == transaction_type
            )
        if document_no:
            like = f"%{document_no}%"
            filters.append(InventoryDocument.document_no.like(like))
        if date_from:
            filters.append(InventoryTransaction.transaction_time >= date_from)
        if date_to:
            filters.append(InventoryTransaction.transaction_time < date_to)

        if filters:
            stmt = stmt.where(*filters)

        total = db.session.execute(
            select(func.count()).select_from(stmt.subquery())
        ).scalar() or 0

        stmt = stmt.offset((page - 1) * page_size).limit(page_size)
        rows = [dict(r) for r in db.session.execute(stmt).mappings().all()]
        return rows, total

    @staticmethod
    def list_stock_out_documents(keyword=None, status=None,
                                  date_from=None, date_to=None,
                                  page=1, page_size=50):
        """
        原料出库单列表（PRODUCTION_OUT + STOCKTAKE 类型）。
        返回 (rows, total)
        """
        stmt = (
            select(
                InventoryDocument.id,
                InventoryDocument.document_no,
                InventoryDocument.document_type,
                InventoryDocument.document_time,
                InventoryDocument.status,
                InventoryDocument.remark,
                InventoryDocument.created_at,
                InventoryDocument.confirmed_at,
                func.count(InventoryTransaction.id).label("line_count"),
                func.coalesce(
                    func.sum(-InventoryTransaction.quantity_delta), 0
                ).label("total_qty"),
                func.coalesce(
                    func.sum(InventoryTransaction.total_cost), 0
                ).label("total_cost"),
            )
            .join(InventoryTransaction,
                  InventoryTransaction.document_id == InventoryDocument.id)
            .where(
                InventoryDocument.document_type.in_(
                    ["PRODUCTION_OUT", "STOCKTAKE", "ADJUSTMENT"]
                ),
                InventoryTransaction.transaction_type.in_(
                    ["PRODUCTION_OUT", "STOCKTAKE_LOSS"]
                ),
            )
            .group_by(
                InventoryDocument.id,
                InventoryDocument.document_no,
                InventoryDocument.document_type,
                InventoryDocument.document_time,
                InventoryDocument.status,
                InventoryDocument.remark,
                InventoryDocument.created_at,
                InventoryDocument.confirmed_at,
            )
            .order_by(InventoryDocument.id.desc())
        )
        if keyword:
            stmt = stmt.where(
                InventoryDocument.document_no.like(f"%{keyword}%")
            )
        if status:
            stmt = stmt.where(InventoryDocument.status == status)
        if date_from:
            stmt = stmt.where(InventoryDocument.document_time >= date_from)
        if date_to:
            stmt = stmt.where(InventoryDocument.document_time < date_to)

        total = db.session.execute(
            select(func.count()).select_from(stmt.subquery())
        ).scalar() or 0

        stmt = stmt.offset((page - 1) * page_size).limit(page_size)
        rows = [dict(r) for r in db.session.execute(stmt).mappings().all()]
        return rows, total

    @staticmethod
    def get_stock_out_detail(document_id: int):
        """
        出库单详情：
        {
          'document': InventoryDocument 行 dict,
          'lines': [{material/warehouse/quantity_delta/total_cost/...}],
          'allocations_map': {line_transaction_id: [alloc, ...]},
          'total_qty', 'total_cost'
        }
        """
        doc = db.session.get(InventoryDocument, document_id)
        if not doc:
            return None
        if doc.document_type not in ("PRODUCTION_OUT", "STOCKTAKE", "ADJUSTMENT"):
            return None

        # 明细行
        line_stmt = (
            select(
                InventoryTransaction.id.label("transaction_id"),
                InventoryTransaction.line_no,
                InventoryTransaction.transaction_type,
                InventoryTransaction.transaction_time,
                InventoryTransaction.quantity_delta,
                InventoryTransaction.unit_cost,
                InventoryTransaction.total_cost,
                Material.id.label("material_id"),
                Material.material_code,
                Material.material_name,
                Material.unit,
                Warehouse.id.label("warehouse_id"),
                Warehouse.warehouse_code,
                Warehouse.warehouse_name,
                Supplier.name.label("supplier_name"),
                ProcessOrder.id.label("process_order_id"),
                ProcessOrder.process_order_no,
            )
            .join(Material, Material.id == InventoryTransaction.material_id)
            .join(Warehouse, Warehouse.id == InventoryTransaction.warehouse_id)
            .join(Supplier, Supplier.id == InventoryTransaction.supplier_id,
                  isouter=True)
            .join(ProcessOrder,
                  ProcessOrder.id == InventoryTransaction.process_order_id,
                  isouter=True)
            .where(InventoryTransaction.document_id == document_id)
            .order_by(InventoryTransaction.line_no.asc())
        )
        lines = [dict(r) for r in db.session.execute(line_stmt).mappings().all()]

        # 分配明细
        tx_ids = [l["transaction_id"] for l in lines]
        allocs_by_tx = {}
        if tx_ids:
            alloc_stmt = (
                select(
                    InventoryCostAllocation.out_transaction_id,
                    InventoryCostAllocation.cost_layer_id,
                    InventoryCostAllocation.quantity,
                    InventoryCostAllocation.unit_cost,
                    InventoryCostAllocation.cost_amount,
                    InventoryCostLayer.layer_type,
                    InventoryCostLayer.effective_time,
                )
                .join(InventoryCostLayer,
                      InventoryCostLayer.id == InventoryCostAllocation.cost_layer_id)
                .where(InventoryCostAllocation.out_transaction_id.in_(tx_ids))
                .order_by(InventoryCostAllocation.out_transaction_id.asc(),
                          InventoryCostAllocation.id.asc())
            )
            for r in db.session.execute(alloc_stmt).mappings().all():
                allocs_by_tx.setdefault(r["out_transaction_id"], []).append(dict(r))

        total_qty = sum((abs(l["quantity_delta"]) for l in lines),
                        Decimal("0"))
        total_cost = sum((l["total_cost"] or Decimal("0") for l in lines),
                         Decimal("0"))

        return {
            "document": {
                "id": doc.id,
                "document_no": doc.document_no,
                "document_type": doc.document_type,
                "document_time": doc.document_time,
                "status": doc.status,
                "remark": doc.remark,
                "created_at": doc.created_at,
                "confirmed_at": doc.confirmed_at,
            },
            "lines": lines,
            "allocations_map": allocs_by_tx,
            "total_qty": total_qty,
            "total_cost": _q6(total_cost),
        }

    @staticmethod
    def list_purchase_in_documents(keyword=None, status=None, inbound_type=None,
                                    date_from=None, date_to=None,
                                    page=1, page_size=50):
        """
        入库单据列表。
        inbound_type ∈ None / 'PURCHASE_IN' / 'STOCKTAKE_GAIN' / 'RETURN_IN'
        """
        stmt = (
            select(
                InventoryDocument.id,
                InventoryDocument.document_no,
                InventoryDocument.document_type,
                InventoryDocument.document_time,
                InventoryDocument.status,
                InventoryDocument.remark,
                InventoryDocument.created_at,
                InventoryDocument.confirmed_at,
                func.count(InventoryTransaction.id).label("line_count"),
                func.coalesce(
                    func.sum(InventoryTransaction.quantity_delta), 0
                ).label("total_qty"),
                func.coalesce(
                    func.sum(InventoryTransaction.total_cost), 0
                ).label("total_cost"),
            )
            .join(InventoryTransaction,
                InventoryTransaction.document_id == InventoryDocument.id)
            .where(
                InventoryDocument.document_type.in_(["PURCHASE_IN", "STOCKTAKE"]),
                InventoryTransaction.transaction_type.in_(
                    ["PURCHASE_IN", "STOCKTAKE_GAIN", "RETURN_IN"]
                ),
            )
            .group_by(
                InventoryDocument.id, InventoryDocument.document_no,
                InventoryDocument.document_type, InventoryDocument.document_time,
                InventoryDocument.status, InventoryDocument.remark,
                InventoryDocument.created_at, InventoryDocument.confirmed_at,
            )
            .order_by(InventoryDocument.id.desc())
        )
        if keyword:
            stmt = stmt.where(InventoryDocument.document_no.like(f"%{keyword}%"))
        if status:
            stmt = stmt.where(InventoryDocument.status == status)
        if date_from:
            stmt = stmt.where(InventoryDocument.document_time >= date_from)
        if date_to:
            stmt = stmt.where(InventoryDocument.document_time < date_to)
        # 按 transaction_type 过滤：若 inbound_type 不是 None，要反查该单据是否含有该流水
        if inbound_type:
            subq = (
                select(InventoryTransaction.document_id)
                .where(InventoryTransaction.transaction_type == inbound_type)
                .subquery()
            )
            stmt = stmt.where(InventoryDocument.id.in_(select(subq.c.document_id)))

        total = db.session.execute(
            select(func.count()).select_from(stmt.subquery())
        ).scalar() or 0
        stmt = stmt.offset((page - 1) * page_size).limit(page_size)
        rows = [dict(r) for r in db.session.execute(stmt).mappings().all()]
        return rows, total

    @staticmethod
    def get_purchase_in_detail(document_id: int):
        """
        入库单详情（复用出库详情结构）。
        """
        doc = db.session.get(InventoryDocument, document_id)
        if not doc:
            return None
        if doc.document_type not in ("PURCHASE_IN", "STOCKTAKE"):
            return None

        # 明细行
        line_stmt = (
            select(
                InventoryTransaction.id.label("transaction_id"),
                InventoryTransaction.line_no,
                InventoryTransaction.transaction_type,
                InventoryTransaction.transaction_time,
                InventoryTransaction.quantity_delta,
                InventoryTransaction.unit_cost,
                InventoryTransaction.total_cost,
                Material.id.label("material_id"),
                Material.material_code,
                Material.material_name,
                Material.unit,
                Warehouse.id.label("warehouse_id"),
                Warehouse.warehouse_code,
                Warehouse.warehouse_name,
                Supplier.id.label("supplier_id"),
                Supplier.name.label("supplier_name"),
            )
            .join(Material, Material.id == InventoryTransaction.material_id)
            .join(Warehouse, Warehouse.id == InventoryTransaction.warehouse_id)
            .join(Supplier, Supplier.id == InventoryTransaction.supplier_id,
                isouter=True)
            .where(InventoryTransaction.document_id == document_id)
            .order_by(InventoryTransaction.line_no.asc())
        )
        lines = [dict(r) for r in db.session.execute(line_stmt).mappings().all()]

        # 成本层（由 source_transaction_id 关联）
        tx_ids = [l["transaction_id"] for l in lines]
        layers_by_tx = {}
        if tx_ids:
            layer_stmt = (
                select(
                    InventoryCostLayer.source_transaction_id,
                    InventoryCostLayer.id.label("layer_id"),
                    InventoryCostLayer.layer_type,
                    InventoryCostLayer.effective_time,
                    InventoryCostLayer.original_qty,
                    InventoryCostLayer.remaining_qty,
                    InventoryCostLayer.unit_cost,
                    InventoryCostLayer.original_cost,
                )
                .where(InventoryCostLayer.source_transaction_id.in_(tx_ids))
                .order_by(InventoryCostLayer.source_transaction_id.asc(),
                        InventoryCostLayer.id.asc())
            )
            for r in db.session.execute(layer_stmt).mappings().all():
                layers_by_tx.setdefault(r["source_transaction_id"], []).append(dict(r))

        total_qty = sum((l["quantity_delta"] for l in lines), Decimal("0"))
        total_cost = sum((l["total_cost"] or Decimal("0") for l in lines),
                        Decimal("0"))

        return {
            "document": {
                "id": doc.id,
                "document_no": doc.document_no,
                "document_type": doc.document_type,
                "document_time": doc.document_time,
                "status": doc.status,
                "remark": doc.remark,
                "created_at": doc.created_at,
                "confirmed_at": doc.confirmed_at,
            },
            "lines": lines,
            "layers_map": layers_by_tx,
            "total_qty": total_qty,
            "total_cost": _q6(total_cost),
        }