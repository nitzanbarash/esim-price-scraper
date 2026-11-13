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
# NEED_JOB (optional): the name of ONE network job (e.g. "orders"). Then the
# network being up is not enough: that job must have SUCCEEDED on a computer
# within MAX_JOB_AGE_S (default 900 - longer than one run of it may last plus
# the two minutes between reports). The site answers jobs.<name>.age_s; an
# answer without it (an older site) is "not known", and the backup runs.
#
# takeover (with NEED_JOB only): may this backup take over the network's own
# CLAIMS? Only on positive proof that the network's job is dead: the site
# answered, and it names a last success older than TAKEOVER_AGE_S (default
# 1500 - a run cut at its 720 s limit, the 60 s rest, the next run of 240 s
# and the 2 minutes between reports all fit inside). "Cannot tell" - an older
# site, an error, no answer in time - is NOT proof: then the backup works on
# what nobody has claimed and leaves the network's claims alone (two buyers
# under one claim was the review's finding of 2026-11-14).
#
# Writes skip=true|false and takeover=true|false to $GITHUB_OUTPUT. Never
# fails the job. The three tries fit inside a 2-minute step.
set -u
out="${GITHUB_OUTPUT:-/dev/stdout}"
skip=false
takeover=false
if [ "${FORCE_RUN:-false}" = "true" ]; then
  echo "Run by hand with force: the network is not asked."
elif [ -z "${ORDERS_TOKEN:-}" ]; then
  echo "ORDERS_TOKEN is not set - cannot ask the network, so this backup runs."
else
  code=0
  for i in 1 2 3; do
    code=$(curl -sS -o net_gate.json -w '%{http_code}' --max-time 20 \
      -X POST 'https://www.waverole.com/api/admin?fn=net_watch' \
      -H "Authorization: Bearer ${ORDERS_TOKEN}" \
      -H 'Content-Type: application/json' --data '{}') || code=0
    [ "$code" = "200" ] && break
    sleep 5
  done
  state=$(python3 -c "import json;d=json.load(open('net_gate.json'));x=d.get('data') or d;print(x.get('state') or '')" 2>/dev/null || echo "")
  age=$(python3 -c "import json;d=json.load(open('net_gate.json'));x=d.get('data') or d;print(x.get('age_s') if x.get('age_s') is not None else '')" 2>/dev/null || echo "")
  job="${NEED_JOB:-}"
  jage=""
  if [ -n "$job" ]; then
    jage=$(NEED_JOB="$job" python3 -c "
import json, os
d = json.load(open('net_gate.json')); x = d.get('data') or d
a = ((x.get('jobs') or {}).get(os.environ['NEED_JOB']) or {}).get('age_s')
print(int(a) if isinstance(a, (int, float)) and not isinstance(a, bool) and a >= 0 else '')" 2>/dev/null || echo "")
  fi
  rm -f net_gate.json
  echo "network: HTTP ${code}, state '${state}', last report ${age:-?}s ago"
  if [ "$code" = "200" ] && [ "$state" = "ok" ]; then
    if [ -z "$job" ]; then
      skip=true
      echo "The network is up and doing this job - this GitHub backup is skipped."
    elif [ -n "$jage" ] && [ "$jage" -le "${MAX_JOB_AGE_S:-900}" ]; then
      skip=true
      echo "The network's '${job}' job last succeeded ${jage}s ago - it is doing this, the GitHub backup stands back."
    else
      echo "The network is up but its '${job}' job has no success within ${MAX_JOB_AGE_S:-900}s (last: ${jage:-unknown}s) - this GitHub backup runs."
    fi
  else
    echo "The network is not reporting - this GitHub backup runs."
  fi
  if [ "$code" = "200" ] && [ -n "$job" ] && [ -n "$jage" ] && [ "$jage" -gt "${TAKEOVER_AGE_S:-1500}" ]; then
    takeover=true
    echo "The network's '${job}' job has had no success for ${jage}s - its claims may be taken over."
  fi
fi
echo "skip=${skip}" >> "$out"
echo "takeover=${takeover}" >> "$out"
exit 0
