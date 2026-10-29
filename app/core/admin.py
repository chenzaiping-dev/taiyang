from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from sqlalchemy import select, or_, cast, String
from sqlalchemy.exc import IntegrityError
from app.core.auth import login_required, admin_required
from app.core.reflection import get_table, get_pk_column, get_fk_for_column
from app.core.table_config import TABLES
from app.extensions import db

admin_bp = Blueprint("admin", __name__)
#admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

@admin_bp.get("/admin")
@login_required
@admin_required
def dashboard():
    groups = {}
    for name, cfg in TABLES.items():
        groups.setdefault(cfg.get("module", "其他"), []).append((name, cfg))
    return render_template("admin/dashboard.html", groups=groups)


def get_config(table_name):
    cfg = TABLES.get(table_name)
    if not cfg:
        abort(404)
    table = get_table(table_name)
    if cfg.get("mode") not in {"crud", "readonly", "document"}:
        abort(403)
    return table, cfg


@admin_bp.route("/admin/t/<table_name>", methods=["GET", "POST"])
@login_required
@admin_required
def table_list(table_name):
    table, cfg = get_config(table_name)
    if request.method == "POST":
        if cfg.get("mode") != "crud" or not cfg.get("can_create"):
            abort(403)
        values = parse_form_values(table, cfg, request.form)
        try:
            db.session.execute(table.insert().values(**values))
            db.session.commit()
            flash("新增成功。", "success")
            return redirect(url_for("admin.table_list", table_name=table_name))
        except IntegrityError:
            db.session.rollback()
            flash("保存失败：可能存在唯一键冲突或数据不符合约束。", "danger")
        except Exception:
            db.session.rollback()
            raise

    query = select(table)
    keyword = request.args.get("keyword", "").strip()
    search_fields = cfg.get("search_fields", [])
    if keyword and search_fields:
        conditions = []
        for field in search_fields:
            if field in table.c:
                conditions.append(cast(table.c[field], String).like(f"%{keyword}%"))
        if conditions:
            query = query.where(or_(*conditions))
    pk = get_pk_column(table)
    query = query.order_by(pk.desc()).limit(200)
    rows = db.session.execute(query).mappings().all()
    fields = [f for f in cfg.get("list_fields", []) if f in table.c]
    form_fields = [f for f in cfg.get("form_fields", []) if f in table.c]
    fk_options = build_fk_options(table, form_fields)
    return render_template(
        "admin/table_list.html", table_name=table_name, cfg=cfg, table=table,
        rows=rows, fields=fields, form_fields=form_fields, keyword=keyword,
        fk_options=fk_options, pk_name=pk.name,
    )


@admin_bp.route("/admin/t/<table_name>/<int:row_id>/edit", methods=["GET", "POST"])
@login_required
@admin_required
def table_edit(table_name, row_id):
    table, cfg = get_config(table_name)
    if cfg.get("mode") != "crud" or not cfg.get("can_edit"):
        abort(403)
    pk = get_pk_column(table)
    row = db.session.execute(select(table).where(pk == row_id)).mappings().first()
    if row is None:
        abort(404)
    form_fields = [f for f in cfg.get("form_fields", []) if f in table.c]
    if request.method == "POST":
        values = parse_form_values(table, cfg, request.form)
        try:
            db.session.execute(table.update().where(pk == row_id).values(**values))
            db.session.commit()
            flash("修改成功。", "success")
            return redirect(url_for("admin.table_list", table_name=table_name))
        except IntegrityError:
            db.session.rollback()
            flash("保存失败：可能存在唯一键冲突或数据不符合约束。", "danger")
    return render_template(
        "admin/table_edit.html", table_name=table_name, cfg=cfg, table=table,
        row=row, form_fields=form_fields, fk_options=build_fk_options(table, form_fields),
        pk_name=pk.name,
    )


def parse_form_values(table, cfg, form):
    values = {}
    for name in cfg.get("form_fields", []):
        if name not in table.c:
            continue
        column = table.c[name]
        raw = form.get(name)
        if raw is None:
            continue
        if raw == "":
            print(f"字段 {name} 为空，设置为 None")
            values[name] = None
            continue
        type_name = type(column.type).__name__.lower()
        try:
            if "int" in type_name:
                values[name] = int(raw)
            elif "numeric" in type_name or "decimal" in type_name:
                from decimal import Decimal
                values[name] = Decimal(raw)
            elif "bool" in type_name:
                #print("type_name:", type_name, "name:", name, "raw value:", raw)
                values[name] = int(raw)
            elif "date" in type_name and "time" not in type_name:
                from datetime import date
                values[name] = date.fromisoformat(raw)
            elif "datetime" in type_name or "timestamp" in type_name:
                from datetime import datetime
                values[name] = datetime.fromisoformat(raw)
            else:
                values[name] = raw.strip() if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            raise ValueError(f"字段 {name} 的值无效")
    print(f"Parsed form values for table {table.name}: {values}")
    return values


def build_fk_options(table, field_names):
    options = {}
    for name in field_names:
        if name not in table.c:
            continue
        fk_info = get_fk_for_column(table, name)
        if not fk_info:
            continue
        target_name, target_col = fk_info
        target = get_table(target_name)
        # Use the first human-readable field if available, otherwise target PK.
        display = next((n for n in ("name", "factory_name", "warehouse_name", "real_name", "username", "material_name", "batch_no") if n in target.c), target_col)
        rows = db.session.execute(select(target.c[target_col], target.c[display]).limit(500)).all()
        options[name] = [{"value": r[0], "label": f"{r[1]} (ID {r[0]})"} for r in rows]
    return options
