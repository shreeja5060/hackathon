#!/bin/sh
# Starts the dashboard (the Docker image's CMD).
#
# With LEDGER_REPLICA_URL set (for example gs://BUCKET/ledger), the ledger is first
# restored from Cloud Storage, then Litestream runs the dashboard and streams every
# change back while it runs. Without it, the dashboard just starts and the ledger
# lives on the container's disk (fine locally or with a mounted persistent volume).
set -eu

cd /app/phase3_dashboard
# The same paths the dashboard uses (phase3_dashboard/core/memory.py), made absolute, so Litestream
# replicates exactly the files the app writes. The simulator keeps its own file.
LEDGER_PATH="$(python -c 'import sys; sys.path.insert(0, "/app")
from phase3_dashboard.core.memory import default_paths
print(default_paths(False)[0])')"
LEDGER_SIM_PATH="$(python -c 'import sys; sys.path.insert(0, "/app")
from phase3_dashboard.core.memory import default_paths
print(default_paths(True)[0])')"
export LEDGER_PATH LEDGER_SIM_PATH

APP="streamlit run app.py --server.port=${PORT:-8080} --server.address=0.0.0.0 \
--server.headless=true --browser.gatherUsageStats=false"

if [ -n "${LEDGER_REPLICA_URL:-}" ]; then
    for db in "$LEDGER_PATH" "$LEDGER_SIM_PATH"; do
        litestream restore -config /app/deploy/litestream.yml -if-db-not-exists -if-replica-exists "$db"
    done
    # Create the files (and their tables) if this is the very first start.
    python -c 'import os, sys; sys.path.insert(0, "/app")
from shared.ledger import Ledger
for path in (os.environ["LEDGER_PATH"], os.environ["LEDGER_SIM_PATH"]):
    Ledger(path)'
    exec litestream replicate -config /app/deploy/litestream.yml -exec "$APP"
fi
exec $APP
