# Design decisions

Short, ADR-style. Each one records what was decided, why, and what it costs.

### 1. The monitor runs on separate hardware

**Decision:** Gatus runs on a small always-on board, not on the server.
**Why:** A monitor that shares a power supply, kernel and disk with what it
watches dies at the same moment and says nothing.
**Cost:** One more device to patch. Its own liveness needs watching too (see 2).

### 2. Something outside the house watches the monitor

**Decision:** The monitor sends an HMAC-signed heartbeat to a serverless relay.
A scheduled trigger on the relay alerts when the heartbeat stops.
**Why:** If home internet, power or the monitor goes, nothing local can report it.
**Cost:** One external dependency, and a whole-house outage takes minutes, not seconds, to surface.

### 3. Jobs push results; heartbeats catch silence

**Decision:** Every scheduled job reports through `run-and-report.sh`, on
success *and* failure, to an external endpoint with a heartbeat.
**Why:** Port checks answer "is it up?". The failures that hurt were "it is up,
and last night's job did nothing".
**Cost:** One endpoint and one token per job. CI checks that every reported
key has an endpoint.

### 4. Heartbeat = cadence + one run of grace, in hours; weekly jobs alert at 1

**Decision:** Daily jobs get 30 h and weekly jobs 192 h. Weekly endpoints use
failure/success thresholds of 1/1.
**Why:** Go durations have no days (`8d` stops Gatus from starting). Default
thresholds of 3/2 would page on a weekly job's *third* bad week.
**Cost:** A weekly job that is late by a day goes red. That is the point.

### 5. `unknown` is red

**Decision:** A restore check that could not run (no docker, over the disk cap,
no permission) reports `unknown`, and the endpoint goes red.
**Why:** Untested is not green. A test that silently skips whatever is hard
gives false confidence.
**Cost:** Occasional red for host prerequisites rather than broken backups. The
message says which.

### 6. Restore into scratch, never in place; containers without network

**Decision:** Copy the snapshot to scratch and open the copy. Credentials are
stat-only. Throwaway containers run with `--network none --pull never`, use
read-only scratch mounts, and are named `restoretest-*`.
**Why:** The test must not be able to damage the backups or production, or
leak data, even when it has a bug.
**Cost:** Disk and time for the copy, bounded by caps.

### 7. Rotate only after a verified snapshot is published

**Decision:** Build the snapshot hidden, verify it, publish it atomically,
*then* rotate.
**Why:** Rotating first means a failing run deletes a good snapshot before it
has produced a replacement.
**Cost:** KEEP+1 snapshots on disk for a moment.

### 8. Drift is checked by blob SHA against the API, without cloning

**Decision:** Compare `sha1("blob <n>\0"+content)` on disk with the default
branch's tree from the GitHub API. Use a read-only token. Exit 2 means "could
not check", which is distinct from 1, "drift".
**Why:** No checkout on the server, no write access, and one API call per repo.
Conflating "could not look" with "found something" trains people to ignore
the alert.
**Cost:** Files must be deployed byte-identical. Templated config needs its
rendered form in git, or an exclusion.

### 9. Only code counts as drift in directory mode

**Decision:** A differing README is `info`, and a differing `.py` is drift.
Runtime state, build output and secrets are excluded.
**Why:** An alert that fires on docs gets muted, and then it misses code.
**Cost:** A deployed doc can drift without paging anyone.

### 10. Cold standby, human-initiated

**Decision:** A stopped instance in AWS, started and taken over only by a
person, with the takeover refusing to run without `--primary-is-down`.
**Why:** The standby shares the tunnel and API credentials. An automatic
failover on a false positive splits traffic and double-publishes. A homelab
can afford hours of downtime more easily than reliable fencing.
**Cost:** RTO in hours, and it needs a person awake.

### 11. The standby can read backups; the uploader cannot delete them

**Decision:** The standby has a read-only instance role. The home uploader can
put and read, with an explicit deny on delete and retention changes. The
bucket uses versioning plus Object Lock.
**Why:** The credential that lives at home is the one most likely to be
compromised. It should be able to add history, never erase it.
**Cost:** Storage for retained versions.

### 12. Recovery secrets live outside the system being recovered

**Decision:** The offsite passphrase, the standby's SSH key, and the cloud,
VPN and DNS admin logins are kept in a password manager that does not run on
the homelab.
**Why:** If recovering the vault needs the vault, there is no recovery.
**Cost:** Two places to keep in sync. The drill checks the emergency entry
exists, by name only.

### 13. Dated claims expire

**Decision:** The coherence check fails when a "last verified YYYY-MM-DD" note
in the docs is older than 30 days. Run logs and history files are exempt.
**Why:** A verification date that nobody refreshes slowly turns into a lie.
**Cost:** Periodic re-verification, or a reworded note. Both are the point.
