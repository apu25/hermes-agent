# Delivery Failure Watchdog Local Ops Script

This local operations script is the source-controlled copy of:

```text
~/.hermes/scripts/delivery-failure-watchdog.py
```

It watches recent Hermes delivery logs for Feishu delivery failures and emits a
low-frequency no-agent cron summary only when there are new or still-undelivered
failures.

## Runtime restore

To restore the live script after rebuilding or replacing `~/.hermes/scripts`:

```bash
install -m 755 scripts/local-ops/delivery-failure-watchdog.py ~/.hermes/scripts/delivery-failure-watchdog.py
```

The live cron job is expected to keep using:

```text
Script: delivery-failure-watchdog.py
Deliver: feishu:oc_35b18c7ff42867517ebc3eb176b29c21
Mode: no-agent
```

## Duplicate alert guard

The script keeps `acknowledged_keys` in:

```text
~/.hermes/delivery-failure-watchdog.json
```

When the watchdog successfully emits a pending summary, those event keys are
acknowledged. This prevents old timestamp-less lines that are still present in
the log tail from being re-collected and re-alerted every repeat interval.
