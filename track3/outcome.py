"""The outcome score: what happened to the network after an approved fix, computed by plain code from Dynatrace data.

The judge score grades what the agents said; this grades what the network did. No model is involved, so it is an
independent grader by construction. The business intent (voice vs video) sets what "recovered" means.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

INTENTS = Path(__file__).parent / "intents"


def load_intent(name: str) -> dict:
    return json.loads((INTENTS / f"{name}.json").read_text())


def score_outcome(samples: list, fix_time: datetime, problem_closed_at: datetime | None, intent: dict) -> dict:
    """samples: one-minute buckets {"minute", "phones_dropped", "median_rate"}, sorted by minute."""
    after = [s for s in samples if s["minute"] >= fix_time]
    by_minute = {s["minute"]: s for s in after}
    clean = intent["clean_minutes"]
    deadline = fix_time + timedelta(minutes=intent["restore_within_minutes"])

    restored_at, window = None, []
    for s in after:
        if s["minute"] > deadline:
            break
        run = [by_minute.get(s["minute"] + timedelta(minutes=k)) for k in range(clean)]
        if all(r is not None and int(r["phones_dropped"]) == 0 for r in run):
            restored_at, window = s["minute"], run
            break

    rated = window or after
    median_after = round(sum(float(s["median_rate"]) for s in rated) / len(rated), 3) if rated else None
    components = {
        "restored": restored_at is not None,
        "problem_closed": problem_closed_at is not None
        and problem_closed_at <= fix_time + timedelta(minutes=intent["close_within_minutes"]),
        "no_regression": median_after is not None and median_after >= intent["median_rate_floor"],
    }
    score = round(sum(w for k, w in intent["weights"].items() if components[k]), 3)

    if all(components[k] for k in intent["required"]):
        verdict = "pass"
    elif not components["restored"] and (not after or after[-1]["minute"] < deadline + timedelta(minutes=clean - 1)):
        verdict = "inconclusive"  # the data ends before the network had its full chance to recover
    else:
        verdict = "fail"

    return {**components, "restored_at": restored_at.isoformat() if restored_at else None, "median_rate_after": median_after,
            "score": score, "verdict": verdict, "components": components, "intent": intent["name"]}


def label(judge_pass: bool, outcome_pass: bool) -> str:
    """Where the judge and the network agree or disagree; the disagreements are the most useful training data."""
    return {(True, True): "confirmed_good", (True, False): "sounded_right_failed",
            (False, True): "judge_miscalibrated", (False, False): "confirmed_bad"}[(judge_pass, outcome_pass)]
