# Reachy Mini Face-Following Diagnostic

Measures how well your Reachy Mini's head tracks a face that is standing still.

## Why you might want this

The robot's head follows faces. Whether it does so *well* — settling cleanly,
or drifting and hunting around the target — is hard to judge by eye. This runs a
fixed set of trials, records what the head actually did at 50 Hz, and writes the
numbers to a file you can compare against a later run.

It is a measuring instrument. It changes nothing about how your robot behaves.

## What it asks of you

A person to stand in six positions — seated and standing, each at centre, left
and right — holding still for about 15 seconds per trial while the robot watches.
The program tells you where to stand, counts you down, then records. There are no
questions to answer during a run; the movement data is the output.

Budget about 10 minutes and a helper, or use yourself and a mirror.

## Before you start

- A Reachy Mini reachable at `reachy-mini.local`.
- Python 3.12+ with `numpy`, `scipy`, `requests`, `websockets` and `reachy_mini`.
- Room to stand roughly 1–2 m from the robot, left, centre and right.

⚠️ **This moves the robot.** It wakes the robot, centres the head and turns on
face tracking, so someone must be watching it throughout.

## Step by step

**1. Rehearse with no robot at all**, to see the flow:

```bash
./run --dry-run
```

**2. Check the connection without moving anything:**

```bash
./run --preflight
```

**3. Run it for real:**

```bash
./run
```

Follow the prompts: read where to stand, press Enter, walk there during the
countdown, hold still while it records. Repeat for each of the six trials.

**4. Find your results** in a timestamped folder, one per session. Each holds
the per-trial recordings plus a summary.

To measure how far the head can turn before the body must help, there is a
second, separate session:

```bash
./run_limits --dry-run     # rehearse first
./run_limits
```

## Safety

Before anything moves, the robot must be properly awake with its head clear of
the body. The runner checks the motors report `enabled` and will refuse to
proceed otherwise. Never start with the head still folded inside the body.

## If something goes wrong

- **"no python found"** — the launcher looks for a virtual environment beside or
  above this folder. Point it at yours with `DIAG_PYTHON=/path/to/python ./run`.
- **The robot isn't found** — try `./run --host <ip-address>` if mDNS
  (`reachy-mini.local`) is unreliable on your network.
- **A trial goes wrong** — stop with Ctrl-C. Completed trials are already
  written to disk; nothing is lost.

## For the technically curious

Six stationary-face trials (seated/standing × centre/left/right), each a ~15 s
recorded window at 50 Hz, with an optional empty-room control. The deliverable is
the head-yaw-relative-to-body stream plus per-trial metadata, timing and a
verdict, written as JSON so runs can be compared directly.

The separate limits session records head yaw relative to body at each operator
keypress, to establish where the head's comfortable range ends. It never commands
the body.

Some measurements need extra visibility into the daemon's internals, which is
supplied by a temporary instrumentation layer — see `instrument/`.

## Running the tests

No robot required:

```bash
for t in tests/test_*.py; do python "$t"; done
```

## Status and licence

Built against **daemon/SDK 1.10.0** on a Reachy Mini Wireless. A personal
project's working tooling, not an officially supported product. Not affiliated
with Pollen Robotics.

---

# Technical reference

## Output

```
../../runs/face_following/results/baseline_YYYYmmdd_HHMMSS/
    session.json                       run config, environment, daemon + camera identity
    trial_1_seated_center.json         per-trial metadata + timing + verdict
    trial_1_seated_center.samples.jsonl  one JSON row per sample (streamed to disk)
    ... trials 2..6 ...
    empty_scene_control.json / .samples.jsonl   (if run)
    summary.json                       machine-readable roll-up
    summary.md                         short human summary with file paths
```

Each sample row carries: wall + monotonic time, head pose (4×4) and joint
positions, and — when the daemon is instrumented — the raw (pre-filter) and
filtered normalised face centre with the daemon's observation timestamp, plus
the computed `tracking_target_pose` and eased `tracking_aim_pose`. Convenience
roll/pitch/yaw angles are added under `derived` (raw matrices are kept).

The operator never copies anything from the terminal; rows are flushed as they
are taken, so an interrupted trial still keeps its evidence.

## Daemon instrumentation (required for the full data set)

