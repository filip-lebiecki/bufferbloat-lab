#!/usr/bin/env bash
# Push the setup scripts from this directory to the three boxes and verify.
#
# This directory is the source of truth; each box runs its own copy from ~.
# Nothing keeps them in sync automatically, and a stale copy is silent and
# expensive: edit the rate here, forget to deploy, re-run setup-router.sh on
# the router, and you shoot a whole cold open at the wrong bandwidth. That
# happened on 2026-08-21. Run this after any edit to a setup-*.sh.
#
#   ./deploy-scripts.sh            push, then verify checksums
#   ./deploy-scripts.sh --check    verify only, change nothing
set -uo pipefail

cd "$(dirname "$0")"

ROUTER="192.168.80.31"
SERVER="192.168.80.33"
# the client is not directly reachable — the router masquerades its replies,
# so ssh to it goes through the router as a jump host. See lab/README.md.
CLIENT_SSH=(ssh -o BatchMode=yes -J "$ROUTER" 192.168.80.32)
ROUTER_SSH=(ssh -o BatchMode=yes "$ROUTER")
SERVER_SSH=(ssh -o BatchMode=yes "$SERVER")

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

sum() { md5sum "$1" | cut -d' ' -f1; }

fail=0
# The two web tools ride along: they have the same stale-copy hazard as the
# setup scripts, and a tuner that is a version behind is worse than none.
for spec in "setup-router.sh:ROUTER_SSH" "setup-router-org.sh:ROUTER_SSH" \
            "setup-router-cake.sh:ROUTER_SSH" \
            "setup-client.sh:CLIENT_SSH" "setup-server.sh:SERVER_SSH" \
            "caketune.py:ROUTER_SSH" "cakemeter.py:CLIENT_SSH"; do
    file=${spec%%:*}; ref=${spec#*:}
    declare -n SSH=$ref
    want=$(sum "$file")

    if [ "$CHECK_ONLY" = 0 ]; then
        if ! "${SSH[@]}" "cat > ~/$file && chmod +x ~/$file" < "$file"; then
            echo "  $file: PUSH FAILED" >&2; fail=1; continue
        fi
    fi

    have=$("${SSH[@]}" "md5sum ~/$file 2>/dev/null | cut -d' ' -f1")
    if [ "$have" = "$want" ]; then
        echo "  $file: ok ($want)"
    else
        echo "  $file: MISMATCH — local $want, remote ${have:-<missing>}" >&2
        fail=1
    fi
done

echo
if [ "$fail" = 0 ]; then
    echo "ALL IN SYNC."
    [ "$CHECK_ONLY" = 0 ] && echo "Re-run the setup scripts on the boxes to apply any change:" \
        && echo "  ssh filip@$SERVER  'sudo ./setup-server.sh'" \
        && echo "  ssh filip@$ROUTER  'sudo ./setup-router.sh'" \
        && echo "  ssh -J $ROUTER filip@192.168.80.32 'sudo ./setup-client.sh'" \
        && echo "The web tools need a restart to pick up a change." \
        && echo "Two ssh calls, not one: pkill -f matches the remote shell's own" \
        && echo "command line as soon as the start command is on it, so a combined" \
        && echo "one-liner kills itself before it forks. And setsid --fork, not" \
        && echo "setsid nohup ... in the background: the shell form printed its banner" \
        && echo "and then died with the ssh session. Both learned the hard way, 2026-08-24." \
        && echo "  ssh $ROUTER 'pkill -f \"[c]aketune.py\"'" \
        && echo "  ssh $ROUTER 'setsid --fork ~/.local/bin/uv run --no-project ~/caketune.py >~/caketune.log 2>&1 </dev/null'" \
        && echo "  ssh -J $ROUTER filip@192.168.80.32 'pkill -f \"[c]akemeter.py\"'" \
        && echo "  ssh -J $ROUTER filip@192.168.80.32 'setsid --fork ~/.local/bin/uv run --no-project ~/cakemeter.py >~/cakemeter.log 2>&1 </dev/null'"
    exit 0
else
    echo "OUT OF SYNC — see above." >&2
    exit 1
fi
