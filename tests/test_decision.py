import json
import io
import pytest
from scripts.automation_core import decision as d


def _reply(answer, model='jev-1.13.0'):
    return {'model': model, 'answers': {'answer': answer},
            'usage': {'input_tokens': 100, 'output_tokens': 10}}


@pytest.fixture
def engine(monkeypatch):
    class StubService:
        def search(self, request):
            class R:
                def to_dict(self):
                    return {'results': [{'path': 'decisions/memory-a.md', 'title': '偏好', 'type': 'decision',
                                        'status': 'active', 'score': 0.42, 'abstract': '摘要'}]}
            return R()
    return d.DecisionEngine(StubService(), timeout=5)


def _patch(monkeypatch, reply):
    captured = {}
    def fake_post(url, payload, *, api_key, timeout):
        captured['url'] = url; captured['payload'] = payload; captured['key'] = api_key
        return reply
    monkeypatch.setattr(d, '_post_json', fake_post)
    return captured


def test_choice_returns_validated_decision(monkeypatch, engine):
    captured = _patch(monkeypatch, _reply({'choice': 'b', 'confidence': 0.62, 'probabilities': {'a': 0.3, 'b': 0.7}}))
    r = engine.decide('保留哪些记忆？', options=['a=全部', 'b=最新'])
    assert r.decision == 'b' and r.confidence == 0.62 and r.mode == 'choice'
    assert r.evidence[0].path == 'decisions/memory-a.md'
    assert captured['payload']['model'] == 'group/jev'
    assert 'evidence_rule' in captured['payload']['state']


def test_choice_must_be_argmax(monkeypatch, engine):
    _patch(monkeypatch, _reply({'choice': 'a', 'confidence': 0.5, 'probabilities': {'a': 0.3, 'b': 0.7}}))
    with pytest.raises(d.DecisionError, match='argmax'):
        engine.decide('保留哪些？', options=['a', 'b'])


def test_probabilities_must_sum_to_one(monkeypatch, engine):
    _patch(monkeypatch, _reply({'choice': 'a', 'confidence': 0.5, 'probabilities': {'a': 0.3, 'b': 0.2}}))
    with pytest.raises(d.DecisionError, match='sum to 1'):
        engine.decide('保留哪些？', options=['a', 'b'])


def test_unknown_choice_is_rejected(monkeypatch, engine):
    _patch(monkeypatch, _reply({'choice': 'zzz', 'confidence': 0.5, 'probabilities': {'a': 0.5, 'b': 0.5}}))
    with pytest.raises(d.DecisionError, match='unknown choice'):
        engine.decide('保留哪些？', options=['a', 'b'])


def test_score_mode_maps_to_legend(monkeypatch, engine):
    _patch(monkeypatch, _reply({'score': 1, 'confidence': 0.7, 'probabilities': {'0': 0.3, '1': 0.7}}))
    r = engine.decide('风险多高？', levels=['低', '中', '高'])
    assert r.mode == 'score' and r.decision == '中' and r.legend['2'] == '高'


def test_requires_options_or_levels(engine):
    with pytest.raises(d.DecisionError, match='options'):
        engine.decide('空问题体')


def test_option_count_bounds(monkeypatch, engine):
    _patch(monkeypatch, _reply({'choice': 'a', 'confidence': 0.5, 'probabilities': {'a': 1.0}}))
    with pytest.raises(d.DecisionError):
        engine.decide('只有一个', options=['a=x'])
    with pytest.raises(d.DecisionError, match='too many'):
        engine.decide('太多', options=[f'o{i}' for i in range(13)])


def test_weak_evidence_is_dropped(monkeypatch):
    """CJK trigram recall can pad the pool with fragment-matching pages; a decision
    must not be steered by evidence that scores far below the strongest hit."""
    class StubService:
        def search(self, request):
            class R:
                def to_dict(self):
                    return {'results': [
                        {'path': 'entities/tiktok.md', 'title': 'TikTok Shop', 'type': 'entity',
                         'status': 'active', 'score': 0.9, 'abstract': '强相关'},
                        {'path': 'concepts/unrelated.md', 'title': 'Unrelated', 'type': 'concept',
                         'status': 'active', 'score': 0.02, 'abstract': '片段命中'},
                    ]}
            return R()
    engine = d.DecisionEngine(StubService(), timeout=5)
    evidence = engine.gather('海外仓还是国内直发好')
    assert [e.path for e in evidence] == ['entities/tiktok.md']
    assert engine.gather('x', min_relative=0.0)[1].score == 0.02  # threshold is explicit, not hardcoded


def test_ungrounded_decision_is_flagged(monkeypatch):
    """When recall yields nothing worth citing, the result must say so instead of
    letting the model's own reasoning pass as a wiki-grounded conclusion."""
    class EmptyService:
        def search(self, request):
            class R:
                def to_dict(self):
                    return {'results': []}
            return R()
    _patch(monkeypatch, _reply({'choice': 'a', 'confidence': 0.5, 'probabilities': {'a': 1.0}}))
    engine = d.DecisionEngine(EmptyService(), timeout=5)
    r = engine.decide('一个知识库里完全没有的话题', options=['a', 'b'])
    assert r.to_dict()['evidence_status'] == 'ungrounded'


def test_absolute_score_floor_rejects_fragment_pool(monkeypatch):
    """Weak OR-trigram hits (~0.003) are not evidence; they must be filtered by floor."""
    class WeakService:
        def search(self, request):
            class R:
                def to_dict(self):
                    return {'results': [
                        {'path': f'a{i}.md', 'title': 't', 'type': 'concept',
                         'status': 'active', 'score': 0.0028, 'abstract': 'x'} for i in range(5)]}
            return R()
    engine = d.DecisionEngine(WeakService(), timeout=5)
    assert engine.gather('海外仓还是国内直发好') == []


def test_cli_exposes_decide_subcommand():
    import subprocess, sys
    out = subprocess.run([sys.executable, 'scripts/automation_cli.py', 'decide', '--help'],
                         capture_output=True, text=True)
    assert out.returncode == 0 and '--option' in out.stdout


def test_memory_hub_sh_dispatches_decide():
    from pathlib import Path
    text = (Path('memory-hub.sh')).read_text()
    assert 'decide)' in text
