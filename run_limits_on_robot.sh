#!/usr/bin/env bash
#
# Run the handoff-limit runner ON the robot's Pi, against localhost -- so the
# flaky Mac<->robot wifi is only in your SSH session (an established connection
# that survives), never between the runner and the daemon.
#
# Optionally start the instrumented daemon first (separate terminal):
#   bash instrument/deploy_to_robot.sh --run
# The runner also works fine against the STOCK daemon (head pose is the primary
# signal here).
#
#   bash run_limits_on_robot.sh                 # push runner, run the session, pull results
#   bash run_limits_on_robot.sh --preflight     # push runner, read-only check, pull nothing
#   bash run_limits_on_robot.sh --window 1.5    # any limits_runner.py args are forwarded
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
CTL="$(mktemp -u /tmp/limits_runner_ctl.XXXXXX)"
SSH_OPTS+=(-o ControlMaster=auto -o "ControlPath=${CTL}" -o ControlPersist=180)

ssh_() { ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "$@"; }
scp_() { scp "${SSH_OPTS[@]}" "$@"; }
trap 'ssh -O exit -o "ControlPath=${CTL}" "$SSH_TARGET" 2>/dev/null || true' EXIT

echo ">> pushing runner to ${REMOTE_DIR} (enter the robot password if asked)"
ssh_ "mkdir -p '${REMOTE_DIR}/diag'"
scp_ "${HERE}/limits_runner.py" "${SSH_TARGET}:${REMOTE_DIR}/"
scp_ "${HERE}/diag/"*.py "${SSH_TARGET}:${REMOTE_DIR}/diag/"

echo ">> running the handoff-limit runner on the robot (localhost)"
echo ">>   (Ctrl-C interrupts the session but partial results are still pulled back)"
echo "--------------------------------------------------------------------------"
# Do NOT let a non-zero / interrupted remote run abort the script before the
# results copy below — an interrupted session is often still useful.
rc=0
ssh "${SSH_OPTS[@]}" -t "$SSH_TARGET" \
  "cd '${REMOTE_DIR}' && '${DPY}' limits_runner.py --host localhost $*" || rc=$?
echo "--------------------------------------------------------------------------"
[ "$rc" -ne 0 ] && echo ">> remote runner exited ${rc} (interrupted or error) — still copying any results"

case " $* " in
  *" --preflight "*|*" --dry-run "*) exit "$rc";;
esac

echo ">> copying results back to ${LOCAL_RESULTS_DIR}/"
mkdir -p "${LOCAL_RESULTS_DIR}"
if scp_ -r "${SSH_TARGET}:${REMOTE_DIR}/results/"* "${LOCAL_RESULTS_DIR}/" 2>/dev/null; then
  echo ">> done. Results in ${LOCAL_RESULTS_DIR}/"
else
  echo "   (no results directory found on the robot)"
fi
exit "$rc"
