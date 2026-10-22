# 阶段六：程序性记忆与矛盾扫描

## 1. 矛盾扫描（conflict_scan.py）

发布路径已有冲突检查（`content_relation` / `semantic_relation` + `supersede_plan`），
但那只管**新声明**。本模块管另一半：几周前分别发布、如今互相矛盾的页面。

**只报告，不改写**——自动裁决矛盾等于猜测哪边是真的，猜错会静默污染检索。

### 迭代过程与真实数据教训

| 版本 | 规则 | 真实扫描结果 |
|---|---|---|
| v1 | `端口[^0-9]{0,4}(\d+)`、`模型\s*(\w+)` | 100 组 `uses` 冲突，全是 `skill` vs `Skill` |
| v2 | 删 `uses`（动词匹配任意标识符） | 全是 `port`：`端口 08`（日期）、`37701`（邮编） |
| v3 | 端口限 5 位且 ≥1024 | 换成 `model`：`model pricing`（英文散文） |
| v4 | 要求显式配置上下文 | 全是 `endpoint`：9222 调试 vs 8787 API（同主机不同端口） |
| **v5（保留）** | 只保留 `port` + `model`，均需配置上下文 | **19 组唯一冲突，全部围绕网关端口** |

**关键教训**：区分"配置"与"闲聊"靠的是**词汇**（网关/端口/服务/默认模型），
不是数字范围或标点形式。端点检测被彻底删除——一个页面合法地列出同主机多个服务，
端点不是断言，`port` 规则已覆盖真正要抓的场景。

### 性能

读 15k 文件 48.4s → 走索引 content 列 **12.9s**（index 里已有全文）。

### 产出

真实发现：**31 页仍写着旧网关端口（已退役），真值见源码默认值**。
写入 `~/.memory-hub/reports/conflicts.json`，每日链路自动跑。

## 2. 程序性记忆（sop_extract.py）

陈述性记忆回答"什么是真"，程序性记忆回答"怎么做"。wiki 目前只有前者，
解决过的问题每次都要从原始对话重新推导。

信号：**重复出现且以成功结尾的工具调用链**。出现 1 次是轶事，N 次才是模式。

- 离线确定性：不用网关、不用 LLM
- 频率门槛：`--min-occurrences 3`
- 成功门槛：窗口内任何一步失败则整链作废（那是 bug report，不是 procedure）
- 工具名归一到粗类（shell/read/write/search/web/delegate），跨会话可比

### 两个真实 bug（TDD 抓到）

1. **`queue.source` 不是工具名**：hermes-native 行的 source 存的是
   `file://...#messages.id=N` 溯源 URI，归一化后成了唯一垃圾 token，
   于是每条链都长得一样——排行榜全是 session 路径。修正：工具名取自
   `messages.tool_name`（需要 hermes_capture 一并采集），文本启发式兜底。
2. **链内失败未作废**：窗口 6 跨越失败步骤时，链尾恰好落在成功行上就被误收。

### 采集补强

`hermes_capture` 之前丢弃了工具身份（tool 消息的 content JSON 里只有
output/exit_code）。已在采集 SQL 中带上 `m.tool_name`，并写入
`staging/tool-names.json` 供 sop_extract 按 turn_id 查询。

### 当前产出

20000 条工具观测中识别出 2 组重复成功链。样本还小（Hermes 工具身份刚接入），
但链路已通——随着每日采集累积，链条会自然浮现。

## 命令

```bash
memory-hub.sh conflicts              # 配置矛盾扫描（只读）
memory-hub.sh conflicts --json       # 机器可读
memory-hub.sh sops                   # 程序性记忆候选
memory-hub.sh sops --min-occurrences 5 --window 6
```

## 验证

全量测试 546 通过（新增矛盾扫描 12 项、SOP 提取 10 项）。