#!/usr/bin/env python3
"""Passively inspect camera-buffer timing on the Reachy Mini daemon IPC feed.

This program never enables face tracking and never sends a motion command. It
only pulls buffers from the daemon-owned local camera distributor and compares
their timestamps with the receiving GStreamer pipeline and Python's monotonic
clock.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from typing import Any

import gi

from reachy_mini.daemon.utils import CAMERA_SOCKET_PATH

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
from gi.repository import Gst, GstApp  # noqa: E402, F401


def _valid_clock_time(value: int | None) -> bool:
    return value is not None and value != Gst.CLOCK_TIME_NONE


def _seconds(value: int | None) -> float | None:
    if not _valid_clock_time(value):
        return None
    assert value is not None
    return float(value) / float(Gst.SECOND)


def _summary(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    return {
        "min": min(values),
        "median": statistics.median(values),
        "max": max(values),
    }


def inspect(duration_s: float) -> dict[str, Any]:
    """Pull buffers for ``duration_s`` and return timing diagnostics."""
    Gst.init([])
    source = Gst.ElementFactory.make("unixfdsrc")
    queue = Gst.ElementFactory.make("queue")
    sink = Gst.ElementFactory.make("appsink")
    if source is None or queue is None or sink is None:
        raise RuntimeError("Required unixfdsrc/queue/appsink elements are unavailable")

    source.set_property("socket-path", CAMERA_SOCKET_PATH)
    queue.set_property("leaky", 2)
    queue.set_property("max-size-buffers", 1)
    sink.set_property("drop", True)
    sink.set_property("max-buffers", 1)
    sink.set_property("sync", False)

    pipeline = Gst.Pipeline.new("camera-pts-probe")
    for element in (source, queue, sink):
        pipeline.add(element)
    if not source.link(queue) or not queue.link(sink):
        raise RuntimeError("Could not link passive camera timestamp pipeline")

    samples: list[dict[str, Any]] = []
    result = pipeline.set_state(Gst.State.PLAYING)
    if result == Gst.StateChangeReturn.FAILURE:
        pipeline.set_state(Gst.State.NULL)
        raise RuntimeError("Could not connect to the daemon camera IPC feed")

    deadline = time.monotonic() + duration_s
    try:
        while time.monotonic() < deadline:
            sample = sink.try_pull_sample(250 * Gst.MSECOND)
            if sample is None:
                continue
            arrival_mono = time.monotonic()
            buffer = sample.get_buffer()
            segment = sample.get_segment()
            pts = int(buffer.pts)
            dts = int(buffer.dts)
            pipeline_running = int(pipeline.get_current_running_time())
            buffer_running: int | None = None
            if segment is not None and _valid_clock_time(pts):
                converted = int(segment.to_running_time(Gst.Format.TIME, pts))
                if _valid_clock_time(converted):
                    buffer_running = converted

            age_s: float | None = None
            if _valid_clock_time(pipeline_running) and _valid_clock_time(buffer_running):
                assert buffer_running is not None
                age_s = float(pipeline_running - buffer_running) / float(Gst.SECOND)

            samples.append(
                {
                    "arrival_mono_s": arrival_mono,
                    "pts_s": _seconds(pts),
                    "dts_s": _seconds(dts),
                    "buffer_running_s": _seconds(buffer_running),
                    "pipeline_running_s": _seconds(pipeline_running),
                    "pipeline_minus_buffer_s": age_s,
                    "offset": int(buffer.offset),
                }
            )
    finally:
        pipeline.set_state(Gst.State.NULL)

    pts_values = [row["pts_s"] for row in samples if row["pts_s"] is not None]
    arrival_values = [row["arrival_mono_s"] for row in samples]
    ages = [
        row["pipeline_minus_buffer_s"]
        for row in samples
        if row["pipeline_minus_buffer_s"] is not None
    ]
    pts_steps = [b - a for a, b in zip(pts_values, pts_values[1:])]
    arrival_steps = [b - a for a, b in zip(arrival_values, arrival_values[1:])]

    return {
        "duration_s": duration_s,
        "camera_socket": CAMERA_SOCKET_PATH,
        "sample_count": len(samples),
        "valid_pts_count": len(pts_values),
        "pts_monotonic": all(b > a for a, b in zip(pts_values, pts_values[1:])),
        "pts_step_s": _summary(pts_steps),
        "arrival_step_s": _summary(arrival_steps),
        "pipeline_minus_buffer_s": _summary(ages),
        "first_samples": samples[:5],
        "last_samples": samples[-5:],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=5.0)
    args = parser.parse_args()
    print(json.dumps(inspect(max(args.duration, 0.5)), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
