#!/usr/bin/env bash
set -euo pipefail

HOST="${ROBOT_HOST:-reachy-mini.local}"
USER_="${ROBOT_USER:-pollen}"
SSH_TARGET="${USER_}@${HOST}"
REMOTE_DIR="/home/${USER_}/ft_debug"
DROPIN_DIR="/etc/systemd/system/reachy-mini-daemon.service.d"
DROPIN="${DROPIN_DIR}/camera_pts_probe.conf"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CTL="$(mktemp -u /tmp/camera_pts_probe_ctl.XXXXXX)"
SSH_OPTS=(-o ControlMaster=auto -o "ControlPath=${CTL}" -o ControlPersist=120
          -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20)

ssh_() { ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" "$@"; }
scp_() { scp "${SSH_OPTS[@]}" "$@"; }

cleanup() {
  ssh_ "sudo rm -f '${DROPIN}'; sudo rmdir '${DROPIN_DIR}' 2>/dev/null || true; sudo systemctl daemon-reload; sudo systemctl restart reachy-mini-daemon" || true
  ssh -O exit -o "ControlPath=${CTL}" "${SSH_TARGET}" 2>/dev/null || true
}
trap cleanup EXIT

ssh_ "mkdir -p '${REMOTE_DIR}'"
scp_ "${HERE}/daemon_camera_pts_probe.py" "${HERE}/sitecustomize.py" "${SSH_TARGET}:${REMOTE_DIR}/"
ssh_ "sudo mkdir -p '${DROPIN_DIR}'; printf '%s\n' '[Service]' 'Environment=REACHY_CAMERA_PTS_PROBE=1' 'Environment=PYTHONPATH=${REMOTE_DIR}' | sudo tee '${DROPIN}' >/dev/null; sudo systemctl daemon-reload; sudo systemctl restart reachy-mini-daemon"
ssh_ "for i in \$(seq 1 45); do curl -sf -m 3 http://127.0.0.1:8000/api/daemon/status >/dev/null 2>&1 && exit 0; sleep 2; done; exit 1"
sleep 7
ssh_ "journalctl -u reachy-mini-daemon --since '-30 seconds' --no-pager | grep CAMERA_PTS_PROBE"
