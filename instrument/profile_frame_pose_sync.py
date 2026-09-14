#!/usr/bin/env python3
"""Micro-profile frame/pose synchronization on Reachy's Pi without robot motion."""

from __future__ import annotations

import json
import platform
import statistics
import time
from collections import deque
from types import SimpleNamespace
from typing import Any, Callable

import numpy as np
from scipy.spatial.transform import Rotation as R

from reachy_mini.daemon.backend.abstract import Backend
from reachy_mini.media.camera_timestamps import CameraFrameTimestamps
from reachy_mini.utils.interpolation import linear_pose_interpolation


def _pose(yaw: float) -> np.ndarray:
    pose = np.eye(4, dtype=np.float64)
    pose[:3, :3] = R.from_euler("z", yaw).as_matrix()
    return pose


def _benchmark(fn: Callable[[], Any], calls: int, batches: int = 15) -> dict[str, float]:
    for _ in range(100):
        fn()
    per_call_ms: list[float] = []
    per_batch = max(calls // batches, 1)
    for _ in range(batches):
        started = time.perf_counter_ns()
        for _ in range(per_batch):
            fn()
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000.0
        per_call_ms.append(elapsed_ms / per_batch)
    return {
        "median_ms": statistics.median(per_call_ms),
        "p95_batch_ms": float(np.percentile(per_call_ms, 95)),
        "max_batch_ms": max(per_call_ms),
    }


def main() -> None:
    current_pose = _pose(0.3)
    history = deque((100.0 + i * 0.02, _pose(i * 0.002)) for i in range(150))
    backend = SimpleNamespace(
        _tracking_pose_history=history,
        _tracking_pose_history_s=3.0,
        get_current_head_pose=lambda: current_pose,
    )

    record_now = 103.0

    def record_pose() -> None:
        nonlocal record_now
        record_now += 0.02
        Backend._record_tracking_pose(backend, record_now)  # type: ignore[arg-type]

    def interpolate_history() -> None:
        # Typical corrected observation age from the physical run was ~190 ms.
        timestamp = backend._tracking_pose_history[-1][0] - 0.19
        Backend._tracking_pose_at(backend, timestamp)  # type: ignore[arg-type]

    def nearest_history() -> None:
        # Comparison only: select the nearest 50 Hz sample without interpolation.
        timestamp = backend._tracking_pose_history[-1][0] - 0.19
        min(backend._tracking_pose_history, key=lambda item: abs(item[0] - timestamp))[
            1
        ].copy()

    registry = CameraFrameTimestamps()
    offset = 0

    def timestamp_roundtrip() -> None:
        nonlocal offset
        offset += 1
        registry.record(
            offset,
            100.0,
            frame_running_time_s=9.98,
            pipeline_running_time_s=10.0,
        )
        registry.pop(offset)

    start = _pose(0.0)
    target = _pose(0.5)

    timings = {
        "record_pose_history_50hz": _benchmark(record_pose, 30_000),
        "lookup_and_interpolate_10hz": _benchmark(interpolate_history, 10_000),
        "nearest_pose_comparison": _benchmark(nearest_history, 10_000),
        "timestamp_registry_roundtrip_10hz": _benchmark(
            timestamp_roundtrip, 30_000
        ),
        # Context only: tracking already paid this cost before the correction.
        "existing_pose_interpolation_50hz": _benchmark(
            lambda: linear_pose_interpolation(start, target, 0.15), 10_000
        ),
    }
    added_ms_per_second = (
        timings["record_pose_history_50hz"]["median_ms"] * 50.0
        + timings["lookup_and_interpolate_10hz"]["median_ms"] * 10.0
        + timings["timestamp_registry_roundtrip_10hz"]["median_ms"] * 10.0
    )
    print(
        json.dumps(
            {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "timings": timings,
                "estimated_added_cpu_ms_per_second": added_ms_per_second,
                "estimated_single_core_percent": added_ms_per_second / 10.0,
                "notes": [
                    "No camera, motor, tracking, or daemon command is used.",
                    "The estimate covers the newly added pure-Python paths, not scheduler contention.",
                    "Nearest-pose timing is a comparison, not a proposed behavior change.",
                ],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
