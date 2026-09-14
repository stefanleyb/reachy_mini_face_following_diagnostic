#!/usr/bin/env bash
#
# Run the diagnostic runner ON the robot's Pi, against localhost -- so the flaky
# wifi is only in your SSH session (an established connection that survives),
# never between the runner and the daemon.
#
# Use this in a SECOND terminal while `instrument/deploy_to_robot.sh --run` keeps
# the instrumented daemon alive in the first terminal.
#
#   bash run_on_robot.sh                 # push runner, run the session, pull results
#   bash run_on_robot.sh --preflight     # push runner, read-only check, pull nothing
#   bash run_on_robot.sh --only 1,4      # any diagnostic_runner.py args are forwarded
#
set -euo pipefail

HOST="${ROBOT_HOST:-reachy-mini.local}"
USER_="${ROBOT_USER:-pollen}"
SSH_TARGET="${USER_}@${HOST}"
REMOTE_DIR="/home/${USER_}/ft_debug"
DPY="/venvs/mini_daemon/bin/python"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_RESULTS_DIR="${REACHY_FACE_ROBOT_RESULTS_DIR:-${HERE}/../../runs/face_following/results_robot}"

SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=20
          -o ServerAliveInterval=15 -o ServerAliveCountMax=4)
CTL="$(mktemp -u /tmp/face_follow_runner_ctl.XXXXXX)"
SSH_OPTS+=(-o ControlMaster=auto -o "ControlPath=${CTL}" -o ControlPersist=180)

ssh_() { ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "$@"; }
scp_() { scp "${SSH_OPTS[@]}" "$@"; }
trap 'ssh -O exit -o "ControlPath=${CTL}" "$SSH_TARGET" 2>/dev/null || true' EXIT

echo ">> pushing runner to ${REMOTE_DIR} (enter the robot password if asked)"
ssh_ "mkdir -p '${REMOTE_DIR}/diag'"
scp_ "${HERE}/diagnostic_runner.py" "${SSH_TARGET}:${REMOTE_DIR}/"
scp_ "${HERE}/diag/"*.py "${SSH_TARGET}:${REMOTE_DIR}/diag/"

echo ">> running the diagnostic on the robot (localhost)"
echo "--------------------------------------------------------------------------"
ssh "${SSH_OPTS[@]}" -t "$SSH_TARGET" \
  "cd '${REMOTE_DIR}' && '${DPY}' diagnostic_runner.py --host localhost $*"
rc=$?
echo "--------------------------------------------------------------------------"

case " $* " in
  *" --preflight "*|*" --dry-run "*) exit $rc;;
esac

echo ">> copying results back to ${LOCAL_RESULTS_DIR}/"
mkdir -p "${LOCAL_RESULTS_DIR}"
scp_ -r "${SSH_TARGET}:${REMOTE_DIR}/results/"* "${LOCAL_RESULTS_DIR}/" 2>/dev/null \
  || echo "   (no results directory found on the robot)"
echo ">> done. Results in ${LOCAL_RESULTS_DIR}/"
exit $rc
