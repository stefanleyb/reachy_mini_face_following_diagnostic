#!/usr/bin/env bash
set -euo pipefail

HOST="${ROBOT_HOST:-reachy-mini.local}"
USER_="${ROBOT_USER:-pollen}"
SSH_TARGET="${USER_}@${HOST}"
PATCH_COMMIT="08d9e40ca"
REMOTE_DIR="/home/${USER_}/frame_pose_sync_${PATCH_COMMIT}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CTL="$(mktemp -u /tmp/frame_pose_profile_ctl.XXXXXX)"
SSH_OPTS=(-o ControlMaster=auto -o "ControlPath=${CTL}" -o ControlPersist=120
          -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20)

ssh_() { ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" "$@"; }
scp_() { scp "${SSH_OPTS[@]}" "$@"; }
trap 'ssh -O exit -o "ControlPath=${CTL}" "$SSH_TARGET" 2>/dev/null || true' EXIT

ssh_ "test -f '${REMOTE_DIR}/reachy_mini/media/camera_timestamps.py'"
scp_ "${HERE}/profile_frame_pose_sync.py" "${SSH_TARGET}:${REMOTE_DIR}/"
ssh_ "PYTHONPATH='${REMOTE_DIR}' /venvs/mini_daemon/bin/python '${REMOTE_DIR}/profile_frame_pose_sync.py'"
