from flask import Blueprint, abort, flash, redirect, render_template, url_for
from sqlalchemy import select
from app.core.auth import login_required, admin_required
from app.core.reflection import get_table, get_pk_column
from app.core.table_config import TABLES
from app.extensions import db

documents_bp = Blueprint("documents", __name__, url_prefix="/documents")


@documents_bp.get("/inventory")
@login_required
@admin_required
def inventory_documents():
    table_name = "inventory_documents"
    cfg = TABLES[table_name]
    table = get_table(table_name)
    pk = get_pk_column(table)
    rows = db.session.execute(
        select(table).order_by(pk.desc()).limit(200)
    ).mappings().all()
    return render_template(
        "documents/inventory_list.html",
        table=table, rows=rows, cfg=cfg,
        fields=[f for f in cfg["list_fields"] if f in table.c],
    )


@documents_bp.post("/inventory/<int:document_id>/confirm")
@login_required
@admin_required
def confirm_inventory_document(document_id):
    # Deliberately NOT implementing stock posting here.
    # Confirmation must later call InventoryService.confirm_document() in one DB transaction.
    table = get_table("inventory_documents")
    pk = get_pk_column(table)
    row = db.session.execute(select(table).where(pk == document_id)).mappings().first()
    if row is None:
        abort(404)
    if row["status"] != "DRAFT":
        flash("只有草稿单据可以进入确认流程。", "warning")
        return redirect(url_for("documents.inventory_documents"))
    flash("当前为框架占位：请先实现库存校验、流水写入、FIFO 成本层更新及幂等控制，再开放确认功能。", "warning")
    return redirect(url_for("documents.inventory_documents"))
