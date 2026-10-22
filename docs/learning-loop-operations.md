# Memory-hub 自动学习：部署与验收

采集器从当前用户的 Hermes state.db 只读导出 default profile 的直接会话消息。每条事件保留原生消息 ID、会话 ID、角色、来源时间；私密凭据会经过现有 clean_text 脱敏。其他 profile、群聊、子代理记录不纳入默认采集。

导入器把观察事件写入现有 codex-memory.db 队列，输入游标和队列必须在同一事务提交。重复执行不会重复入队；半行保留等待下一轮，坏行或缺少来源进入隔离回执。

发布继续使用现有 memory_worker：明确证据、作用域与冲突检查 → Git 精确提交 → 索引刷新 → 检索验收。candidate 表示待审核，不等于已学习；failed、busy、disabled、pending_index 分别记录，原始记录保留。

调度规范：单轮有超时，每5分钟调用 scripts/learning_tick.py。运行状态写入 ~/.memory-hub/learning-schedule-state.json；learning_loop 的逐轮回执用于审计。运行脚本以 Git 仓库版本为准，agent-cron 仅保留转发入口。launchd 使用显式模型网关环境变量，不能依赖 .zshrc。

验收要区分：
- fixture/mock 测试：证明事务、恢复、发布和索引机制。
- 实际采集：证明源库访问、真实ID与队列导入。
- 真实模型：允许返回空候选，只有符合证据门槛并入库可检索的结果才计为发布。

本阶段不自动重置历史 candidate/failed，不自动替换冲突知识，不把来源不明的旧日志ID补造出来。
