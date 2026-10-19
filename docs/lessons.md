# Lessons: the failures that shaped this

Every mechanism in this repo exists because something went wrong first. These
are short, blameless write-ups, sanitized, with the fix each one led to.

### A job that never ran looked exactly like a job that worked

**What happened:** A weekly data ingest silently didn't run. It left no file
and no log line, and the dashboard was green because the service's port
answered. Around the same time an upstream token expired unnoticed, and a
publisher logged a warning about an unconfirmed upload and moved on.
**Root cause:** Monitoring checked processes, not outcomes. And cron has no
catch-up, so a run missed while the box is down simply never happens.
**Fix:** `run-and-report.sh` on every job, pushing on success and failure, to
endpoints with heartbeats ([`monitoring/`](../monitoring/)).

### The monitor silenced its own health report

**What happened:** Gatus's listen address was changed, and the script that
pushes the server's health report kept using the old one. For about a day the
pushes failed with "connection refused", and the endpoint sat at "no update
received". The backups were fine throughout, but nobody was watching them.
**Root cause:** The component that alerts was the component that broke, so
there was no alert. Only the heartbeat's expiry, much later, showed it.
**Fix:** The bind address and every pusher's base URL are documented as one
setting, and pushers log "unreachable" as its own error. The broader case,
the monitor itself going quiet, is now covered from outside the house by the
watchdog.

### A 404 that read like "monitor unreachable"

**What happened:** While the job heartbeats were being deployed against the
real Gatus, a push to `/endpoints/<name>/external` returned 404, because Gatus
keys endpoints as `<group>_<name>`. Read as a generic failure, that looks like
"Gatus unreachable", and the job keeps reporting into the void until its
heartbeat expires. Later, a sibling problem actually happened: a job was
wrapped but its endpoint was never added, and a week of daily failures went
to a log file while the board stayed green.
**Fix:** The reporter prepends the group, and logs 404 ("unknown endpoint"),
401/403 ("token rejected") and unreachable as three different errors. A CI
lint fails if any cron line or unit reports to a key with no endpoint.

### An alert that mathematically could not fire

**What happened:** An afternoon completeness check reported to the same
endpoint as a morning job. The morning success reset the failure counter every
day, so the check could never reach the default threshold of three
consecutive failures. A config review caught it.
**Fix:** One endpoint per job, and threshold 1 for checks that run once a day
or less.

### "Fresh" is not "full"

**What happened:** The offsite job checked that each `latest` snapshot was
recent, and nothing more. A job that started producing empty directories
would have passed every night and shipped nothing offsite, with a green tick.
**Fix:** Missing, stale *and empty* are all failures. Beyond that, the weekly
restore test proves a snapshot is usable, not merely present.

### `8d` took the monitor down

**What happened:** A weekly heartbeat was written as `8d`. Gatus parses
durations with Go's `time.Duration`, which has no unit for days, so the
restart left the service failed.
**Fix:** Heartbeats in hours (`192h`), and a lint that parses every duration
the way Go does, before deploy.

### Code that only existed on the server

**What happened:** The first nightly drift run found an API route that had
been added by hand on the server and never committed. One bad disk away from
being lost.
**Fix:** It was rescued into git. The drift check now runs nightly, and new
services join its manifest when they are deployed.

### The first automated drill failed on capacity

**What happened:** The first scripted standby drill could not start the
instance: `InsufficientInstanceCapacity` for its type in that zone.
**Fix:** Retry, then fall back to other arm64 types while stopped, and restore
the resting type afterwards. The runbook says the same for a real takeover.
A drill that fails for a boring reason is a drill doing its job.
