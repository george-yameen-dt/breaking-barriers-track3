"""Freezes the cell layout and phone positions the swarm's RF model works on. Run once, in the mobile-env venv:

    ~/.local/venvs/mobile-env/bin/python tools/make_layout.py

Station positions come from mobile-env's medium scenario (towers A-G = bs_id 0-6, as in practice/sim.py).
Phone positions are 12 samples, one every 8 steps, of one seeded episode. Phones move the same way whatever
the agents do, so these are the emulator's own movements. Writes scenarios/swarm-layout.json.
"""

import json
from importlib.metadata import version
from pathlib import Path

import gymnasium
import mobile_env  # noqa: F401  registers the envs
import numpy as np

SEED = 7
SAMPLES = 12
EVERY = 8
OUT = Path(__file__).resolve().parent.parent / "scenarios" / "swarm-layout.json"


def main() -> None:
    env = gymnasium.make("mobile-medium-central-v0", render_mode=None)
    core = env.unwrapped
    env.reset(seed=SEED)
    stations = {chr(65 + bs.bs_id): [round(float(bs.x), 1), round(float(bs.y), 1)] for bs in core.stations.values()}
    phones = sorted(core.users.values(), key=lambda ue: ue.ue_id)
    samples = []
    for step in range(1, SAMPLES * EVERY + 1):
        env.step(np.zeros(len(phones), dtype=int))
        if step % EVERY == 0:
            samples.append([[round(float(ue.x), 1), round(float(ue.y), 1)] for ue in phones])
    layout = {"source": "mobile-env medium (mobile-medium-central-v0)", "mobile_env_version": version("mobile-env"),
              "seed": SEED, "every_steps": EVERY, "map_units": [core.width, core.height],
              "stations": stations, "ue_samples": samples}
    OUT.write_text(json.dumps(layout) + "\n")
    print(f"wrote {OUT.name}: {len(stations)} towers, {len(samples)} samples x {len(samples[0])} phones")


if __name__ == "__main__":
    main()
