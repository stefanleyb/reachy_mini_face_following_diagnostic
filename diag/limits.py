"""Mark sequence + recorder for the head->body handoff-limit runner.

The operator walks around with corrected face tracking on and the body held at
yaw 0. The tool names one limit at a time; the operator lets the head follow
them and presses Enter at the angle that feels right. Two limit types, two
readings each, per side -- eight marks:

    1 left  sustained   ("compensated by the body after several seconds")
    2 right sustained
    3 left  immediate   ("the body should move right now")
    4 right immediate
    5 left  sustained
    6 right sustained
    7 left  immediate
    8 right immediate

Alternating L/R and rep-1 then rep-2 so there is a full sweep between any two
consecutive marks. The recorded quantity is head yaw relative to the body
(= world head-pose yaw - body_yaw); the full 50 Hz stream is written for the
whole session so the marks can be re-analysed offline.

This is a measurement aid, not a product app or a calibration UI. It does not
choose dwell / rate / hysteresis -- those stay later behaviour-test outputs
(see ``../../../coordination/archive/face_following_full_history.md``).
"""

from __future__ import annotations

import json
import math
import os
import platform
import socket
import statistics
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Optional

from diag.recording import _atomic_write, _now_stamp, pose_euler_xyz

# --------------------------------------------------------------------------
# mark sequence
# --------------------------------------------------------------------------

SIDES = ("left", "right")
KINDS = ("sustained", "immediate")

_KIND_PHRASE = {
    "sustained": "should be compensated by a body turn after it is held for several seconds",
    "immediate": "is far enough that the body should start turning immediately",
}
_KIND_SHORT = {"sustained": "SUSTAINED (delayed body)", "immediate": "IMMEDIATE (turn now)"}


def _build_sequence() -> list[dict[str, Any]]:
    seq: list[dict[str, Any]] = []
    n = 0
    for rep in (1, 2):
        for kind in KINDS:              # sustained pair, then immediate pair
            for side in SIDES:          # left then right
                n += 1
                seq.append({"n": n, "side": side, "kind": kind, "rep": rep})
    return seq


MARK_SEQUENCE: list[dict[str, Any]] = _build_sequence()


def mark_label(m: dict[str, Any]) -> str:
    return f"mark_{m['n']}_{m['side']}_{m['kind']}_r{m['rep']}"


def mark_prompt(m: dict[str, Any]) -> str:
    return (
        f"Move to the robot's {m['side'].upper()}. Let the head follow you, then "
        f"press Enter at the head-rotation angle that {_KIND_PHRASE[m['kind']]}."
    )


def mark_headline(m: dict[str, Any]) -> str:
    return (
        f"Mark {m['n']} of {len(MARK_SEQUENCE)} "
        f"— {m['side'].upper()} · {_KIND_SHORT[m['kind']]} · reading {m['rep']}/2"
    )


# --------------------------------------------------------------------------
# recorder
# --------------------------------------------------------------------------

_DEG = 180.0 / math.pi


def _yaw_of(row: dict[str, Any]) -> Optional[float]:
    """World-frame head yaw (rad) for a sample row, or None."""
    d = (row.get("derived") or {}).get("head_euler")
    if d and d.get("yaw") is not None:
        return float(d["yaw"])
    he = pose_euler_xyz(row.get("head_pose"))
    return None if he is None else he["yaw"]


