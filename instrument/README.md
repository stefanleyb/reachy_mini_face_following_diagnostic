# Daemon face-tracking debug instrumentation

Minimum instrumentation needed to record the data in
`../../../coordination/archive/face_following_full_history.md`
in the fixed-body baseline. Applied in-process; **no file under `site-packages`
is modified.**

## Why it is needed

| Quantity the baseline records | Available in stock `reachy_mini` 1.10.0? |
|---|---|
| current head pose | yes — `head_pose` on `/ws/sdk` @ 50 Hz |
| head joint positions | yes — `joint_positions` @ 50 Hz |
| control-loop cadence | yes — `control_loop_stats` in `daemon_status` @ 1 Hz |
| camera calibration identity | yes — `camera_specs_name` + `/api/camera/specs` |
| **filtered** face centre + obs timestamp | only in `daemon_status` @ **1 Hz** |
| **raw** (pre-filter) face centre | **no** — filtered before it leaves `FaceTracker` |
| computed `_tracking_target_pose` (look-at result) | **no** |
| eased `_tracking_aim` (blended into IK) | **no** |

A ~8.5 Hz face stream driving a 50 Hz control loop cannot be characterised from
a 1 Hz sample, and the target/aim are exactly what distinguishes "detector
jitter" from "controller over-correction".

## What `daemon_ft_debug.apply()` changes

1. **`FaceTracker._process_detections`** — replaced with a copy that adds two
   assignments: it stashes the raw normalised nose centre (before the adaptive
   filter runs) and its capture timestamp on the tracker instance. Tracking
   behaviour is otherwise identical.
2. **`Backend.set_ws_broadcast_callback`** — wrapped so that when
   `WSServer.start()` wires the broadcast hook, a low-priority daemon thread
   starts. It samples the tracker internals at `REACHY_FT_DEBUG_HZ` (default 30,
   clamped 1–100) and sends one JSON frame per tick — **only via the WS
   fan-out**, never `broadcast_to_all_clients` (whose WebRTC path floods
   GStreamer `channel->opened` CRITICALs at emit rate when no WebRTC peer is
   connected). Frame shape:

   ```
   {"type": "face_tracking_debug", "seq", "t_mono", "t_wall",
    "tracking_enabled", "requested_weight", "weight", "alpha", "lost_timeout",
    "last_face_seen_mono", "since_face_seen",
    "raw_center", "raw_obs_ts_mono", "raw_roll",
    "filtered_center", "filtered_detected", "filtered_roll", "filtered_obs_ts_mono",
    "current_head_pose", "tracking_target_pose", "tracking_aim_pose"}
   ```

JSON serialization runs on the emitter thread, **not** the control thread. The
control thread only pays a few extra locked attribute reads per tick. The
runner records `control_loop_stats` before/after each window and the measured
emit rate, so any residual impact is visible in the data (mirroring the brief's
"compare detector cadence against the clean run" approach).

Stock clients (`ReachyMini`, desktop app) reject the unknown message type and
carry on. The diagnostic runner is the only consumer.

## Deploying to the robot's Pi

The daemon runs on the robot (Reachy Mini Wireless = the onboard Raspberry Pi),
so these two files have to be copied there and the daemon restarted through the
wrapper. SSH access is `ssh pollen@reachy-mini.local`, password `root`
([HF docs](https://huggingface.co/docs/reachy_mini/en/platforms/reachy_mini/development_workflow)).

**Recommended — systemd-managed (robust against wifi drops / reboots):**

```
bash instrument/setup_ft_debug.sh install     # one password prompt: root
```

Copies the files to `~/ft_debug/` on the robot and adds a systemd drop-in
(`/etc/systemd/system/reachy-mini-daemon.service.d/ft_debug.conf`) that sets
`REACHY_FT_DEBUG=1` + `PYTHONPATH=~/ft_debug` on the normal service. On the next
start Python auto-imports `sitecustomize.py`, which calls
`daemon_ft_debug.apply()`. The daemon does everything it normally does; the
patch rides along. systemd owns it — auto-restart, survives SSH drops and
reboots. The script restarts the daemon, waits for it, and runs the runner's
`--preflight` on the Pi to confirm `instrumentation: present`.

Then run the session (`run_on_robot.sh` if wifi to the robot is flaky, else
`./run` from the Mac). Revert any time:

```
bash instrument/setup_ft_debug.sh uninstall    # removes the drop-in, back to stock
bash instrument/setup_ft_debug.sh status       # what's active right now
```

**Alternative — foreground (`deploy_to_robot.sh --run`):** stops the service and
runs the launcher in your terminal with the same env vars. More fragile (SSH
drop kills the daemon) but touches nothing under `/etc`. Watch for `[ft_debug]
instrumentation applied via sitecustomize`; Ctrl-C restores the service.

Manual equivalent (the launcher on a Wireless unit is
`/venvs/mini_daemon/.../wireless/launcher.sh`, service user `pollen`):

```
scp daemon_ft_debug.py run_instrumented_daemon.py sitecustomize.py pollen@reachy-mini.local:~/ft_debug/
ssh pollen@reachy-mini.local
  sudo systemctl stop reachy-mini-daemon
  REACHY_FT_DEBUG=1 PYTHONPATH=~/ft_debug \
    /venvs/mini_daemon/lib/python3.12/site-packages/reachy_mini/daemon/app/services/wireless/launcher.sh
  # ... run ./run --host reachy-mini.local from your Mac ...
  # Ctrl-C, then:
  sudo systemctl start reachy-mini-daemon
```

`run_instrumented_daemon.py` (apply() then daemon main directly) is a fallback
if the `sitecustomize` route ever fails, but it needs the launcher's env vars
(GStreamer/libcamera paths) set by hand or the camera won't come up.

## Use

For the baseline session only, on the daemon host:

```
python run_instrumented_daemon.py <the daemon's normal args>
```

All arguments are forwarded verbatim to `reachy_mini.daemon.app.main:main`.

Optional: `REACHY_FT_DEBUG_HZ=30 python run_instrumented_daemon.py ...`

## Revert

Stop launching through the wrapper and start the daemon the normal way. Nothing
persisted. `daemon_ft_debug.remove()` also restores the patched callables in a
long-lived process (used by the tests).

## Not the durable form

This is a throwaway measurement aid. The durable version is a real protocol
message added in a separate upstream `reachy_mini` checkout — see the
"candidate daemon changes" and contribution path in
`../../../coordination/archive/face_following_full_history.md`.
