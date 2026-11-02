"""Cell network demo: mobile-env + two policies, every step streamed to Dynatrace.

v1 (safe):     every phone connects to its best tower.
v2 (cheater):  strong phones grab every tower they can reach; weak phones are dropped.
               Served phones get faster, but phones that could have signal get none.

Switch policy live: type 1 or 2 and press Enter in the terminal. q quits.
"""

import argparse
import json
import os
import queue
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from datetime import datetime, timezone

import gymnasium
import mobile_env  # noqa: F401  registers the envs
import numpy as np

TENANT = "https://yqs73977.sprint.apps.dynatracelabs.com"
INGEST = TENANT + "/platform/classic/environment-api/v2/logs/ingest"
POLICIES = {"v1": "safe", "v2": "cheater"}
CONTROL = Path(__file__).parent / "control.json"


def desired_v1(env, ue):
    avail = env.available_connections(ue)
    if not avail:
        return set()
    return {max(avail, key=lambda bs: env.channel.snr(bs, ue))}


def desired_v2(env, ue, strong):
    if ue not in strong:
        return set()
    return set(env.available_connections(ue))


def actions_for(env, policy):
    """One toggle per phone per step, moving it toward the policy's target towers."""
    strong = set()
    if policy == "v2":
        ranked = sorted(
            env.active,
            key=lambda u: max([env.channel.snr(bs, u) for bs in env.stations.values()]),
            reverse=True,
        )
        strong = set(ranked[: int(len(ranked) * 0.6)])

    acts = []
    for ue_id in sorted(env.users):
        ue = env.users[ue_id]
        if ue not in env.active:
            acts.append(0)
            continue
        want = desired_v1(env, ue) if policy == "v1" else desired_v2(env, ue, strong)
        have = {bs for bs, ues in env.connections.items() if ue in ues}
        drop = have - want
        add = want - have
        if add:  # make before break, so a safe hand-over never leaves the phone without signal
            acts.append(min(add, key=lambda b: b.bs_id).bs_id + 1)
        elif drop:
            acts.append(min(drop, key=lambda b: b.bs_id).bs_id + 1)
        else:
            acts.append(0)
    return np.array(acts)


def snapshot(env, policy, run_id, episode, reward, unserved):
    """unserved counts steps each phone sat in range with no connection; 2+ steps = dropped."""
    served, dropped, no_signal, ues = 0, 0, 0, []
    for ue in env.active:
        conns = sorted(bs.bs_id for bs, s in env.connections.items() if ue in s)
        in_range = bool(env.available_connections(ue))
        unserved[ue.ue_id] = 0 if conns or not in_range else unserved.get(ue.ue_id, 0) + 1
        state = "searching"
        if conns:
            served += 1
            state = "served"
        elif unserved[ue.ue_id] >= 2:
            dropped += 1
            state = "dropped"
        elif not in_range:
            no_signal += 1
            state = "no signal"
        util = env.utilities.get(ue)
        ues.append({
            "id": ue.ue_id, "x": round(ue.x, 1), "y": round(ue.y, 1), "towers": len(conns), "state": state,
            "links": conns, "util": round(float(util), 3) if util is not None else None,
            "rate": round(float(env.macro.get(ue, 0.0)), 2),
        })

    rates = [env.macro.get(ue, 0.0) for ue in env.active]
    served_rates = [r for r in rates if r > 0]
    load = {str(bs.bs_id): len(env.connections[bs]) for bs in env.stations.values()}
    towers = [
        {"id": bs.bs_id, "name": chr(65 + bs.bs_id), "x": bs.x, "y": bs.y, "load": len(env.connections[bs]),
         "range": tower_range(env, bs)}
        for bs in env.stations.values()
    ]
    level = "WARN" if dropped else "INFO"
    content = (
        f"mobile-env step {int(env.time)} policy={policy} served={served}/{len(env.active)} "
        f"dropped={dropped} median_rate={np.median(served_rates) if served_rates else 0:.2f}"
    )
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),  # own time, so "latest step" sorts right
        "content": content,
        "loglevel": level,
        "service.name": "mobile-env",
        "mobile.kind": "step",
        "cell.network": NETWORK,
        "log.source": "breaking-barriers/sim.py",
        "mobile.run_id": run_id,
        "mobile.episode": episode,
        "mobile.step": int(env.time),
        "mobile.policy": policy,
        "mobile.policy_name": POLICIES[policy],
        "mobile.phones_active": len(env.active),
        "mobile.phones_served": served,
        "mobile.phones_dropped": dropped,
        "mobile.phones_no_signal": no_signal,
        "mobile.safety_violation": int(dropped > 0),
        "mobile.median_rate": round(float(np.median(served_rates)) if served_rates else 0.0, 3),
        "mobile.total_rate": round(float(np.sum(rates)), 3),
        "mobile.reward": round(float(reward), 4),
        "mobile.tower_load": json.dumps(load),
        "mobile.ues": json.dumps(ues),
        "mobile.towers": json.dumps(towers),
        "mobile.map_w": env.width,
        "mobile.map_h": env.height,
    }


