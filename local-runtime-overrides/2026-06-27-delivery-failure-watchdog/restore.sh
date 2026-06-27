#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
SCRIPTS_DIR="$HERMES_HOME/scripts"
JOBS_FILE="$HERMES_HOME/cron/jobs.json"
SPEC_FILE="$ROOT/cron/jobs.restore.json"

mkdir -p "$SCRIPTS_DIR" "$(dirname "$JOBS_FILE")"

install -m 0755 "$ROOT/scripts/hermes-it-health-check.py" "$SCRIPTS_DIR/hermes-it-health-check.py"
install -m 0755 "$ROOT/scripts/delivery-failure-watchdog.py" "$SCRIPTS_DIR/delivery-failure-watchdog.py"

ensure_job() {
  local job_id="$1"
  local name="$2"
  local display="$3"
  local script="$4"
  local deliver="$5"

  if python3 - "$JOBS_FILE" "$job_id" "$name" <<'PY'
import json
import sys
path, job_id, name = sys.argv[1:4]
try:
    with open(path) as f:
        data = json.load(f)
except FileNotFoundError:
    sys.exit(1)
for job in data.get("jobs", []):
    if job.get("id") == job_id or job.get("name") == name:
        sys.exit(0)
sys.exit(1)
PY
  then
    python3 - "$JOBS_FILE" "$SPEC_FILE" "$job_id" <<'PY'
import json
import sys

jobs_path, spec_path, target_id = sys.argv[1:4]
with open(jobs_path) as f:
    data = json.load(f)
with open(spec_path) as f:
    specs = {job["id"]: job for job in json.load(f)["jobs"]}
spec = specs[target_id]
for job in data.get("jobs", []):
    if job.get("id") == spec["id"] or job.get("name") == spec["name"]:
        job["id"] = spec["id"]
        job["name"] = spec["name"]
        job["script"] = spec["script"]
        job["no_agent"] = spec["no_agent"]
        job["schedule"] = spec["schedule"]
        job["schedule_display"] = spec["schedule"].get("display")
        job["deliver"] = spec["deliver"]
        job["enabled"] = spec["enabled"]
        break
with open(jobs_path, "w") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
    f.write("\n")
PY
  else
    hermes cron create "$display" --name "$name" --script "$script" --no-agent --deliver "$deliver"
  fi
}

python3 - "$SPEC_FILE" <<'PY' | while IFS=$'\t' read -r job_id name display script deliver; do
import json
import sys
with open(sys.argv[1]) as f:
    data = json.load(f)
for job in data["jobs"]:
    print("\t".join([
        job["id"],
        job["name"],
        job["schedule"]["display"],
        job["script"],
        job["deliver"],
    ]))
PY
  ensure_job "$job_id" "$name" "$display" "$script" "$deliver"
done

python3 -m py_compile "$SCRIPTS_DIR/hermes-it-health-check.py" "$SCRIPTS_DIR/delivery-failure-watchdog.py"
hermes cron list | grep -E 'IT|delivery-failure-watchdog' || true
