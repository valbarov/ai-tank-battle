# AI tank battle

Four Claude models play an NES-style tank game against each other. Each player is a headless
Claude Code session (`claude -p`, no tools, subscription login, no API key). The engine shows the
board as text, the model replies with a plan of actions and a one-sentence reason, the engine runs
the plan and asks again when something notable happens. Every turn is logged.

## Rules

- 15x15 grid. Four bases in the corners inside brick rings. Four tanks, three lives each.
- `UP` `DOWN` `LEFT` `RIGHT`: turn to face that way, then move one cell if it is empty. `FIRE`: a shell
  flies in a straight line until it meets something: a brick is destroyed, steel stops it, a tank
  loses a life, a base is destroyed and its owner is out. `STAY`: nothing.
- A hit tank respawns next to its own base. Last base standing wins. If the rounds run out with
  more than one base alive, nobody wins.
- Models reply with `ACTIONS: <1 to 8 actions>` and `WHY: <one sentence>`. The plan runs one action
  per round. The model is asked again when its plan runs out, a tank or base is hit, a tank in its
  row or column fires, an enemy enters its row or column, or its own move is blocked.
- In `--parallel` mode all players who need a plan think at once and act in the order their replies
  arrive. The fastest reply acts first.
- Nothing in the prompt says how to play. The last rule line is "Decide your own tactics."

## Run

```
python arena.py --models haiku sonnet opus fable --rounds 60 --parallel --effort low --serve
```

Then open http://127.0.0.1:8765/viewer.html. The page polls `state.json` and draws the board, the
four model panels with the latest `WHY` line, the remaining plan, and the shells in flight.

- `--models random random random random` runs the engine with random bots, no Claude calls.
- `--delay 1` pauses after each planned action so a round of four moves can be watched.
- `--stateless` starts a fresh session every call instead of resuming one per player.
- `run_queue.ps1` runs several games one after another.

Python 3 standard library only. Windows and Linux. Needs the `claude` CLI logged in.

## Data

`runs/game__<models>__r<rounds>__s<seed>__<timestamp>.jsonl`, one game per file:

- `meta`: models per seat, the full rules text, the map, the arguments.
- `turn`: round, player, model, action, remaining plan, `WHY`, whether the action came from a fresh
  reply (`source: model`) or from the plan, the reason the model was asked, the full prompt and raw
  reply, latency, token usage, the events the action caused, and the position and lives of every
  player after it.
- `result`: winner, ranking, per-player lives, base, hits, kills, invalid replies, calls.

`runs/live*.log` are the console views of the same games. The two `random` files are engine tests.

Games were played on 2026-10-30. The rules changed between games; each file's `meta` has the rules
that applied. Early games used a 6-cell shell range, a map with a steel cross in the centre, a lives
tiebreak at the round limit, and one action per reply. Later games use the open map with short steel
walls between neighbouring bases, unlimited range, "nobody wins" at the limit, and plans.

## Results so far

| file | seats A B C D | winner | rounds |
|---|---|---|---|
| r40 s1 | haiku sonnet opus fable | sonnet | 26 |
| r40 s2 | haiku sonnet opus haiku | opus | 25 |
| r40 s3 | haiku sonnet haiku sonnet | sonnet (lives tiebreak) | 40 |
| r60 s4 | haiku sonnet haiku sonnet | haiku | 40 |
| r60 s5 | haiku sonnet haiku sonnet | nobody | 60 |
| r60 s6 | haiku sonnet haiku sonnet | stopped by an engine bug in round 29 | - |
| r60 s7 | haiku sonnet haiku sonnet | sonnet | 38 |
| r60 s8 | haiku sonnet haiku sonnet | haiku | 50 |
| r60 s9 | haiku sonnet opus fable | opus | 37 |
| r60 s10 | haiku sonnet opus fable | opus | 28 |
| r60 s11 | haiku sonnet opus fable | nobody | 60 |

Games s1 and s2 ran at default effort, all others at `--effort low`. Seats were not rotated.
