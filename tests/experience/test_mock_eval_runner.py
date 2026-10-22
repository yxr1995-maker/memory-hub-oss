import json

from evaluation.experience_v1.mock_eval import MockAdapter, run_evaluation, write_result


def test_mock_abcs_evaluation_is_deterministic_and_explicitly_synthetic(tmp_path):
    first = run_evaluation(MockAdapter())
    second = run_evaluation(MockAdapter())

    assert first == second
    assert first["is_mock"] is True
    assert first["protocol"] == "experience-mock-abcs-1"
    assert first["scores"]["C"] > first["scores"]["B"]
    assert first["adapter_calls"] == 6

    output = tmp_path / "result.json"
    write_result(first, output)
    assert json.loads(output.read_text()) == first
