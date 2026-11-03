"""One Cedar policy set, loaded from a file and evaluated the same way for every kind of change.

The base checkpoint holds the promotion set (policies/promotion.cedar, for changes to the agents themselves).
The swarm decides network changes with policies/swarm.cedar (agents/governance.py).
"""

import hashlib
import re
from pathlib import Path

import cedarpy

def critical_power_step(text: str | None = None) -> int:
    """The largest power step (dB) the policy lets a critical cell gain: the number in rule critical-power-step. One source of
    truth, so the risk checks, the demo and the tests follow a policy change by themselves."""
    if text is None:
        text = (Path(__file__).parent / "policies" / "swarm.cedar").read_text()
    m = re.search(r'@id\("critical-power-step"\)\s*\nforbid[^;]*?context\.delta > (\d+)', text)
    if not m:
        raise ValueError("rule critical-power-step not found in the policy")
    return int(m.group(1))


NO_PERMIT = "no policy permits this call"


class PolicySet:
    def __init__(self, path: Path, text: str | None = None):
        """From a file, or (a candidate that is not a file yet) from text."""
        self.path = Path(path)
        self.text = self.path.read_text() if text is None else text
        self.sha256 = hashlib.sha256(self.text.encode()).hexdigest()
        # Cedar names policies policy0, policy1... in file order; map those to our @id names.
        self.ids = re.findall(r'@id\("([^"]+)"\)', self.text)

    def evaluate(self, principal: str, action: str, resource: str, context: dict, attrs: dict | None = None) -> tuple:
        """Returns (decision, reasons, errors). Reasons are the @id names of the policies that decided."""
        rtype, rid = resource.split("::", 1)
        entities = [{"uid": {"__entity": {"type": rtype, "id": rid.strip('"')}}, "attrs": attrs or {}, "parents": []}]
        result = cedarpy.is_authorized({"principal": principal, "action": f'Action::"{action}"', "resource": resource,
                                        "context": context}, self.text, entities)
        decision = "Allow" if result.decision == cedarpy.Decision.Allow else "Deny"
        reasons = [self.ids[int(r[6:])] if r.startswith("policy") and r[6:].isdigit() and int(r[6:]) < len(self.ids) else r
                   for r in result.diagnostics.reasons]
        why = reasons or ([NO_PERMIT] if decision == "Deny" else [])
        return decision, why, [str(e) for e in result.diagnostics.errors]
