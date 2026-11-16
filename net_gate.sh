#!/usr/bin/env bash
# GitHub is the BACKUP for the update network (owner, 2026-11-12): the PCs
# scrape, sync and read usage; a GitHub run of the same job goes ahead only
# when the network is NOT doing it.
#
# Asks the site's net_watch (the same call net-watch.yml makes every 10
# minutes, Bearer ORDERS_TOKEN). state "ok" = a network report arrived in the
# last 30 minutes. Anything else - silent, no report, not configured, the
# site unreachable, no token - and the job RUNS: a backup that cannot tell
# must do the work rather than leave it undone.
#
# NEED_JOB (optional, but every backup of a job the site reports should set
# it): the name of ONE network job ("orders", "stellar_prices", "usage",
# "coupons", "chooser"). Then the network being up is not enough: that job
# must have SUCCEEDED on a computer within MAX_JOB_AGE_S (default 900). The
# site answers jobs.<name> = {age_s, running}; an answer without age_s (an
# older site, or a job no computer has finished yet) is "not known", and the
# backup runs. Without NEED_JOB, state "ok" alone skips the job - right only
# for work the site does not report per job (the per-country scrape).
# 2026-11-16: stellar-prices, usage and coupons stood back on "ok" alone, so
# a network whose computers never ran that job (no Stellar key in any vault,
# a job in shadow, a job failing every run) left it undone everywhere.
#
# takeover (with NEED_JOB only): may this backup take over the network's own
# CLAIMS? Only on positive proof that the network's job is dead, ALL of:
#   * the site answered (HTTP 200) and the network is reporting (state "ok"):
#     a 'silent' network is one we cannot see, not one we saw die - its PC
#     may be mid-purchase behind a dead uplink;
#   * a last success older than TAKEOVER_AGE_S (default 1500 - a run cut at
#     its 720 s limit, the 60 s rest, the next run of 240 s and the 2 minutes
#     between reports all fit inside);
#   * jobs.<name>.running is exactly false: no computer reports a run of it
#     in progress. null / missing (an older site) is not proof;
#   * the report itself is at most TAKEOVER_REPORT_MAX_S (300) old: an older
#     one cannot say what started since.
# "Cannot tell" - an older site, an error, no answer in time - is NOT proof:
# then the backup works on what nobody has claimed and leaves the network's
# claims alone (two buyers under one claim was the review's finding of
# 2026-11-14).
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
  jrun=""
  if [ -n "$job" ]; then
    jage=$(NEED_JOB="$job" python3 -c "
import json, os
d = json.load(open('net_gate.json')); x = d.get('data') or d
a = ((x.get('jobs') or {}).get(os.environ['NEED_JOB']) or {}).get('age_s')
print(int(a) if isinstance(a, (int, float)) and not isinstance(a, bool) and a >= 0 else '')" 2>/dev/null || echo "")
    # true / false only when the site says so in a JSON boolean; anything
    # else (null, missing, a string) stays '' = not known.
    jrun=$(NEED_JOB="$job" python3 -c "
import json, os
d = json.load(open('net_gate.json')); x = d.get('data') or d
r = ((x.get('jobs') or {}).get(os.environ['NEED_JOB']) or {}).get('running')
print('true' if r is True else 'false' if r is False else '')" 2>/dev/null || echo "")
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
      echo "The network is up but its '${job}' job has no success within ${MAX_JOB_AGE_S:-900}s (last: ${jage:-unknown}s, running: ${jrun:-unknown}) - this GitHub backup runs."
    fi
  else
    echo "The network is not reporting - this GitHub backup runs."
  fi
  # Only a backup that runs can take anything over (a job whose MAX_JOB_AGE_S
  # is above TAKEOVER_AGE_S would otherwise say "stand back" and "take over"
  # in one breath). Only stellar-buy.yml reads takeover.
  if [ "$skip" = "false" ] && [ -n "$job" ] && [ -n "$jage" ] && [ "$jage" -gt "${TAKEOVER_AGE_S:-1500}" ]; then
    if [ "$code" = "200" ] && [ "$state" = "ok" ] && [ "$jrun" = "false" ] && [ -n "$age" ] && [ "$age" -le "${TAKEOVER_REPORT_MAX_S:-300}" ]; then
      takeover=true
      echo "The network's '${job}' job has had no success for ${jage}s and no computer is running it - its claims may be taken over."
    else
      echo "No takeover: the '${job}' job is old (${jage}s) but not proven dead (HTTP ${code}, state '${state}', running: ${jrun:-unknown})."
    fi
  fi
fi
echo "skip=${skip}" >> "$out"
echo "takeover=${takeover}" >> "$out"
exit 0
