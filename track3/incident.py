"""What the swarm, the promotion flow and the Arize Signal gate still share from the first two-agent incident.

The two-agent incident itself (network agent, change agent, track3.cedar, runtime track3_agents) is gone. What stays:
the two model ids the declarations and the registry name, parse_json for every agent's JSON answer, and DIAGNOSE,
the network agent's v1 prompt that the promotion flow starts from (prompts/network-agent/v1.txt must equal it).
"""

import json
import re

AGENT_MODEL = "openai.gpt-oss-120b-1:0"
JUDGE_MODEL = "us.anthropic.claude-haiku-5-5"

DIAGNOSE = """You are the network agent in a team of AI agents that runs a mobile network.
Read the Dynatrace problem and the live network state. Reply with JSON only:
{"summary": "<= 30 words", "cause": "<= 30 words", "affected_phones": ["phone-.."], "towers_involved": ["A"]}"""


def parse_json(text: str) -> dict:
    """The first JSON object in the text: a model may write prose, or a second object, around the one that was asked for."""
    decoder = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text[m.start():])
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return {}
