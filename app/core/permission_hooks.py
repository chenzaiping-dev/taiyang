"""Centralized backend RBAC for all modules.

Every endpoint in a managed blueprint must be explicitly mapped. Unknown endpoints
fail closed, so newly-added routes cannot accidentally become public.
"""
from flask import abort, jsonify, redirect, request, session, url_for

from app.services.permission_service import has_permission, is_super_admin, get_data_scopes

# endpoint -> all required permission codes
ENDPOINT_REQUIREMENTS = {
    # Raw-material inventory / procurement
    "inventory_query.material_stock_page": ("inventory.stock.view",),
    "inventory_query.material_stock_with_cost_layers_page": ("inventory.cost.view",),
    "inventory_query.material_io_summary_page": ("inventory.summary.view",),
    "inventory_query.material_transactions_page": ("inventory.transactions.view",),
    "inventory_query.document_list_page": ("inventory.documents.view",),
    "inventory_query.document_detail_page": ("inventory.documents.view",),
    "inventory_query.stock_out_confirm": ("inventory.stock_out.confirm",),
    "inventory_query.purchase_in_confirm": ("inventory.purchase_in.confirm",),

    "purchase_in.api_purchase_in": ("inventory.purchase_in.create", "inventory.cost.edit"),
    "purchase_in.purchase_in_page": ("inventory.purchase_in.create", "inventory.cost.edit"),
    "purchase_in.purchase_in_submit": ("inventory.purchase_in.create", "inventory.cost.edit"),
    "purchase_in.purchase_in_edit_page": ("inventory.purchase_in.edit", "inventory.cost.edit"),
    "purchase_in.purchase_in_edit_submit": ("inventory.purchase_in.edit", "inventory.cost.edit"),

    "stock_out.api_stock_out": ("inventory.stock_out.create",),
    "stock_out.api_confirm": ("inventory.stock_out.confirm",),
    "stock_out.stock_out_page": ("inventory.stock_out.create",),
    "stock_out.stock_out_submit": ("inventory.stock_out.create",),
    "stock_out.stock_out_edit_page": ("inventory.stock_out.edit",),
    "stock_out.stock_out_edit_submit": ("inventory.stock_out.edit",),

    # Finished_goods
    "finished_goods.api_search_process_orders": ("finished_goods.inbound.create",),
    "finished_goods.inbound_page": ("finished_goods.inbound.create",),
    "finished_goods.inbound_submit": ("finished_goods.inbound.create",),
    "finished_goods.outbound_page": ("finished_goods.outbound.create",),
    "finished_goods.outbound_submit": ("finished_goods.outbound.create",),
    "finished_goods.warehouses_page": ("finished_goods.inventory.view",),
    "finished_goods.transactions_page": ("finished_goods.transactions.view",),
    "finished_goods.io_summary_page": ("finished_goods.summary.view",),
    "finished_goods.document_list_page": ("finished_goods.documents.view",),
    "finished_goods.document_detail_page": ("finished_goods.documents.view",),
    "finished_goods.document_confirm": ("finished_goods.documents.confirm",),
    "finished_goods.document_edit_page": ("finished_goods.documents.edit",),
    "finished_goods.document_edit_submit": ("finished_goods.documents.edit",),
    "finished_goods.document_reverse": ("finished_goods.documents.reverse",),

    # Process orders
    "process_order.list_page": ("process_order.view",),
    "process_order.detail_page": ("process_order.view",),
    "process_order.api_create": ("process_order.create",),
    "process_order.new_page": ("process_order.create",),
    "process_order.create_submit": ("process_order.create",),
    "process_order.edit_page": ("process_order.edit",),
    "process_order.update_submit": ("process_order.edit",),
    "process_order.place_order": ("process_order.place",),
    "process_order.produce": ("process_order.produce",),
    "process_order.receive": ("process_order.receive",),
    "process_order.cancel": ("process_order.cancel",),
    "process_order.api_change_status": ("process_order.status.change",),
    "process_order.adjust_materials": ("process_order.materials.adjust",),
    "process_order.adjust_materials_page": ("process_order.materials.adjust",),
    "process_order.adjustments_list_page": ("process_order.adjustments.view",),
    "process_order.api_get_order_materials": ("process_order.view",),

    # Production plans
    "production_plan.new_page": ("production_plan.create",),
    "production_plan.create_submit": ("production_plan.create",),
    "production_plan.edit_page": ("production_plan.edit",),
    "production_plan.update_submit": ("production_plan.edit",),
    "production_plan.list_page": ("production_plan.view",),
    "production_plan.detail_page": ("production_plan.view",),
    "production_plan.confirm_plan": ("production_plan.confirm",),
    "production_plan.process_plan": ("production_plan.process",),
    "production_plan.complete_plan": ("production_plan.complete",),
    "production_plan.cancel_plan": ("production_plan.cancel",),
    "production_plan.reopen_plan": ("production_plan.reopen",),

    # Shipment plans
    "shipment_plan.new_page": ("shipment.create",),
    "shipment_plan.create_submit": ("shipment.create",),
    "shipment_plan.edit_page": ("shipment.edit",),
    "shipment_plan.update_submit": ("shipment.edit",),
    "shipment_plan.list_page": ("shipment.view",),
    "shipment_plan.detail_page": ("shipment.view",),
    "shipment_plan.ship_submit": ("shipment.ship",),
    "shipment_plan.mark_planned": ("shipment.confirm",),
    "shipment_plan.cancel_plan": ("shipment.cancel",),
    "shipment_plan.ship_plan": ("shipment.ship",),

    # Sales performance
    "sales_performance.list_page": ("sales_performance.view",),
    "sales_performance.new_page": ("sales_performance.create",),
    "sales_performance.create_submit": ("sales_performance.create",),
    "sales_performance.edit_page": ("sales_performance.edit",),
    "sales_performance.update_submit": ("sales_performance.edit",),
    "sales_performance.detail_page": ("sales_performance.view",),
    "sales_performance.change_status": ("sales_performance.status.change",),
    "sales_performance.reopen": ("sales_performance.reopen",),
}

