#!/bin/sh
# Copy the application to the NAS and restart it, in a single SSH session.
#
#   ./deploy.sh
#
# Settings come from deploy.env next to this script (see deploy.env.example),
# or from the environment. Only the code is copied: your config, database,
# cover cache and logs on the NAS are never touched.
set -e

cd "$(dirname "$0")"
[ -f deploy.env ] && . ./deploy.env

: "${OPDS_HOST:?set OPDS_HOST, e.g. cp deploy.env.example deploy.env and edit it}"
: "${OPDS_REMOTE:=/volume1/Contents/OpdsServer}"
: "${OPDS_PYTHON:=python3}"
: "${LOCAL_PYTHON:=python3}"

echo "==> self-check, locally"
"$LOCAL_PYTHON" test_server.py > /dev/null

echo "==> copying to $OPDS_HOST:$OPDS_REMOTE and restarting"
# One connection, so one password prompt: the files travel through the same
# SSH session that then runs the commands.
tar czf - app test_server.py README.md | ssh "$OPDS_HOST" "set -e
    cd '$OPDS_REMOTE'
    tar xzf -
    rm -rf app/__pycache__

    # a failing self-check must not take the running server down
    if ! '$OPDS_PYTHON' test_server.py > /tmp/opds-selftest.log 2>&1; then
        echo '==> self-check FAILED on the NAS, server left untouched'
        tail -20 /tmp/opds-selftest.log
        exit 1
    fi
    echo '==> self-check passed on the NAS'

    '$OPDS_PYTHON' app/server.py restart
    '$OPDS_PYTHON' app/server.py status
    [ -f logs/server.log ] && tail -3 logs/server.log
    exit 0"

echo "==> done"
