"""One Cedar policy set, loaded from a file and evaluated the same way for every kind of change.

The base checkpoint holds the promotion set (policies/promotion.cedar, for changes to the agents themselves).
The swarm decides network changes with policies/swarm.cedar (agents/governance.py).
"""

import hashlib
import re
from pathlib import Path

import cedarpy

NO_PERMIT = "no policy permits this call"


class PolicySet:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.text = self.path.read_text()
        self.sha256 = hashlib.sha256(self.path.read_bytes()).hexdigest()
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
