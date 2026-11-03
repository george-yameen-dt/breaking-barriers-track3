"""The agents run on reasoning models: a token budget that is too small ends a specialist mid-thought and the run goes on degraded."""

from types import SimpleNamespace

from agents import risk, specialist


def _capture(monkeypatch, module):
    seen = {}

    def fake(cp, role, model, system, prompt, tools, **kw):
        seen.update(kw)
        return {"assessments": [], "findings": [], "proposals": [], "domain_verdict": "x"}

    monkeypatch.setattr(module, "run_agent", fake)
    return seen


def _run():
    cp = SimpleNamespace(run_id="r", trace_id="t", record=lambda *a, **k: None)
    net = SimpleNamespace(snapshot={"snapshot_sha256": "s"}, prompt_note="")
    return SimpleNamespace(cp=cp, net=net, alert="a", proposals=[], reports={}, tools_for=lambda role: {},
                           context_for=lambda *a: {})


def test_a_specialist_gets_a_budget_for_a_reasoning_model(monkeypatch):
    seen = _capture(monkeypatch, specialist)
    specialist.investigate(_run(), "capacity")
    assert seen["max_tokens"] >= 8000, seen


def test_the_critic_gets_a_budget_for_a_reasoning_model(monkeypatch):
    seen = _capture(monkeypatch, risk)
    risk.assess_proposals(_run())
    assert seen["max_tokens"] >= 8000, seen