class LimitsRecorder:
    """Owns the session dir, a background 50 Hz sampler, and the mark log."""

    # Body yaw magnitude (rad) still treated as "near zero" for this experiment.
    DEFAULT_BODY_YAW_TOL_RAD = 3.0 * math.pi / 180.0

    def __init__(
        self,
        base_dir: str,
        run_config: dict[str, Any],
        body_yaw_tol_rad: Optional[float] = None,
    ) -> None:
        self.stamp = _now_stamp()
        self.dir = os.path.join(base_dir, f"limits_{self.stamp}")
        os.makedirs(self.dir, exist_ok=False)
        self.run_config = run_config
        self.body_yaw_tol_rad = (
            self.DEFAULT_BODY_YAW_TOL_RAD if body_yaw_tol_rad is None else body_yaw_tol_rad
        )
        self.session_path = os.path.join(self.dir, "session.json")
        self.marks_path = os.path.join(self.dir, "marks.jsonl")
        self.samples_path = os.path.join(self.dir, "session.samples.jsonl")

        self._marks: list[dict[str, Any]] = []
        self._marks_fp = open(self.marks_path, "w")

        self._samples_fp: Optional[Any] = None
        self._sampler: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._buf: deque[dict[str, Any]] = deque(maxlen=4000)  # ~80 s at 50 Hz
        self._buf_lock = threading.Lock()
        self._sample_count = 0
        self._sampler_fail = 0

        # Body-yaw monitoring. The body is commanded once to yaw 0 and never
        # again; the run stays valid only while that holds. A missing reading is
        # NOT silently treated as zero — it flags the mark / session.
        self._by_lock = threading.Lock()
        self._by_samples: list[tuple[float, float]] = []  # (t_mono, body_yaw_rad)
        self._by_read_ok = 0
        self._by_read_fail = 0
        self._by_max_abs = 0.0
        self._baseline_body_yaw_rad: Optional[float] = None
        self._baseline_body_yaw_known = False

    # -- body-yaw baseline -----------------------------------------

    def set_baseline_body_yaw(self, value: Optional[float]) -> None:
        """Record the post-centring body yaw. ``None`` = the read failed."""
        self._baseline_body_yaw_known = value is not None
        self._baseline_body_yaw_rad = value
        if value is not None:
            with self._by_lock:
                self._by_samples.append((time.monotonic(), float(value)))
                self._by_read_ok += 1
                self._by_max_abs = max(self._by_max_abs, abs(float(value)))

    def _record_body_yaw(self, value: Optional[float]) -> None:
        with self._by_lock:
            if value is None:
                self._by_read_fail += 1
            else:
                self._by_samples.append((time.monotonic(), float(value)))
                self._by_read_ok += 1
                self._by_max_abs = max(self._by_max_abs, abs(float(value)))

    def _recent_body_yaw(self, t_ref: float, max_age_s: float = 5.0) -> Optional[float]:
        with self._by_lock:
            for t, v in reversed(self._by_samples):
                if 0 <= (t_ref - t) <= max_age_s:
                    return v
        return None

    # -- session file ------------------------------------------------

    def write_session(
        self,
        daemon_identity: dict[str, Any],
        camera_specs: Optional[dict[str, Any]],
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        doc = {
            "kind": "reachy_mini_head_body_handoff_limits_session",
            "created": datetime.now(timezone.utc).isoformat(),
            "stamp": self.stamp,
            "run_config": self.run_config,
            "mark_sequence": MARK_SEQUENCE,
            "environment": {
                "host": socket.gethostname(),
                "platform": platform.platform(),
                "python": platform.python_version(),
            },
            "daemon_identity": daemon_identity,
            "camera_specs": camera_specs,
            "body_yaw_tol_rad": self.body_yaw_tol_rad,
            "body_yaw_tol_deg": self.body_yaw_tol_rad * _DEG,
        }
        if extra:
            doc.update(extra)
        _atomic_write(self.session_path, json.dumps(doc, indent=2))

    # -- background sampler ----------------------------------------

    def start_sampling(self, client: Any, sample_hz: float) -> None:
        self._samples_fp = open(self.samples_path, "w")
        self._stop.clear()
        self._sampler = threading.Thread(
            target=self._sample_loop, args=(client, sample_hz), daemon=True
        )
        self._sampler.start()

    def _sample_loop(self, client: Any, sample_hz: float) -> None:
        period = 1.0 / sample_hz
        seq = 0
        next_t = time.monotonic()
        next_by = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= next_by:
                next_by = now + 1.0  # ~1 Hz body-yaw watch
                try:
                    self._record_body_yaw(client.present_body_yaw())
                except Exception:  # noqa: BLE001 - flaky wifi
                    self._record_body_yaw(None)
            try:
                snap = client.snapshot()
                row = dict(snap)
                row["seq"] = seq
                ftd = row.get("ft_debug") or {}
                row["derived"] = {
                    "head_euler": pose_euler_xyz(row.get("head_pose")),
                    "target_euler": pose_euler_xyz(ftd.get("tracking_target_pose")),
                    "aim_euler": pose_euler_xyz(ftd.get("tracking_aim_pose")),
                }
                assert self._samples_fp is not None
                self._samples_fp.write(json.dumps(row) + "\n")
                seq += 1
                self._sample_count = seq
                if seq % 25 == 0:
                    self._samples_fp.flush()
                with self._buf_lock:
                    self._buf.append(row)
            except Exception:  # noqa: BLE001 - flaky wifi; keep the loop alive
                self._sampler_fail += 1
            next_t += period
            sleep = next_t - time.monotonic()
            if sleep > 0:
                self._stop.wait(sleep)
            else:
                next_t = time.monotonic()

    def stop_sampling(self) -> None:
        self._stop.set()
        if self._sampler is not None:
            self._sampler.join(timeout=2.0)
        if self._samples_fp is not None:
            self._samples_fp.flush()
            self._samples_fp.close()

    @property
    def sample_count(self) -> int:
        return self._sample_count

    # -- one mark ------------------------------------------------

    def mark(
        self,
        m: dict[str, Any],
        client: Any,
        window_s: float,
    ) -> dict[str, Any]:
        """Record one Enter press. Returns a dict for the on-screen confirmation."""
        t_press_mono = time.monotonic()
        t_press_wall = time.time()

        # Body yaw at the press: a direct read first, then a recent watch
        # sample, then give up — never silently assume zero.
        body_yaw: Optional[float] = None
        body_yaw_source = "unknown"
        try:
            body_yaw = client.present_body_yaw()
        except Exception:  # noqa: BLE001
            body_yaw = None
        if body_yaw is not None:
            body_yaw_source = "press_read"
            self._record_body_yaw(body_yaw)
        else:
            body_yaw = self._recent_body_yaw(t_press_mono)
            if body_yaw is not None:
                body_yaw_source = "recent_watch_sample"
        body_yaw_known = body_yaw is not None
        within_tol = body_yaw_known and abs(body_yaw) <= self.body_yaw_tol_rad
        rel_reliable = body_yaw_known and within_tol

        with self._buf_lock:
            recent = list(self._buf)
        window = [
            r for r in recent
            if r.get("t_mono") is not None and 0 <= (t_press_mono - r["t_mono"]) <= window_s
        ]
        used_fallback = False
        if not window:
            # sampler starved (wifi) — take one direct reading so the mark is
            # never empty.
            used_fallback = True
            try:
                snap = client.snapshot()
                snap["derived"] = {"head_euler": pose_euler_xyz(snap.get("head_pose"))}
                window = [snap]
            except Exception:  # noqa: BLE001
                window = recent[-1:] if recent else []

        world = [y for y in (_yaw_of(r) for r in window) if y is not None]
        # If body yaw is unknown we still report a relative value assuming 0,
        # but the mark is flagged unreliable so it is never used blindly.
        bz = body_yaw if body_yaw_known else 0.0
        rel = [y - bz for y in world]

        def _stats(vals: list[float]) -> dict[str, Any]:
            if not vals:
                return {"n": 0, "mean": None, "min": None, "max": None, "std": None}
            return {
                "n": len(vals),
                "mean": statistics.fmean(vals),
                "min": min(vals),
                "max": max(vals),
                "std": statistics.pstdev(vals) if len(vals) > 1 else 0.0,
            }

        rel_stats = _stats(rel)
        world_stats = _stats(world)
        last = window[-1] if window else {}
        instant_world = _yaw_of(last)
        ftd_last = last.get("ft_debug") or {}

        row = {
            "kind": "reachy_mini_head_body_handoff_mark",
            "label": mark_label(m),
            "n": m["n"],
            "side": m["side"],
            "limit_kind": m["kind"],
            "rep": m["rep"],
            "prompt": mark_prompt(m),
            "t_press_mono": t_press_mono,
            "t_press_wall": t_press_wall,
            "window_s": window_s,
            "used_fallback_reading": used_fallback,
            "body_yaw_rad": body_yaw,
            "body_yaw_known": body_yaw_known,
            "body_yaw_source": body_yaw_source,
            "body_yaw_within_tol": within_tol,
            "rel_reliable": rel_reliable,
            "head_yaw_world_rad": world_stats,
            "head_yaw_rel_body_rad": rel_stats,
            "head_yaw_rel_body_deg": {
                k: (None if v is None else v * _DEG) if k != "n" else v
                for k, v in rel_stats.items()
            },
            "instant_world_yaw_rad": instant_world,
            "instant_rel_yaw_deg": None if instant_world is None else (instant_world - bz) * _DEG,
            "ft_debug_at_press": ftd_last or None,
            "window_samples": window,
        }
        self._marks.append(row)
        self._marks_fp.write(json.dumps(row) + "\n")
        self._marks_fp.flush()

        return {
            "label": row["label"],
            "n": m["n"],
            "side": m["side"],
            "limit_kind": m["kind"],
            "rep": m["rep"],
            "rel_deg_mean": None if rel_stats["mean"] is None else rel_stats["mean"] * _DEG,
            "rel_deg_min": None if rel_stats["min"] is None else rel_stats["min"] * _DEG,
            "rel_deg_max": None if rel_stats["max"] is None else rel_stats["max"] * _DEG,
            "rel_deg_std": None if rel_stats["std"] is None else rel_stats["std"] * _DEG,
            "n_samples": rel_stats["n"],
            "body_yaw_rad": body_yaw,
            "body_yaw_known": body_yaw_known,
            "body_yaw_deg": None if body_yaw is None else body_yaw * _DEG,
            "rel_reliable": rel_reliable,
            "used_fallback_reading": used_fallback,
        }

    # -- summary --------------------------------------------------

    def _reliable_mean(self, mk: dict[str, Any]) -> Optional[float]:
        if not mk.get("rel_reliable"):
            return None
        return mk["head_yaw_rel_body_deg"]["mean"]

    def _bucket_means(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for kind in KINDS:
            for side in SIDES:
                vals = [
                    v for v in (
                        self._reliable_mean(mk)
                        for mk in self._marks
                        if mk["side"] == side and mk["limit_kind"] == kind
                    ) if v is not None
                ]
                out[f"{side}_{kind}"] = {
                    "readings_deg": vals,
                    "mean_deg": statistics.fmean(vals) if vals else None,
                    "mean_abs_deg": statistics.fmean([abs(v) for v in vals]) if vals else None,
                }
        provisional: dict[str, Any] = {}
        for kind in KINDS:
            mags = [
                abs(v) for v in (self._reliable_mean(mk) for mk in self._marks
                                 if mk["limit_kind"] == kind) if v is not None
            ]
            if mags:
                m = statistics.fmean(mags)
                provisional[kind] = {
                    "mean_abs_deg": m,
                    "suggested_symmetric_deg": round(m / 5.0) * 5,
                }
            else:
                provisional[kind] = {"mean_abs_deg": None, "suggested_symmetric_deg": None}
        return {"buckets": out, "suggestion": provisional}

    def _session_validity(self) -> dict[str, Any]:
        tol = self.body_yaw_tol_rad
        baseline_ok = (
            self._baseline_body_yaw_known
            and self._baseline_body_yaw_rad is not None
            and abs(self._baseline_body_yaw_rad) <= tol
        )
        with self._by_lock:
            max_abs = self._by_max_abs
            ok = self._by_read_ok
            fail = self._by_read_fail
        drifted = max_abs > tol
        no_monitoring = ok == 0
        unreliable_marks = [mk["n"] for mk in self._marks if not mk.get("rel_reliable")]
        valid = baseline_ok and not drifted and not no_monitoring and not unreliable_marks
        reasons: list[str] = []
        if not self._baseline_body_yaw_known:
            reasons.append("baseline body-yaw read failed (centring not verified)")
        elif not baseline_ok:
            reasons.append(
                f"body was at {self._baseline_body_yaw_rad * _DEG:+.1f}° after centring "
                f"(tolerance ±{tol * _DEG:.1f}°)"
            )
        if drifted:
            reasons.append(
                f"body yaw reached {max_abs * _DEG:.1f}° during the run "
                f"(tolerance ±{tol * _DEG:.1f}°)"
            )
        if no_monitoring:
            reasons.append("no body-yaw reading ever succeeded — 'held at zero' is unverified")
        if unreliable_marks:
            reasons.append(f"marks {unreliable_marks} have no reliable body-yaw reference")
        return {
            "valid": valid,
            "reasons": reasons,
            "baseline_body_yaw_rad": self._baseline_body_yaw_rad,
            "baseline_body_yaw_known": self._baseline_body_yaw_known,
            "body_yaw_tol_rad": tol,
            "body_yaw_max_abs_rad": max_abs,
            "body_yaw_reads_ok": ok,
            "body_yaw_reads_fail": fail,
            "unreliable_mark_numbers": unreliable_marks,
        }

    def _warnings(self, rollup: dict[str, Any]) -> list[str]:
        w: list[str] = []
        b = rollup["buckets"]
        # 'immediate' should be a LARGER angle than 'sustained'. Flag inversions.
        for side in SIDES:
            s = b[f"{side}_sustained"]["mean_abs_deg"]
            i = b[f"{side}_immediate"]["mean_abs_deg"]
            if s is not None and i is not None and i < s:
                w.append(
                    f"{side}: 'immediate' mean |{i:.1f}°| is SMALLER than 'sustained' "
                    f"|{s:.1f}°| — likely a mis-pressed mark; check {side} marks."
                )
        si = rollup["suggestion"]
        s_all, i_all = si["sustained"]["mean_abs_deg"], si["immediate"]["mean_abs_deg"]
        if s_all is not None and i_all is not None and i_all < s_all:
            w.append(
                f"overall: 'immediate' |{i_all:.1f}°| < 'sustained' |{s_all:.1f}°| — "
                "the two limit types may be swapped."
            )
        # Consistent left/right asymmetry worth preserving (not averaging away).
        for kind in KINDS:
            lft = b[f"left_{kind}"]["mean_abs_deg"]
            rgt = b[f"right_{kind}"]["mean_abs_deg"]
            if lft is not None and rgt is not None and abs(lft - rgt) >= 5.0:
                earlier = "left" if lft < rgt else "right"
                w.append(
                    f"{kind}: left |{lft:.1f}°| vs right |{rgt:.1f}°| differ by "
                    f"{abs(lft - rgt):.1f}° — {earlier} consistently feels earlier; "
                    "keep the per-side values, do not average them for symmetry."
                )
        return w

    def finish(self) -> dict[str, Any]:
        self._marks_fp.flush()
        self._marks_fp.close()
        rollup = self._bucket_means()
        validity = self._session_validity()
        warnings = self._warnings(rollup)
        # summary.json keeps the per-mark stats but not the bulky per-mark
        # sample windows (those live in marks.jsonl).
        marks_slim = [
            {k: v for k, v in mk.items() if k != "window_samples"}
            for mk in self._marks
        ]
        summary = {
            "kind": "reachy_mini_head_body_handoff_limits_summary",
            "stamp": self.stamp,
            "dir": self.dir,
            "run_config": self.run_config,
            "sample_count": self._sample_count,
            "sampler_fail": self._sampler_fail,
            "session_validity": validity,
            "review_warnings": warnings,
            "marks": marks_slim,
            "rollup": rollup,
        }
        _atomic_write(os.path.join(self.dir, "summary.json"), json.dumps(summary, indent=2))
        _atomic_write(
            os.path.join(self.dir, "summary.md"),
            self._summary_md(rollup, validity, warnings),
        )
        return summary

    def _summary_md(
        self,
        rollup: dict[str, Any],
        validity: dict[str, Any],
        warnings: list[str],
    ) -> str:
        tol_deg = self.body_yaw_tol_rad * _DEG
        L = [f"# Head→body handoff limits — {self.stamp}", "", f"Result directory: `{self.dir}`", ""]

        if not validity["valid"]:
            L += ["> ## ⚠ SESSION VALIDITY — the body was NOT verified near zero",
                  ">",
                  "> Treat the angles below as unreliable until this is resolved:"]
            L += [f"> - {r}" for r in validity["reasons"]]
            L += [">"]
        else:
            L += [f"Body yaw stayed within ±{tol_deg:.1f}° "
                  f"(max {validity['body_yaw_max_abs_rad'] * _DEG:.1f}°, "
                  f"{validity['body_yaw_reads_ok']} reads ok / "
                  f"{validity['body_yaw_reads_fail']} failed).", ""]

        L += [
            f"Continuous stream: `session.samples.jsonl` ({self._sample_count} rows, "
            f"{self._sampler_fail} sampler errors). Per-press detail: `marks.jsonl`.",
            "",
            "Signal = head yaw **relative to the body** (world head-pose yaw − body_yaw). "
            "A `!` in the table flags a mark with no reliable body-yaw reference.",
            "",
            "| # | Side | Limit | Rep | rel yaw mean (°) | min (°) | max (°) | σ (°) | n | body yaw (°) |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for mk in self._marks:
            d = mk["head_yaw_rel_body_deg"]

            def _f(v: Optional[float]) -> str:
                return "—" if v is None else f"{v:+.1f}"

            std = d["std"]
            std_s = "—" if std is None else f"{std:.1f}"
            flag = "" if mk.get("rel_reliable") else " !"
            by = mk.get("body_yaw_rad")
            by_s = "unknown" if by is None else f"{by * _DEG:+.1f}"
            L.append(
                f"| {mk['n']}{flag} | {mk['side']} | {mk['limit_kind']} | {mk['rep']} "
                f"| {_f(d['mean'])} | {_f(d['min'])} | {_f(d['max'])} | {std_s} | {d['n']} | {by_s} |"
            )

        L += ["", "## Per side (reliable marks only)", ""]
        for kind in KINDS:
            for side in SIDES:
                b = rollup["buckets"][f"{side}_{kind}"]
                reads = ", ".join(f"{v:+.1f}" for v in b["readings_deg"]) or "—"
                mean = "—" if b["mean_deg"] is None else f"{b['mean_deg']:+.1f}"
                L.append(f"- **{side} {kind}**: readings [{reads}] → mean {mean}°")

        L += ["", "## Suggested symmetric value — a starting point, not a decision", "",
              "Mean of the reliable per-side magnitudes, rounded to 5°. The per-side "
              "readings above are the real output: if one side consistently feels "
              "earlier, keep that difference — do not average it away for symmetry. "
              "Dwell, immediate-turn timing, body rate and hysteresis are still later "
              "behaviour tests (coordination/workstreams/face-and-body/BRIEF.md).", ""]
        for kind in KINDS:
            p = rollup["suggestion"][kind]
            if p["suggested_symmetric_deg"] is None:
                L.append(f"- **{kind}**: no reliable reading")
            else:
                L.append(
                    f"- **{kind}**: |mean| {p['mean_abs_deg']:.1f}° → "
                    f"suggestion **±{p['suggested_symmetric_deg']}°**"
                )

        if warnings:
            L += ["", "## ⚠ Review warnings", ""]
            L += [f"- {w}" for w in warnings]

        L += ["", "`marks.jsonl` keeps every signed reading and records which "
              "physical side maps to which yaw sign on this robot.", ""]
        return "\n".join(L) + "\n"
