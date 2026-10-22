# Synthetic A/B/C pipeline check

Run the deterministic, local-only mock evaluation from the repository root:

```sh
PYTHONPATH=. python -m evaluation.experience_v1.mock_eval --output /tmp/memory-hub-mock-eval.json
```

It loads `mock_fixtures.json`, invokes `MockAdapter` for groups A/B/C, scores
expected-term coverage, and records per-case answers and aggregate scores. The
output has `is_mock: true`, `fixture_type: synthetic_fixture`, and an explicit
interpretation disclaimer. This checks only the adapter/scoring/recording path;
it is not an LLM evaluation, does not call a model, and says nothing about
production benefit. To reuse the scoring path with another adapter, call
`run_evaluation(adapter)` from `mock_eval.py`; any real model route requires its
own current authorization and cost checks before use.
