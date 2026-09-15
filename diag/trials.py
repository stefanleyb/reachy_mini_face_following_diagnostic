"""Trial matrix, run configuration, and shared console helpers."""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Callable, Optional

import numpy as np

# The six stationary-face trials, in run order.
TRIALS: list[dict[str, str]] = [
    {"height": "seated", "position": "center"},
    {"height": "seated", "position": "left"},
    {"height": "seated", "position": "right"},
    {"height": "standing", "position": "center"},
    {"height": "standing", "position": "left"},
    {"height": "standing", "position": "right"},
]


def trial_label(index: int) -> str:
    t = TRIALS[index]
    return f"trial_{index + 1}_{t['height']}_{t['position']}"


_WHERE = {
    "center": "directly in front of the robot",
    "left": "off to the robot's left — far enough that it has to turn to face you",
    "right": "off to the robot's right — the opposite side",
}


def trial_where(index: int) -> str:
    """Plain instruction: where to be, no distances or marks."""
    t = TRIALS[index]
    verb = "Sit" if t["height"] == "seated" else "Stand"
    return (
        f"{verb} {_WHERE[t['position']]}, about where you'd normally be to watch it. "
        f"Then hold still, face toward the robot, and make sure the camera can see you."
    )


@dataclass
class RunConfig:
    host: str = "reachy-mini.local"
    port: int = 8000
    countdown_s: float = 10.0
    tracking_window_s: float = 15.0
    sample_hz: float = 50.0
    center_duration_s: float = 1.5
    tracking_weight: float = 1.0
    recenter_between_trials: bool = True
    empty_scene_control: bool = True
    terminal_bell: bool = True
    dry_run: bool = False
    only_trials: Optional[list[int]] = None  # 1-based indices; None = all six

    def as_dict(self) -> dict:
        return asdict(self)


# -- console -----------------------------------------------------------

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
RESET = "\033[0m"


def _supports_color() -> bool:
    return sys.stdout.isatty()


def c(text: str, color: str) -> str:
    if not _supports_color():
        return text
    return f"{color}{text}{RESET}"


def banner(text: str, color: str = CYAN) -> None:
    line = "=" * max(40, len(text) + 4)
    print()
    print(c(line, color))
    print(c(f"  {text}", color))
    print(c(line, color))


def bell(enabled: bool, count: int = 1) -> None:
    if not enabled:
        return
    for _ in range(count):
        sys.stdout.write("\a")
        sys.stdout.flush()
        time.sleep(0.15)


def ask(prompt: str, default: Optional[str] = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    try:
        resp = input(c(f"{prompt}{suffix}: ", YELLOW)).strip()
    except EOFError:
        resp = ""
    if not resp and default is not None:
        return default
    return resp


def ask_yes_no(prompt: str, default: Optional[bool] = None) -> bool:
    d = None if default is None else ("y" if default else "n")
    while True:
        resp = ask(f"{prompt} (y/n)", d).lower()
        if resp in ("y", "yes"):
            return True
        if resp in ("n", "no"):
            return False
        print(c("  please answer y or n", RED))


def press_enter(prompt: str) -> None:
    """Block until the operator hits Enter (any input is accepted)."""
    try:
        input(c(f"{prompt} ", YELLOW))
    except EOFError:
        pass


def countdown(
    seconds: float,
    message: str,
    bell_enabled: bool = True,
    on_tick: Optional[Callable[[int], None]] = None,
) -> None:
    """Visible whole-second countdown. Blocks for ``seconds``."""
    total = int(round(seconds))
    for remaining in range(total, 0, -1):
        line = f"  {message}  ->  starting in {c(str(remaining).rjust(2), BOLD)} s "
        sys.stdout.write("\r" + line)
        sys.stdout.flush()
        if on_tick is not None:
            on_tick(remaining)
        if remaining <= 3:
            bell(bell_enabled, 1)
            time.sleep(0.85)
        else:
            time.sleep(1.0)
    sys.stdout.write("\r" + " " * 78 + "\r")
    sys.stdout.flush()


IDENTITY_POSE = np.eye(4)
