#!/usr/bin/env bash
set -euo pipefail

ACTION="${1:-status}"
HOST="${ROBOT_HOST:-reachy-mini.local}"
USER_="${ROBOT_USER:-pollen}"
SSH_TARGET="${USER_}@${HOST}"
PATCH_COMMIT="08d9e40ca"
REMOTE_DIR="/home/${USER_}/frame_pose_sync_${PATCH_COMMIT}"
DROPIN_DIR="/etc/systemd/system/reachy-mini-daemon.service.d"
DROPIN="${DROPIN_DIR}/frame_pose_sync.conf"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${HERE}/../../../upstream/reachy_mini" && pwd)"
SOURCE="${REPO}/src/reachy_mini"
FT_DEBUG_SOURCE="${HERE}/daemon_ft_debug.py"
SITE_CUSTOMIZE_SOURCE="${HERE}/sitecustomize.py"
FT_HZ="${REACHY_FT_DEBUG_HZ:-30}"
CTL="$(mktemp -u /tmp/frame_pose_sync_ctl.XXXXXX)"
SSH_OPTS=(-o ControlMaster=auto -o "ControlPath=${CTL}" -o ControlPersist=180
          -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20)

ssh_() { ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" "$@"; }
scp_() { scp "${SSH_OPTS[@]}" "$@"; }

close_connection() {
  ssh -O exit -o "ControlPath=${CTL}" "${SSH_TARGET}" 2>/dev/null || true
}
trap close_connection EXIT

wait_daemon() {
  ssh_ "for i in \$(seq 1 45); do curl -sf -m 3 http://127.0.0.1:8000/api/daemon/status >/dev/null 2>&1 && exit 0; sleep 2; done; exit 1"
}

write_dropin() {
  local debug="${1}"
  if [ "${debug}" = "on" ]; then
    ssh_ "sudo mkdir -p '${DROPIN_DIR}'; printf '%s\n' '[Service]' 'Environment=PYTHONPATH=${REMOTE_DIR}' 'Environment=REACHY_FT_DEBUG=1' 'Environment=REACHY_FT_DEBUG_HZ=${FT_HZ}' | sudo tee '${DROPIN}' >/dev/null; sudo systemctl daemon-reload; sudo systemctl restart reachy-mini-daemon"
  else
    ssh_ "sudo mkdir -p '${DROPIN_DIR}'; printf '%s\n' '[Service]' 'Environment=PYTHONPATH=${REMOTE_DIR}' | sudo tee '${DROPIN}' >/dev/null; sudo systemctl daemon-reload; sudo systemctl restart reachy-mini-daemon"
  fi
  wait_daemon
}

show_status() {
  ssh_ "
    if [ -f '${DROPIN}' ]; then echo 'frame-pose sync: INSTALLED'; else echo 'frame-pose sync: stock daemon'; fi
    printf 'service: '; systemctl is-active reachy-mini-daemon 2>/dev/null || true
    printf 'daemon API: '; curl -sf -m 3 http://127.0.0.1:8000/api/daemon/status >/dev/null 2>&1 && echo OK || echo unavailable
    systemctl show reachy-mini-daemon --property=Environment --no-pager | grep -o 'PYTHONPATH=[^ ]*' || true
  "
}

case "${ACTION}" in
  install)
    test "$(git -C "${REPO}" rev-parse --short=9 HEAD)" = "${PATCH_COMMIT}"
    ssh_ "mkdir -p '${REMOTE_DIR}'"
    scp_ -r "${SOURCE}" "${SSH_TARGET}:${REMOTE_DIR}/"
    ssh_ "PYTHONPATH='${REMOTE_DIR}' /venvs/mini_daemon/bin/python -c 'import reachy_mini.media.camera_timestamps as m; print(m.__file__)'"
    write_dropin off
    show_status
    echo "Patch installed. Robot remains asleep; do not start tracking before the verified wake sequence."
    ;;
  instrument)
    ssh_ "test -f '${DROPIN}' && test -d '${REMOTE_DIR}/reachy_mini'"
    scp_ "${FT_DEBUG_SOURCE}" "${SITE_CUSTOMIZE_SOURCE}" "${SSH_TARGET}:${REMOTE_DIR}/"
    write_dropin on
    show_status
    echo "Patch instrumentation enabled. Robot remains asleep until the runner performs the verified wake sequence."
    ;;
  plain)
    ssh_ "test -d '${REMOTE_DIR}/reachy_mini'"
    write_dropin off
    show_status
    echo "Patch remains installed; temporary measurement stream is disabled."
    ;;
  uninstall)
    ssh_ "sudo rm -f '${DROPIN}'; sudo rmdir '${DROPIN_DIR}' 2>/dev/null || true; sudo systemctl daemon-reload; sudo systemctl restart reachy-mini-daemon"
    wait_daemon
    show_status
    echo "Stock daemon restored. The versioned source copy remains recoverable at ${REMOTE_DIR}."
    ;;
  status)
    show_status
    ;;
  *)
    echo "usage: $0 {install|instrument|plain|status|uninstall}" >&2
    exit 2
    ;;
esac
