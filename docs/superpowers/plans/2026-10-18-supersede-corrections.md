# 自动学习闭环：阶段二（可纠错）实现计划

阶段一已完成：原生采集、持久化导入、调度与回执。
阶段二目标：让知识可以被新证据纠正，而不是冲突后永久待审核。

1. RED：4 条测试（tests/test_memory_supersede.py）
   - 更新的显式用户声明 → 发布新页 + 旧机器记忆页标记 deprecated 并写 deprecated_by 链接。
   - 与人工整理页（concepts/ 等）冲突 → 保持候选，页面不动。
   - 声明日期不新于旧页 → 保持候选。
   - 语义冲突（确定性判定为 new）→ 同样可替代；检索解析到新页。
2. GREEN：memory_worker.supersede_plan 确定性门槛
   - 仅 preference/decision；证据全部来自 user 角色；
   - 旧页必须是机器生成（decisions/memory-*），人工整理页永不自动作废；
   - 作用域一致（project 相同，或同为 global-user）；旧页未作废；
   - 新声明日期严格晚于旧页 updated/created。
3. 发布集成：prepared 增加 superseded 条目，写入 status/deprecated_by/superseded_at；
   published 只统计新页；作废条目单独校验 frontmatter；交易与索引沿用既有机制。
4. 回归：104 项通过（supersede/worker/relations/learning_loop/hermes_capture/tick/ranker/successor/recall）。
5. 生产验证：learning_loop --once 正常返回 noop，候选与隔离计数如实记录；本轮无符合条件的替代。

后续阶段（未做）：候选积压分批复核、程序性经验提炼、发布质量评测集、自动策略优化。