MANAGED_BLUEPRINTS = {
    "inventory_query", "purchase_in", "stock_out", "finished_goods", "process_order",
    "production_plan", "shipment_plan", "sales_performance",
}


def register_permission_hooks(app):
    @app.before_request
    def enforce_permissions():
        endpoint = request.endpoint or ""
        blueprint = endpoint.split(".", 1)[0] if "." in endpoint else ""

        if blueprint not in MANAGED_BLUEPRINTS:
            return None

        # Employee and bootstrap admin must both have an authenticated session.
        if not session.get("logged_in"):
            if request.path.startswith("/inventory/api/"):
                return jsonify({"code": 401, "message": "请先登录"}), 401
            return redirect(url_for("auth.employee_login", next=request.path))

        # Initial password must be changed before any operation.
        if session.get("auth_source") == "employee" and session.get("must_change_password"):
            return redirect(url_for("auth.change_password"))

        requirements = ENDPOINT_REQUIREMENTS.get(endpoint)
        if endpoint == "process_order.api_change_status":
            api_action = (request.view_args or {}).get("action")
            if api_action == "cancel":
                requirements = ("process_order.cancel",)
            elif api_action == "place-order":
                requirements = ("process_order.place",)
            elif api_action == "ship-out":
                requirements = ("process_order.status.change",)
        if requirements is None:
            abort(403)

        if is_super_admin():
            return None

        missing = [code for code in requirements if not has_permission(code)]
        # Fail closed for non-all scopes until each business query has a verified
        # owner/assignment/factory filter. Never silently treat `own` as `all`.
        unsupported_scope = [
            code for code in requirements
            if has_permission(code)
            and (get_data_scopes(code) and "all" not in get_data_scopes(code))
        ]
        if missing or unsupported_scope:
            if request.is_json or request.path.startswith("/inventory/api/"):
                return jsonify({
                    "code": 403,
                    "message": (
                        "没有执行该操作的权限" if missing
                        else "该权限的数据范围尚未接入查询过滤，暂时拒绝访问"
                    ),
                    "required_permissions": missing,
                    "unsupported_data_scope_permissions": unsupported_scope,
                }), 403
            abort(403)
        return None
