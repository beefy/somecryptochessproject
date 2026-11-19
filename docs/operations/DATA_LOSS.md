# Losing data, or the disk, or a key

| | |
| --- | --- |
| Severity | SEV1 — history is the product; a key is an identity |
| Blast radius | `chain.sqlite` is the chain; the `stack` volume is every key on the network |
| First move | `df -h /` — a full disk impersonates every other failure on this page |

Two things live on the primary and only one of them can be rebuilt:

| Volume | Holds | If it is gone |
| --- | --- | --- |
| `sccp-testnet_node-data` | `/data/chain.sqlite`, the canonical block store and the adopted tip | the chain can be re-synced from a peer, or re-bootstrapped |
| `sccp-testnet_stack` | miner, relay, witness and faucet keys, the wrapping passphrase, the fleet file | every identity on the network is gone; balances and bonds at those addresses are frozen |

On this testnet both are reconstructible, and that is a property of the testnet,
not of the software: the keys are derived deterministically from published seeds
and a published passphrase by `deploy/init_testnet_stack.py`, which is exactly why
the deploy workflow can start from `down -v` every time. On a network with value
that property is the vulnerability — a rebuilt `stack` must be a *new* identity,
not a re-derived one — and the only honest recovery from losing it is a new
network. Decide which network you are on before you read the rest of this page.

## Start with the disk

```bash
ssh sccp@proofofchess.com 'df -h /; df -i /; docker system df'
```

| Symptom | Cause | Fix |
| --- | --- | --- |
| `/` at 100%, node restarting | Docker layers, three log files per service, the chain database | `docker image prune -af` (never the running tag), then check whether the DB is intact |
| `/` has space, `df -i` at 100% | too many deleted-but-open files, usually logs | restart the service holding them; `lsof +L1 \| head` shows which |
| `SQLITE_FULL` / `database or disk is full` in the node log | the write hit a full filesystem, possibly mid-transaction | free space, restart, then `pragma integrity_check` below |

Docker's own housekeeping is bounded already (`max-size: 10m`, `max-file: 3`), so
growth is usually image layers from repeated deploys, not logs. Do not reclaim
space by deleting `chain.sqlite`, and do not run `docker system prune --volumes`
— that is `-v` with a friendlier name.

## Deciding which failure this is

The node's own error, at boot or on the first write, names the case. Get it
verbatim:

```bash
ssh sccp@proofofchess.com 'cd /opt/sccp-testnet && docker compose logs --tail=40 --no-color node'
```

| Message | Case |
| --- | --- |
| `database /data/chain.sqlite holds testnet, not mainnet` | the data dir belongs to another network; the store refuses to open it rather than mix ledgers |
| `database disk image is malformed` / `file is not a database` | corruption: power loss mid-write, a full disk, or a copy taken while the DB was open |
| `no such table` / a schema-version error | the file is from an incompatible build; a downgrade, or a partially-restored copy |
| `unable to open database file` | permissions or a missing directory — the container runs as an unprivileged user, so a file copied in as root and not `chown`ed is unreadable |
| boot OK, height 0 | the volume is empty: the chain is not corrupt, it is absent |

The wrong-network and schema cases are not corruption and must not be "repaired":
the file is intact and it is the wrong file. Move it aside, do not delete it, and
stop before restoring the right one — a mixed ledger is worse than a day of
downtime.

## Is the store actually corrupt?

`sccp chain verify` replays every stored block through full validation and is the
authority; SQLite's own check tells you whether the *file* is sound, which is a
different question.

```bash
ssh sccp@proofofchess.com 'cd /opt/sccp-testnet && docker compose exec -T node \
  python -c "import sqlite3; print(sqlite3.connect(\"/data/chain.sqlite\").execute(\"pragma integrity_check\").fetchone()[0])"'
ssh sccp@proofofchess.com 'cd /opt/sccp-testnet && docker compose exec -T node \
  sccp --network testnet --data-dir /data chain verify'
```

`integrity_check` = `ok` and `chain verify` exiting zero means the data is fine and
the failure is somewhere else — go back to [`CHAIN_STALLED.md`](CHAIN_STALLED.md) or
[`PRIMARY_DOWN.md`](PRIMARY_DOWN.md). A non-zero exit names the height where
validation stops: everything up to it is good, and that height is your restore
target if you have a copy.

Run both checks against the **copy**, not the live file, once the node is serving
traffic: SQLite readers do not corrupt anything, but the checks take a lock on a
file that the miner is writing to.

## Taking a backup that is actually restorable

Today there is none, and that is the single largest gap on this page. The chain is
rebuildable; `stack` is not, on any network where the keys were generated rather
than derived. Start with the volume that cannot be rebuilt:

```bash
ssh sccp@proofofchess.com 'cd /opt/sccp-testnet \
  && docker compose stop node relay miner-1 miner-2 miner-3 miner-4 drip traffic \
  && docker run --rm -v sccp-testnet_stack:/s -v "$PWD":/backup alpine \
       tar czf /backup/stack-$(date -u +%Y%m%dT%H%M%SZ).tar.gz -C /s . \
  && docker compose up -d'
```

Quiescing first matters for both volumes. A `tar` of `chain.sqlite` while the node
is writing produces a file that fails `integrity_check` in a way that looks like
corruption — the copy is torn, the chain is fine. If you cannot stop the node, ask
SQLite for a consistent copy instead of copying the file; the backup API
checkpoints the log for you:

