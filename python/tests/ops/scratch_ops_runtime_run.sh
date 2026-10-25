#!/usr/bin/env bash
# PACT r3-ops-runtime on a real scratch instance (okg 0.2.0 worker, stub proposer, marker mediator):
#   acc.r3-ops-runtime.e2e      L0, L1 and L2 (approve, reject) run end to end; records published and read back
#   acc.r3-ops-runtime.timeout  a timed-out proposal does not execute; a late approval is refused
#                               (and an approval that arrives after a worker restart does not execute either)
#   acc.r3-ops-runtime.halt     `archi ops halt` stops an approved run before its mediator
#   acc.r3-ops-runtime.demote   a stale source drops a live automation to shadow, reason on the proposal
#   acc.r3-ops-runtime.no-l3    L3 in archi.yaml is refused at run start
#   req.r3-ops-runtime.control  budget exhaustion; `archi ops status|pending|approve|reject|halt|resume`
#
# Usage: ARCHI_SCRATCH_DSN=postgresql://... scratch_ops_runtime_run.sh <empty work dir>
#
# Needs an EMPTY database on an AKMON Postgres (ghcr.io/mitdbg/akmon-postgres), okg 0.2.0 and archi
# installed in the Python environment whose bin/ holds `python` (set V to that bin/ directory).
# Starts one `okg runtime worker` in the background; its PID goes to <work dir>/pids and it is the only
# process this script kills. No paid LLM calls: the proposer is a stub.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
V="${V:-$(dirname "$(command -v python)")}"
RUN="$1"
DSN="${ARCHI_SCRATCH_DSN:?set ARCHI_SCRATCH_DSN to an empty AKMON database}"
F="$REPO/python/tests/ops/fixtures/scratch"
mkdir -p "$RUN"
[ -z "$(ls -A "$RUN")" ] || { echo "$RUN is not empty"; exit 1; }
RUN="$(cd "$RUN" && pwd)"
cd "$RUN"
unset OKG_PROFILES_DIR
D=r3rt
INST="$RUN/instance"
export ARCHI_INSTANCE="$INST" OKG_DSN="$DSN" ARCHI_OPS_DEPLOYMENT="$D" OKG_DEPLOYMENTS_DIR="$INST/deployments"
export PYTHONDONTWRITEBYTECODE=1
T="$V/python $REPO/python/tests/ops/scratch_ops_runtime_tools.py"
OPS="$V/archi ops --instance $INST --dsn $DSN --deployment $D"
PIDS="$RUN/pids"
FAILS=0

check() {  # check <label> <python expression over stdin as `d`/`rows`>
  "$V/python" -c "
import json, sys
text = sys.stdin.read()
try:
    d = json.loads(text)
except ValueError:
    d = None
rows = [json.loads(l) for l in text.splitlines() if l.startswith('{') and l.rstrip().endswith('}')]
ok = $2
print(('PASS' if ok else 'FAIL') + ': $1')
sys.exit(0 if ok else 1)
" || FAILS=$((FAILS + 1))
}

start_worker() {
  (cd "$INST" && nohup "$V/okg" runtime worker --deployment $D >> "$RUN/worker.log" 2>&1 & echo $! > "$RUN/worker.pid")
  cat "$RUN/worker.pid" >> "$PIDS"
  echo "worker started, pid $(cat "$RUN/worker.pid")"
  sleep "${1:-12}"
}
kill_worker() {  # kill -9 only the PID this script started
  kill -9 "$(cat "$RUN/worker.pid")" 2>/dev/null || true
  sleep 3
  if ps -p "$(cat "$RUN/worker.pid")" > /dev/null 2>&1; then echo "worker STILL RUNNING"; else echo "worker stopped"; fi
}
trigger() { "$V/okg" runtime trigger-schedule --deployment $D archi_ops_scan > "$RUN/trigger.out" 2>&1 || tail -3 "$RUN/trigger.out" >&2; }
publish() { "$V/okg" run --once --deployment $D --apply --json > "$RUN/publish-$1.json" 2>&1 || tail -3 "$RUN/publish-$1.json"; }
node() {  # node <type> <record id>: the published node, one JSON line
  "$V/okg" filter --deployment $D --type "$1" --where "attrs.record_id=$2" --all --jsonl 2>/dev/null
}
markers() { $T markers; }
new_run() {  # new_run <signal id>: add the signal, let the scanner start it, print the run id
  local wid; wid="$($T signal "$1")"; trigger; echo "$wid"
}
configure() { $T configure "$@"; }

