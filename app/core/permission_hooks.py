"""Centralized endpoint-to-permission mapping for finished-goods routes."""
from flask import abort, request, session
from app.services.permission_service import has_permission

# Read access is deliberately split by function. Mutating routes have their own
# permissions and are not implicitly granted by a page-view permission.
ENDPOINT_PERMISSIONS = {
    "api_search_process_orders": "finished_goods.inbound.create",
    "inbound_page": "finished_goods.inbound.create",
    "inbound_submit": "finished_goods.inbound.create",
    "outbound_page": "finished_goods.outbound.create",
    "outbound_submit": "finished_goods.outbound.create",
    "warehouses_page": "finished_goods.inventory.view",
    "transactions_page": "finished_goods.transactions.view",
    "io_summary_page": "finished_goods.summary.view",
    "document_list_page": "finished_goods.documents.view",
    "document_detail_page": "finished_goods.documents.view",
    "document_confirm": "finished_goods.documents.confirm",
    "document_edit_page": "finished_goods.documents.edit",
    "document_edit_submit": "finished_goods.documents.edit",
    "document_reverse": "finished_goods.documents.reverse",
}


def register_finished_goods_permission_hooks(bp):
    @bp.before_request
    def enforce_finished_goods_permission():
        if not session.get("logged_in"):
            # API clients receive a status code rather than an HTML redirect.
            if request.path.startswith("/finished-goods/api/"):
                return {"code": 401, "message": "请先登录"}, 401
            from flask import redirect, url_for
            return redirect(url_for("auth.employee_login", next=request.path))

        from app.services.permission_service import is_super_admin
        if is_super_admin():
            return None

        endpoint = request.endpoint.rsplit(".", 1)[-1]
        permission_code = ENDPOINT_PERMISSIONS.get(endpoint)
        if not permission_code:
            abort(403)
        if not has_permission(permission_code):
            abort(403)
