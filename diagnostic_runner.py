#!/usr/bin/env python3
"""Stationary-face fixed-body daemon baseline — interactive diagnostic runner.

One local program, launched once, that walks an operator through the six
stationary-face trials from
``../../coordination/archive/face_following_full_history.md``
(seated/standing ×
center/left/right) and records the daemon head-tracking data to automatic
timestamped files. Per trial the only interaction is: read the spot, press
Enter, walk there during a countdown, hold still for a ~15 s recorded window.
No observer questions — the movement data is the deliverable.

It does **not** tune anything, drive body-following, or control the TV. It is
not a Hugging Face app. The head is only ever centred (body yaw held at 0);
face tracking is on for the recorded window only, and the return-to-neutral is
kept outside it.

Scope guard: the runner wakes the robot, sends head-centre gotos, and toggles
face tracking, so an operator must be watching it. Run ``--dry-run`` to
rehearse with no daemon/robot; ``--preflight`` for a read-only check.

Daemon instrumentation: for the raw pre-filter face centre and the exact
daemon target/aim, launch the daemon through
``instrument/run_instrumented_daemon.py`` (see ``instrument/deploy_to_robot.sh``).
Without it the runner records head pose at ~50 Hz + the filtered face target at
~20 Hz (status polling) and marks trials ``instrumented: false``.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any, Optional

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
def _default_results_dir(env_var: str, area: str) -> str:
    """Where sessions are written when no ``--results-dir`` is given.

    The umbrella workspace keeps generated evidence in ``runs/`` beside the tool
    repositories rather than inside them. Prefer that when this checkout sits in
    the workspace, so a direct ``python diagnostic_runner.py`` invocation
    lands in the same place as the ``./run`` launcher; fall back to a
    tool-local ``results/`` for a standalone checkout. An explicit environment
    variable always wins.
    """
    override = os.environ.get(env_var)
    if override:
        return override
    workspace_runs = os.path.normpath(
        os.path.join(THIS_DIR, os.pardir, os.pardir, "runs", area)
    )
    if os.path.isdir(workspace_runs):
        return os.path.join(workspace_runs, "results")
    return os.path.join(THIS_DIR, "results")

if THIS_DIR not in sys.path:
    sys.path.insert(0, THIS_DIR)

from diag import trials as T  # noqa: E402
from diag.recording import SessionRecorder  # noqa: E402
from diag.trials import (  # noqa: E402
    RunConfig,
    c,
    banner,
    bell,
    ask_yes_no,
    countdown,
    press_enter,
)


def build_client(cfg: RunConfig):
    if cfg.dry_run:
        from diag.fake import FakeClient

        return FakeClient(oscillate=True, instrumented=True)
    from diag.client import DiagnosticClient

    return DiagnosticClient(cfg.host, cfg.port)


# --------------------------------------------------------------------------
# wake / neutral / sleep
# --------------------------------------------------------------------------

NEUTRAL_OFFSET_OK = 0.15  # Frobenius norm of (head_pose - identity) that still counts as "at neutral"


def _motor_mode(client: Any) -> str:
    try:
        return client.motor_status()
    except Exception as e:  # noqa: BLE001
        print(c(f"  could not read motor status: {e}", T.RED))
        return "unknown"


def wake_sequence(client: Any, cfg: RunConfig) -> bool:
    """The mandatory daemon-level wake, per AGENTS.md.

    enable motors -> verify enabled -> standard wake_up motion (unfold) ->
    ONE operator physical confirmation that the head is clear. The robot boots
    with its head folded inside the body; this brings it to awake-neutral.
    """
    banner("Waking the robot", T.YELLOW)
    mode = _motor_mode(client)
    if mode != "enabled":
        print("  enabling motors (they hold the current folded pose)…")
        try:
            client.set_motor_mode("enabled")
        except Exception:  # noqa: BLE001
            try:
                client.set_motor_mode_cmd("enabled")
            except Exception as e:  # noqa: BLE001
                print(c(f"  enable failed: {e}", T.RED))
                return False
        time.sleep(0.6)
        mode = _motor_mode(client)
    if mode != "enabled":
        print(c("  motors did not report enabled — aborting.", T.RED))
        return False

    print("  running the standard wake_up motion (unfold to neutral)…")
    try:
        client.wake_up_motion()
    except Exception as e:  # noqa: BLE001
        print(c(f"  wake_up command failed: {e}", T.RED))
    deadline = time.monotonic() + 12.0
    reached_neutral = False
    while time.monotonic() < deadline:
        off = client.head_pose_offset_from_neutral()
        if off is not None and off <= NEUTRAL_OFFSET_OK:
            reached_neutral = True
            break
        time.sleep(0.3)
    if not reached_neutral:
        print(
            c(
                "  The pose stream did not verify awake-neutral; physical confirmation is required.",
                T.YELLOW,
            )
        )

    # No direct centring command is allowed before this physical confirmation:
    # an accepted wake request is not proof that the folded head actually moved.
    return ask_yes_no(
        "  Look at the robot — is the head fully clear of the body and upright?", None
    )


def pretrial_check(client: Any, cfg: RunConfig) -> bool:
    """Fast gate before each trial: motors still enabled + head still near neutral.

    Only escalates to a physical re-confirm when the automated check fails.
    """
    mode = _motor_mode(client)
    off = client.head_pose_offset_from_neutral()
    if mode == "enabled" and off is not None and off <= NEUTRAL_OFFSET_OK:
        return True
    print(c(f"  pre-trial check: motor_mode={mode} head_offset={off}", T.YELLOW))
    if mode != "enabled":
        if not wake_sequence(client, cfg):
            return False
    else:
        print("  Head is not at neutral. Re-centring…")
        try:
            client.center_head(duration=max(cfg.center_duration_s, 2.0))
        except Exception as e:  # noqa: BLE001
            print(c(f"  centring failed: {e}", T.RED))
        if not ask_yes_no("  Confirm the head is clear of the body and upright?", None):
            return False
    return True


def sleep_sequence(client: Any, settle_s: float = 6.0) -> None:
    """Fold the head back into the body and drop torque — safe rest state."""
    banner("Sleep sequence", T.YELLOW)
    try:
        client.set_head_tracking(False)
    except Exception:  # noqa: BLE001
        pass
    if not ask_yes_no("  Put the robot to sleep now (fold head into body)?", True):
        print("  Leaving the robot awake at neutral. Sleep it yourself when done.")
        return
    try:
        client.goto_sleep_motion()
        time.sleep(settle_s)
    except Exception as e:  # noqa: BLE001
        print(c(f"  goto_sleep failed: {e}", T.RED))
    try:
        client.set_motor_mode("disabled")
    except Exception:  # noqa: BLE001
        try:
            client.set_motor_mode_cmd("disabled")
        except Exception:  # noqa: BLE001
            pass
    print("  Robot asleep, motors disabled.")


def daemon_identity(client: Any) -> dict[str, Any]:
    ident: dict[str, Any] = {}
    try:
        ds = client.daemon_status_rest()
        ident["version"] = ds.get("version")
        ident["robot_name"] = ds.get("robot_name")
        ident["camera_specs_name"] = ds.get("camera_specs_name")
        bs = ds.get("backend_status") or {}
        ident["backend_status"] = bs
    except Exception as e:  # noqa: BLE001
        ident["error"] = str(e)
    return ident


# --------------------------------------------------------------------------
# one trial
# --------------------------------------------------------------------------


def run_tracking_window(
    client: Any,
    trial_rec: Any,
    cfg: RunConfig,
) -> dict[str, Any]:
    """Enable tracking, sample for the window, disable. Returns window stats."""
    period = 1.0 / cfg.sample_hz
    n_target = int(round(cfg.tracking_window_s * cfg.sample_hz))

    raw_obs_ts: set[float] = set()
    filt_obs_ts: set[float] = set()
    status_face_ts: set[float] = set()
    clstats_last: Optional[str] = None

    if hasattr(client, "start_status_polling"):
        client.start_status_polling()

    banner("START — daemon face tracking ON", T.GREEN)
    bell(cfg.terminal_bell, 2)
    t_start_mono = time.monotonic()
    t_start_wall = time.time()
    client.set_head_tracking(True, cfg.tracking_weight)

    next_t = time.monotonic()
    for i in range(n_target):
        snap = client.snapshot()
        trial_rec.add_sample(snap)

        ds = snap.get("daemon_status") or {}
        ft = ds.get("face_target") or {}
        if ft.get("ts") is not None:
            status_face_ts.add(float(ft["ts"]))
        bs = ds.get("backend_status") or {}
        cls = bs.get("control_loop_stats")
        if cls is not None:
            key = repr(cls)
            if key != clstats_last:
                trial_rec.control_loop_stats.append(
                    {"t_mono": snap["t_mono"], "stats": cls}
                )
                clstats_last = key

        ftd = snap.get("ft_debug") or {}
        if ftd.get("raw_obs_ts_mono") is not None:
            raw_obs_ts.add(round(float(ftd["raw_obs_ts_mono"]), 4))
        if ftd.get("filtered_obs_ts_mono") is not None:
            filt_obs_ts.add(round(float(ftd["filtered_obs_ts_mono"]), 4))

        remaining = cfg.tracking_window_s - (time.monotonic() - t_start_mono)
        if i % int(cfg.sample_hz) == 0:
            sys.stdout.write(
                "\r  " + c(f"recording… {remaining:5.1f} s left", T.DIM) + "   "
            )
            sys.stdout.flush()

        next_t += period
        sleep = next_t - time.monotonic()
        if sleep > 0:
            time.sleep(sleep)
        else:
            next_t = time.monotonic()

    t_end_mono = time.monotonic()
    t_end_wall = time.time()
    client.set_head_tracking(False)
    poll_ok = poll_fail = None
    if hasattr(client, "stop_status_polling"):
        poll_ok, poll_fail = client.stop_status_polling()
    sys.stdout.write("\r" + " " * 60 + "\r")
    banner("END — daemon face tracking OFF", T.RED)
    bell(cfg.terminal_bell, 3)

    elapsed = t_end_mono - t_start_mono
    measured_hz = trial_rec.sample_count / elapsed if elapsed > 0 else None
    instrumented_here = bool(raw_obs_ts or filt_obs_ts)
    face_frames = (
        len(filt_obs_ts) if instrumented_here else (len(status_face_ts) or None)
    )
    trial_rec.timing = {
        "window_start_mono": t_start_mono,
        "window_start_wall": t_start_wall,
        "window_end_mono": t_end_mono,
        "window_end_wall": t_end_wall,
        "window_elapsed_s": elapsed,
        "requested_window_s": cfg.tracking_window_s,
        "requested_sample_hz": cfg.sample_hz,
    }
    return {
        "measured_sample_hz": None if measured_hz is None else round(measured_hz, 2),
        "face_frames_seen": face_frames,
        "raw_obs_frames": len(raw_obs_ts) or None,
        "filtered_obs_frames": len(filt_obs_ts) or None,
        "status_face_target_frames": len(status_face_ts) or None,
        "status_poll_ok": poll_ok,
        "status_poll_fail": poll_fail,
    }


def run_trial(
    client: Any,
    session: SessionRecorder,
    cfg: RunConfig,
    index: int,
    instrumented: bool,
) -> None:
    t = T.TRIALS[index]
    label = T.trial_label(index)
    banner(f"Trial {index + 1} of 6 — {t['height'].upper()} / {t['position'].upper()}", T.CYAN)
    print(f"  {c(T.trial_where(index), T.BOLD)}")
    press_enter("  Stand by the robot (not yet in position). Press Enter to start the countdown…")

    if not pretrial_check(client, cfg):
        print(c("  Skipping this trial (head not confirmed clear).", T.RED))
        return
    try:
        client.set_head_tracking(False)
        time.sleep(0.2)
        client.center_head(duration=cfg.center_duration_s)
    except Exception as e:  # noqa: BLE001
        print(c(f"  centring goto failed: {e}", T.RED))

    meta = {
        "index": index + 1,
        "height": t["height"],
        "position": t["position"],
        "where": T.trial_where(index),
        "countdown_s": cfg.countdown_s,
        "window_s": cfg.tracking_window_s,
    }
    trial_rec = session.new_trial(label, meta)

    countdown(
        cfg.countdown_s,
        f"walk to position ({t['height']}, {t['position']}) and hold still",
        bell_enabled=cfg.terminal_bell,
    )

    window_stats = run_tracking_window(client, trial_rec, cfg)

    if cfg.recenter_between_trials:
        try:
            client.center_head(duration=cfg.center_duration_s)
        except Exception as e:  # noqa: BLE001
            print(c(f"  recentre goto failed: {e}", T.RED))

    result = trial_rec.finish(
        verdict={},
        instrumented=instrumented,
        measured_sample_hz=window_stats["measured_sample_hz"],
        face_frames_seen=window_stats["face_frames_seen"],
        extra={
            "raw_obs_frames": window_stats["raw_obs_frames"],
            "filtered_obs_frames": window_stats["filtered_obs_frames"],
            "status_face_target_frames": window_stats["status_face_target_frames"],
            "status_poll_ok": window_stats["status_poll_ok"],
            "status_poll_fail": window_stats["status_poll_fail"],
        },
    )
    session.register_trial_result(result)
    fc = result.get("face_frames_seen")
    print(c(f"  saved {trial_rec.sample_count} rows"
            + (f" · {fc} face-target frames" if fc else " · no face detected"), T.GREEN))


def run_empty_control(
    client: Any, session: SessionRecorder, cfg: RunConfig, instrumented: bool
) -> None:
    banner("Optional control — empty scene, no face", T.CYAN)
    if not ask_yes_no(f"  Run a {cfg.tracking_window_s:.0f}s empty-scene control (step out of view)?", True):
        return
    if not pretrial_check(client, cfg):
        return
    try:
        client.center_head(duration=cfg.center_duration_s)
    except Exception as e:  # noqa: BLE001
        print(c(f"  centring failed: {e}", T.RED))
    trial_rec = session.new_trial("empty_scene_control", {"height": "n/a", "position": "empty"})
    countdown(cfg.countdown_s, "step fully out of the camera view", bell_enabled=cfg.terminal_bell)
    stats = run_tracking_window(client, trial_rec, cfg)
    if cfg.recenter_between_trials:
        try:
            client.center_head(duration=cfg.center_duration_s)
        except Exception:  # noqa: BLE001
            pass
    result = trial_rec.finish(
        verdict={},
        instrumented=instrumented,
        measured_sample_hz=stats["measured_sample_hz"],
        face_frames_seen=stats["face_frames_seen"],
        extra={
            "raw_obs_frames": stats["raw_obs_frames"],
            "filtered_obs_frames": stats["filtered_obs_frames"],
            "status_face_target_frames": stats["status_face_target_frames"],
            "status_poll_ok": stats["status_poll_ok"],
            "status_poll_fail": stats["status_poll_fail"],
        },
    )
    session.register_trial_result(result)
    print(c(f"  saved {trial_rec.sample_count} rows", T.GREEN))


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def parse_args(argv: Optional[list[str]] = None) -> RunConfig:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="reachy-mini.local", help="daemon host (default: reachy-mini.local)")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--countdown", type=float, default=10.0, help="seconds to walk to position before START (default 10)")
    p.add_argument("--window", type=float, default=15.0, help="tracking window seconds (default 15)")
    p.add_argument("--sample-hz", type=float, default=50.0)
    p.add_argument("--center-duration", type=float, default=1.5)
    p.add_argument("--weight", type=float, default=1.0, help="head-tracking blend weight")
    p.add_argument("--no-recenter", action="store_true", help="do not recentre between trials")
    p.add_argument("--no-empty-control", action="store_true")
    p.add_argument("--no-bell", action="store_true", help="disable terminal bell cue")
    p.add_argument(
        "--results-dir",
        default=_default_results_dir("REACHY_FACE_RESULTS_DIR", "face_following"),
    )
    p.add_argument("--only", type=str, default=None, help="comma list of 1-based trial numbers, e.g. 2,3")
    p.add_argument("--dry-run", action="store_true", help="rehearse with a fake client, no daemon/robot")
    p.add_argument("--preflight", action="store_true",
                   help="connect, report daemon/camera/motor/stream status, then exit (no robot motion)")
    a = p.parse_args(argv)

    only = None
    if a.only:
        only = [int(x) for x in a.only.split(",") if x.strip()]

    cfg = RunConfig(
        host=a.host,
        port=a.port,
        countdown_s=a.countdown,
        tracking_window_s=a.window,
        sample_hz=a.sample_hz,
        center_duration_s=a.center_duration,
        tracking_weight=a.weight,
        recenter_between_trials=not a.no_recenter,
        empty_scene_control=not a.no_empty_control,
        terminal_bell=not a.no_bell,
        dry_run=a.dry_run,
        only_trials=only,
    )
    cfg._results_dir = a.results_dir  # type: ignore[attr-defined]
    cfg._preflight = a.preflight  # type: ignore[attr-defined]
    return cfg


def measure_stream_rates(client: Any, seconds: float = 3.0) -> dict[str, Any]:
    """Sample the cached streams for a few seconds; report observed rates."""
    hp_ts: set[float] = set()
    ft_seq: set[int] = set()
    face_ts: set[float] = set()
    if hasattr(client, "start_status_polling"):
        client.start_status_polling()
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        s = client.snapshot()
        if s.get("head_pose") is not None and s.get("head_pose_age") is not None:
            hp_ts.add(round(s["t_mono"] - s["head_pose_age"], 3))
        ftd = s.get("ft_debug") or {}
        if ftd.get("seq") is not None:
            ft_seq.add(int(ftd["seq"]))
        ds = s.get("daemon_status") or {}
        fto = ds.get("face_target") or {}
        if fto.get("ts") is not None:
            face_ts.add(float(fto["ts"]))
        time.sleep(0.01)
    poll = client.stop_status_polling() if hasattr(client, "stop_status_polling") else (None, None)
    return {
        "seconds": seconds,
        "head_pose_hz": round(len(hp_ts) / seconds, 1),
        "ft_debug_hz": round(len(ft_seq) / seconds, 1),
        "face_target_distinct_hz": round(len(face_ts) / seconds, 1),
        "status_poll_ok": poll[0],
        "status_poll_fail": poll[1],
    }


def preflight(client: Any, cfg: RunConfig, instrumented: bool) -> int:
    banner("Preflight — read only, no robot motion", T.CYAN)
    ident = daemon_identity(client)
    print(f"  daemon version : {ident.get('version')}")
    print(f"  robot name     : {ident.get('robot_name')}")
    print(f"  camera specs   : {ident.get('camera_specs_name')}")
    bs = ident.get("backend_status") or {}
    print(f"  motor mode     : {c(str(bs.get('motor_control_mode')), T.BOLD)}")
    print(f"  control loop   : {bs.get('control_loop_stats')}")
    try:
        cam = client.camera_specs()
        print(f"  /api/camera/specs: name={cam.get('name')}  K set={'K' in cam}  D set={'D' in cam}")
    except Exception as e:  # noqa: BLE001
        print(c(f"  camera specs error: {e}", T.RED))
    off = client.head_pose_offset_from_neutral() if hasattr(client, "head_pose_offset_from_neutral") else None
    if off is None:
        where = "unknown"
    elif off <= NEUTRAL_OFFSET_OK:
        where = "at neutral"
    else:
        where = "not at neutral (folded / asleep / turned)"
    print(f"  head offset from neutral: {None if off is None else round(off, 3)}  ({where})")
    print(f"  instrumentation: {c('present' if instrumented else 'absent (fallback: fast status poll)', T.BOLD)}")
    print("  measuring stream rates (3 s)…")
    rates = measure_stream_rates(client, 3.0)
    print(f"  head_pose ~ {rates['head_pose_hz']} Hz   ft_debug ~ {rates['ft_debug_hz']} Hz   "
          f"face_target distinct ~ {rates['face_target_distinct_hz']} Hz")
    print(f"  status poll ok/fail: {rates['status_poll_ok']}/{rates['status_poll_fail']}")
    if rates["status_poll_fail"] and rates["status_poll_ok"] is not None and \
            rates["status_poll_fail"] > rates["status_poll_ok"]:
        print(c("  WARNING: status polling is failing often — wifi link looks unreliable.", T.RED))
    print()
    print(c("  Preflight OK. Nothing was moved.", T.GREEN))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    cfg = parse_args(argv)
    results_dir = getattr(cfg, "_results_dir", os.path.join(THIS_DIR, "results"))
    os.makedirs(results_dir, exist_ok=True)

    banner("Reachy Mini — fixed-body stationary-face baseline", T.CYAN)
    if cfg.dry_run:
        print(c("  DRY RUN: fake client, no daemon, no robot. Files are still written.", T.YELLOW))
    print(f"  daemon: {cfg.host}:{cfg.port}")
    print(f"  window {cfg.tracking_window_s:.0f}s  ·  countdown {cfg.countdown_s:.0f}s  ·  "
          f"sample {cfg.sample_hz:.0f} Hz  ·  weight {cfg.tracking_weight}")

    client = build_client(cfg)
    tries = 1 if cfg.dry_run else 6
    for attempt in range(1, tries + 1):
        try:
            client.connect()
            client.wait_ready(timeout=10.0)
            break
        except Exception as e:  # noqa: BLE001
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
            if attempt == tries:
                print(c(f"  Could not connect to the daemon after {tries} tries: {e}", T.RED))
                print(c("  (the Wireless unit drops off wifi when idle — wake it / retry)", T.DIM))
                return 2
            print(c(f"  connect attempt {attempt}/{tries} failed, retrying in 5s…", T.YELLOW))
            client = build_client(cfg)
            time.sleep(5)

    instrumented = client.wait_ft_debug(timeout=3.0)

    if getattr(cfg, "_preflight", False):
        try:
            return preflight(client, cfg, instrumented)
        finally:
            client.close()

    if instrumented:
        print(c("  face_tracking_debug stream present — full data set will be recorded.", T.GREEN))
    else:
        print(c(
            "  No face_tracking_debug stream (daemon not instrumented).\n"
            "  Fallback data set: head pose @50Hz + face target @~20Hz (status poll).\n"
            "  Missing: raw pre-filter centre, exact daemon target/aim.\n"
            "  This is a solid FIRST baseline pass. Deploy the instrumentation for\n"
            "  the follow-up 'why' pass:  bash instrument/deploy_to_robot.sh --run",
            T.YELLOW,
        ))
        if not cfg.dry_run and not ask_yes_no("  Run the first baseline pass now (without instrumentation)?", True):
            client.close()
            return 1

    session = SessionRecorder(results_dir, cfg.as_dict())
    ident = daemon_identity(client)
    try:
        cam = client.camera_specs()
    except Exception as e:  # noqa: BLE001
        cam = {"error": str(e)}
    session.write_session(ident, cam, extra={"instrumented": instrumented})
    print(c(f"  Session dir: {session.dir}", T.GREEN))

    selected = list(range(6))
    if cfg.only_trials:
        selected = [i - 1 for i in cfg.only_trials if 1 <= i <= 6]

    aborted = False
    try:
        if not wake_sequence(client, cfg):
            print(c("  Wake not confirmed — aborting.", T.RED))
            return 1
        print()
        print(f"  {len(selected)} trials. Each: the runner names a spot, you press Enter,")
        print(f"  a {cfg.countdown_s:.0f}s countdown lets you walk there, then {cfg.tracking_window_s:.0f}s of tracking is recorded.")
        print("  Hold still through each window. No questions during the run.")
        if not cfg.dry_run and not ask_yes_no("  Ready?", True):
            return 0

        for idx in selected:
            run_trial(client, session, cfg, idx, instrumented)

        if cfg.empty_scene_control and not cfg.only_trials:
            run_empty_control(client, session, cfg, instrumented)
    except KeyboardInterrupt:
        aborted = True
        print(c("\n  Interrupted.", T.RED))
    finally:
        try:
            client.set_head_tracking(False)
        except Exception:  # noqa: BLE001
            pass
        sleep_sequence(client, settle_s=0.1 if cfg.dry_run else 6.0)
        client.close()

    banner("Session complete" if not aborted else "Session aborted", T.GREEN if not aborted else T.RED)
    print(f"  Results: {c(session.dir, T.BOLD)}")
    print(f"  Summary: {os.path.join(session.dir, 'summary.md')}")
    print(f"           {os.path.join(session.dir, 'summary.json')}")
    print()
    print("  Next: capture the external observer video filename(s) alongside this")
    print("  directory, then analyse per")
    print("  coordination/workstreams/face-and-body/BRIEF.md.")
    return 0 if not aborted else 130


if __name__ == "__main__":
    raise SystemExit(main())
