"""The agents are told the limit the policy enforces, so they propose inside it."""
from adapters.twin import PROMPT_NOTE
from policy import critical_power_step


def test_the_prompt_states_the_critical_cell_power_step_from_the_policy():
    assert f"by at most +{critical_power_step()} dB" in PROMPT_NOTE
