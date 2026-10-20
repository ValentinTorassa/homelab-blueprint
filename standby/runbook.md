# Runbook: the server is down

The standby is cold and manual on purpose (see [`README.md`](README.md)).
Follow the steps in order. **Never run the primary and the standby as live
hosts at the same time.**

## 0. Before you ever need it (checked by the quarterly drill)

- [ ] `Jobs / backup-offsite-s3` is green: the bundles in S3 are fresh.
- [ ] The emergency entry exists in the external password manager (check it by
      name; don't open it). It holds the offsite passphrase, the standby's SSH
      key, cloud console access, VPN admin, DNS and the registrar logins.
- [ ] The last drill passed, and its evidence log is filed.
- [ ] The standby is `stopped`, and its resting instance type is the expected one.

## 1. Detect

Gatus on the monitor alerts through the relay: `homeserver SSH`, the
`server-health` push, and the per-service probes. If the monitor itself goes
quiet, the relay's watchdog says so within minutes.

**First try the boring fix.** Most "server down" alerts are a hung or
powered-off box, not a disaster. Wake it over the LAN from the monitor, or power-cycle it.
That is not disaster recovery, and it does not need the standby.

## 2. Decide (a person, not a script)

Confirm the primary is **really down** and **will stay down** for long enough
to justify a takeover. The standby holds the same tunnel and API credentials.
With both up, traffic splits between two hosts, bots answer twice, and
schedulers publish twice.

If you are not sure, do not take over. Waiting costs downtime. Taking over
wrongly costs duplicated actions that can't be undone.

## 3. Start the standby

```bash
aws ec2 start-instances --instance-ids <standby-id> --profile <profile> --region <region>
aws ec2 wait instance-running --instance-ids <standby-id> --profile <profile> --region <region>
```

On `InsufficientInstanceCapacity`, retry. If that keeps failing, change the
type while the instance is stopped (`modify-instance-attribute`, then try
another arm64 type) and start again. Note the original type so you can put it
back later.

Join the standby to the VPN with a fresh auth key, then SSH in over the VPN.

## 4. Take over

```bash
sudo install -m 600 /dev/stdin /root/offsite.pass     # paste the passphrase, then Ctrl-D
sudo /root/takeover.sh --pass-file /root/offsite.pass --primary-is-down
sudo shred -u /root/offsite.pass
```

`takeover.sh` pulls the newest bundles, restores secrets and data, holds due
jobs, removes the rehearsal block, starts the services and the tunnel, and
prints what answers.

## 5. Verify from the outside

- Every health URL printed by the takeover answers 200, and every unit is `active`.
- From a device **off** the home network, the public hostnames load.
- The monitor's probes for the moved services turn green (they follow DNS
  and the tunnel).

## 6. Review held jobs

Every scheduled job that was already due when the snapshot was taken is now
`held-failover`. Before releasing each one, check whether the primary had
already published it before it died. Release them one by one.

## 7. Fail back (manual)

1. **Stop the services and the tunnel on the standby first.** Never run two
   tunnel connectors at once.
2. Carry over anything that changed on the standby, such as job-store state
   and anything published from it, into the primary's data, or note it for
   reconciliation.
3. Bring the primary back with its tunnel, and confirm the probes are green.
4. Re-arm the rehearsal block on the standby (or re-provision it), restore its
   resting instance type, and **stop** it.
5. Write down what happened, with times, what surprised you, and what the
   runbook missed, then fix the runbook.

## Quarterly drill

```bash
standby/takeover-drill.sh --dry-run    # walk the flow; prints every command, executes none
standby/takeover-drill.sh              # the real drill: start → takeover --dry-run → stop
```

The drill can never run a real takeover, and it always stops the standby.
File its evidence log, and compare the timings with the previous drill. A
different number is information. Do not average it into a promise.
