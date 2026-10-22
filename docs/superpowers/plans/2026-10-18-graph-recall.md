# 阶段五：图谱召回（借鉴 HippoRAG 的联想检索）

## 调研结论

对标 Mem0 / Zep(Graphiti) / Letta / Cognee / LightRAG / HippoRAG 后，判定本项目**下一步的
瓶颈不在存储能力，而在召回的多跳性**：

- Mem0：事实抽取 + 向量召回，无图遍历。适合个性化，机构知识弱。
- Zep/Graphiti：时序知识图谱，**能力强但代价高**（Mem0 论文实测 Zep 记忆体量超 60 万 token/会话；
  摄入后立即检索常常失败，需等后台建图完成）。
- Letta：Agent 自己管理上下文分层，与本项目「人工策展 + 证据门槛」路线冲突，不采纳。
- LightRAG：naive/local/global/hybrid/mix 五档检索强度。**采纳其"档位"思想**。
- HippoRAG：用 KG 做检索索引而非知识来源，配合 Personalized PageRank 做多跳。

**决定**：不引入 Neo4j 这类外部图库（违背本地优先、且 Zep 的教训是建图成本与实时性冲突），
而是利用**已有的 links 边表**（索引时从 wikilink 构建）实现一跳联想召回 + 三档检索强度。

## 实现

1. `scripts/automation_core/graph_recall.py`：一跳邻域扩展，邻居得分为发现它的命中分数的一半
   （`GRAPH_SCORE_DECAY=0.5`），扇出上限 6（`GRAPH_MAX_NEIGHBORS`），排序确定（按路径名）。
   同页被多个命中发现时保留最高分。
2. `service.py`：作为新召回通道 `original_graph` 注入，种子是 FTS 的原始命中，且排除所有已有
   通道命中过的页（只补充"字面零重叠但主题相邻"的页）。异常一律吞掉，不影响主查询。
3. `ranker.py`：`original_graph` 权重固定 0.30（其余预算不变），因此图召回只能加分、不能盖过字面证据。
4. CLI `search --depth fast|standard|deep`：fast 关闭扩展与向量（最低延迟），standard 为默认，
   deep 预留（与 LightRAG mix 同思路，先占位避免接口破坏）。

## 边界

- 只做一跳，不做 PageRank 迭代：多跳需要独立的迭代预算与终止条件，当前 15k 页规模下收益不明。
- 图召回不参与"证据强度"判定：decision.py 的 min_score/min_relative 门槛会把弱图邻居挡掉，
  这是刻意的——决策不能靠联想投票。

## 有效性实测（2026-10-18，40 样本）

量化口径：`graph_only` = 字面通道（FTS+向量）完全够不到、却因图通道进入结果集的页。

| 指标 | 加护栏前 | 加护栏后 |
|---|---|---|
| graph_only_hits | 67 | 71 |
| graph_only_share | 0.335 | 0.355 |
| literal_hit_rate | 0.925 | 0.925 |

**加 hub 护栏（GRAPH_HUB_DEGREE=32）修的是质量问题，不是数量问题**：修复前查
"SQLite 压缩文本修订史原型" 返回 `ai-agent-hf-intrusion`、`growth-potential`
（随机采样）；修复后同一查询返回 `sqlite-utils.md`、`ai-compute-cost-paradox.md`
（主题相邻）。所以护栏保留。

**但更重要的结论来自真实问题型查询**（8 条，不是我合成的标题）：

```
<网关> 端口是多少             neighbor=0
Hermes 用哪个飞书 app        neighbor=0
怎么防止 agent 重复犯错       neighbor=0
LUNARA 巡检规范              neighbor=0
飞书网关怎么切换到 VPS        neighbor=0
opencodex 为什么 403         neighbor=0
memory-hub 索引重建要多久     neighbor=0
海外仓还是国内直发           neighbor=1
```

**真实使用场景下图通道几乎不生效（1/8），因为字面通道已经够好**。合成标题探针的高
graph_only 份额是"标题即查询"这种人工构造场景的产物，不代表实际使用。

### 决策：不加第二跳

理由：第二跳的前提是"一跳能带来增量"，而实测一跳在真实查询上贡献几乎为零。加第二跳
只会把噪声扩散到两跳远。当前形态（一跳 + hub 护栏 + 0.30 权重）是安全的形状：它在
需要时补位，在不需要时不干扰。

**留存的测量工具**：`scripts/automation_core/graph_eval.py`，任何改动召回策略后都应
重跑——它给出的 graph_only_share 与 literal_hit_rate 就是决策依据。