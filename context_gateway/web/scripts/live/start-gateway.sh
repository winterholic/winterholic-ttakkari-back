#!/bin/bash
# 격리 PostgreSQL 17 + 실제 public(18787)/control(18789) 앱을 임시 디렉터리에 띄운다. 합성 값만 쓴다.
# 사용: scripts/live/start-gateway.sh   (끝나면 scripts/live/stop-gateway.sh)
set -e
export LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8   # 없으면 postgres가 "postmaster became multithreaded"로 죽는다
HERE="$(cd "$(dirname "$0")" && pwd)"
CORE="$(cd "$HERE/../../.." && pwd)"            # context_gateway/
RUN="${CONTEXT_GATEWAY_LIVE_DIR:-/tmp/ctx-gw-live}"
PGB=/opt/homebrew/opt/postgresql@17/bin
PGPORT=54871
rm -rf "$RUN"; mkdir -p "$RUN/socket" "$RUN/snap"
"$PGB/initdb" -D "$RUN/data" -A trust --no-locale --encoding=UTF8 -c dynamic_shared_memory_type=mmap >"$RUN/initdb.log" 2>&1
"$PGB/pg_ctl" -D "$RUN/data" -o "-h 127.0.0.1 -p $PGPORT -k $RUN/socket -F" -w start >"$RUN/pg.log" 2>&1
export CONTEXT_GATEWAY_DATABASE_URL="postgresql+asyncpg://$(whoami)@127.0.0.1:$PGPORT/postgres"
export CONTEXT_GATEWAY_SNAPSHOT_DIR="$RUN/snap" CONTEXT_GATEWAY_CONTROL_TOKEN=synthetic-control-token-not-secret
cd "$CORE"
nohup .venv/bin/uvicorn context_gateway.public:app --port 18787 --host 127.0.0.1 >"$RUN/public.log" 2>&1 &
echo $! >"$RUN/public.pid"
sleep 3   # public이 스키마를 만든 뒤 control을 띄운다
nohup .venv/bin/uvicorn control_launch:app --app-dir "$HERE" --port 18789 --host 127.0.0.1 >"$RUN/control.log" 2>&1 &
echo $! >"$RUN/control.pid"
echo "public http://127.0.0.1:18787  control http://127.0.0.1:18789  run-dir $RUN"
