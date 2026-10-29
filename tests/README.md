建议测试覆盖：
- 未登录访问 /admin 会跳转登录页
- 不在 TABLES 白名单中的表返回 404
- readonly 表 POST 返回 403
- 原料编码唯一约束冲突时回滚并提示
- 仓库外键下拉选项来自 factories / employees
- 库存单据确认占位入口不改变 status、库存流水和成本层