echo "== 0. inputs: cern-team regrouped into packs, ops-record, jira and the scratch automation"
"$V/python" -c "
import sys; sys.path.insert(0, '$REPO/python/tests')
from pathlib import Path
from bundles.bundle_support import build_cern_team_catalog
build_cern_team_catalog(Path('$RUN/catalog'))
"
mkdir -p data/jira
cp "$REPO/python/tests/packs/fixtures/ops_record/jira_records_open.json" data/jira/records.json
echo '{"record_count": 2}' > data/jira/meta.json

echo "== 1. render with the scratch automation; the renderer declares the ops workflow module"
mkdir instance
"$V/archi" render --bundle cern-team --with ops-record,jira,scratch-tools,scratch-triage --instance instance \
  --pack-root "$RUN/catalog/packs" --pack-root "$F/packs" --bundle-root "$RUN/catalog" 2>&1 | grep -v "also provided"
mkdir -p instance/deployments
check "the rendered defaults declare runtime.workflows: [archi.ops.workflow]" \
  "'archi.ops.workflow' in open('$INST/profiles/cern-team/deployment-defaults.yaml').read()" < /dev/null
ls instance/automations

echo "== 2. install with okg; deployment.yaml must carry the runtime line from the start"
cd instance
"$V/okg" install --profile cern-team --deployment-name $D --postgres-dsn "$DSN" \
  --deployments-root "$OKG_DEPLOYMENTS_DIR" --archi-data-root "$RUN/data" \
  --non-interactive --json > "$RUN/install.json" || { echo "install failed"; tail -c 3000 "$RUN/install.json"; exit 1; }
"$V/okg" db role-passwords --apply --deployment $D > /dev/null
grep -n "^runtime" -A4 "$OKG_DEPLOYMENTS_DIR/$D/deployment.yaml" | tee "$RUN/runtime-line.txt"
check "deployment.yaml has runtime.enabled and archi.ops.workflow (written before okg install)" \
  "'enabled: true' in open('$RUN/runtime-line.txt').read() and 'archi.ops.workflow' in open('$RUN/runtime-line.txt').read()" < /dev/null
"$V/okg" runtime bootstrap --deployment $D 2>&1 | tail -3
cd "$RUN"
$T configure --level L0 --mode live
$V/python -c "from archi.ops.okg_io import OpsIO; OpsIO('$DSN', '$D').ensure_tables()"

echo "== 3. data: jira ingest (a fresh watermark for the gate), a seed incident the proposals cite"
"$V/okg" ingest --deployment $D --include jira.jira --json > "$RUN/ingest.json" 2>&1 || tail -5 "$RUN/ingest.json"
"$V/python" -m archi.ops.record submit --deployment $D --records "$F/seed.jsonl" > "$RUN/seed.json"
check "seed incident admitted" "d['output']['status'] == 'accepted'" < "$RUN/seed.json"
publish seed
node ops_incident inc-seed-1 > "$RUN/seed-node.jsonl"
check "the seed incident is published" "len(rows) == 1 and rows[0]['attrs']['entity'] == 'T2_US_MIT'" < "$RUN/seed-node.jsonl"
"$V/okg" doctor --check sources --deployment $D --json > "$RUN/doctor-sources.json" 2>&1 || true
check "okg doctor lists jira.jira with a watermark" \
  "any(r.get('name') == 'jira.jira' and ((r.get('coverage') or {}).get('watermark') or {}).get('last_reconcile_at') for r in d['sources'])" < "$RUN/doctor-sources.json"

echo "== 4. start the worker; the scanner schedule exists"
start_worker 15
$V/python -c "
from archi.ops.okg_io import OpsIO
print(OpsIO('$DSN', '$D').schedules())" | tee "$RUN/schedules-0.txt"
check "the archi_ops_scan schedule is registered and active" "'archi_ops_scan' in open('$RUN/schedules-0.txt').read() and 'False' in open('$RUN/schedules-0.txt').read()" < /dev/null

echo "== 5. L0 (acc e2e): observed, nothing escalated, no marker"
configure --level L0
W0=$(new_run l0-1); $T wait "$W0" | tee "$RUN/l0.out"
check "L0 run finished observed" "d['status'] == 'SUCCESS' and 'observed' in str(d['output'])" < "$RUN/l0.out"
check "no marker at L0" "d == 0" < <(markers)

