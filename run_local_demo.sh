#!/usr/bin/env bash
# Modemsiz sinash (mock modemlar, SQLite): web panel + worker
set -e
cd "$(dirname "$0")"
export CREATE_DEMO_MODEM=1 DATABASE_URL="sqlite:///$(pwd)/data/demo.db"
mkdir -p data
python -m app.worker &
WORKER=$!
trap "kill $WORKER" EXIT
python -m uvicorn app.web:app --host 127.0.0.1 --port 8088
