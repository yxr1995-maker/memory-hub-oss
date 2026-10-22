# 状态层升级：KV / 时序图谱 / 衰减 / 跨机同步（阶段四）

对照市面记忆系统（Mem0 原子 CRUD、Zep/Graphiti 时序图谱、Generative Agents 衰减、
Letta Server 多端同步）补四项能力。实现原则：不引入外部服务，全部落在本地 SQLite +
现有 wiki Git 通道上。

## 1. 即时键值层（entity_kv）

- 文件：`scripts/automation_core/state_store.py`，独立 `state.db`（WAL），与索引重建解耦。
- `kv set/get/list/rm` CLI（`memory-hub.sh kv …`），get 自动计 access_count（访问即“复活”）。
- 注入：inject.sh 输出「当前状态（键值速览）」，排除 cold 键。
- MCP：`memory_kv` 工具（plugins/memory-hub/mcp → hub/mcp/server.py）。

## 2. 时序关系图（entity_timeline）

- 边 = (subject, predicate, object, valid_at, invalid_at, superseded_by, evidence_path,
  confidence)。同一谓词可多值。
- `exclusive=True` 表示单值谓词：新值关闭同 (subject,predicate) 的其他开边并回链
  superseded_by。默认 False——历史永不静默作废。
- `timeline add/active/history/at` CLI；`memory_timeline` MCP 工具（when 参数支持
  任意时刻快照查询）。
- 设计要点（TDD 中发现的坑）：
  1) 关闭条件必须按 (subject,predicate) 而非三元组精确匹配，否则旧值不关；
  2) 同秒重录同一三元组会撞主键——改为刷新 evidence/confidence 而非新行；
  3) 索引重建时必须把 entity_kv/entity_timeline 从旧库拷入新库（build_index 的
     source_db 携带逻辑），否则一次重建清空状态。

## 3. 衰减与热度

- `apply_decay(max_inactive_days=180)`：只打 `cold:` 标记，绝不删除；cold 键从注入
  与默认检索中退出。
- get 访问会“复活”（last_accessed 刷新 + access_count 累加）。
- run.sh 每日链路在 maintain 前执行 decay + 导出。

## 4. 跨机同步（state-sync）

- `state-sync export <file>` → JSONL（KV + timeline 全量）。
- `state-sync import <file>`：KV 按 updated 后写胜（last-write-wins），timeline 主键
  去重并集；不删除本地行。
- 每日 run.sh 导出到 `$WIKI_PATH/_state/state.jsonl`，随 wiki Git 提交 → VPS 侧
  `state-sync import` 即可合并。

## 验证

- 新增测试 33 项（state_store 15、state_cli 5 等），全量 514 通过。
- 真机：生产数据目录 kv set/get/list、timeline add/active/at、export 全通；
  inject.sh 显示「当前状态」段落。
- Hermes provider 新增 memoryhub_kv / memoryhub_timeline 工具（只读转发 CLI），
  并修复 decide 的 --json/--top 参数错配（provider 传参曾在运行时直接失败，
  现由 test_provider_shaped_flags_are_accepted 锁定）。

## 每日链路接入（~/agent-cron/memory-hub/run.sh）

- maintain 之前执行：decay 标记 + state-sync export 到 `$WIKI_PATH/_state/state.jsonl`
  （随 maintain 的 wiki 提交一起入库 → VPS 侧 import 即完成跨机同步）。
