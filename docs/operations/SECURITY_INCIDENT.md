# A security incident

| | |
| --- | --- |
| Severity | SEV1 — contain first, understand second |
| Blast radius | whatever the credential or port reaches: the host, the keys, the faucet, the image registry |
| First move | close the path, keep the evidence, then read [`FAUCET_AND_KEYS.md`](FAUCET_AND_KEYS.md) |

This page is about the network being used against itself: an exposed port, a
credential in the wrong hands, a host that is not behaving like the one you
configured. It is not about protocol-level attacks — those are the design's
subject, and [`docs/design/THREAT_MODEL.md`](../design/THREAT_MODEL.md) is the
authority on which ones are known and which are mitigated.

## What is actually exposed

Know this list before you go looking, because "is this port supposed to be open"
is the question every investigation starts with.

| Surface | Where | Intended exposure |
| --- | --- | --- |
| `:8645` node API | primary | public, unauthenticated, read and write — writes are signed transactions, so exposure is not authority |
| `:8646` relay | primary | loopback only; the browser reaches it through `/relay` on the app origin |
| `:80`/`:443` | primary | public, Caddy → nginx → `/srv`, plus `/v1` and `/relay` proxies |
| `:22` | primary | `sccp-testnet-ssh`, and IAP |
| `:80`/`:443` | mirror | public; GET only, everything else `405` |
| `:8645` | mirror | loopback only, **no firewall rule**, and there must never be one |
| `:22` | mirror | `sccp-edge-iap-ssh` and `sccp-edge-ssh-from-primary`; any rule open to the internet is temporary and must be deleted and recorded |

```bash
gcloud compute firewall-rules list --project=chessproofofwork --filter='name~sccp' \
  --format='table(name,sourceRanges,allowed[].map().firewall_rule().list(),targetTags.list())'
ssh sccp@proofofchess.com 'sudo ss -ltnp'
ssh sccp@136.119.76.215 'sudo ss -ltnp'
```

Compare those three outputs against the table. Anything listening that is not in
the table, and anything allowed that the table does not justify, is the start of
the investigation — not the end of it.

## Contain

Order matters: the fastest way to lose an incident is to close the hole and
destroy the evidence in the same command.

1. **Preserve the state that a restart erases**: `docker compose ps -a`, the last
   200 log lines per service, `ss -ltnp`, `last`, `auth.log`, `docker images`,
   `docker events --since 24h`. Write them to a file *off* the host.
2. **Close the path**: delete the firewall rule, or narrow its source range;
   `docker compose stop node` if the API itself is the problem; remove the name
   from DNS if the attack is volumetric and you need the traffic to stop before it
   is understood.
3. **Freeze the keys** before you know whether they leaked: stop the miner and
   relay containers so nothing signs with a key you are about to distrust. A
   stopped signer is a mild outage; a signing key in an attacker's hands is a
   consensus problem.

```bash
ssh sccp@proofofchess.com 'cd /opt/sccp-testnet && docker compose stop miner-1 miner-2 miner-3 miner-4 relay'
```

## Identify

**An exposed port.** The pattern is a rule someone added "temporarily" and a
service that grew a listener to match. Both halves need to be found, because
closing the rule without removing the listener leaves the next rule one command
away.

```bash
ssh sccp@proofofchess.com 'sudo ss -ltnp | grep -v 127.0.0.1'
docker ps --format '{{.Names}}\t{{.Ports}}' --filter 'publish=8645'
```

**An unexpected process or container.** Compare against the compose files, which
are the only legitimate source of services on either host:

```bash
ssh sccp@proofofchess.com 'docker ps -a; docker images --format "{{.Repository}}:{{.Tag}} {{.CreatedAt}}" | head'
ssh sccp@proofofchess.com 'systemctl list-units --state=running --no-pager | grep -viE "docker|ssh|systemd|chrony|gcp|google|cron"'
ssh sccp@proofofchess.com 'crontab -l; sudo crontab -l; ls -la /etc/systemd/system | grep -v "\->"'
```

A container whose image is not `ghcr.io/chessproofofwork/somecryptochessproject`
is either a leftover local build or the thing you are looking for. A unit or
crontab that survives a reboot is how a foothold outlives a restart.

**Abuse of the public API.** The node authenticates nothing, so the question is
not "who" but "what is it being asked to do". A full mempool, a faucet draining,
and a hot CPU with no games in progress are three different signals:

