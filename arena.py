"""Tank arena: four Claude Code models play a turn-based NES-style tank game.

Each player is a headless `claude -p` session (subscription login, no tools, no API key), resumed
every call so it keeps its own history. The engine shows the board, the model replies with a plan
(one or more ACTIONS) and one WHY line. The engine runs the plan one action per round and asks the
model again when the plan runs out or when something notable happens. Everything is logged to
runs/*.jsonl; state.json feeds the live viewer (viewer.html, served by --serve).

Usage:
  python arena.py --models haiku sonnet haiku sonnet --rounds 60 --parallel --effort low --serve
  python arena.py --models random random random random --rounds 20 --serve --delay 0.5   (engine test, no Claude)
"""
import argparse
import http.server
import json
import os
import random
import re
import subprocess
import tempfile
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
W = H = 15
WALL = 3          # length of each steel wall from the edge toward the centre
EXTRA_BRICKS = 5  # random brick positions per quadrant, mirrored 4 ways
LIVES = 3
PLAN_MAX = 8
IDS = ["A", "B", "C", "D"]
DIRS = {"UP": (0, -1), "DOWN": (0, 1), "LEFT": (-1, 0), "RIGHT": (1, 0)}
ACTIONS = ["UP", "DOWN", "LEFT", "RIGHT", "FIRE", "STAY"]
BASE_POS = {"A": (1, 1), "B": (13, 1), "C": (1, 13), "D": (13, 13)}
SPAWN_POS = {"A": (2, 2), "B": (12, 2), "C": (2, 12), "D": (12, 12)}
START_FACE = {"A": "RIGHT", "B": "LEFT", "C": "RIGHT", "D": "LEFT"}

RULES = """You are player {me}, a tank commander in a turn-based tank battle against three other players (A, B, C, D).
The board is a {w}x{h} grid. Coordinates are (x,y): x is the column 0-{wm} left to right, y is the row 0-{hm} top to bottom. UP means y-1, DOWN y+1, LEFT x-1, RIGHT x+1.
Symbols: '.' empty, '#' brick wall (a shell destroys it), '@' steel wall (indestructible, blocks shells), uppercase letter = that player's tank, lowercase letter = that player's base (headquarters).
Actions:
- UP / DOWN / LEFT / RIGHT: turn to face that direction, then move one cell if that cell is empty ('.'). If it is not empty, you only turn.
- FIRE: shoot a shell in the direction you face. It flies in a straight line with no range limit until it meets something. The first thing it meets: a brick is destroyed; steel stops it; a tank loses one life; a base is destroyed and its owner is out of the game. Shells do not distinguish friend from foe: you can hit your own base.
- STAY: do nothing.
Each tank has {lives} lives. A hit tank respawns immediately next to its own base. A player with zero lives has no tank any more, but their base is still on the board and can be destroyed.
Goal: protect your base, destroy the other tanks and their bases. The game ends when at most one base is alive: the owner of that base wins. If more than one base is still alive after {rounds} rounds, nobody wins and all remaining players lose. Only destroying bases wins the game.
{order_line} Decide your own tactics.
You reply with a plan: a list of 1 to {plan_max} actions. They run one per round in the given order. You are asked again when the list runs out or when something notable happens (a tank or base was hit, a tank in your row or column fired, an enemy tank entered your row or column, or your move was blocked). Then you may send a new list, which replaces the rest of the old one.
Reply format, nothing else:
ACTIONS: <one or more of UP DOWN LEFT RIGHT FIRE STAY, separated by spaces>
WHY: <one sentence>"""
ORDER = {
    "sequential": "Players act in the order A, B, C, D, one action each per round.",
    "parallel": "All players act once per round. Planned actions run first; players who are asked for a new plan act in the order their replies arrive, the fastest reply first.",
}


