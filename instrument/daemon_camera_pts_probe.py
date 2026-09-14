"""Temporary passive probe for camera timestamps immediately before daemon IPC."""

from __future__ import annotations

import logging
import statistics
import threading
import time
from typing import Any

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

logger = logging.getLogger("reachy_mini.camera_pts_probe")

_applied = False


def _valid(value: int) -> bool:
    return value != int(Gst.CLOCK_TIME_NONE)


def _find_ipc_sink(pipeline: Gst.Pipeline) -> Gst.Element | None:
    iterator = pipeline.iterate_sinks()
    while True:
        result, element = iterator.next()
        if result == Gst.IteratorResult.DONE:
            return None
        if result == Gst.IteratorResult.RESYNC:
            iterator.resync()
            continue
        if result != Gst.IteratorResult.OK or element is None:
            return None
        factory = element.get_factory()
        if factory is not None and factory.get_name() in {
            "unixfdsink",
            "win32ipcvideosink",
        }:
            return element


class _Probe:
    def __init__(self, pipeline: Gst.Pipeline) -> None:
        self._pipeline = pipeline
        self._lock = threading.Lock()
        self._ages: list[float] = []
        self._count = 0

    def __call__(
        self, pad: Gst.Pad, info: Gst.PadProbeInfo, _data: Any
    ) -> int:
        buffer = info.get_buffer()
        if buffer is None:
            return int(Gst.PadProbeReturn.OK)

        pts = int(buffer.pts)
        running = int(self._pipeline.get_current_running_time())
        frame_running = int(Gst.CLOCK_TIME_NONE)
        event = pad.get_sticky_event(Gst.EventType.SEGMENT, 0)
        if event is not None and _valid(pts):
            segment = event.parse_segment()
            frame_running = int(segment.to_running_time(Gst.Format.TIME, pts))

        age_s: float | None = None
        if _valid(frame_running) and _valid(running):
            age_s = (running - frame_running) / float(Gst.SECOND)

        with self._lock:
            self._count += 1
            if age_s is not None:
                self._ages.append(age_s)
            if self._count <= 5 or self._count in {10, 20, 30, 50}:
                logger.warning(
                    "CAMERA_PTS_PROBE sample=%d offset=%d pts_s=%.6f "
                    "frame_running_s=%.6f pipeline_running_s=%.6f age_s=%s "
                    "monotonic_s=%.6f",
                    self._count,
                    int(buffer.offset),
                    pts / float(Gst.SECOND) if _valid(pts) else float("nan"),
                    (
                        frame_running / float(Gst.SECOND)
                        if _valid(frame_running)
                        else float("nan")
                    ),
                    running / float(Gst.SECOND) if _valid(running) else float("nan"),
                    "none" if age_s is None else f"{age_s:.6f}",
                    time.monotonic(),
                )
            if self._count == 50 and self._ages:
                logger.warning(
                    "CAMERA_PTS_PROBE summary count=%d valid_age=%d "
                    "age_min_s=%.6f age_median_s=%.6f age_max_s=%.6f",
                    self._count,
                    len(self._ages),
                    min(self._ages),
                    statistics.median(self._ages),
                    max(self._ages),
                )
        return int(Gst.PadProbeReturn.OK)


def apply() -> None:
    """Patch pipeline construction to observe producer timestamps without motion."""
    global _applied
    if _applied:
        return

    from reachy_mini.media.media_server import GstMediaServer

    original = GstMediaServer._build_ipc_branch

    def wrapped(self: Any, tee: Any, pipeline: Gst.Pipeline, *, is_rpi: bool) -> None:
        original(self, tee, pipeline, is_rpi=is_rpi)
        sink = _find_ipc_sink(pipeline)
        if sink is None:
            logger.warning("CAMERA_PTS_PROBE could not find IPC sink")
            return
        pad = sink.get_static_pad("sink")
        if pad is None:
            logger.warning("CAMERA_PTS_PROBE IPC sink has no sink pad")
            return
        probe = _Probe(pipeline)
        # Keep the callback alive with the media server instance.
        self._camera_pts_probe = probe
        pad.add_probe(Gst.PadProbeType.BUFFER, probe, None)
        logger.warning("CAMERA_PTS_PROBE attached before local camera IPC")

    GstMediaServer._build_ipc_branch = wrapped
    _applied = True

