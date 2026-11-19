# A deploy failed, or "succeeded" and changed nothing

| | |
| --- | --- |
| Severity | SEV2 if the stack is down or half-deployed; SEV3 for the web and mirror deploys |
| Blast radius | the primary deploy resets the chain by design — a failed one can leave it empty |
| First move | `gh run view <id>` and read *which step* failed, not the badge |

Three workflows deploy: `deploy-testnet-miners.yml` (build + push the image, then
reset and start the stack over SSH), `deploy-explorer.yml` (upload two release
trees, switch two symlinks), and `deploy-edge.yml` (redeploy the mirror after a
successful stack deploy). They fail in different places and the difference matters,
because one of them can leave the network with no chain while the other can only
leave a page stale.

## A green run is not a deploy

Check the steps, not the badge:

```bash
gh run list --workflow=deploy-testnet-miners.yml --limit 5
gh run view <run-id> --log-failed
gh run view <run-id>          # the step list, with skips marked
```

Every deploy workflow is gated on repository configuration: `vars.TESTNET_DEPLOY_HOST`
and `secrets.TESTNET_SSH_KEY` for the stack, `vars.EDGE_DEPLOY_HOST` for the mirror,
and the explorer's own variables. With a gate unset the job is *skipped*, and a run
whose only executed step is "Read the configuration" is green, correct, and has
deployed nothing. That is the failure mode to check first when someone says "the
deploy worked but the site is old", and the run summary now says so in as many
words.

```bash
gh variable list; gh secret list
```

A missing variable is not the same as an empty one: `vars.EDGE_DEPLOY_HOST=""`
still counts as present in some contexts, and a workflow that treats it as
configured will try to SSH to nothing.

## Where the stack deploy fails, in order

**1. Build.** The tests or the lint failed. Nothing was pushed and nothing was
deployed; the running stack is untouched. Fix the merge, not the VM.

**2. Push to GHCR.** `denied: permission_denied` means the job's token lacked
`packages: write`. The workflow asks for it at the top; if the permission block was
edited, this is the symptom.

**3. SSH.** `Permission denied (publickey)` — the key in `secrets.TESTNET_SSH_KEY`
is not in `~/.ssh/authorized_keys` for the deploy user on the VM. This is also what
you see after rebuilding the VM, because instance metadata keys are attached per
instance.

`Host key verification failed` is the opposite problem and the safer one: the VM's
host key changed, which means the instance was recreated (or someone is in the
middle). Do not delete the check; verify the instance first, then update
`secrets.TESTNET_KNOWN_HOSTS` from the console's key or let the workflow learn it
with `ssh-keyscan`.

**4. `docker compose pull` inside the deploy.** `unauthorized` here is GHCR again,
but a different side of it: the *package* must be readable with the token the VM
logs in with. The package is private, and the only credential the workflow has is
its own `GITHUB_TOKEN` with `packages: read`. A registry token minted from a
personal account without `read:packages` fails the same way, which is why this
step runs on the VM and not from the operator's laptop.

**5. `docker compose up`.** A port already in use (`8645`, `8646`) means something
outside Compose owns it — a hand-run `sccp node run` is the usual culprit:

```bash
ssh sccp@proofofchess.com 'sudo ss -ltnp | grep -E ":8645|:8646"'
```

**6. The bootstrap or the smoke test.** The deploy has already run
`docker compose down -v`, so the chain is *empty* and this is the dangerous part
of the page. `init` writes the deterministic stack, `bootstrap` funds and registers
the seats, the node settles, the relay opens a match. If any of those fail the
workflow leaves a stack that is up but has no chain, and the previous chain is
gone — that is the design of a testnet deploy and the reason the deploy is not yet
fit for a network with value.

The failing step's own log is the diagnosis; the two most common are a bootstrap
that could not reach the node and a smoke test that timed out waiting for a game
that the roster cannot finish ([`CHAIN_STALLED.md`](CHAIN_STALLED.md)). The
workflow dumps `docker compose ps -a` and the last 80 lines of every service on
failure — read those before re-running, because a re-run resets the chain again.

## Recovering a half-deployed stack

The fastest correct move is to run the deploy again, because a re-run starts from
the same `down -v` and either completes or fails in the same place. Before that,
confirm what is broken so the second run can succeed:

```bash
ssh sccp@proofofchess.com 'cd /opt/sccp-testnet && docker compose ps -a'
ssh sccp@proofofchess.com 'cd /opt/sccp-testnet && docker compose logs --tail=80 --no-color init bootstrap seed node relay'
curl -fsS https://app.proofofchess.com/v1/status | jq '{height, tip_hash}' || echo "no API"
```