def make_map(seed):
    """Open field. Each base sits in a corner inside a brick ring. Four steel walls run in from the
    middle of each edge (length WALL), so no straight shot along an edge connects one base area to
    another. A few random bricks, mirrored four ways so the map stays fair."""
    g = [["." for _ in range(W)] for _ in range(H)]
    for pid, (bx, by) in BASE_POS.items():
        g[by][bx] = pid.lower()
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                x, y = bx + dx, by + dy
                if (dx or dy) and 0 <= x < W and 0 <= y < H and (x, y) != SPAWN_POS[pid]:
                    g[y][x] = "#"
    mid = W // 2
    for i in range(WALL):
        g[i][mid] = "@"              # top edge, between a and b
        g[H - 1 - i][mid] = "@"      # bottom edge, between c and d
        g[mid][i] = "@"              # left edge, between a and c
        g[mid][W - 1 - i] = "@"      # right edge, between b and d
    rng = random.Random(seed)
    cand = [(x, y) for x in range(mid) for y in range(mid) if g[y][x] == "." and not (x <= 3 and y <= 3)]
    for x, y in rng.sample(cand, EXTRA_BRICKS):
        for px, py in [(x, y), (W - 1 - x, y), (x, H - 1 - y), (W - 1 - x, H - 1 - y)]:
            g[py][px] = "#"
    return g


