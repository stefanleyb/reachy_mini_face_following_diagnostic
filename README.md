# Fixed-body stationary-face diagnostic runner

One local, interactive program that runs the preserved six stationary-face
trials from
[`../../coordination/archive/face_following_full_history.md`](../../coordination/archive/face_following_full_history.md)
in a single session and records
the daemon head-tracking data to automatic timestamped files. **The movement
data is the deliverable — there are no observer questions during the run.**

This is a **measurement aid**, not a product. It does not tune the tracker, does
not drive body-following, and does not touch the TV. It is not a Hugging Face
app.

## Wake / neutral / sleep

The robot boots with its head folded inside the body and motors off. At the
start of a session the runner performs the mandatory wake sequence
(`AGENTS.md`): enable motors -> verify enabled -> standard daemon `wake_up`
motion (unfold) -> operator physically confirms the head is clear. Every
centring afterwards targets the **exact same** daemon-defined neutral pose
(`INIT_HEAD_POSE` + `INIT_ANTENNAS_JOINT_POSITIONS`, body yaw 0), so "neutral"
never drifts between trials. At the end (or on Ctrl-C) it runs `goto_sleep` and
disables motors so the robot is left folded and safe.

## What each trial looks like (one keypress)

```
=== Trial 3 of 6 — SEATED / RIGHT ===
  Sit off to the robot's right ... hold still, face toward the robot.
  Press Enter to start the countdown…            <- you press Enter
                                                 <- head centres silently
  walk to position (seated, right) ... 10  9  8 ...   <- 10 s to get there
  ▶ START   [tracking on, ~15 s recorded]
  ■ END     [tracking off, head returns to neutral]
  saved 740 rows · 128 face-target frames
```

No distance, no marks, no verdict. The runner names the spot, you get there
during the countdown, then hold still. Defaults: 10 s countdown, 15 s window
(`--countdown`, `--window` to change). Automated per-trial check: motors still
enabled + head still at neutral; it only asks you anything if that fails.

After the six trials it offers one optional empty-scene control (step out of
view). At the end (or Ctrl-C) it sleeps the robot and disables motors.

Run `./run --preflight --host <robot>` first: connects, reports
daemon/camera/motor status and measured stream rates, exits **without moving
the robot**.

## Wake / neutral / sleep

The robot boots with its head folded inside the body and motors off. At session
start the runner does the mandatory wake (`AGENTS.md`): enable motors -> verify
-> standard daemon `wake_up` unfold -> **one** physical confirmation from you
that the head is clear. Every centring afterwards targets the identical
daemon-defined neutral pose (`INIT_HEAD_POSE` + `INIT_ANTENNAS`, yaw 0), so
neutral never drifts.

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
(`../../coordination/archive/face_following_full_history.md`,
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
results are in `../../coordination/archive/research_and_test_results.md`
Sections 20–21. Keep it for a
future focused measurement, not as a default step for body-following work.
