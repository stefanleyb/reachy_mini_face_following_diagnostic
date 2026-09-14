#!/usr/bin/env bash
#
# Robust deploy of the face-tracking debug instrumentation, managed by systemd.
#
# Instead of babysitting a foreground daemon over a flaky SSH link, this adds a
# systemd drop-in so the NORMAL `reachy-mini-daemon` service loads the debug
# hook. systemd then owns it: auto-restart, survives SSH drops and robot
# reboots -- until you uninstall.
#
#   bash instrument/setup_ft_debug.sh install     # deploy + enable + verify
#   bash instrument/setup_ft_debug.sh status      # is it active?
#   bash instrument/setup_ft_debug.sh verify      # run the runner's preflight on the Pi
#   bash instrument/setup_ft_debug.sh uninstall   # remove drop-in, back to stock
#
# Puts on the robot (all removed by `uninstall` + the printed rm):
#   /home/pollen/ft_debug/            daemon_ft_debug.py, sitecustomize.py, runner
#   /etc/systemd/system/reachy-mini-daemon.service.d/ft_debug.conf
#
set -euo pipefail

HOST="${ROBOT_HOST:-reachy-mini.local}"
USER_="${ROBOT_USER:-pollen}"
SSH_TARGET="${USER_}@${HOST}"
REMOTE_DIR="/home/${USER_}/ft_debug"
DROPIN_DIR="/etc/systemd/system/reachy-mini-daemon.service.d"
DROPIN="${DROPIN_DIR}/ft_debug.conf"
FT_HZ="${REACHY_FT_DEBUG_HZ:-30}"
DPY="/venvs/mini_daemon/bin/python"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJ="$(cd "${HERE}/.." && pwd)"
ACTION="${1:-status}"

CTL="$(mktemp -u /tmp/ft_setup_ctl.XXXXXX)"
SSH_OPTS=(-o ControlMaster=auto -o "ControlPath=${CTL}" -o ControlPersist=180
          -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20
          -o ServerAliveInterval=15 -o ServerAliveCountMax=4)
ssh_()  { ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "$@"; }
ssht_() { ssh "${SSH_OPTS[@]}" -t "$SSH_TARGET" "$@"; }
scp_()  { scp "${SSH_OPTS[@]}" "$@"; }
trap 'ssh -O exit -o "ControlPath=${CTL}" "$SSH_TARGET" 2>/dev/null || true' EXIT

wait_daemon() {
  echo ">> waiting for the daemon on localhost:8000 (up to 90s)"
  ssh_ 'for i in $(seq 1 45); do
          curl -sf -m 3 http://127.0.0.1:8000/api/daemon/status >/dev/null 2>&1 && { echo "  daemon up"; exit 0; }
          sleep 2
        done
        echo "  !! no answer within 90s"; exit 1'
}

show_status() {
  echo ">> status"
  ssh_ "
    if [ -f '${DROPIN}' ]; then echo '  drop-in : INSTALLED'; sed 's/^/    | /' '${DROPIN}'
    else echo '  drop-in : not installed (stock daemon)'; fi
    printf '  service : '; systemctl is-active reachy-mini-daemon 2>/dev/null || true
    printf '  daemon localhost:8000 : '; curl -sf -m 3 http://127.0.0.1:8000/api/daemon/status >/dev/null 2>&1 && echo OK || echo 'no answer'
    echo '  ft_debug log lines (last 5):'
    journalctl -u reachy-mini-daemon -n 500 --no-pager 2>/dev/null | grep -E 'ft_debug|sitecustomize' | tail -5 | sed 's/^/    /' || true
  "
}

push_files() {
  echo ">> copying files to ${REMOTE_DIR}"
  ssh_ "mkdir -p '${REMOTE_DIR}/diag'"
  scp_ "${HERE}/daemon_ft_debug.py" "${HERE}/sitecustomize.py" \
       "${HERE}/run_instrumented_daemon.py" "${SSH_TARGET}:${REMOTE_DIR}/"
  scp_ "${PROJ}/diagnostic_runner.py" "${SSH_TARGET}:${REMOTE_DIR}/"
  scp_ "${PROJ}/diag/"*.py "${SSH_TARGET}:${REMOTE_DIR}/diag/"
}

verify_stream() {
  echo ">> running the runner's preflight ON the Pi (localhost)"
  echo "--------------------------------------------------------------------------"
  ssht_ "cd '${REMOTE_DIR}' && '${DPY}' diagnostic_runner.py --preflight --host localhost"
  echo "--------------------------------------------------------------------------"
  echo "   Look for:  instrumentation: present   and   ft_debug ~ NN Hz"
}

case "$ACTION" in
  install)
    echo ">> opening SSH (enter the robot password once if asked)"
    ssh_ true
    push_files
    echo ">> installing systemd drop-in ${DROPIN}"
    printf '[Service]\nEnvironment=REACHY_FT_DEBUG=1\nEnvironment=REACHY_FT_DEBUG_HZ=%s\nEnvironment=PYTHONPATH=%s\n' \
      "${FT_HZ}" "${REMOTE_DIR}" \
      | ssh_ "sudo mkdir -p '${DROPIN_DIR}' && sudo tee '${DROPIN}' >/dev/null && sudo systemctl daemon-reload && sudo systemctl restart reachy-mini-daemon"
    wait_daemon
    sleep 3
    show_status
    echo
    verify_stream
    echo
    echo "Done. Instrumented daemon runs under systemd (auto-restart, reboot-safe)."
    echo "Session:  bash run_on_robot.sh --preflight   then   bash run_on_robot.sh"
    echo "Revert :  bash instrument/setup_ft_debug.sh uninstall"
    ;;
  uninstall)
    echo ">> removing drop-in, restoring stock daemon"
    ssh_ "sudo rm -f '${DROPIN}'; sudo rmdir '${DROPIN_DIR}' 2>/dev/null || true; sudo systemctl daemon-reload; sudo systemctl restart reachy-mini-daemon"
    wait_daemon
    sleep 2
    show_status
    echo
    echo "Stock daemon restored. Remove leftover files with:"
    echo "   ssh ${SSH_TARGET} 'rm -rf ${REMOTE_DIR}'"
    ;;
  status)  show_status ;;
  verify)  verify_stream ;;
  push)    ssh_ true; push_files; echo "files updated (systemctl restart reachy-mini-daemon to reload)";;
  *)  echo "usage: $0 {install|status|verify|uninstall|push}" >&2; exit 2 ;;
esac
