# Hermes runtime ops backup: delivery watchdog

Created on 2026-06-27 for the local Hermes Feishu gateway incident.

This folder is intentionally a runtime backup, not a source-code change to the
Hermes agent package. The affected files live under `~/.hermes`, outside the
tracked `hermes-agent` checkout, so they need a separate GitHub-backed restore
point.

## Contents

- `scripts/hermes-it-health-check.py`
  - Treats launchd-supervised Hermes gateway processes as healthy.
  - Prevents false restarts when `hermes gateway status` reports
    `Gateway is supervised by launchd (PID ...)`.
- `scripts/delivery-failure-watchdog.py`
  - No-agent cron watchdog for recent Feishu delivery failures.
  - Reads only log tails, keeps a small local state file, and stays silent when
    there are no new unresolved delivery failures.
  - Keeps acknowledged event keys after successful watchdog delivery so old
    timestamp-less log lines do not re-enter pending on later scans.
- `cron/jobs.restore.json`
  - Restore metadata for the IT health check and delivery-failure watchdog cron
    jobs.
- `restore.sh`
  - Copies scripts into `~/.hermes/scripts` and ensures both cron jobs exist.

## Restore

From this directory:

```bash
./restore.sh
```

The restore script does not copy runtime state such as
`~/.hermes/delivery-failure-watchdog.json`; that file is intentionally local and
ephemeral.
