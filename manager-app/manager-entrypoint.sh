#!/bin/sh
set -eu

export DEFAULT_QUOTA_RESET_HOURS="3.1666666667"
export DEFAULT_QUOTA_LIMIT_TOKENS="67000"


python /app/quota_sync.py &

if [ -f /app/startup.py ]; then
    exec python /app/startup.py
fi

exec python /app/app.py
