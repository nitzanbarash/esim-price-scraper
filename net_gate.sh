#!/usr/bin/env bash
# GitHub is the BACKUP for the update network (owner, 2026-11-12): the PCs
# scrape, sync and read usage; a GitHub run of the same job goes ahead only
# when the network is NOT reporting.
#
# Asks the site's net_watch (the same call net-watch.yml makes every 10
# minutes, Bearer ORDERS_TOKEN). state "ok" = a network report arrived in the
# last 30 minutes, so the job is skipped. Anything else - silent, no report,
# not configured, the site unreachable, no token - and the job RUNS: a backup
# that cannot tell must do the work rather than leave it undone.
#
# Writes skip=true|false to $GITHUB_OUTPUT. Never fails the job.
set -u
out="${GITHUB_OUTPUT:-/dev/stdout}"
skip=false
if [ "${FORCE_RUN:-false}" = "true" ]; then
  echo "Run by hand with force: the network is not asked."
elif [ -z "${ORDERS_TOKEN:-}" ]; then
  echo "ORDERS_TOKEN is not set - cannot ask the network, so this backup runs."
else
  code=0
  for i in 1 2 3; do
    code=$(curl -sS -o net_gate.json -w '%{http_code}' --max-time 40 \
      -X POST 'https://www.waverole.com/api/admin?fn=net_watch' \
      -H "Authorization: Bearer ${ORDERS_TOKEN}" \
      -H 'Content-Type: application/json' --data '{}') || code=0
    [ "$code" = "200" ] && break
    sleep 15
  done
  state=$(python3 -c "import json;d=json.load(open('net_gate.json'));x=d.get('data') or d;print(x.get('state') or '')" 2>/dev/null || echo "")
  age=$(python3 -c "import json;d=json.load(open('net_gate.json'));x=d.get('data') or d;print(x.get('age_s') if x.get('age_s') is not None else '')" 2>/dev/null || echo "")
  rm -f net_gate.json
  echo "network: HTTP ${code}, state '${state}', last report ${age:-?}s ago"
  if [ "$code" = "200" ] && [ "$state" = "ok" ]; then
    skip=true
    echo "The network is up and doing this job - this GitHub backup is skipped."
  else
    echo "The network is not reporting - this GitHub backup runs."
  fi
fi
echo "skip=${skip}" >> "$out"
exit 0
