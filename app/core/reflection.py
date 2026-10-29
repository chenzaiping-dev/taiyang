from sqlalchemy import MetaData, Table, inspect
from app.extensions import db

metadata = MetaData()
tables = {}


def reflect_database(database):
    # 使用现有数据库表结构；此操作只读取元数据，不执行 create_all()
    metadata.reflect(bind=database.engine)
    tables.clear()
    tables.update(metadata.tables)


def get_table(name):
    if name not in tables:
        raise KeyError(f"未注册或不存在的表: {name}")
    return tables[name]


def get_pk_column(table):
    pk = list(table.primary_key.columns)
    if len(pk) != 1:
        raise ValueError(f"当前通用 CRUD 仅支持单字段主键: {table.name}")
    return pk[0]


def get_fk_for_column(table, column_name):
    for fk in table.foreign_keys:
        if fk.parent.name == column_name:
            return fk.column.table.name, fk.column.name
    return None