echo "== 6. L1: the draft is recorded, nothing is sent or run"
configure --level L1 --proposal draft
W1=$(new_run l1-1); $T wait "$W1" | tee "$RUN/l1.out"
check "L1 run finished drafted" "d['status'] == 'SUCCESS' and 'drafted' in str(d['output'])" < "$RUN/l1.out"
check "no marker at L1" "d == 0" < <(markers)

echo "== 7. L2 approve: escalation waits; \`archi ops pending\` and \`approve\` answer it; marker appears"
configure --level L2 --proposal marker --timeout 180
W2=$(new_run l2a-1); ESC2=$($T pending "$W2" --timeout 120); echo "escalation $ESC2"
$OPS pending | tee "$RUN/pending-2.txt"
check "ops pending lists the escalation" "'$ESC2' in open('$RUN/pending-2.txt').read()" < /dev/null
$OPS status --json > "$RUN/status-2.json" 2>"$RUN/status-2.err" || cat "$RUN/status-2.err"
check "status shows level L2, mode live, one pending escalation" \
  "d['automations'][0]['level'] == 'L2' and d['automations'][0]['mode'] == 'live' and len(d['pending']) == 1 and d['scanner'] == {'archi_ops_scan': 'active'}" < "$RUN/status-2.json"
check "no marker before approval" "d == 0" < <(markers)
$OPS approve "$ESC2" --as scratch-op --note "scratch approve"
$T wait "$W2" | tee "$RUN/l2a.out"
check "L2 approved run executed and the outcome resolved" "d['status'] == 'SUCCESS' and 'executed' in str(d['output']) and 'resolved' in str(d['output'])" < "$RUN/l2a.out"
check "the marker was written exactly once" "d == 1" < <(markers)
$T escalation "$W2" > "$RUN/esc-2.json"
check "the escalation is applied, decided by scratch-op" "d['status'] == 'applied' and d['decided_by'] == 'scratch-op'" < "$RUN/esc-2.json"

echo "== 8. L2 reject: no marker"
W3=$(new_run l2r-1); ESC3=$($T pending "$W3" --timeout 120)
$OPS reject "$ESC3" --as scratch-op --note "not now"
$T wait "$W3" | tee "$RUN/l2r.out"
check "rejected run ends rejected" "d['status'] == 'SUCCESS' and 'rejected' in str(d['output'])" < "$RUN/l2r.out"
check "still one marker" "d == 1" < <(markers)

echo "== 9. L2 timeout (acc timeout): nobody answers; a late approval is refused; no marker"
configure --level L2 --proposal marker --timeout 15
W4=$(new_run l2t-1); ESC4=$($T pending "$W4" --timeout 60)
$T wait "$W4" --timeout 90 | tee "$RUN/l2t.out"
check "timed-out run ends timed_out" "d['status'] == 'SUCCESS' and 'timed_out' in str(d['output'])" < "$RUN/l2t.out"
$OPS approve "$ESC4" --as scratch-op > "$RUN/late-approve.out" 2>&1; echo "exit $?" >> "$RUN/late-approve.out"
cat "$RUN/late-approve.out"
check "the late approval was refused" "'timed_out' in open('$RUN/late-approve.out').read() and 'exit 1' in open('$RUN/late-approve.out').read()" < /dev/null
check "still one marker after the late approval" "d == 1" < <(markers)

echo "== 10. stale source (acc demote): a live L2 automation drops to shadow with the reason recorded"
$T source stale
configure --level L2 --proposal marker --timeout 120
W5=$(new_run l2d-1); $T wait "$W5" | tee "$RUN/l2d.out"
check "demoted run ends observed, in shadow" "d['status'] == 'SUCCESS' and 'observed' in str(d['output']) and 'shadow' in str(d['output'])" < "$RUN/l2d.out"
check "no escalation was opened" "d is None" < <($T escalation "$W5")
check "still one marker" "d == 1" < <(markers)
$T source fresh

