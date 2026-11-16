#!/bin/bash
RUN="${CONTEXT_GATEWAY_LIVE_DIR:-/tmp/ctx-gw-live}"
for f in public control; do [ -f "$RUN/$f.pid" ] && kill "$(cat "$RUN/$f.pid")" 2>/dev/null; done
LC_ALL=en_US.UTF-8 /opt/homebrew/opt/postgresql@17/bin/pg_ctl -D "$RUN/data" -m fast -w stop 2>/dev/null
echo stopped
