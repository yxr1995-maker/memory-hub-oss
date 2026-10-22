# 自动学习闭环：阶段三（质量与积压治理）实现记录

前置：阶段一（采集/导入/调度/回执）、阶段二（可纠错替代）已完成。

## 本阶段改动

1. 退役端点清理：memory_worker / memory_relations / distill.sh / status.sh 的
   OPENCODEX_URL 默认端口改成本机网关端口；默认模型对齐 group/route。
2. 检索评测基线（scripts/automation_core/learning_eval.py）：
   抽样标题命中率、drafts note 计数、deprecated 无后继计数、拒答探针。
   基线存 ~/.memory-hub/learning-eval-baseline.json，供后续对比。
3. 队列语义修正：批量处理中未被任何提案引用的行 → filtered/no_proposal，
   不再计为候选。回填生产库：77,001 → 7 条真实候选。
   test_memory_worker 相应契约更新（网关不调用、最新轮可见、真实候选持久化保留）。
4. 回归修复：
   - service.fts 增加 CJK 回退，恢复钩子子类 _ngram_match 的 OR 三元组契约
     （test_recall_question_matches_partial_chinese）。
   - indexer 恢复 drafts 入索引：全链路契约要求蒸馏草稿可检索
     （test_run_apply_full_chain_and_searchable）。噪声治理改为排序层：
     type note ×0.3 + curated 类型保留半数候选位（已有机制，勿再索引排除）。

## 验证

- 全套测试 482 通过（含新增 learning_eval 3 项、supersede 4 项、learning_loop 等）。
- 生产索引重建后（15,216 页含 drafts），重跑评测基线确认命中率未回退。

## 边界

- 7 条真实候选保留待人工/后续复核。
- 程序性经验提炼、自动提示词优化仍未开放，需要运行期数据积累后再评估。