class Game:
    def __init__(self, models, seed, rounds):
        self.grid = make_map(seed)
        self.rounds = rounds
        self.round = 0
        self.game_id = f"{int(time.time())}-{seed}"
        self.players = {}
        for pid, m in zip(IDS, models):
            self.players[pid] = {"model": m, "pos": SPAWN_POS[pid], "face": START_FACE[pid], "lives": LIVES,
                                 "base": True, "hits": 0, "kills": 0, "invalid": 0, "action": None, "why": "",
                                 "latency_s": None, "plan": [], "source": None, "calls": 0, "blocked": False, "line_seen": set(), "events_since": []}
        self.events = []  # (round, actor, text)
        self.shots = []   # for the viewer: path of every shell
        self.thinking = []
        self.over = False
        self.winner = None

    # ---- helpers
    def tank_at(self, x, y):
        for pid, p in self.players.items():
            if p["lives"] > 0 and p["base"] and p["pos"] == (x, y):
                return pid
        return None

    def cell(self, x, y):
        return self.grid[y][x]

    def free(self, x, y):
        return 0 <= x < W and 0 <= y < H and self.cell(x, y) == "." and self.tank_at(x, y) is None

    def respawn(self, pid):
        p = self.players[pid]
        if self.free(*SPAWN_POS[pid]):
            p["pos"] = SPAWN_POS[pid]
        else:
            seen, q = {BASE_POS[pid]}, deque([BASE_POS[pid]])
            while q:
                x, y = q.popleft()
                for dx, dy in DIRS.values():
                    nx, ny = x + dx, y + dy
                    if (nx, ny) in seen or not (0 <= nx < W and 0 <= ny < H):
                        continue
                    seen.add((nx, ny))
                    if self.free(nx, ny):
                        p["pos"] = (nx, ny)
                        q.clear()
                        break
                    if self.cell(nx, ny) != "@":
                        q.append((nx, ny))
        p["face"] = START_FACE[pid]

    def log(self, actor, text):
        self.events.append((self.round, actor, text))
        for pid, p in self.players.items():
            if pid != actor:
                p["events_since"].append(text)

    def can_act(self, pid):
        p = self.players[pid]
        return p["base"] and p["lives"] > 0

    def in_line(self, pid):
        """Enemy tanks sharing a row or column with this tank (walls ignored)."""
        me = self.players[pid]["pos"]
        return [o for o in IDS if o != pid and self.can_act(o)
                and (self.players[o]["pos"][0] == me[0] or self.players[o]["pos"][1] == me[1])]

    def need_call(self, pid):
        """Why the model must be asked this round, or None if its plan simply continues."""
        p = self.players[pid]
        line = self.in_line(pid)
        new_in_line = [o for o in line if o not in p["line_seen"]]
        p["line_seen"] = set(line)
        if not p["plan"]:
            return "your plan is finished" if p["calls"] else "the game starts"
        hits = [e for e in p["events_since"] if "HIT" in e or "DESTROYED" in e]
        if hits:
            return "a tank or base was hit: " + "; ".join(hits[-3:])
        shots = [e for e in p["events_since"] if "fired" in e and e[0] in line]
        if shots:
            return "a tank in your row or column fired: " + "; ".join(shots[-3:])
        if p["action"] in DIRS and p["blocked"]:
            return f"your move {p['action']} was blocked"
        if new_in_line:
            return "enemy tank entered your row or column: " + ", ".join(f"{o} at {self.players[o]['pos']}" for o in new_in_line)
        return None

    # ---- actions
    def apply(self, pid, action):
        p = self.players[pid]
        p["action"] = action
        p["blocked"] = False
        if action in DIRS:
            p["face"] = action
            dx, dy = DIRS[action]
            nx, ny = p["pos"][0] + dx, p["pos"][1] + dy
            if self.free(nx, ny):
                p["pos"] = (nx, ny)
                self.log(pid, f"{pid} moved {action} to {p['pos']}")
            else:
                p["blocked"] = True
                self.log(pid, f"{pid} turned {action} at {p['pos']} (blocked)")
        elif action == "FIRE":
            dx, dy = DIRS[p["face"]]
            x, y = p["pos"]
            path = []
            shot = {"id": len(self.shots) + 1, "actor": pid, "from": p["pos"], "dir": p["face"], "path": path, "hit": None, "at": None}
            self.shots.append(shot)
            while True:
                x, y = x + dx, y + dy
                if not (0 <= x < W and 0 <= y < H):
                    shot["hit"] = "edge"
                    self.log(pid, f"{pid} fired {p['face']} from {p['pos']}: shell left the board")
                    return
                path.append((x, y))
                shot["at"] = (x, y)
                c = self.cell(x, y)
                t = self.tank_at(x, y)
                if t:
                    shot["hit"] = "tank"
                    self.hit_tank(pid, t, (x, y))
                    return
                if c == "#":
                    shot["hit"] = "brick"
                    self.grid[y][x] = "."
                    self.log(pid, f"{pid} fired {p['face']} from {p['pos']}: destroyed brick at ({x},{y})")
                    return
                if c == "@":
                    shot["hit"] = "steel"
                    self.log(pid, f"{pid} fired {p['face']} from {p['pos']}: shell hit steel at ({x},{y})")
                    return
                if c.isalpha():
                    shot["hit"] = "base"
                    self.hit_base(pid, c.upper(), (x, y))
                    return
        else:
            self.log(pid, f"{pid} stayed at {p['pos']}")

    def hit_tank(self, pid, t, at):
        p, v = self.players[pid], self.players[t]
        p["hits"] += 1
        v["lives"] -= 1
        v["plan"] = []  # a respawned tank needs a new plan
        if v["lives"] > 0:
            self.respawn(t)
            self.log(pid, f"{pid} fired {p['face']}: HIT tank {t} at {at}, {t} has {v['lives']} lives, respawned at {v['pos']}")
        else:
            p["kills"] += 1
            self.log(pid, f"{pid} fired {p['face']}: HIT tank {t} at {at}, {t} has no lives left, tank {t} is gone")

    def hit_base(self, pid, t, at):
        p, v = self.players[pid], self.players[t]
        p["hits"] += 1
        p["kills"] += 1
        v["base"] = False
        v["plan"] = []
        self.grid[at[1]][at[0]] = "."
        self.log(pid, f"{pid} fired {p['face']}: DESTROYED base of {t} at {at}, player {t} is out")
        self.check_end()

    def check_end(self):
        alive = [pid for pid, p in self.players.items() if p["base"]]
        if len(alive) <= 1:
            self.over = True
            self.winner = alive[0] if alive else None

    def ranking(self):
        return sorted(self.players, key=lambda i: (self.players[i]["base"], self.players[i]["lives"], self.players[i]["hits"]), reverse=True)

    # ---- views
    def board(self):
        out = ["    " + "".join(f"{x:>3}" for x in range(W))]
        for y in range(H):
            line = f"{y:>3} "
            for x in range(W):
                t = self.tank_at(x, y)
                line += f"{t or self.cell(x, y):>3}"
            out.append(line)
        return "\n".join(out)

    def prompt(self, pid, reason):
        p = self.players[pid]
        lines = [f"Round {self.round} of {self.rounds}. Player {pid}, you are asked now because {reason}.", "", self.board(), ""]
        lines.append(f"You: tank {pid} at {p['pos']} facing {p['face']}, lives {p['lives']}. Your base '{pid.lower()}' at {BASE_POS[pid]}.")
        for o, q in self.players.items():
            if o == pid:
                continue
            if not q["base"]:
                lines.append(f"{o}: out of the game (base destroyed).")
            elif q["lives"] <= 0:
                lines.append(f"{o}: no tank (0 lives), base '{o.lower()}' at {BASE_POS[o]} still alive.")
            else:
                lines.append(f"{o}: tank at {q['pos']} facing {q['face']}, lives {q['lives']}, base '{o.lower()}' at {BASE_POS[o]} alive.")
        lines.append("")
        if p["events_since"]:
            lines.append("Since you were last asked:")
            lines += [f"- {e}" for e in p["events_since"]]
        else:
            lines.append("Since you were last asked: nothing happened.")
        if p["plan"]:
            lines.append(f"Your remaining plan was: {' '.join(p['plan'])}. Your reply replaces it.")
        lines.append("")
        lines.append("Reply with ACTIONS and WHY.")
        return "\n".join(lines)

    def state(self, status=""):
        return {
            "game": self.game_id, "round": self.round, "rounds": self.rounds, "grid": ["".join(r) for r in self.grid],
            "players": {pid: {k: v for k, v in p.items() if k not in ("events_since", "line_seen")} for pid, p in self.players.items()},
            "events": [{"round": r, "actor": a, "text": t} for r, a, t in self.events[-40:]],
            "shots": self.shots[-12:], "thinking": self.thinking, "over": self.over, "winner": self.winner, "ranking": self.ranking(),
            "status": status, "updated": time.time(),
        }


