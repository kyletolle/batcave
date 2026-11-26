#!/bin/bash
# ExecStop helper for services that sometimes hang on SIGTERM (netdata 1.43).
# Usage (in a unit):  ExecStop=/usr/local/sbin/graceful-stop $MAINPID 30
#                     SuccessExitStatus=KILL
#
# Sends SIGTERM, waits up to GRACE seconds, then SIGKILLs the main process.
# Because the kill happens inside ExecStop (not via systemd's stop timeout) and
# the unit lists KILL as a success status, a hung shutdown ends as
# Result=success instead of Result=timeout, so OnFailure= never fires for a
# routine restart. A real crash at runtime still counts as a failure.
#
# Installed root-owned to /usr/local/sbin by setup-vps.sh, because ExecStop runs
# as root under PermissionsStartOnly=true.
PID="${1:?usage: graceful-stop MAINPID [GRACE_SECONDS]}"
GRACE="${2:-30}"

kill -TERM "$PID" 2>/dev/null || exit 0
for _ in $(seq 1 "$GRACE"); do
    kill -0 "$PID" 2>/dev/null || exit 0
    sleep 1
done
echo "graceful-stop: pid ${PID} ignored SIGTERM for ${GRACE}s, sending SIGKILL"
kill -KILL "$PID" 2>/dev/null
exit 0
