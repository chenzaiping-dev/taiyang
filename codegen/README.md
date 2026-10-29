# Jinja2 独立文件代码生成扩展

## 1. 安装依赖
在项目 requirements.txt 增加 `Jinja2>=3.1,<4`（Flask 已依赖 Jinja2，通常无需额外安装）。

## 2. 复制文件
把本目录中的 `codegen/templates/` 和 `codegen/generator.py`、`codegen/config.py` 复制到原项目的 `codegen/` 目录。
保留原有 `codegen/introspect.py` 和 `app/core/table_config.py`。

## 3. 检查配置
确保 `app/core/table_config.py` 的 TABLES 字段与当前 schema.json 一致：
- `mode`: crud / readonly / document
- `list_fields`: 列表展示字段
- `form_fields`: 允许编辑字段（只对 crud 生效）
- `search_fields`: 搜索字段

先运行：
```bash
python -m codegen.generator --check
```

## 4. 生成文件
```bash
python -m codegen.generator
```

生成到 `app/generated/`。该目录是生成器专属目录，运行生成器会清空并重建它；不要把手写代码放进去。

## 5. 注册 Blueprint
生成器输出 `app/generated/registry.py`。在 `app/__init__.py` 的 create_app() 中、reflect_database() 之后加入：

```python
from app.generated.registry import GENERATED_BLUEPRINTS

for module_path, expected_name in GENERATED_BLUEPRINTS:
    module = __import__(module_path, fromlist=["bp"])
    app.register_blueprint(module.bp)
```

如果你的项目已经有同名通用 CRUD 路由，请先避免 URL 冲突，或停用旧路由。

## 6. 模板目录
Flask 默认会从项目 `templates/` 查找模板。建议在 app/__init__.py 中增加生成模板目录搜索路径，或者将 `app/generated/templates/<table>/` 复制/输出到项目 `templates/generated/<table>/`。
最简单做法：修改 generator.py 中的输出路径，把 `OUT / "templates"` 改成 `ROOT / "templates" / "generated"`，并确认不清理整个 templates/generated 以外的目录。

## 重要限制
这个扩展是可继续完善的脚手架，不是完整生产 CRUD：
- 仅支持单字段主键；联合主键表会跳过。
- 生成的 ORM 字段类型映射是近似映射；请复核 ENUM、UNSIGNED、server_default、ON UPDATE、索引和外键。
- 生成器当前模板的新增/编辑入口是基础示例，正式使用前要加登录/角色权限、CSRF、字段白名单、业务校验和审计日志。
- 库存流水、成本层、成本分配等表只能只读；不要把它们配置成 crud。
- 不要对生产库直接测试生成页面。