```bash
curl -fsS https://app.proofofchess.com/v1/status | jq '{mempool: (.mempool | length), peers}'
curl -fsS https://app.proofofchess.com/v1/faucet | jq '{address, amount, client_grants}'
ssh sccp@proofofchess.com 'top -bn1 | head -15'
```

`peers` climbing without any deploy is worth a second look: something is polling
you, and on a `:8645` that answers any GET, that is free for the caller and not
for the host.

**A credential.** Revoked or leaked keys are
[`FAUCET_AND_KEYS.md`](FAUCET_AND_KEYS.md), including the rotation order. The one
addition here: check what the credential reached before rotating, because the
rotation is what hides the answer.

```bash
gcloud logging read 'resource.type="gce_instance" AND protoPayload.methodName:"v1.compute.instances.setMetadata"' \
  --project=chessproofofwork --limit=20 --format='table(timestamp,protoPayload.authenticationInfo.principalEmail)'
```

That audit query is also how you find out *when* a firewall rule or an SSH key
appeared, which is the fact that turns a suspicion into a timeline.

## A volumetric attack

The surfaces are small and the host is two vCPUs, so a flood does not need to be
clever. In order, cheapest first:

1. **Narrow the firewall** to the readers you know. A mirror and an app origin do
   not need the whole internet for the API; the browser needs `/v1` through Caddy,
   not `:8645` directly.
2. **Turn the faucet off** on a node under load: restart it with `--no-faucet`
   (the flag lives in the compose command, so this is a compose edit and a
   restart). The faucet is the one endpoint that does work on request — the rest
   are reads of state that already exists.
3. **Put a proxy in front** only if you can keep the client IP meaningful. The
   faucet's per-client limits key on the client address; a proxy that hides it
   turns every user into one client, which is worse than the flood. This is a
   design change, not an incident action.
4. **Scale the instance** last. It does not stop an attack; it buys time to
   decide, and the disk and the CPU are shared with the chain.

## What "contained" means here

The incident is contained when the path is closed *and* you can say what the
attacker did with it. Both halves:

```bash
gcloud compute firewall-rules list --project=chessproofofwork --filter='name~sccp' \
  --format='table(name,sourceRanges,allowed[].map().firewall_rule().list())'
curl -fsS https://app.proofofchess.com/v1/status | jq '{height, tip_hash}'
ssh sccp@proofofchess.com 'cd /opt/sccp-testnet && docker compose ps -a'
```

If the keys signed anything while exposed, compare the chain: an extra block you
did not mine, a match you did not play, or a transfer from a seat you control is
the evidence that the credential was used rather than merely exposed. Record the
height you checked at; it is the boundary of the investigation.

## Recovery

1. Rebuild the host rather than cleaning it, unless you can name every foothold.
   The chain is re-syncable from a peer and the stack is re-derivable on testnet
   ([`DATA_LOSS.md`](DATA_LOSS.md)); a cleaned host is a host you have to trust
   again for no proven reason.
2. Rotate every credential that lived there — keys, SSH, tokens — in the order in
   [`FAUCET_AND_KEYS.md`](FAUCET_AND_KEYS.md).
3. Restore the surfaces from the table at the top of this page, and nothing more.
   The temptation to keep the debugging rule is how this page gets used twice.
4. Confirm the certificates still issue, the mirror still syncs, and the chain
   still advances — the three checks that any restored host must pass.

## Prevention

1. **Audit the rule list monthly.** It is one `gcloud` command and it is the
   difference between a temporary rule and a permanent one. Every rule open to
   `0.0.0.0/0` should have a comment saying why, and there should be at most two.
2. **Keep `:8645` on the mirror unpublished.** The mirror's whole design is "one
   HTTP name, GET only"; a rule for its API undoes it.
3. **No long-lived credentials.** The deploy uses the job's `GITHUB_TOKEN`; humans
   use IAP. Every personal token that exists is a key that cannot be rotated
   quickly, because nobody knows where it is used.
4. **Turn on GCP audit logs you would want at 2 a.m.**: firewall changes, metadata
   changes, and instance stop/start. Without them the timeline starts when you
   noticed, not when it happened.
5. **Say the testnet's security posture out loud.** Coins have no value, keys are
   published, and the passphrase is in the compose file. That is safe only as long
   as nobody points the faucet at something real — and it is the reason this whole
   folder exists before mainnet rather than after it.