# ---- players
def parse_plan(text):
    """Return (list of actions, why). Empty list = unreadable reply."""
    m = re.search(r"ACTIONS?\s*:\s*\**\s*([A-Za-z ,/\->]+)", text)
    body = m.group(1) if m else text
    acts = [a.upper() for a in re.findall(r"\b(UP|DOWN|LEFT|RIGHT|FIRE|STAY)\b", body, re.I)]
    if not m and acts:
        acts = acts[-1:]  # no ACTIONS line: take the last bare action word only
    w = re.search(r"WHY\s*:\s*\**\s*(.+)", text, re.I)
    return acts[:PLAN_MAX], (w.group(1).strip() if w else "")


def claude_turn(model, session, first, system, prompt, cwd, timeout, effort=None):
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    cmd = ["claude", "-p", "--model", model, "--tools", "", "--strict-mcp-config", "--setting-sources", "",
           "--system-prompt", system, "--output-format", "stream-json", "--verbose"]
    if effort:
        cmd += ["--effort", effort]
    cmd += ["--session-id", session] if first else ["--resume", session]
    t0 = time.time()
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", cwd=cwd, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"text": "", "error": "timeout", "latency_s": round(time.time() - t0, 1)}
    if r.returncode:
        return {"text": "", "error": f"exit {r.returncode}: {r.stderr[-500:]}", "latency_s": round(time.time() - t0, 1)}
    events = [json.loads(l) for l in r.stdout.splitlines() if l.strip()]
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})
    if init.get("tools") or init.get("mcp_servers"):
        raise SystemExit(f"tools present: {init.get('tools')} mcp: {init.get('mcp_servers')}")
    blocks = [b for e in events if e.get("type") == "assistant" for b in e["message"]["content"]]
    if any(b.get("type") not in ("text", "thinking") for b in blocks):
        raise SystemExit("non-text block in reply")
    res = next((e for e in events if e.get("type") == "result"), {})
    return {"text": res.get("result", "").strip(), "model_id": init.get("model"), "usage": res.get("usage"),
            "latency_s": round(time.time() - t0, 1), "error": res.get("subtype") if res.get("is_error") else None}


def random_turn(rng):
    plan = " ".join(rng.choice(ACTIONS[:5]) for _ in range(rng.randint(1, 4)))
    return {"text": f"ACTIONS: {plan}\nWHY: random bot", "model_id": "random", "latency_s": 0.0, "error": None}