The public `reachy_mini` 1.10.0 API only exposes the *filtered* face centre, and
only inside `daemon_status` at **1 Hz** — too slow to characterise the
oscillation, and it never exposes the raw centre or the tracking target/aim.

`instrument/` adds exactly those at control-loop rate, **without editing
`site-packages`**. On the machine that runs the daemon (the robot's Raspberry Pi
for a Wireless unit), launch the daemon through the wrapper instead of the
normal command, for the baseline session only:

```
python instrument/run_instrumented_daemon.py <normal daemon args>
```

See [`instrument/README.md`](instrument/README.md) for exactly what it patches
and how to revert (stop using the wrapper — nothing on disk changes).

If you run the diagnostic against a **stock** daemon, the runner falls back to:
head pose at ~50 Hz (WebSocket) plus the filtered face target recovered at
~20 Hz by polling `/api/daemon/status` during each window (a REST GET recomputes
`face_target` live, so this beats the 1 Hz push). Trials are marked
`instrumented: false`. What the fallback cannot give: the raw pre-filter face
centre, and the daemon's exact internal `tracking_target_pose` / `tracking_aim`
(these are reconstructable offline from the filtered centre + a time-synced head
pose + K/D, but not identical).

Note: connect by IP or a resolvable name — the client pins the `.local` name to
an IP once and reuses a keep-alive HTTP session, because macOS otherwise re-runs
mDNS (~1 s) on every REST call and throttles polling to ~1 Hz.

## Usage

Use `./run` (finds the workspace venv for you) or
`../../.venv/bin/python diagnostic_runner.py`.

Rehearse first with no daemon and no robot:

```
./run --dry-run --countdown 5 --window 5
```

Real run:

```
./run --preflight --host reachy-mini.local   # read-only check, nothing moves
./run --host reachy-mini.local               # the session
```

Useful flags: `--countdown`, `--window` (default 30), `--sample-hz` (default
50), `--weight` (head-tracking blend), `--only 2,3` (repeat specific trials),
`--no-empty-control`, `--no-bell`, `--results-dir PATH`.

Run with `../../.venv/bin/python` so `reachy_mini` and its deps resolve.

## Second tool: head→body handoff-limit runner

`limits_runner.py` (`./run_limits`) is a separate interactive tool in this
folder for the body-following experiment's next step
(the project workstream history,
"Simple live threshold measurement"). It runs corrected face tracking
continuously with the body held at yaw 0 while the operator walks around, and
records the head pose each time the operator presses Enter at a felt limit.

Fixed 8-mark sequence, alternating so there is a full sweep between marks:
left/right × {**sustained** — "body should follow after several seconds";
**immediate** — "body should turn now"} × 2 readings each. Each press records
head yaw relative to the body over a `--window` (default 1.0 s) slice; the whole
session is streamed at 50 Hz to `session.samples.jsonl`.

Body yaw is watched (~1 Hz) and verified within `--body-yaw-tol` (default 3°)
for the whole run — a missing or drifted reading flags that mark/session
unreliable instead of assuming zero. `summary.md` leads with a session-validity
block, gives the **per-side** means (the real output), and a **suggested**
symmetric ±N° (rounded to 5°) that is a starting point only — a consistent
left/right difference is kept, and it warns if `immediate` came out smaller than
`sustained`. It does not choose dwell / rate / hysteresis and never commands the
body.

```
./run_limits --dry-run                      # rehearse, no daemon/robot
./run_limits --preflight --host reachy-mini.local   # read-only, nothing moves
./run_limits --host reachy-mini.local               # the session
bash run_limits_on_robot.sh                  # run on the Pi (localhost) when wifi is flaky
```

Shares `diag/client.py` + `instrument/` with the baseline runner; the wake /
neutral / sleep safety sequence lives in `diag/safety.py`. Instrumentation is
optional here (head pose, the primary signal, is on the stock 50 Hz stream).

## Tests

```
../../.venv/bin/python -m unittest discover -s tests
```

Stdlib `unittest` only — no new packages are installed into `.venv`. The
instrumentation tests apply the monkeypatch against the installed `reachy_mini`
and assert it is reversible.

## Not done here

This runner does not perform product face-following or body-following. Its
original physical baseline and corrected seated retest are complete; their
results are in the project workstream history
Sections 20–21. Keep it for a
future focused measurement, not as a default step for body-following work.
