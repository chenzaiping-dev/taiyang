from decimal import Decimal
from sqlalchemy import select, case, func, or_

from app.extensions import db
from app.generated.models.employees import Employee
from app.generated.models.materials import Material
from app.generated.models.warehouses import Warehouse
from app.generated.models.suppliers import Supplier

from app.generated.models.inventory_documents import InventoryDocument
from app.generated.models.inventory_transactions import InventoryTransaction
from app.generated.models.inventory_cost_layers import InventoryCostLayer

from app.generated.models.process_orders import ProcessOrder

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
                (cost / qty).quantize(Decimal("0.000001"))
                if qty > 0 else Decimal("0")
            )
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