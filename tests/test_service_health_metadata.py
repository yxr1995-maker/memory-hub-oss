from scripts.automation_core.service_health import health_metadata


def test_health_metadata_exposes_only_service_metadata(monkeypatch):
    monkeypatch.setattr("scripts.automation_core.service_health.os.getpid", lambda: 321)
    monkeypatch.setattr("scripts.automation_core.service_health.time.time", lambda: 1234.5)

    result = health_metadata()

    assert result["version"]
    assert "build" in result
    assert set(result["capabilities"]) == {"recall_for_decision", "read_evidence", "record_episode", "service_health"}
    assert result["process"]["pid"] == 321
    assert isinstance(result["process"]["started_at"], float)
    assert not ({"session", "database", "staging", "wiki", "usage"} & set(result))