A stack left at `height 0` with `init` exited zero is a bootstrap problem; a stack
at the bootstrap height with a relay that has no matches is a relay problem; a
stack that reaches height 19 and stops is the roster, not the deploy.

## Rolling back

There are three rollbacks in this repository and only two of them are safe.

**The web bundle — always safe.** The last five releases of each origin stay on
disk and the deploy is a symlink rename, so the rollback is the same rename
backwards:

```bash
ssh sccp@proofofchess.com "ls -1t /opt/sccp-explorer/www/app/releases | head -5"
ssh sccp@proofofchess.com "cd /opt/sccp-explorer/www/app \
  && ln -sfn releases/<release> current.next && mv -T current.next current \
  && readlink -f current"
```

Use `landing` in place of `app` for the apex. `mv -T` is required: without it `mv`
moves the new link *inside* the existing directory and reports success while the
site keeps serving the old bundle. Rolling back the bundle does not roll back the
chain, so an old bundle talking to a new API fails in the browser — the fix there
is forward, not back.

**The mirror — safe.** The mirror holds no unique data; a rollback is an image pin
and a restart, and the worst case is a resync:

```bash
ssh sccp@136.119.76.215 'cd /srv/sccp-edge && cat .env'
ssh sccp@136.119.76.215 'cd /srv/sccp-edge \
  && SCCP_IMAGE=ghcr.io/chessproofofwork/somecryptochessproject:<sha> \
     docker compose pull node && docker compose up -d --no-build'
```

`<sha>` is the full commit sha of the build you want; `gh run list` and the
publish step's log both carry it. Do not pin the mirror ahead of the primary: the
replica must run a tag the primary is not, or the two disagree about the chain
rules.


**The stack — only the image, never the chain.** The workflow pushes two tags,
`:testnet` and `:$GITHUB_SHA`, so a specific build can be pinned by its commit
sha. What cannot be rolled back is the chain: the deploy starts from `down -v`, so
"roll back" means "reset the chain and start an older image". On this testnet that
is acceptable and takes one merged revert. On a network with value it is a
data-loss operation, and there is currently no chain-preserving path: the honest
options are to fix forward with a hotfix merge, or to accept the reset and say so
publicly. Closing that gap — pin the image by digest, snapshot before the swap,
stop wiping volumes when the chain has value — is a mainnet prerequisite, not an
operator tip.


If you do need the previous image on the VM, keep it before the deploy pulls the
new one:

```bash
ssh sccp@proofofchess.com 'cd /opt/sccp-testnet \
  && docker tag "$(docker compose images -q node)" sccp-testnet:previous'
```

## The deploy surfaces that are not the stack

| Workflow | Fails like | First check |
| --- | --- | --- |
| `deploy-explorer.yml` | upload or symlink switch failed; the site serves the previous release and the run is red | `readlink -f /opt/sccp-explorer/www/app/current`, then the job's upload step |
| `deploy-edge.yml` | the mirror does not answer, or answers with the old chain | `ssh` to the edge host, `docker compose ps`, then the smoke-test step |
| `deploy-edge.yml` with no `EDGE_DEPLOY_HOST` | green, mirror untouched | `gh variable list` |

The edge deploy is deliberately allowed to fail without failing the primary: a
red mirror job with a green stack job means the chain is fine and the read-only
copy is not. Treat it as SEV4 and finish the primary work first.

## Verifying a deploy that claims to be done

The workflow's own gate is the smoke test, so a green run with a real deploy is
strong evidence. Confirm the two things it does not check: that the chain
*advances*, and that the mirror follows.

```bash
curl -fsS https://app.proofofchess.com/v1/status | jq '{height, tip_hash}'
sleep 65; curl -fsS https://app.proofofchess.com/v1/status | jq '{height, tip_hash}'
curl -fsS https://mirror.proofofchess.com/v1/status | jq '{height, tip_hash}'
```

Two calls, sixty-five seconds apart, is the whole difference between "the deploy
started things" and "the deploy produced a network".

## Prevention

1. Keep the last good image on the host before a deploy, as above. It is one
   `docker tag` and it is the only rollback the stack has.
2. Pin the deploy to a digest, and stop `down -v` when the chain has value. Both
   are code changes, not habits.
3. Treat a skipped job as a failure in review: a green run that deployed nothing
   cost an afternoon precisely once because nobody read the step list.
4. Never re-run a half-failed deploy without reading its failure dump first. The
   second run resets the chain again, so a wrong first theory costs the network
   twice.

