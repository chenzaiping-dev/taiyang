# 数据库代码生成入口

1. 复制 `.env.example` 为 `.env` 并填写数据库连接。
2. 安装 requirements.txt。
3. 执行 `python -m codegen.introspect`，输出 `build/schema.json`。
4. 在 `app/core/table_config.py` 中配置允许显示/编辑的表和字段。

这个版本先生成结构元数据，再使用统一的反射 CRUD 页面运行。它不会覆盖手写业务 Service。
下一步可以在 `codegen/` 中增加 Jinja2 模板，把 schema.json + TABLES 配置生成独立 Model/Controller 文件。
