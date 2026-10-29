"""Generate independent SQLAlchemy models, Flask controllers and Jinja templates.

Run from project root:
    python -m codegen.generator
    python -m codegen.generator --check
"""
import argparse
import json
import keyword
import re
import shutil
import inflect

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

_inflect = inflect.engine()
ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "build" / "schema.json"
TEMPLATE_DIR = ROOT / "codegen" / "templates"
OUT = ROOT / "app" / "generated"

env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    undefined=StrictUndefined,
    autoescape=False,
    trim_blocks=True,
    lstrip_blocks=True,
)

def table_to_class_name(table_name: str) -> str:
    """将复数数据库表名转换为单数 Python 类名。"""
    # inventory_transactions -> inventory transactions
    words = table_name.lower().split("_")

    # 仅转换最后一个单词，保留前面的业务修饰词
    words[-1] = _inflect.singular_noun(words[-1]) or words[-1]

    # inventory transactions -> InventoryTransaction
    return "".join(word.capitalize() for word in words)

def snake_to_pascal(value):
    return "".join(part.capitalize() for part in value.split("_"))


def safe_identifier(value):
    value = re.sub(r"\W", "_", value)
    if not value or value[0].isdigit():
        value = "_" + value
    if keyword.iskeyword(value):
        value += "_"
    return value


def type_expression(sql_type):
    """Map schema.json SQL type strings to SQLAlchemy type expressions."""
    t = sql_type.lower()
    if "bigint" in t:
        return "db.BigInteger"
    if "smallint" in t:
        return "db.SmallInteger"
    if "tinyint" in t and "bool" in t:
        return "db.Boolean"
    if "tinyint" in t:
        return "db.SmallInteger"
    if "integer" in t or t.startswith("int"):
        return "db.Integer"
    if "decimal" in t or "numeric" in t:
        m = re.search(r"\((\d+)\s*,\s*(\d+)\)", t)
        return f"db.Numeric({m.group(1)}, {m.group(2)})" if m else "db.Numeric"
    if "datetime" in t or "timestamp" in t:
        return "db.DateTime"
    if re.search(r"\bdate\b", t):
        return "db.Date"
    if "time" in t:
        return "db.Time"
    if "json" in t:
        return "db.JSON"
    if "text" in t:
        return "db.Text"
    if "enum" in t:
        # Keep as string until project-specific enum constraints are added.
        return "db.String(255)"
    m = re.search(r"\((\d+)\)", t)
    if "char" in t and m:
        return f"db.String({m.group(1)})"
    if "char" in t:
        return "db.String(255)"
    if "float" in t:
        return "db.Float"
    if "double" in t:
        return "db.Float"
    return "db.String(255)"


def python_kind(sql_type):
    t = sql_type.lower()
    if any(x in t for x in ("int", "tinyint", "smallint", "bigint")):
        return "int"
    if "decimal" in t or "numeric" in t or "float" in t or "double" in t:
        return "decimal"
    if "datetime" in t or "timestamp" in t:
        return "datetime"
    if re.search(r"\bdate\b", t):
        return "date"
    if "bool" in t:
        return "bool"
    return "str"


