import json
import os
from pathlib import Path
from sqlalchemy import create_engine, inspect
from dotenv import load_dotenv

load_dotenv()
url = os.getenv("DATABASE_URL")
if not url:
    raise SystemExit("请先在 .env 中配置 DATABASE_URL")

engine = create_engine(url)
inspector = inspect(engine)
output = {"tables": []}

for table_name in sorted(inspector.get_table_names()):
    output["tables"].append({
        "name": table_name,
        "columns": [{
            "name": c["name"],
            "type": str(c["type"]),
            "nullable": c.get("nullable", True),
            "default": str(c.get("default")) if c.get("default") is not None else None,
            "primary_key": c["name"] in inspector.get_pk_constraint(table_name).get("constrained_columns", []),
            "comment": c.get("comment"),
        } for c in inspector.get_columns(table_name)],
        "primary_key": inspector.get_pk_constraint(table_name),
        "foreign_keys": inspector.get_foreign_keys(table_name),
        "indexes": inspector.get_indexes(table_name),
        "unique_constraints": inspector.get_unique_constraints(table_name),
    })

path = Path("build/schema.json")
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"已生成 {path}，共 {len(output['tables'])} 张表")
