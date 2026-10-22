# 自动学习闭环第一阶段实现计划

1. 原生 Hermes 消息只读导出：default profile、直接会话、真实消息主键，脱敏；append+fsync 后推进游标。测试 profile 边界、角色、失败和轮换。
2. staging 导入现有 codex-memory.db：独立学习游标/来源回执表，游标和queue在同事务提交；完整行处理、幂等、坏行隔离、flock互斥。
3. 复用 memory_worker：保留证据门槛、冲突候选、发布事务与pending_index恢复；输出持久逐轮收据。测试真实临时Git知识库发布和检索，模型提案用明确fixture。
4. 调度脚本 scripts/learning_tick.py：采集45秒/整理180秒上限，分阶段回执；复用launchd现有worker label，5分钟间隔，显式网关环境。
5. 验收：81项相关测试，真实Hermes有限采集/导入/重复运行，生产worker返回和launchd实际执行记录。来源不明旧记录隔离，不造ID、不自动reset candidate。

阶段边界：完成自动采集/导入/处理/回执与恢复基础设施；自动冲突替代、程序性经验提炼、自动策略优化属于后续阶段，需要新的评测门槛。
