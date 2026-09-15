"""Automatic, timestamped result files for the baseline session.

Layout (created under ``--results-dir``, default ``./results``)::

    baseline_YYYYmmdd_HHMMSS/
        session.json                     run config + environment + daemon identity
        trial_1_seated_center.json       per-trial metadata + observer verdict
        trial_1_seated_center.samples.jsonl   one JSON row per sample (streamed)
        ...
        empty_scene_control.json / .samples.jsonl   (optional 30 s control)
        summary.json                     machine-readable roll-up
        summary.md                       short human summary with file paths

The operator never has to copy anything from the terminal: every sample row is
flushed to disk as it is taken, so an interrupted trial still leaves its
evidence behind.
"""

from __future__ import annotations

import json
import math
import os
import platform
import socket
import tempfile
from datetime import datetime, timezone
from typing import Any, Optional

import numpy as np


def _now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _atomic_write(path: str, text: str) -> None:
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def pose_euler_xyz(pose: Optional[list[list[float]]]) -> Optional[dict[str, float]]:
    """Roll/pitch/yaw (rad) of a 4x4 pose's rotation, XYZ intrinsic.

    Small helper so quick-look tools don't have to re-derive angles; the full
    matrix is always kept in the row too.
    """
    if pose is None:
        return None
    r = np.asarray(pose, dtype=float)[:3, :3]
    sy = math.hypot(r[0, 0], r[1, 0])
    if sy > 1e-6:
        roll = math.atan2(r[2, 1], r[2, 2])
        pitch = math.atan2(-r[2, 0], sy)
        yaw = math.atan2(r[1, 0], r[0, 0])
    else:
        roll = math.atan2(-r[1, 2], r[1, 1])
        pitch = math.atan2(-r[2, 0], sy)
        yaw = 0.0
    return {"roll": roll, "pitch": pitch, "yaw": yaw}


class SessionRecorder:
    def __init__(self, base_dir: str, run_config: dict[str, Any]) -> None:
        self.stamp = _now_stamp()
        self.dir = os.path.join(base_dir, f"baseline_{self.stamp}")
        os.makedirs(self.dir, exist_ok=False)
        self.run_config = run_config
        self._trials: list[dict[str, Any]] = []
        self.session_path = os.path.join(self.dir, "session.json")

    # -- session ------------------------------------------------------

    def write_session(
        self,
        daemon_identity: dict[str, Any],
        camera_specs: Optional[dict[str, Any]],
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        doc = {
            "kind": "reachy_mini_fixed_body_face_baseline_session",
            "created": datetime.now(timezone.utc).isoformat(),
            "stamp": self.stamp,
            "run_config": self.run_config,
            "environment": {
                "host": socket.gethostname(),
                "platform": platform.platform(),
                "python": platform.python_version(),
            },
            "daemon_identity": daemon_identity,
            "camera_specs": camera_specs,
        }
        if extra:
            doc.update(extra)
        _atomic_write(self.session_path, json.dumps(doc, indent=2))

    # -- trials -------------------------------------------------------

    def new_trial(self, label: str, meta: dict[str, Any]) -> "TrialRecorder":
        tr = TrialRecorder(self.dir, label, meta)
        return tr

    def register_trial_result(self, result: dict[str, Any]) -> None:
        self._trials.append(result)
        self._write_summary()

    # -- summary -----------------------------------------------------

    def _write_summary(self) -> None:
        summary = {
            "kind": "reachy_mini_fixed_body_face_baseline_summary",
            "stamp": self.stamp,
            "dir": self.dir,
            "run_config": self.run_config,
            "trials": self._trials,
        }
        _atomic_write(
            os.path.join(self.dir, "summary.json"), json.dumps(summary, indent=2)
        )

        lines = [
            f"# Fixed-body face baseline — {self.stamp}",
            "",
            f"Result directory: `{self.dir}`  ·  instrumented: "
            f"{'yes' if self.run_config.get('_instrumented') or (self._trials and self._trials[0].get('instrumented')) else 'no'}",
            "",
            "| Trial | Height | Position | Rows | Sample Hz | Window s | Face frames | Poll ok/fail |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for t in self._trials:
            m = t.get("meta", {})
            tm = t.get("timing", {})
            lines.append(
                "| {label} | {height} | {position} | {n} | {hz} | {win} | {ff} | {ok}/{fail} |".format(
                    label=t.get("label", "?"),
                    height=m.get("height", "?"),
                    position=m.get("position", "?"),
                    n=t.get("sample_count", 0),
                    hz=t.get("measured_sample_hz", "?"),
                    win=round(tm.get("window_elapsed_s", 0), 1),
                    ff=t.get("face_frames_seen") or "-",
                    ok=t.get("status_poll_ok", "-"),
                    fail=t.get("status_poll_fail", "-"),
                )
            )
        lines += [
            "",
            "Each trial: `<label>.samples.jsonl` has one row per sample "
            "(head pose 4x4 + joints, face target, ft_debug when instrumented, "
            "plus `derived` euler angles). `<label>.json` has timing + meta.",
            "",
            "Analyse per coordination/workstreams/face-and-body/"
            "BRIEF.md: first-move overshoot, oscillation "
            "amplitude/frequency, settle time, face-loss behaviour.",
            "",
        ]
        with open(os.path.join(self.dir, "summary.md"), "w") as f:
            f.write("\n".join(lines) + "\n")


class TrialRecorder:
    def __init__(self, out_dir: str, label: str, meta: dict[str, Any]) -> None:
        self.label = label
        self.meta = meta
        self.dir = out_dir
        self.meta_path = os.path.join(out_dir, f"{label}.json")
        self.samples_path = os.path.join(out_dir, f"{label}.samples.jsonl")
        self._samples_fp = open(self.samples_path, "w")
        self._n = 0
        self.timing: dict[str, Any] = {}
        self.control_loop_stats: list[dict[str, Any]] = []

    def add_sample(self, row: dict[str, Any]) -> None:
        # Enrich with derived euler angles for quick-look; keep raw matrices.
        row = dict(row)
        row["seq"] = self._n
        ftd = row.get("ft_debug") or {}
        row["derived"] = {
            "head_euler": pose_euler_xyz(row.get("head_pose")),
            "target_euler": pose_euler_xyz(ftd.get("tracking_target_pose")),
            "aim_euler": pose_euler_xyz(ftd.get("tracking_aim_pose")),
        }
        self._samples_fp.write(json.dumps(row) + "\n")
        self._n += 1
        if self._n % 25 == 0:
            self._samples_fp.flush()

    @property
    def sample_count(self) -> int:
        return self._n

    def finish(
        self,
        verdict: dict[str, Any],
        instrumented: bool,
        measured_sample_hz: Optional[float],
        face_frames_seen: Optional[int],
        extra: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        self._samples_fp.flush()
        self._samples_fp.close()
        result = {
            "kind": "reachy_mini_fixed_body_face_baseline_trial",
            "label": self.label,
            "meta": self.meta,
            "timing": self.timing,
            "instrumented": instrumented,
            "sample_count": self._n,
            "measured_sample_hz": measured_sample_hz,
            "face_frames_seen": face_frames_seen,
            "control_loop_stats": self.control_loop_stats,
            "observer_verdict": verdict,
            "samples_file": os.path.basename(self.samples_path),
        }
        if extra:
            result.update(extra)
        _atomic_write(self.meta_path, json.dumps(result, indent=2))
        return result