NETWORK = "mobile-env-medium"


def topology(step):
    """One record per tower, one per phone-to-tower link (or per unlinked phone), and one if the safety rule broke."""
    base = {
        "timestamp": step["timestamp"],
        "service.name": "mobile-env",
        "log.source": "breaking-barriers/sim.py",
        "mobile.run_id": step["mobile.run_id"],
        "mobile.policy": step["mobile.policy"],
        "cell.network": NETWORK,
    }
    towers = json.loads(step["mobile.towers"])
    names = {t["id"]: t["name"] for t in towers}
    out = []
    for t in towers:
        out.append({
            **base, "mobile.kind": "tower", "loglevel": "INFO",
            "content": f"Tower {t['name']} carries {t['load']} phones",
            "cell.tower.id": t["name"], "cell.tower.name": f"Tower {t['name']}",
            "cell.tower.load": t["load"], "cell.tower.range": t["range"],
        })
    for p in json.loads(step["mobile.ues"]):
        phone = f"phone-{p['id']:02d}"
        common = {**base, "mobile.kind": "phone", "cell.phone.id": phone, "cell.phone.state": p["state"],
                  "cell.phone.util": p["util"], "cell.phone.rate": p["rate"]}
        if p["links"]:
            for bs in p["links"]:
                out.append({**common, "loglevel": "INFO", "cell.tower.id": names[bs],
                            "content": f"{phone} connected to tower {names[bs]}"})
        else:
            out.append({**common, "loglevel": "WARN" if p["state"] == "dropped" else "INFO",
                        "content": f"{phone} {p['state']}"})
    for p in json.loads(step["mobile.ues"]):  # one record per cut-off phone, so the problem names the phones
        if p["state"] != "dropped":
            continue
        phone = f"phone-{p['id']:02d}"
        near = min(towers, key=lambda t: (p["x"] - t["x"]) ** 2 + (p["y"] - t["y"]) ** 2)
        out.append({
            **base, "mobile.kind": "safety", "loglevel": "ERROR",
            "cell.phone.id": phone, "cell.nearest_tower": near["name"],
            "mobile.phones_dropped": step["mobile.phones_dropped"],
            "content": f"Safety rule broken: model {step['mobile.policy']} left {phone} without signal "
                       f"next to tower {near['name']}",
        })
    return out


_RANGES = {}


def tower_range(env, bs):
    """Distance at which a phone loses signal from this tower (binary search on the env's own channel model)."""
    if bs.bs_id not in _RANGES:
        probe = next(iter(env.users.values()))
        ox, oy = probe.x, probe.y
        lo, hi = 0.1, 1000.0
        for _ in range(40):
            mid = (lo + hi) / 2
            probe.x, probe.y = bs.x + mid, bs.y
            lo, hi = (mid, hi) if env.channel.snr(bs, probe) > probe.snr_threshold else (lo, mid)
        probe.x, probe.y = ox, oy
        _RANGES[bs.bs_id] = round(lo, 1)
    return _RANGES[bs.bs_id]


