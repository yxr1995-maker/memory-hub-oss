# Prepared Offline Evaluation Specification & Verification Record

## 1. Overview & Protocol Specification

- Protocol: experience-prepared-eval-v1
- Harness Module: evaluation/experience_v1/prepared_eval.py
- Fixture Path: evaluation/experience_v1/synthetic_tasks_v1.json
- Unit Test: tests/experience/test_prepared_eval.py
- Primary Objective: Provide a deterministic, leak-free offline evaluation harness for A/B/C conditions across synthetic task categories.

## 2. Fixture Integrity & Frozen Checksums

| File | SHA256 Checksum |
| :--- | :--- |
| evaluation/experience_v1/synthetic_tasks_v1.json | 1cce7d53f2b5e4ab9ec6a65ea85bbdeb528f96c81ecc768037e03910dcec7b49 |
| evaluation/experience_v1/prepared_eval.py | 5bbbefdea004121fb42c837933b00e3bb9b3e5bbe0f26ef358cbe9beb13e1761 |
| tests/experience/test_prepared_eval.py | 1ff70d27afed4e94bd51961e7b27065da659f903dee51bcae44b0e96434035fb |

### Partition Structure
- Train Split: 3 cases (train-dedup-01, train-retry-01, train-sanitize-01)
- Dev Split: 3 cases (dev-dedup-01, dev-retry-01, dev-sanitize-01)
- Test Split: 6 cases across 3 categories (2 cases each):
  1. deduplicate_preserve_order (test-dedup-01, test-dedup-02)
  2. retry_budget_calc (test-retry-01, test-retry-02)
  3. field_sanitization (test-sanitize-01, test-sanitize-02)
- Disjointness Boundary: 不同split完整任务ID、任务文本和参数记录不重叠；算法类别/schema有意复用；个别gold scalar可因独立计算相同（如count=3），不构成样本复制。例如，train-dedup-01 与 test-dedup-01 均算出 count=3，但 train 是从 ["apple", "banana", "apple", "cherry"] 计算得出，test 是从 ["alpha", "beta", "alpha", "gamma", "beta"] 独立计算得出；test-retry-01 参数为 attempt_limit=4, base_timeout_s=3，与 train-retry-01 的 attempt_limit=3, base_timeout_s=5 参数记录严格独立。
- Rule Verifiability: Expected outputs are mathematically computable from transformation specifications rather than arbitrary constant mappings.

## 3. Evaluation Conditions & Budget Contracts

| Condition Group | Name | Description | Test Expected Leakage |
| :--- | :--- | :--- | :--- |
| A | No Memory | Empty memory context provided to adapter | None |
| B | Static Train Memory | Raw demonstration examples from train split only | Strictly disjoint |
| C | Experience Rules Reuse | Distilled operational transformation rules derived from train | Strictly disjoint |

### Contract Parameters & Limitations
- Model Baseline: unverified_real_config (no live LLM invocation)
- Prompt Framework: standard_zero_shot
- Sampling: temperature=0.0, top_p=1.0
- Declared Planned Token Budget: 512 (planning declaration only; not real token measurement)
- Declared Timeout Seconds: 15 (planning declaration only; not live timeout enforcement)
- Measured Latency: Measures wall-clock execution time of local adapter call (time.perf_counter()), not network API round-trip.

## 4. Evaluation Engine Mechanics

1. Payload Sanitization: prepare_adapter_request strips expected and known_error fields. Adapter receives only case_id, category, group, memory, contract, and input.
2. Metric Definitions:
   - success_rate = (exact matches meeting strict_equal) / (total adapter calls in group).
   - known_error_rate = (occurrences matching pre-registered synthetic error patterns) / (total adapter calls in group). Note: This specifically measures matching against registered signature patterns (e.g. duplicate_retained, off_by_one_retries, credential_leak), not all execution errors. Unmatched discrepancies are classified as mismatched_output or adapter_exception.
