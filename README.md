# AI tank battle

Four Claude models play an NES-style tank game against each other. Each player is a headless
Claude Code session (`claude -p`, no tools, subscription login, no API key). The engine shows the
board as text, the model replies with a plan of actions and a one-sentence reason, the engine runs
the plan and asks again when something notable happens. Every turn is logged.

## Rules

- 15x15 grid (or `--size N`). Four bases in the corners inside brick rings. Four tanks, three lives each.
- `UP` `DOWN` `LEFT` `RIGHT`: turn to face that way, then move one cell if it is empty. `FIRE`: a shell
  flies in a straight line until it meets something: a brick is destroyed, steel stops it, a tank
  loses a life, a base is destroyed and its owner is out. `STAY`: nothing.
- A hit tank respawns next to its own base. Last base standing wins. If the rounds run out with
  more than one base alive, nobody wins.
- Models reply with `ACTIONS: <1 to 8 actions>` and `WHY: <one sentence>`. The plan runs one action
  per round. The model is asked again when its plan runs out, a tank or base is hit, a tank in its
  row or column fires, an enemy enters its row or column, or its own move is blocked.
- Default order is sequential: seats A, B, C, D act one after another, and a player is asked only
  after the previous move is on the board. In `--parallel` mode all players who need a plan think
  at once and act in the order their replies arrive. The fastest reply acts first.
- Nothing in the prompt says how to play. The last rule line is "Decide your own tactics."

Variants:

- `--fog N`: enemy tanks are visible only within N cells of your tank or your base. Hits and
  destroyed bases are reported to everyone; for enemies out of sight the model gets the position
  where it last saw them.
- `--size N`: bigger square board; walls and random bricks scale.
- `--moves K`: in sequential mode each player performs K actions in a row on its turn.
- `--maze`: a brick maze with steel posts instead of the open field, mirrored four ways. Each base
  has one open side. One shot removes one brick, so tanks can tunnel.

## Run

```
python arena.py --models opus fable sonnet haiku --rounds 60 --seed 22 --effort low --fog 4 --maze --serve
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

- `meta`: models per seat, the full rules text, the map, fog, size, moves, maze, the arguments.
- `turn`: round, player, model, action, remaining plan, `WHY`, whether the action came from a fresh
  reply (`source: model`) or from the plan, the reason the model was asked, the full prompt and raw
  reply, latency, token usage, the events the action caused, and the position and lives of every
  player after it.
- `result`: winner, ranking, per-player lives, base, hits, kills, invalid replies, calls.

`runs/live*.log` and `runs/queue*.log` are the console views of the same games. The `random` files
are engine tests.

Games were played on 2026-10-30. The rules changed between games; each file's `meta` has the rules
that applied. Games s1 to s8 used a 6-cell shell range, a map with a steel cross in the centre, a
lives tiebreak at the round limit (s1 to s3), and one action per reply. Later games use the open map
with short steel walls between neighbouring bases, unlimited range, "nobody wins" at the limit, and
plans.

## Results

29 finished games, 4345 player turns, 1386 model calls, 0 invalid replies.

| seed | seats A B C D | order | map | fog | effort | winner | rounds | first base to fall |
|---|---|---|---|---|---|---|---|---|
| 1 | haiku sonnet opus fable | sequential | map 1 | - | default | sonnet | 26 | fable |
| 2 | haiku sonnet opus haiku | sequential | map 1 | - | default | opus | 25 | haiku |
| 3 | haiku sonnet haiku sonnet | parallel | map 1 | - | low | sonnet (lives tiebreak) | 40 | sonnet |
| 4 | haiku sonnet haiku sonnet | parallel | map 1 | - | low | haiku | 40 | sonnet |
| 5 | haiku sonnet haiku sonnet | parallel | map 1 | - | low | nobody | 60 | haiku |
| 6 | haiku sonnet haiku sonnet | parallel | map 1 | - | low | stopped by an engine bug in round 29 | - | - |
| 7 | haiku sonnet haiku sonnet | parallel | map 1 | - | low | sonnet | 38 | haiku |
| 8 | haiku sonnet haiku sonnet | parallel | map 1 | - | low | haiku | 50 | haiku |
| 9 | haiku sonnet opus fable | parallel | open | - | low | opus | 37 | fable |
| 10 | haiku sonnet opus fable | parallel | open | - | low | opus | 28 | fable |
| 11 | haiku sonnet opus fable | parallel | open | - | low | nobody | 60 | fable |
| 12 | haiku sonnet opus fable | parallel | open | 4 | low | nobody | 60 | fable |
| 13 | haiku sonnet opus fable | parallel | open 21 | 4 | low | opus | 55 | fable |
| 14 | haiku sonnet opus fable | parallel | open | 4 | low | sonnet | 40 | haiku |
| 15 | haiku sonnet opus fable | parallel | open 21 | 4 | low | opus | 50 | sonnet |
| 16 | opus sonnet haiku fable | parallel | open | 4 | low | opus | 34 | fable |
| 17 | opus sonnet haiku fable | parallel | open 21 | 4 | low | opus | 53 | haiku |
| 18 | opus sonnet haiku fable | sequential | open | 4 | low | nobody | 60 | sonnet |
| 19 | opus sonnet haiku fable | sequential | open 21 | 4 | low | opus | 59 | haiku |
| 20 | opus sonnet haiku fable | sequential | open | 4 | default | opus | 34 | sonnet |
| 21 | opus fable sonnet haiku | sequential | open | 4 | low | fable | 37 | haiku |
| 21 | opus fable sonnet haiku | sequential | open | 4 | low | sonnet | 46 | fable |
| 21 | opus fable fable fable | sequential | open | 4 | low | nobody | 60 | fable |
| 21 | opus fable fable opus | sequential | open | 4 | low | opus | 58 | fable |
| 21 | haiku fable haiku haiku | sequential | open | 4 | low | nobody | 60 | haiku |
| 21 | haiku fable haiku haiku | sequential | open | - | low | haiku | 40 | fable |
| 21 | haiku fable haiku haiku | sequential, 3 moves | open | 4 | low | haiku | 24 | haiku |
| 22 | haiku sonnet haiku sonnet | sequential | maze | 4 | low | nobody | 60 | sonnet |
| 22 | opus fable sonnet haiku | sequential | maze | 4 | low | nobody | 60 | haiku |
| 22 | opus fable sonnet haiku | sequential | maze | 4 | low | nobody | 60 | fable |

Per model, counting every seat a model played:

| model | seats | wins | bases destroyed | its base lost | first to fall | median seconds per reply |
|---|---|---|---|---|---|---|
| haiku | 40 | 4 | 20 | 28 | 11 | 4.0 |
| sonnet | 30 | 5 | 21 | 20 | 6 | 4.2 |
| opus | 21 | 10 | 26 | 7 | 0 | 4.2 |
| fable | 25 | 1 | 9 | 21 | 12 | 8.6 |

Reply time includes Claude Code startup and the session resume. In sequential mode reply time has
no effect on the game.