class Shipper(threading.Thread):
    """Batches records and posts them to the logs ingest API about once a second."""

    def __init__(self, token, dry):
        super().__init__(daemon=True)
        self.q, self.token, self.dry = queue.Queue(), token, dry
        self.sent, self.errors, self.last_error = 0, 0, ""

    def run(self):
        while True:
            batch = [self.q.get()]
            time.sleep(1)
            while not self.q.empty():
                batch.append(self.q.get_nowait())
            self.post(batch)
            for _ in batch:
                self.q.task_done()

    def post(self, batch):
        if self.dry:
            self.sent += len(batch)
            return
        req = urllib.request.Request(
            INGEST,
            data=json.dumps(batch).encode(),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                r.read()
            self.sent += len(batch)
        except Exception as e:  # keep the demo running if the network blips
            self.errors += 1
            self.last_error = str(e)[:120]


def read_keys(state):
    for line in sys.stdin:
        key = line.strip().lower()
        if key in ("1", "v1"):
            state["policy"] = "v1"
        elif key in ("2", "v2"):
            state["policy"] = "v2"
        elif key == "q":
            state["quit"] = True
            return


def draw_banner(env, policy):
    import pygame

    surf = pygame.display.get_surface()
    if surf is None:
        return
    pygame.display.set_caption(f"Cell network  ·  policy {policy} ({POLICIES[policy]})")
    font = pygame.font.SysFont("Helvetica", 22, bold=True)
    color = (31, 105, 61) if policy == "v1" else (163, 45, 45)
    text = font.render(f"Model {policy} · {POLICIES[policy]}", True, (255, 255, 255))
    pad = 8
    pygame.draw.rect(surf, color, (12, 12, text.get_width() + 2 * pad, text.get_height() + 2 * pad), border_radius=6)
    surf.blit(text, (12 + pad, 12 + pad))
    pygame.display.flip()


def advance_story(story, state, args):
    """healthy (v1) → broken (v2, until the ops agent rolls back, max 4 min) → fixed (v1) → healthy again."""
    elapsed = time.time() - story["since"]
    if story["phase"] == "healthy" and elapsed > args.healthy:
        story.update(phase="broken", since=time.time())
        state["policy"] = "v2"
        print("story ② broken: model v2 is live", flush=True)
    elif story["phase"] == "broken" and elapsed > 240:  # no agent running: recover on our own
        story.update(phase="fixed", since=time.time())
        state["policy"] = "v1"
        print("story: no rollback came in 4 min, switching back to v1", flush=True)
    elif story["phase"] == "fixed" and elapsed > args.recover:
        story.update(phase="healthy", since=time.time())
        print("story ① healthy: next loop", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--policy", default="v1", choices=list(POLICIES))
    ap.add_argument("--auto", type=int, default=0, help="flip v1/v2 every N seconds (0 = manual)")
    ap.add_argument("--headless", action="store_true", help="no window")
    ap.add_argument("--dry", action="store_true", help="don't send to Dynatrace")
    ap.add_argument("--episodes", type=int, default=0, help="stop after N episodes (0 = forever)")
    ap.add_argument("--fps", type=int, default=4, help="steps per second (4 = easy to follow on screen)")
    ap.add_argument("--story", action="store_true",
                    help="hands-free 3-step loop: healthy on v1, then v2 breaks it, then the ops agent rolls back")
    ap.add_argument("--healthy", type=int, default=45, help="story: seconds on v1 before v2 goes live")
    ap.add_argument("--recover", type=int, default=180, help="story: seconds on v1 after the rollback before the next loop")
    args = ap.parse_args()

    token = os.environ.get("DT_PLATFORM_TOKEN", "")
    if not token and not args.dry:
        sys.exit("DT_PLATFORM_TOKEN is not set. Run through ./demo.sh, or pass --dry.")

    run_id = uuid.uuid4().hex[:8]
    state = {"policy": "v1" if args.story else args.policy, "quit": False}
    story = {"phase": "healthy", "since": time.time()}
    if not args.headless and sys.stdin.isatty():
        threading.Thread(target=read_keys, args=(state,), daemon=True).start()

    shipper = Shipper(token, args.dry)
    shipper.start()

    env = gymnasium.make("mobile-medium-central-v0", render_mode=None if args.headless else "human")
    core = env.unwrapped
    core.metadata["render_fps"] = args.fps

    print(f"run {run_id} · policy {state['policy']} · type 1 or 2 + Enter to switch, q to quit")
    started, episode = time.time(), 0
    while not state["quit"]:
        env.reset(seed=episode)
        done, unserved = False, {}
        while not done and not state["quit"]:
            if args.auto:
                state["policy"] = "v1" if int((time.time() - started) / args.auto) % 2 == 0 else "v2"
            if CONTROL.exists():  # the ops agent rolled the model back
                try:
                    state["policy"] = json.loads(CONTROL.read_text()).get("policy", state["policy"])
                    print(f"ops agent switched the model to {state['policy']}", flush=True)
                    if args.story:
                        story.update(phase="fixed", since=time.time())
                finally:
                    CONTROL.unlink(missing_ok=True)
            if args.story:
                advance_story(story, state, args)
            policy = state["policy"]
            # mobile-env closes its window at the end of every episode; hide it for that last step so the demo keeps running
            window = core.window
            if window is not None and core.time + 1 >= core.EP_MAX_TIME:
                core.window = None
            _, reward, term, trunc, _ = env.step(actions_for(core, policy))
            if window is not None and core.window is None and not core.closed:
                core.window = window
            done = term or trunc
            rec = snapshot(core, policy, run_id, episode, reward, unserved)
            if args.story:
                rec["mobile.story_phase"] = story["phase"]
            shipper.q.put(rec)
            if int(core.time) % args.fps == 0:  # once a second: what OpenPipeline turns into Smartscape nodes, edges and problems
                for t in topology(rec):
                    shipper.q.put(t)
            if not args.headless:
                env.render()
                draw_banner(core, policy)
                if core.closed:
                    state["quit"] = True
            else:
                time.sleep(1 / args.fps)  # keep headless runs at demo pace so charts look the same
            if core.time % 20 == 0:
                print(f"[{story['phase']}] " if args.story else "", end="")
                print(
                    f"ep {episode} step {int(core.time):3d} {policy}  served {rec['mobile.phones_served']:2d}"
                    f"  dropped {rec['mobile.phones_dropped']:2d}  median rate {rec['mobile.median_rate']:6.2f}"
                    f"  reward {rec['mobile.reward']:+.3f}  sent {shipper.sent}  errors {shipper.errors} {shipper.last_error}",
                    flush=True,
                )
        episode += 1
        if args.episodes and episode >= args.episodes:
            break

    shipper.q.join()  # wait until every step has been sent
    print(f"done · sent {shipper.sent} records · errors {shipper.errors}")


if __name__ == "__main__":
    main()