echo "== 11. budget exhaustion: a proposal that costs more than the 0.5 per-run cap stops before escalation"
configure --level L2 --proposal marker --timeout 120 --cost 0.6
W6=$(new_run l2b-1); $T wait "$W6" | tee "$RUN/l2b.out"
check "run ends budget_exhausted" "d['status'] == 'SUCCESS' and 'budget_exhausted' in str(d['output'])" < "$RUN/l2b.out"
check "no escalation, still one marker" "d is None" < <($T escalation "$W6")
"$V/python" -c "
import psycopg
with psycopg.connect('$DSN') as c:
    print({r[0]: float(r[1]) for r in c.execute(\"select scope, sum(usd_consumed) from okg.deployment_cost_ledger group by scope\")})" > "$RUN/ledger.txt"
cat "$RUN/ledger.txt"
check "the ledger charged 0.6 to the run scope" "'run:scratch-triage-l2b-1-1' in open('$RUN/ledger.txt').read()" < /dev/null

echo "== 12. L3 in archi.yaml (acc no-l3): refused at run start, nothing runs"
configure --level L3 --proposal marker
W7=$(new_run l3-1); $T wait "$W7" | tee "$RUN/l3.out"
check "run ends refused, citing D13" "d['status'] == 'SUCCESS' and 'refused' in str(d['output']) and 'D13' in str(d['output'])" < "$RUN/l3.out"
$OPS status --json > "$RUN/status-l3.json" 2>"$RUN/status-l3.err" || cat "$RUN/status-l3.err"
check "status names the L3 problem" "'L3' in str(d['automations'][0]['level_problem'])" < "$RUN/status-l3.json"
check "still one marker" "d == 1" < <(markers)

echo "== 13. worker-restart late approval: the guard refuses it"
configure --level L2 --proposal marker --timeout 40
W8=$(new_run l2w-1); ESC8=$($T pending "$W8" --timeout 90)
kill_worker
echo "waiting past the deadline (40 s) with the worker down"; sleep 50
$OPS approve "$ESC8" --as scratch-op > "$RUN/restart-approve.out" 2>&1; cat "$RUN/restart-approve.out"
check "the late approval was ACCEPTED by okg (worker down, no sweep)" "'approved' in open('$RUN/restart-approve.out').read()" < /dev/null
start_worker 15
$T wait "$W8" --timeout 120 | tee "$RUN/l2w.out"
check "the recovered run did not execute: timed_out" "d['status'] == 'SUCCESS' and 'timed_out' in str(d['output'])" < "$RUN/l2w.out"
check "still one marker (the guard refused)" "d == 1" < <(markers)

echo "== 14. halt during an approved run before its mediator (acc halt)"
configure --level L2 --proposal marker --timeout 300
W9=$(new_run l2h-1); ESC9=$($T pending "$W9" --timeout 90)
kill_worker
$OPS approve "$ESC9" --as scratch-op | tee "$RUN/halt-approve.out"
$OPS halt --as scratch-op --json > "$RUN/halt.json" 2> "$RUN/halt.err"; cat "$RUN/halt.json"; cat "$RUN/halt.err"
check "halt paused the scanner and cancelled the approved run" "d['paused'] == ['archi_ops_scan'] and d['cancelled'] == ['$W9']" < "$RUN/halt.json"
start_worker 15
sleep 15
$T wait "$W9" --timeout 60 | tee "$RUN/l2h.out"
check "the run is CANCELLED" "d['status'] == 'CANCELLED'" < "$RUN/l2h.out"
check "no marker was written (still one)" "d == 1" < <(markers)
$T escalation "$W9" > "$RUN/esc-9.json"
check "the escalation is closed (decided, not pending)" "d['status'] in ('decided', 'applied')" < "$RUN/esc-9.json"
$OPS status --json > "$RUN/status-halted.json"
check "status shows the scanner paused" "d['scanner'] == {'archi_ops_scan': 'paused'}" < "$RUN/status-halted.json"

echo "== 15. halt while a run waits for approval: the escalation is closed with a reject, then the run is cancelled"
$OPS resume --json | tee "$RUN/resume.json"
check "resume un-paused the scanner halt paused" "d['resumed'] == ['archi_ops_scan']" < "$RUN/resume.json"
W10=$(new_run l2h2-1); ESC10=$($T pending "$W10" --timeout 90)
$OPS halt --as scratch-op --json > "$RUN/halt2.json" 2> "$RUN/halt2.err"; cat "$RUN/halt2.json"
check "halt closed the open escalation and cancelled the run" "d['closed'] == ['$ESC10'] and d['cancelled'] == ['$W10']" < "$RUN/halt2.json"
$T wait "$W10" --timeout 60 | tee "$RUN/l2h2.out"
check "the waiting run is CANCELLED" "d['status'] == 'CANCELLED'" < "$RUN/l2h2.out"
$T escalation "$W10" > "$RUN/esc-10.json"
check "its escalation was answered reject by archi ops halt" "d['status'] in ('decided', 'applied') and d['decided_by'] == 'archi ops halt'" < "$RUN/esc-10.json"
check "still one marker" "d == 1" < <(markers)
$OPS resume > /dev/null

echo "== 16. publish and read the ops records back"
sleep 20   # let the queued halted-record admissions run
publish final
rb() { node "$1" "$2" > "$RUN/rb-$2.jsonl"; }
rb ops_decision "dec-$W0";  check "L0: decision observed" "rows[0]['attrs']['verdict'] == 'observed' and rows[0]['attrs']['actor_kind'] == 'automation' and rows[0]['attrs']['generation_read']" < "$RUN/rb-dec-$W0.jsonl"
rb ops_proposal "prop-$W1"; check "L1: the draft is on the proposal" "'looking at the SRM' in rows[0]['attrs']['draft'] and rows[0]['attrs']['mode'] == 'live'" < "$RUN/rb-prop-$W1.jsonl"
rb ops_decision "dec-$W1";  check "L1: decision drafted" "rows[0]['attrs']['verdict'] == 'drafted'" < "$RUN/rb-dec-$W1.jsonl"
rb ops_decision "dec-$W2";  check "L2 approve: decision approved by scratch-op, escalation id recorded" "rows[0]['attrs']['verdict'] == 'approved' and rows[0]['attrs']['actor'] == 'scratch-op' and rows[0]['attrs']['escalation_id'].startswith('esc-')" < "$RUN/rb-dec-$W2.jsonl"
rb ops_action "act-$W2";    check "L2 approve: action succeeded" "rows[0]['attrs']['status'] == 'succeeded' and rows[0]['attrs']['action_class'] == 'write_marker'" < "$RUN/rb-act-$W2.jsonl"
rb ops_outcome "out-$W2";   check "L2 approve: outcome resolved" "rows[0]['attrs']['result'] == 'resolved'" < "$RUN/rb-out-$W2.jsonl"
rb ops_decision "dec-$W3";  check "L2 reject: decision rejected" "rows[0]['attrs']['verdict'] == 'rejected'" < "$RUN/rb-dec-$W3.jsonl"
rb ops_decision "dec-$W4";  check "timeout: decision timed_out" "rows[0]['attrs']['verdict'] == 'timed_out'" < "$RUN/rb-dec-$W4.jsonl"
rb ops_proposal "prop-$W5"; check "demotion: proposal is shadow with the failing check as the reason" "rows[0]['attrs']['mode'] == 'shadow' and rows[0]['attrs']['mode_reason'].startswith('demoted to shadow: ') and 'freshness:jira.jira' in rows[0]['attrs']['mode_reason']" < "$RUN/rb-prop-$W5.jsonl"
rb ops_decision "dec-$W6";  check "budget: decision budget_exhausted" "rows[0]['attrs']['verdict'] == 'budget_exhausted'" < "$RUN/rb-dec-$W6.jsonl"
rb ops_incident "inc-$W7";  check "L3: a run-level incident says why nothing ran" "'run not started' in rows[0]['attrs']['summary'] and 'D13' in rows[0]['attrs']['summary'] and not rows[0]['attrs'].get('generation_read')" < "$RUN/rb-inc-$W7.jsonl"
rb ops_decision "dec-$W8";  check "restart: decision timed_out, the late approval was not executed" "rows[0]['attrs']['verdict'] == 'timed_out' and 'after the deadline' in rows[0]['attrs']['reason']" < "$RUN/rb-dec-$W8.jsonl"
rb ops_decision "dec-$W9-halt"; check "halt: a halted decision for the cancelled approved run" "rows[0]['attrs']['verdict'] == 'halted' and rows[0]['attrs']['actor'] == 'scratch-op'" < "$RUN/rb-dec-$W9-halt.jsonl"
rb ops_decision "dec-$W10-halt"; check "halt: a halted decision for the waiting run" "rows[0]['attrs']['verdict'] == 'halted'" < "$RUN/rb-dec-$W10-halt.jsonl"

echo "== 17. cleanup: stop the worker this script started"
kill_worker
if [ "$FAILS" = 0 ]; then echo "== done: every check passed"; else echo "== done: $FAILS check(s) FAILED"; exit 1; fi
