from flask import Blueprint, request, send_file, abort
from app.exports.queries import iter_export_rows
from app.exports.base import build_xlsx
from app.exports.definitions.columns import get_columns
from app.services.permission_service import (
    has_permission, can_view_inventory_cost, can_view_finished_goods_cost,
    can_view_shipment_money, can_view_sales_performance_cost,
    can_view_sales_performance_money,
)

bp = Blueprint("exports", __name__, url_prefix="/exports")

CONFIG = {
    "material-stock-with-cost-layers": ("material_stock_layers", "原料FIFO成本层"),
    "material-transactions": ("material_transactions", "原料出入库流水"),
    "finished-goods-warehouses": ("finished_goods_stock", "成品库库存"),
    "finished-goods-transactions": ("finished_goods_transactions", "成品库流水"),
    "shipment-plans": ("shipment_plans", "发货计划"),
    "sales-performances": ("sales_performances", "业绩单"),
}


def _can_view(code):
    if code == "inventory.cost.view":
        return can_view_inventory_cost()
    if code == "finished_goods.cost.view":
        return can_view_finished_goods_cost()
    if code == "shipment.money.view":
        return can_view_shipment_money()
    if code == "sales_performance.money.view":
        return can_view_sales_performance_money()
    if code == "sales_performance.cost.view":
        return can_view_sales_performance_cost()
    return has_permission(code)


@bp.get("/<export_name>.xlsx")
def download(export_name):
    config = CONFIG.get(export_name)
    if not config:
        abort(404)
    kind, title = config
    rows = iter_export_rows(kind, request.args, chunk_size=2000)
    columns = get_columns(kind, _can_view)
    output = build_xlsx(rows, columns, sheet_name=title)
    filename = f"{title}.xlsx"
    return send_file(
        output, as_attachment=True, download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        max_age=0,
    )
