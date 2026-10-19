# Drift detection

Most services on a homelab are not git checkouts. They get deployed by
copying files, and sooner or later something gets fixed "just on the server".
[`check_drift.py`](check_drift.py) runs every night and answers one question:
**does every installed file still match the default branch?** It names exactly
the files that don't.

## How it compares, without cloning anything

Git identifies file content by its **blob SHA-1**: `sha1("blob <size>\0" + content)`,
the same value `git hash-object` prints. The GitHub API returns the whole tree of a
branch with one blob SHA per path in a single request
(`GET /repos/{owner}/{repo}/git/trees/{branch}?recursive=1`). So the check:

1. fetches the tree of each repo's default branch, with a **read-only** token;
2. computes the blob SHA of each installed file on disk;
3. compares the two.

It needs no clone and no checkout on the server, and it needs no write access
anywhere. It runs as an unprivileged user.

## What the manifest describes

[`drift-manifest.example.json`](drift-manifest.example.json) has three kinds of
entries:

| Type | Compares | Catches |
|---|---|---|
| `files` | repo path → absolute path, one by one (scripts in `/usr/local/bin`, units, cron files) | A script edited in place, or a unit never redeployed |
| `tree` | repo directory → server directory | The same, plus **code that exists only on the server** (`only-on-server`) |
| `command` | a repo file → a command's output (`crontab -l`) | A crontab edited by hand |

Runtime state, build output and secrets are excluded by default (`node_modules`,
`data`, `.env*`, logs and so on). In `tree` mode **only code counts as drift**:
a README that differs is reported as `info`, because docs are not always
deployed. The manifest lists **itself** and the checker, so a stale checker
cannot quietly hide drift.

## Exit codes and the alert

| Exit | Meaning | Gatus `Jobs / config-drift` |
|---|---|---|
| 0 | everything matches | green |
| 1 | drift found | red |
| 2 | the check **could not run** (token, network, API, bad manifest) | red, with a different message |

"Could not look" and "found drift" are different statements, so they get
different exit codes. On drift, one `drift: <kind> <path>` line per file goes
to stderr. `run-and-report.sh` sends the stderr tail to Gatus, so the alert
names the files instead of saying "exit 1".

## Try it locally

`--local OWNER/REPO=PATH` reads a repo's tree from a local checkout
(`git ls-tree HEAD`) instead of the API. That works for air-gapped hosts, for
CI, and for this demo:

```bash
tmp=$(mktemp -d) && cd "$tmp"
git init -q repo && mkdir -p repo/server srv
echo 'console.log(1)' > repo/server/app.js && echo '# app' > repo/server/README.md
git -C repo add . && git -C repo -c user.name=me -c user.email=me@example.com commit -qm init
cp repo/server/* srv/                                    # "deploy" by copying
echo 'console.log("hotfix")' > srv/hotfix.js             # a fix made only on the server
echo '# edited' > srv/README.md                          # a doc edited on the server
cat > m.json <<EOF
{"entries": [{"name": "app-a", "type": "tree", "repo": "example-org/app-a",
              "repo_dir": "server", "server_dir": "$tmp/srv"}]}
EOF
python3 /path/to/drift/check_drift.py --manifest m.json --local example-org/app-a=repo
```

```text
DRIFT app-a: 1 (+1 info)
        info differs            server/README.md  (…/srv/README.md)
        only-on-server     server/hotfix.js  (…/srv/hotfix.js)
total: 1 file(s) out of sync, 1 informational
drift: only-on-server server/hotfix.js
```

## What it found in practice

On its first run in production, it found an API route that had been added by
hand on the server and never committed; it was rescued into the repo. Every
service added since then joins the manifest when it is deployed.
