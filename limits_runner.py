#!/usr/bin/env python3
"""Head→body handoff-limit measurement — interactive runner.

One local program that runs corrected face tracking with the body held at yaw 0
while the operator walks around, and records the head pose each time the operator
presses Enter at a felt limit. Eight marks, fixed alternating order (see
``diag/limits.py``):

    left/right × {sustained: "body should follow after several seconds",
                  immediate: "body should move right now"} × 2 readings each.

The deliverable is the recorded head-yaw-relative-to-body at each press plus the
continuous 50 Hz stream for the whole session. It does **not** choose dwell /
rate / hysteresis and it never commands the body — see
``../../coordination/workstreams/face-and-body/BRIEF.md``.

Scope guard: the runner wakes the robot, centres the head, and enables face
tracking, so an operator must be watching it. ``--dry-run`` rehearses with a fake
client; ``--preflight`` is a read-only check.

Daemon instrumentation is optional here (head pose, the primary signal, is on the
stock 50 Hz stream). With ``instrument/run_instrumented_daemon.py`` the marks
also capture the raw/filtered face centre and the daemon target/aim pose.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import asdict, dataclass
from typing import Any, Optional

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
def _default_results_dir(env_var: str, area: str) -> str:
    """Where sessions are written when no ``--results-dir`` is given.

    The umbrella workspace keeps generated evidence in ``runs/`` beside the tool
    repositories rather than inside them. Prefer that when this checkout sits in
    the workspace, so a direct ``python limits_runner.py`` invocation lands
    in the same place as the ``./run`` launcher; fall back to a tool-local
    ``results/`` for a standalone checkout. An explicit environment variable
    always wins.
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
from diag.limits import MARK_SEQUENCE, LimitsRecorder, mark_headline, mark_prompt  # noqa: E402
from diag.safety import (  # noqa: E402
    daemon_identity,
    motor_mode,
    preflight,
    sleep_sequence,
    wake_sequence,
)
from diag.trials import c, banner, ask_yes_no, press_enter  # noqa: E402

_DEG = 57.29577951308232


@dataclass
class LimitsConfig:
    host: str = "reachy-mini.local"
    port: int = 8000
    window_s: float = 1.0
    sample_hz: float = 50.0
    center_duration_s: float = 1.5
    tracking_weight: float = 1.0
    body_yaw_tol_deg: float = 3.0
    terminal_bell: bool = False
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_client(cfg: LimitsConfig) -> Any:
    if cfg.dry_run:
        from diag.fake import FakeClient

        return FakeClient(oscillate=True, instrumented=True)
    from diag.client import DiagnosticClient

    return DiagnosticClient(cfg.host, cfg.port)


def parse_args(argv: Optional[list[str]] = None) -> tuple[LimitsConfig, str, bool]:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--host", default="reachy-mini.local")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--window", type=float, default=1.0,
                   help="seconds of samples before each Enter to average (default 1.0)")
    p.add_argument("--sample-hz", type=float, default=50.0)
    p.add_argument("--center-duration", type=float, default=1.5)
    p.add_argument("--weight", type=float, default=1.0, help="head-tracking blend weight")
    p.add_argument("--body-yaw-tol", type=float, default=3.0,
                   help="deg of body yaw still treated as 'near zero' (default 3.0)")
    p.add_argument("--bell", action="store_true", help="terminal bell on each prompt")
    p.add_argument(
        "--results-dir",
        default=_default_results_dir("REACHY_FACE_RESULTS_DIR", "face_following"),
    )
    p.add_argument("--dry-run", action="store_true",
                   help="rehearse with a fake client, no daemon/robot")
    p.add_argument("--preflight", action="store_true",
                   help="connect, report status, exit — no robot motion")
    a = p.parse_args(argv)
    cfg = LimitsConfig(
        host=a.host,
        port=a.port,
        window_s=a.window,
        sample_hz=a.sample_hz,
        center_duration_s=a.center_duration,
        tracking_weight=a.weight,
        body_yaw_tol_deg=a.body_yaw_tol,
        terminal_bell=a.bell,
        dry_run=a.dry_run,
    )
    return cfg, a.results_dir, a.preflight


def _connect(cfg: LimitsConfig) -> Optional[Any]:
    client = build_client(cfg)
    tries = 1 if cfg.dry_run else 6
    for attempt in range(1, tries + 1):
        try:
            client.connect()
            client.wait_ready(timeout=10.0)
            return client
        except Exception as e:  # noqa: BLE001
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
            if attempt == tries:
                print(c(f"  Could not connect after {tries} tries: {e}", T.RED))
                print(c("  (the Wireless unit drops off wifi when idle — wake it / retry)", T.DIM))
                return None
            print(c(f"  connect attempt {attempt}/{tries} failed, retrying in 5s…", T.YELLOW))
            client = build_client(cfg)
            time.sleep(5)
    return None


