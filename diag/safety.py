"""Shared daemon wake / neutral / sleep safety helpers.

Extracted from ``diagnostic_runner.py`` so every interactive tool in this folder
(the six-trial baseline runner and the handoff-limit runner) performs the exact
same mandatory sequence from ``AGENTS.md``:

    enable motors -> verify enabled -> standard daemon ``wake_up`` unfold ->
    ONE physical confirmation that the head is clear -> ... -> ``goto_sleep`` +
    disable motors at the end / on Ctrl-C.

Nothing here drives body-following or the TV. The head is only ever centred to
the daemon-defined neutral pose (body yaw 0).
"""

from __future__ import annotations

import sys
import time
from typing import Any, Optional

from diag import trials as T
from diag.trials import c, banner, ask_yes_no

# Frobenius norm of (head_pose - identity) that still counts as "at neutral".
NEUTRAL_OFFSET_OK = 0.15


def motor_mode(client: Any) -> str:
    try:
        return client.motor_status()
    except Exception as e:  # noqa: BLE001
        print(c(f"  could not read motor status: {e}", T.RED))
        return "unknown"


def wake_sequence(client: Any, center_duration_s: float = 1.5) -> bool:
    """The mandatory daemon-level wake, per AGENTS.md.

    enable motors -> verify enabled -> standard wake_up motion (unfold) ->
    ONE operator physical confirmation that the head is clear. The robot boots
    with its head folded inside the body; this brings it to awake-neutral.
    Returns True only once the operator confirms the head is clear.
    """
    banner("Waking the robot", T.YELLOW)
    mode = motor_mode(client)
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
        mode = motor_mode(client)
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


def preflight(client: Any, instrumented: bool) -> int:
    """Read-only status dump; never moves the robot. Returns a process code."""
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
    try:
        by = client.present_body_yaw()
        print(f"  present body yaw: {None if by is None else round(float(by), 4)} rad")
    except Exception as e:  # noqa: BLE001
        print(c(f"  body yaw read error: {e}", T.RED))
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
