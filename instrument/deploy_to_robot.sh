#!/usr/bin/env bash
#
# Deploy the face-tracking debug instrumentation to the robot's Raspberry Pi
# and (optionally) run the daemon through it for one baseline session.
#
# Run this ON YOUR MAC, in your own terminal (it prompts for the robot's SSH
# password unless you have a key set up).
#
#   bash instrument/deploy_to_robot.sh              # copy files + inspect + print next steps
#   bash instrument/deploy_to_robot.sh --run        # copy, stop service, run instrumented daemon
#                                                   #   (Ctrl-C restarts the normal service)
#
# How it works: it runs the robot's OWN daemon launcher, unchanged, but with
# PYTHONPATH=~/ft_debug and REACHY_FT_DEBUG=1 set, so sitecustomize.py applies
# the patch at interpreter startup. Nothing on the robot is modified.
#
# SSH: ssh pollen@reachy-mini.local   (password: root)
# Docs: https://huggingface.co/docs/reachy_mini/en/platforms/reachy_mini/development_workflow
#
set -euo pipefail

HOST="${ROBOT_HOST:-reachy-mini.local}"
USER_="${ROBOT_USER:-pollen}"
REMOTE_DIR="/home/${USER_}/ft_debug"
FT_HZ="${REACHY_FT_DEBUG_HZ:-30}"
DO_RUN=0
[ "${1:-}" = "--run" ] && DO_RUN=1

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSH_TARGET="${USER_}@${HOST}"

CTL="$(mktemp -u /tmp/ft_deploy_ctl.XXXXXX)"
SSH_OPTS=(-o ControlMaster=auto -o "ControlPath=${CTL}" -o ControlPersist=180
          -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15)
ssh_() { ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "$@"; }
scp_() { scp "${SSH_OPTS[@]}" "$@"; }
cleanup_ctl() { ssh -O exit -o "ControlPath=${CTL}" "$SSH_TARGET" 2>/dev/null || true; }
trap cleanup_ctl EXIT

echo ">> opening SSH connection to ${SSH_TARGET} (enter the password once if asked)"
ssh_ true

PROJ="$(cd "${HERE}/.." && pwd)"
echo ">> copying instrumentation + runner to ${REMOTE_DIR}"
ssh_ "mkdir -p '${REMOTE_DIR}/diag'"
scp_ "${HERE}/daemon_ft_debug.py" "${HERE}/run_instrumented_daemon.py" \
     "${HERE}/sitecustomize.py" "${SSH_TARGET}:${REMOTE_DIR}/"
# The runner too, so it can be run ON the Pi against localhost -- no wifi
# between client and daemon (the only wifi link is your SSH session).
scp_ "${PROJ}/diagnostic_runner.py" "${SSH_TARGET}:${REMOTE_DIR}/"
scp_ "${PROJ}/diag/"*.py "${SSH_TARGET}:${REMOTE_DIR}/diag/"

echo ">> inspecting the reachy-mini-daemon service"
EXECSTART="$(ssh_ "systemctl cat reachy-mini-daemon 2>/dev/null | sed -n 's/^ExecStart=//p'" || true)"
SVC_USER="$(ssh_ "systemctl show -p User --value reachy-mini-daemon 2>/dev/null" || true)"
SVC_WD="$(ssh_ "systemctl show -p WorkingDirectory --value reachy-mini-daemon 2>/dev/null" || true)"
[ -z "$SVC_USER" ] && SVC_USER="root"
if [ -z "$EXECSTART" ]; then
  echo "!! could not read the reachy-mini-daemon service."
  exit 1
fi
LAUNCH_FIRST="$(printf '%s\n' "$EXECSTART" | awk '{print $1}')"
LAUNCHER_BODY=""
case "$LAUNCH_FIRST" in
  *.sh) LAUNCHER_BODY="$(ssh_ "cat '${LAUNCH_FIRST}' 2>/dev/null" || true)";;
esac

echo
echo "   ExecStart    : ${EXECSTART}"
echo "   runs as user : ${SVC_USER}"
if [ -n "$LAUNCHER_BODY" ]; then
  echo "   ---- ${LAUNCH_FIRST} ----"
  printf '%s\n' "$LAUNCHER_BODY" | sed 's/^/   | /'
  echo "   -------------------------------------------"
fi
echo

# systemctl on a system service always via sudo (passwordless on this image).
# The launcher runs as the service user (pollen) -- no sudo -- with our hook on
# PYTHONPATH. Match the service WorkingDirectory so relative paths behave.
CD_PART=""
[ -n "$SVC_WD" ] && [ "$SVC_WD" != "-" ] && CD_PART="cd '${SVC_WD}' && "
# We SSH in as $USER_. Only step down to a different service user.
RUN_AS=""
[ -n "$SVC_USER" ] && [ "$SVC_USER" != "$USER_" ] && [ "$SVC_USER" != "-" ] && RUN_AS="sudo -u '${SVC_USER}' "
RUN_CMD="${CD_PART}${RUN_AS}env REACHY_FT_DEBUG=1 REACHY_FT_DEBUG_HZ=${FT_HZ} PYTHONPATH='${REMOTE_DIR}' ${EXECSTART}"

if [ "$DO_RUN" -eq 0 ]; then
  cat <<EOF
Files are on the robot. To run the instrumented daemon for one session:

  ssh ${SSH_TARGET}
  sudo systemctl stop reachy-mini-daemon
  ${RUN_CMD}
  #   ... from your Mac:  ./run --host ${HOST}
  #   Ctrl-C here when done, then:
  sudo systemctl start reachy-mini-daemon

Re-run this script with --run to do the stop / launch / restore automatically.
Look for  "[ft_debug] instrumentation applied via sitecustomize"  in the daemon
output -- that means it took.
EOF
  exit 0
fi

DPY="/venvs/mini_daemon/bin/python"
cat <<EOF
>> stopping the normal daemon; launching it through the debug hook

   Wait for BOTH of these before running the diagnostic:
     [ft_debug] instrumentation applied via sitecustomize
     Uvicorn running on http://0.0.0.0:8000        (startup finished)

   Then, in a SEPARATE terminal, run the diagnostic. If your wifi to the robot
   is flaky, run it ON the robot (no wifi between runner and daemon):

     ssh -t ${SSH_TARGET} '${DPY} ${REMOTE_DIR}/diagnostic_runner.py --host localhost'

   ...and afterwards copy the results back:

     scp -r ${SSH_TARGET}:${REMOTE_DIR}/results ./results_robot

   Or, if wifi is fine, from your Mac:  ./run --host ${HOST}

   Ctrl-C here when the session is done -- it restores the normal service.
--------------------------------------------------------------------------
EOF
restore() {
  echo; echo "--------------------------------------------------------------------------"
  echo ">> restoring the normal daemon service"
  ssh_ "sudo systemctl start reachy-mini-daemon" \
    || echo "!! restart it manually: ssh ${SSH_TARGET} 'sudo systemctl start reachy-mini-daemon'"
  cleanup_ctl
}
trap 'restore; exit 0' INT TERM
trap 'restore' EXIT
ssh_ -t "sudo systemctl stop reachy-mini-daemon; ${RUN_CMD}"