3. Order Randomization: Shuffles case presentation order per repetition, and shuffles group execution order (A, B, C) per case using deterministic seed to reduce positional and ordering artifacts.
4. Strict Type-Sensitive Scoring: Evaluates exact JSON match where types (bool vs int), list ordering, and dictionary keys must match exactly.
5. Failure Categorization: Classifies failures into granular buckets: none, duplicate_retained, off_by_one_retries, credential_leak, json_decode_error, mismatched_output, adapter_exception.

## 5. Mock Adapter Specification

- MockAlgorithmAdapter implements uniform rule solver across all groups.
- Condition-Agnostic: Does not branch on group labels (A/B/C), does not read expected answers, and does not hardcode superiority (C > B).
- Disclaimer: Marked explicitly with is_mock=True and disclaimer: synthetic pipeline check only; not model evidence or a production benefit estimate.

## 6. Verification Evidence

### Pytest Verification
Command: ./.venv/bin/python -m pytest -rs tests/experience/test_prepared_eval.py
Output: 7 passed in 0.12s

### CLI Mock Execution Reproduction
Command: ./.venv/bin/python -m evaluation.experience_v1.prepared_eval --split test --repetitions 3 --seed 42 --output /tmp/prepared_eval_rep3.json
Artifact Verification:
- Output Path: /tmp/prepared_eval_rep3.json
- SHA256 Checksum: b4e773b0156c382f661ac9f4c568060098692b9f1ae66e58c0f98bf052cc983f
- Verified Record Count: 54 evaluation result items parsed via standard library json.loads.

Output Summary:
- Total Adapter Calls: 54 (6 distinct test cases * 3 condition groups * 3 repetitions)
- Group Success Rates: A=1.0 (18/18), B=1.0 (18/18), C=1.0 (18/18)
- Group Known Error Rates: A=0.0, B=0.0, C=0.0
- Mean Latencies: A=0.0032 ms, B=0.0040 ms, C=0.0033 ms
- Failure Categories: none=18 for each group
- Verification of Parity: No artificial advantage configured for Group C over Group B.

## 7. Limitations & Production Divergence Boundaries

1. Sample Size & Statistical Significance:
   - The test set comprises exactly 6 independent test cases across 3 task families (2 cases each).
   - 3 repetitions represent deterministic harness re-runs across permutations (yielding 18 calls per group, 54 total calls), not 18 independent tasks.
   - This harness demonstrates harness determinism and pipeline integrity only; it yields zero statistical significance and zero conclusions regarding real model reasoning capability.
2. Output Schema Conveyance Constraint for Production Models:
   - In live model evaluation, common prompts across groups A, B, and C must uniformly provide the target output schema for each task category:
     * deduplicate_preserve_order: {"unique_items": list, "count": int}
     * retry_budget_calc: {"max_retries": int, "can_retry": bool, "budget_seconds": int}
     * field_sanitization: {"sanitized_record": dict, "masked_count": int}
   - Group B must not be the sole beneficiary of discovering field names through train demonstrations; otherwise, strict equality scoring would penalize Group A for valid semantic outputs that merely used alternative key names.
   - In this mock harness, the internal algorithm solver standardizes output shapes across all groups; actual prompt rendering and schema communication have not been connected to a live adapter and remain unverified (status: 未核验，正式运行前必须共同固定).
3. Condition C Memory Pipeline Scope:
   - Group C in this offline setup utilizes a static, pre-frozen rule transformation package derived from the train split.
   - It does not integrate or validate real live MemoryHub retrieval pipelines, dynamic embeddings, or runtime context assembly.
4. Legacy Production Runner Audit & Live Authorization Requirements:
   - The legacy production runner (evaluation/experience_v1/runner.py) hardcodes model "agnes/agnes-2.5-flash" and POSTs to local gateway "http://127.0.0.1:10100/v1/chat/completions".
   - Configuration parameter paid_cap=0 is merely a local authorization policy constraint, and output field billing_amount=None indicates absence of billing telemetry rather than proof of zero financial cost.
   - The legacy runner was intentionally not executed during this preparation. Upstream routing, subscription validity, and actual provider costs remain unverified.
   - Any future live invocation against external or proxy LLM endpoints strictly requires prior verification of zero-cost transport and explicit outbound user authorization covering this batch of prompt and task data.