```bash
ssh sccp@proofofchess.com 'cd /opt/sccp-testnet \
  && docker compose exec -T node python -c \
     "import sqlite3; s=sqlite3.connect(\"/data/chain.sqlite\"); d=sqlite3.connect(\"/tmp/chain-backup.sqlite\"); s.backup(d); d.close()" \
  && docker compose cp node:/tmp/chain-backup.sqlite ./chain-backup.sqlite'
```

Copy all three files (`chain.sqlite`, `-wal`, `-shm`) if you copy by hand, or none.
Then move the archive off the VM — a backup on the same boot disk is a backup of
the failure. A GCS bucket in the same project is the minimum; a snapshot schedule
on the instance's disk is the cheap version of the same thing, with the caveat
that a snapshot is crash-consistent, so treat it as a last resort for `chain.sqlite`
and a fine one for `stack`.

## Restoring what you have

```bash
ssh sccp@proofofchess.com 'cd /opt/sccp-testnet && docker compose stop node'
scp chain-backup.sqlite sccp@proofofchess.com:/opt/sccp-testnet/
ssh sccp@proofofchess.com 'cd /opt/sccp-testnet \
  && docker compose cp chain-backup.sqlite node:/data/chain.sqlite \
  && docker compose exec -T -u root node chown sccp:sccp /data/chain.sqlite \
  && docker compose up -d node'
```

The `chown` is not optional: the image runs as `sccp`, and a file that arrives
root-owned opens as `unable to open database file`, which reads like corruption of
a perfectly good restore. Then verify before letting the rest of the stack write
to it:

```bash
ssh sccp@proofofchess.com 'cd /opt/sccp-testnet && docker compose exec -T node \
  sccp --network testnet --data-dir /data chain info'
ssh sccp@proofofchess.com 'cd /opt/sccp-testnet && docker compose exec -T node \
  sccp --network testnet --data-dir /data chain verify'
curl -fsS https://app.proofofchess.com/v1/status | jq '{height, tip_hash}'
```

Cross-check the genesis hash against the mirror and against the value in
`docs/design/PROTOCOL.md`'s network table. A restore onto the wrong network is
refused by the store, which is the good outcome; a restore onto the right network
at an older height is a reorg waiting to reason about — be explicit in the
incident log about the height you restored to and why.

## Rebuilding when there is no copy

The chain does not have to come from a backup: it comes from a peer. Every node
verifies the genesis hash before adopting a block, so a peer cannot fork you onto
another chain, and the mirror holds a complete copy precisely so that it can serve
when the primary is gone — including this read.

```bash
ssh sccp@proofofchess.com 'cd /opt/sccp-testnet \
  && docker compose down \
  && docker volume rm sccp-testnet_node-data \
  && docker run --rm -v sccp-testnet_node-data:/data \
       ghcr.io/chessproofofwork/somecryptochessproject:testnet \
       --data-dir /data node run --network testnet --no-faucet \
       --peer https://mirror.proofofchess.com --peer-poll-seconds 5 \
       --host 127.0.0.1 --port 18645'
```

Let it reach the mirror's height (watch `curl 127.0.0.1:18645/v1/status` from a
second session, or the container's log), then `Ctrl-C` and `docker compose up -d`.
The empty volume is fine here: it *is* the loss you are recovering from, and the
blocks are exactly what the peer supplies. This is also the supported way to move a
node's chain to a new instance without a backup.

`docker volume rm` in that block is safe because the volume is already gone or
corrupt. Do not reach for it as a shortcut when the volume is healthy but the stack
is misbehaving.

## Losing `stack`

If the volume is gone, the keys in it are gone with it. On this testnet:

```bash
ssh sccp@proofofchess.com 'cd /opt/sccp-testnet && docker compose up -d init bootstrap seed'
```

`init` re-derives the roster from the published seeds and passphrase, `bootstrap`
rebuilds the funding and registrations, and the chain it writes is the one the
workflow expects. Any coins at the old addresses are unreachable and any bond the
old seats posted is stranded; say so plainly in the incident log rather than
describing it as "restored".

On a value-bearing network, stop here:

1. do not re-derive, restore or copy keys from anywhere — a key that arrives from
   an unverified source is a key you cannot trust;
2. treat every address in the old `stack` as lost, and as compromised if you cannot
   prove otherwise;
3. freeze the network (halt mining, or announce a halt), because the alternative is
   an attacker holding seats nobody is watching;
4. rotate everything that touched the host (SSH, GHCR token, CI key) per
   [`FAUCET_AND_KEYS.md`](FAUCET_AND_KEYS.md);
5. plan the new network with the operators who lost value, not around them.

The one thing that must not happen is a partial restore: a rebuilt `stack` with
some old keys and some new ones silently changes who can mine, and nobody can tell
that from the outside.

## Prevention

1. Back up `stack` on a schedule, off the host. It is small, static, and the only
   unrecoverable thing on the VM.
2. Keep the mirror. It is not a backup — it lags by a poll and it is read-only —
   but it is the peer in the recovery above, and a second failure domain for the
   exact page you are reading.
3. Alert on `df -h` above 80%. Every corruption story on a single-disk VM starts
   with a full disk.
4. Never copy a live SQLite file. Stop the node, or use the backup API.