def load_schema():
    if not SCHEMA_PATH.exists():
        raise SystemExit(f"找不到 {SCHEMA_PATH}，请先运行 python -m codegen.introspect")
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="只检查 schema 和配置，不写文件")
    args = parser.parse_args()

    # Import table config only after project root is added to sys.path.
    import sys
    sys.path.insert(0, str(ROOT))
    from app.core.table_config import TABLES

    schema = load_schema()
    tables_by_name = {item["name"]: item for item in schema["tables"]}
    missing = sorted(set(TABLES) - set(tables_by_name))
    if missing:
        raise SystemExit("TABLES 中配置了但 schema.json 不存在的表: " + ", ".join(missing))

    plan = []
    for table_name, cfg in TABLES.items():
        if cfg.get("mode") not in {"crud", "readonly", "document"}:
            continue
        table = tables_by_name[table_name]
        columns = table["columns"]
        pk_columns = table.get("primary_key", {}).get("constrained_columns", [])
        if len(pk_columns) != 1:
            print(f"跳过 {table_name}: 当前生成器仅支持单字段主键")
            continue
        pk_field = pk_columns[0]
        col_names = {c["name"] for c in columns}
        display_fields = [f for f in cfg.get("list_fields", []) if f in col_names]
        form_fields = [f for f in cfg.get("form_fields", []) if f in col_names]
        search_fields = [f for f in cfg.get("search_fields", []) if f in col_names]
        mode = cfg["mode"]
        if mode != "crud":
            form_fields = []
        class_name = table_to_class_name(table_name)
        module_name = safe_identifier(table_name)
        endpoint_prefix = f"gen_{module_name}"
        field_labels = {
            c["name"]: (c.get("comment") or c["name"])
            for c in columns
        }
        model_columns = []
        for c in columns:
            name = safe_identifier(c["name"])
            model_columns.append({
                "name": name,
                "type_expr": type_expression(c["type"]),
                "primary_key": c["name"] in pk_columns,
                "nullable": c.get("nullable", True),
                "autoincrement": None,
            })
        plan.append({
            "table_name": table_name, "cfg": cfg, "table": table,
            "class_name": class_name, "module_name": module_name,
            "blueprint_name": endpoint_prefix,
            "endpoint_prefix": endpoint_prefix,
            "url_prefix": f"/generated/{table_name.replace('_', '-')}",
            "pk_field": pk_field,
            "display_fields": display_fields,
            "form_fields": form_fields,
            "search_fields": search_fields,
            "field_labels": field_labels,
            "mode": mode,
            "model_columns": model_columns,
            "columns": model_columns,
            "label": cfg.get("label", table_name),
        })

    if args.check:
        print(f"检查通过：{len(plan)} 张表可生成；不会写入文件。")
        return

    # Only clear generated code, never hand-written services/controllers.
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "models").mkdir(parents=True)
    (OUT / "controllers").mkdir(parents=True)
    (OUT / "templates").mkdir(parents=True)

    registry = []
    for item in plan:
        table_name = item["table_name"]
        model_file = OUT / "models" / f"{item['module_name']}.py"
        controller_file = OUT / "controllers" / f"{item['module_name']}.py"
        template_dir = OUT / "templates" / table_name
        template_dir.mkdir(parents=True)

        model_template = env.get_template("model.py.j2")
        model_file.write_text(model_template.render(
            class_name=item["class_name"],
            table_name=table_name,
            columns=item["model_columns"],
        ), encoding="utf-8")

        controller_template = env.get_template("controller.py.j2")
        controller_file.write_text(controller_template.render(
            **item,
            FORM_FIELDS={f: python_kind(next(c["type"] for c in item["table"]["columns"] if c["name"] == f)) for f in item["form_fields"]},
        ).replace(
            f"from app.generated.models.{item['module_name']} import {item['class_name']}",
            f"from app.generated.models.{item['module_name']} import {item['class_name']}\nfrom app.generated.registry_config import TABLE_CONFIGS\nTABLE_CONFIG = TABLE_CONFIGS[{table_name!r}]\nFORM_FIELDS = { {f: python_kind(next(c['type'] for c in item['table']['columns'] if c['name'] == f)) for f in item['form_fields']}!r}\n".rstrip()
        ), encoding="utf-8")

        list_template = env.get_template("list.html.j2")
        list_html = list_template.render(
            label=item["label"], mode=item["mode"],
            display_fields=item["display_fields"], field_labels=item["field_labels"],
            endpoint_prefix=item["endpoint_prefix"], pk_field=item["pk_field"],
        )
        (template_dir / "list.html").write_text(list_html, encoding="utf-8")

        if item["mode"] == "crud":
            form_template = env.get_template("form.html.j2")
            form_html = form_template.render(
                label=item["label"], form_fields=item["form_fields"],
                field_labels=item["field_labels"], endpoint_prefix=item["endpoint_prefix"],
            )
            (template_dir / "form.html").write_text(form_html, encoding="utf-8")

        registry.append({
            "table_name": table_name,
            "module_name": item["module_name"],
            "class_name": item["class_name"],
            "controller_import": f"app.generated.controllers.{item['module_name']}",
            "blueprint_name": item["endpoint_prefix"],
            "mode": item["mode"],
        })

    # Runtime-safe config used by generated controllers.
    config_text = "# Generated file. Do not edit manually.\nTABLE_CONFIGS = " + repr({
        item["table_name"]: {
            "label": item["label"],
            "field_labels": item["field_labels"],
            "mode": item["mode"],
        } for item in plan
    }) + "\n"
    (OUT / "registry_config.py").write_text(config_text, encoding="utf-8")

    (OUT / "__init__.py").write_text("", encoding="utf-8")
    (OUT / "models" / "__init__.py").write_text("", encoding="utf-8")
    (OUT / "controllers" / "__init__.py").write_text("", encoding="utf-8")
    registry_text = "# Generated file. Register these blueprints in app factory.\nGENERATED_BLUEPRINTS = [\n"
    for item in registry:
        registry_text += f"    ({item['controller_import']!r}, {item['blueprint_name']!r}),\n"
    registry_text += "]\n"
    (OUT / "registry.py").write_text(registry_text, encoding="utf-8")
    print(f"生成完成：{len(plan)} 张表 -> {OUT}")


if __name__ == "__main__":
    main()