# ---- live viewer
def serve(directory, port):
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=directory, **k)

        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def write_state(path, state):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    for _ in range(20):  # the viewer may hold state.json open for a moment (Windows lock)
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.1)
    path.write_text(json.dumps(state), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs=4, default=["haiku", "sonnet", "haiku", "sonnet"], help="4 models for A B C D; 'random' = test bot")
    ap.add_argument("--rounds", type=int, default=60)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--timeout", type=int, default=240, help="seconds per claude call")
    ap.add_argument("--stateless", action="store_true", help="fresh session every call (no memory)")
    ap.add_argument("--serve", action="store_true", help="serve viewer.html on --port")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--out", default="runs")
    ap.add_argument("--state", default=str(HERE / "state.json"), help="state file for the viewer")
    ap.add_argument("--delay", type=float, default=0.0, help="pause per planned action and per random-bot reply (viewing speed)")
    ap.add_argument("--parallel", action="store_true", help="players who need a new plan think at once; replies apply in arrival order")
    ap.add_argument("--effort", default=None, help="claude --effort level for players, e.g. low")
    args = ap.parse_args()

    g = Game(args.models, args.seed, args.rounds)
    rng = random.Random(args.seed)
    sessions = {pid: {"id": str(uuid.uuid4()), "calls": 0} for pid in IDS}
    cwd = tempfile.mkdtemp(prefix="tank_arena_")
    order_line = ORDER["parallel" if args.parallel else "sequential"]
    rules = {pid: RULES.format(me=pid, w=W, h=H, wm=W - 1, hm=H - 1, lives=LIVES, rounds=args.rounds,
                               order_line=order_line, plan_max=PLAN_MAX) for pid in IDS}

    def ask(pid, prompt):
        p = g.players[pid]
        if p["model"] == "random":
            time.sleep(args.delay + rng.random() * 0.5)
            return random_turn(rng)
        s = sessions[pid]
        if args.stateless:
            s["id"], s["calls"] = str(uuid.uuid4()), 0
        first = s["calls"] == 0
        s["calls"] += 1
        return claude_turn(p["model"], s["id"], first, rules[pid], prompt, cwd, args.timeout, args.effort)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = HERE / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    log = out_dir / f"game__{'_'.join(args.models)}__r{args.rounds}__s{args.seed}__{stamp}.jsonl"
    state_path = Path(args.state)
    if args.serve:
        serve(str(HERE), args.port)
        print(f"viewer: http://127.0.0.1:{args.port}/viewer.html")
    write_state(state_path, g.state("starting"))

    with log.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "meta", "started": stamp, "models": dict(zip(IDS, args.models)), "rules": rules["A"],
                            "map": ["".join(r) for r in g.grid], "args": vars(args), "log": str(log)}) + "\n")

        def record(pid, action, source, order, prompt=None, reason=None, r=None, valid=True, n_before=0):
            p = g.players[pid]
            rec = {"type": "turn", "round": g.round, "player": pid, "model": p["model"], "source": source, "order": order,
                   "action": action, "plan_after": list(p["plan"]), "why": p["why"], "valid": valid,
                   "reason": reason, "prompt": prompt, "reply": r["text"] if r else None, "model_id": r.get("model_id") if r else None,
                   "error": r.get("error") if r else None, "latency_s": r.get("latency_s") if r else None, "usage": r.get("usage") if r else None,
                   "events": [t for _, _, t in g.events[n_before:]],
                   "state": {q: {"pos": g.players[q]["pos"], "lives": g.players[q]["lives"], "base": g.players[q]["base"]} for q in IDS}}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            write_state(state_path, g.state())
            ev = g.events[n_before][2] if len(g.events) > n_before else ""
            lat = f"[{r.get('latency_s')}s]" if r else "[plan]"
            print(f"r{g.round:02d} {pid} {p['model']:>7} #{order} {lat:>8} {action:<5} | {ev} | {p['why'][:80]}", flush=True)

        def run_plan(pid, order):
            """Next queued action, no model call."""
            p = g.players[pid]
            action = p["plan"].pop(0) if p["plan"] else "STAY"
            p["source"] = "plan"
            p["latency_s"] = None
            n_before = len(g.events)
            g.apply(pid, action)
            record(pid, action, "plan", order, n_before=n_before)
            if args.delay:
                time.sleep(args.delay)

        def commit(pid, prompt, reason, r, order):
            """Apply the first action of a fresh reply, queue the rest, log it."""
            p = g.players[pid]
            p["calls"] += 1
            if g.thinking:
                g.thinking = [q for q in g.thinking if q != pid]
            if not g.can_act(pid):  # lost the tank or base to an earlier reply this round
                p["why"], p["plan"] = "(no tank: hit before this move was applied)", []
                record(pid, "STAY", "model", order, prompt, reason, r, True, len(g.events))
                return
            acts, why = parse_plan(r["text"])
            valid = bool(acts)
            if not valid:
                acts, why = ["STAY"], f"(invalid reply: {r.get('error') or r['text'][:80]!r})"
                p["invalid"] += 1
            p["why"], p["plan"], p["source"], p["latency_s"] = why, acts[1:], "model", r.get("latency_s")
            n_before = len(g.events)
            g.apply(pid, acts[0])
            record(pid, acts[0], "model", order, prompt, reason, r, valid, n_before)

        for rnd in range(1, args.rounds + 1):
            g.round = rnd
            if args.parallel:
                active = [pid for pid in IDS if g.can_act(pid)]
                reasons = {pid: g.need_call(pid) for pid in active}
                order = 0
                for pid in active:  # planned actions first, in seat order
                    if reasons[pid] is None and g.can_act(pid) and not g.over:
                        if not g.players[pid]["plan"]:  # hit by an earlier planned shot this round: plan was cleared
                            reasons[pid] = g.need_call(pid)
                            continue
                        order += 1
                        run_plan(pid, order)
                if g.over:
                    break
                thinkers = [pid for pid in active if reasons[pid] is not None and g.can_act(pid)]
                if not thinkers:
                    continue
                prompts = {pid: g.prompt(pid, reasons[pid]) for pid in thinkers}
                for pid in thinkers:
                    g.players[pid]["events_since"] = []
                g.thinking = list(thinkers)
                write_state(state_path, g.state(", ".join(f"{pid} ({g.players[pid]['model']})" for pid in thinkers) + " thinking"))
                with ThreadPoolExecutor(max_workers=len(thinkers)) as ex:
                    futs = {ex.submit(ask, pid, prompts[pid]): pid for pid in thinkers}
                    for fut in as_completed(futs):
                        pid = futs[fut]
                        if g.over:
                            break
                        order += 1
                        commit(pid, prompts[pid], reasons[pid], fut.result(), order)
                g.thinking = []
            else:
                for order, pid in enumerate(IDS, 1):
                    if g.over:
                        break
                    if not g.can_act(pid):
                        continue
                    reason = g.need_call(pid)
                    if reason is None:
                        run_plan(pid, order)
                        continue
                    p = g.players[pid]
                    g.thinking = [pid]
                    write_state(state_path, g.state(f"{pid} ({p['model']}) is thinking"))
                    prompt = g.prompt(pid, reason)
                    p["events_since"] = []
                    commit(pid, prompt, reason, ask(pid, prompt), order)
            if g.over:
                break
        if not g.over:  # round limit with more than one base alive: nobody wins
            g.over = True
            g.winner = None
        result = {"type": "result", "winner": g.winner, "winner_model": g.players[g.winner]["model"] if g.winner else None,
                  "ranking": [(pid, g.players[pid]["model"]) for pid in g.ranking()], "rounds_played": g.round,
                  "players": {pid: {k: p[k] for k in ("model", "lives", "base", "hits", "kills", "invalid", "calls")} for pid, p in g.players.items()}}
        f.write(json.dumps(result) + "\n")
    write_state(state_path, g.state("game over"))
    print("\nRESULT:", json.dumps(result, indent=1))
    print("log:", log)
    if args.serve:
        print("viewer still up, press Ctrl+C to quit")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