def _confirmation_line(res: dict[str, Any], first_left_sign: Optional[str]) -> str:
    mean = res["rel_deg_mean"]
    if mean is None:
        return c("  recorded — but no head-yaw sample was available!", T.RED)
    txt = (
        f"  recorded {res['side']} · {res['limit_kind']}: "
        f"head yaw ≈ {mean:+.1f}° rel. body "
        f"(min {res['rel_deg_min']:+.1f}°, max {res['rel_deg_max']:+.1f}°, "
        f"σ {res['rel_deg_std']:.1f}°, {res['n_samples']} samples)"
    )
    if res["used_fallback_reading"]:
        txt += c("  [single fallback reading — sampler was starved]", T.YELLOW)
    line = c(txt, T.GREEN if res.get("rel_reliable") else T.YELLOW)
    if not res.get("rel_reliable"):
        by = res.get("body_yaw_deg")
        why = "body yaw unknown" if by is None else f"body yaw {by:+.1f}° (out of tolerance)"
        line += c(f"\n  ! this mark is flagged unreliable: {why}", T.RED)
    if first_left_sign is not None:
        line += c(
            f"\n  note: the robot's LEFT reads as {first_left_sign} yaw on this setup.",
            T.DIM,
        )
    return line


def run(cfg: LimitsConfig, results_dir: str, want_preflight: bool) -> int:
    os.makedirs(results_dir, exist_ok=True)
    banner("Reachy Mini — head→body handoff-limit measurement", T.CYAN)
    if cfg.dry_run:
        print(c("  DRY RUN: fake client, no daemon, no robot. Files are still written.", T.YELLOW))
    print(f"  daemon: {cfg.host}:{cfg.port}")
    print(f"  window {cfg.window_s:.1f}s  ·  sample {cfg.sample_hz:.0f} Hz  ·  weight {cfg.tracking_weight}")
    print(f"  {len(MARK_SEQUENCE)} marks: left/right × (sustained, immediate) × 2 readings")

    client = _connect(cfg)
    if client is None:
        return 2

    instrumented = client.wait_ft_debug(timeout=3.0)

    if want_preflight:
        try:
            return preflight(client, instrumented)
        finally:
            client.close()

    if instrumented:
        print(c("  face_tracking_debug stream present — marks include daemon target/aim.", T.GREEN))
    else:
        print(c("  No face_tracking_debug stream — recording head pose @50Hz + face "
                "target @~20Hz. That is enough for this measurement.", T.YELLOW))

    body_yaw_tol_rad = cfg.body_yaw_tol_deg / _DEG
    recorder = LimitsRecorder(results_dir, cfg.as_dict(), body_yaw_tol_rad=body_yaw_tol_rad)
    ident = daemon_identity(client)
    try:
        cam = client.camera_specs()
    except Exception as e:  # noqa: BLE001
        cam = {"error": str(e)}

    aborted = False
    sampling = False
    try:
        if not wake_sequence(client, cfg.center_duration_s):
            print(c("  Wake not confirmed — aborting.", T.RED))
            recorder.write_session(ident, cam, extra={"instrumented": instrumented, "aborted": "wake"})
            recorder.finish()
            return 1

        print("  centring the head (body yaw 0)…")
        try:
            client.set_head_tracking(False)
            time.sleep(0.2)
            client.center_head(duration=cfg.center_duration_s)
        except Exception as e:  # noqa: BLE001
            print(c(f"  centring goto failed: {e}", T.RED))

        try:
            body_yaw0 = client.present_body_yaw()
        except Exception:  # noqa: BLE001
            body_yaw0 = None
        recorder.set_baseline_body_yaw(body_yaw0)
        if body_yaw0 is None:
            print(c(f"  baseline body yaw: READ FAILED — cannot verify the body is at "
                    f"zero. The session will be flagged unreliable.", T.RED))
        elif abs(body_yaw0) > body_yaw_tol_rad:
            print(c(f"  baseline body yaw: {body_yaw0 * _DEG:+.1f}° — NOT within "
                    f"±{cfg.body_yaw_tol_deg:.1f}°. The body is turned; recentre or "
                    f"the session is invalid.", T.RED))
        else:
            print(c(f"  baseline body yaw: {body_yaw0 * _DEG:+.1f}° "
                    f"(within ±{cfg.body_yaw_tol_deg:.1f}°)", T.GREEN))
        recorder.write_session(
            ident, cam,
            extra={"instrumented": instrumented, "baseline_body_yaw_rad": body_yaw0},
        )
        print(c(f"  Session dir: {recorder.dir}", T.GREEN))
        if (not cfg.dry_run
                and (body_yaw0 is None or abs(body_yaw0) > body_yaw_tol_rad)
                and not ask_yes_no("  Body yaw is not verified near zero. Continue anyway "
                                   "(session stays flagged)?", False)):
            recorder.finish()
            return 1

        print()
        print("  Face tracking will now stay ON for the whole session and follow you.")
        print("  For each mark: read the limit, move so the head reaches that angle,")
        print("  hold briefly, and press Enter. No need to return to centre between marks.")
        if not cfg.dry_run and not ask_yes_no("  Ready to enable tracking and start?", True):
            recorder.finish()
            return 0

        client.set_head_tracking(True, cfg.tracking_weight)
        recorder.start_sampling(client, cfg.sample_hz)
        sampling = True
        time.sleep(0.5)  # let the sample buffer prime

        first_left_sign: Optional[str] = None
        left_sign_announced = False
        for m in MARK_SEQUENCE:
            banner(mark_headline(m), T.CYAN)
            print(f"  {c(mark_prompt(m), T.BOLD)}")
            press_enter("  Press Enter when the head is at the limit…")
            res = recorder.mark(m, client, cfg.window_s)

            sign_note = None
            if (not left_sign_announced and m["side"] == "left"
                    and res["rel_deg_mean"] is not None):
                first_left_sign = "negative" if res["rel_deg_mean"] < 0 else "positive"
                sign_note = first_left_sign
                left_sign_announced = True
            print(_confirmation_line(res, sign_note))

            if not cfg.dry_run:
                mode = motor_mode(client)
                if mode != "enabled":
                    print(c(f"  WARNING: motor mode is '{mode}', not 'enabled'.", T.RED))
                    if not ask_yes_no("  Re-run the wake sequence and continue?", True):
                        raise KeyboardInterrupt
                    if not wake_sequence(client, cfg.center_duration_s):
                        raise KeyboardInterrupt
                    client.set_head_tracking(True, cfg.tracking_weight)

        client.set_head_tracking(False)
        recorder.stop_sampling()
        sampling = False
        summary = recorder.finish()

        val = summary["session_validity"]
        banner("Session complete" if val["valid"] else "Session complete — FLAGGED",
               T.GREEN if val["valid"] else T.RED)
        if not val["valid"]:
            print(c("  Body yaw was not verified near zero — the angles below are unreliable:", T.RED))
            for r in val["reasons"]:
                print(c(f"    - {r}", T.RED))
        buckets = summary["rollup"]["buckets"]
        for kind in ("sustained", "immediate"):
            lft = buckets[f"left_{kind}"]["mean_deg"]
            rgt = buckets[f"right_{kind}"]["mean_deg"]
            s = summary["rollup"]["suggestion"][kind]
            lft_s = "—" if lft is None else f"{lft:+.1f}°"
            rgt_s = "—" if rgt is None else f"{rgt:+.1f}°"
            sug = "" if s["suggested_symmetric_deg"] is None else f"  → suggestion ±{s['suggested_symmetric_deg']}°"
            print(f"  {kind:10s}: left {lft_s}   right {rgt_s}{sug}")
        for w in summary["review_warnings"]:
            print(c(f"  ⚠ {w}", T.YELLOW))
        print("  The per-side numbers are the output; ±N° is only a starting point.")
        print(f"  Results: {c(recorder.dir, T.BOLD)}")
        print(f"  Summary: {os.path.join(recorder.dir, 'summary.md')}")
    except KeyboardInterrupt:
        aborted = True
        print(c("\n  Interrupted.", T.RED))
    finally:
        try:
            client.set_head_tracking(False)
        except Exception:  # noqa: BLE001
            pass
        if sampling:
            try:
                recorder.stop_sampling()
                recorder.finish()
            except Exception:  # noqa: BLE001
                pass
        sleep_sequence(client, settle_s=0.1 if cfg.dry_run else 6.0)
        client.close()

    if aborted:
        print(f"  Partial results: {c(recorder.dir, T.BOLD)}")
        return 130
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    cfg, results_dir, want_preflight = parse_args(argv)
    return run(cfg, results_dir, want_preflight)


if __name__ == "__main__":
    raise SystemExit(main())
