# Primary down

| | |
| --- | --- |
| Severity | SEV2 — no blocks, no settlement, no faucet; reads survive on the mirror |
| Blast radius | `app.proofofchess.com` and `:8645`, every write, the web origins on the same VM |
| First move | is the VM up, is SSH up, is Docker up, is the container restarting — in that order |

This page is for "nothing answers". If something answers and the numbers are
wrong, it is [`CHAIN_STALLED.md`](CHAIN_STALLED.md); if the API answers but the
container state is odd, start here anyway — the ladder below ends at the same
place.

## How to tell

The failure has a shape. Learn the exit codes; they separate a dead listener from
a dead machine from a dead network path, and that is most of the diagnosis.

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://app.proofofchess.com/v1/status; echo "exit=$?"
```

| Result | Means |
| --- | --- |
| `curl: (7) Failed to connect` | nothing is listening, or a firewall dropped the SYN |
| `curl: (28) Operation timed out` | a firewall is *dropping* rather than rejecting, or the host is wedged |
| `curl: (35) SSL connect error` / `tlsv1 alert` | the listener exists, TLS is not ready — [`TLS_AND_DNS.md`](TLS_AND_DNS.md) |
| `curl: (52) Empty reply` | something accepted the connection and closed it: Caddy up, nginx or node gone |
| HTTP `502`/`504` | the proxy is up and its upstream is not |

Then the machine itself:

```bash
gcloud compute instances list --project=chessproofofwork \
  --format='table(name,zone,status,networkInterfaces[0].networkIP)'
export VM=<the instance serving proofofchess.com> ZONE=<its zone>
gcloud compute instances describe "$VM" --zone="$ZONE" \
  --format='value(status,lastStartTimestamp,lastStopTimestamp)'
```

`TERMINATED` or `STOPPED` with a recent `lastStopTimestamp` is the easiest case
and the one worth checking first, because the fix is one command and it is
usually someone else's billing or quota problem.

## The ladder

Work top to bottom; each rung has one command and one meaning.

**1. The instance is stopped.** Nobody can SSH, and nothing is listening.

```bash
gcloud compute instances start "$VM" --zone="$ZONE"
```

The reserved address follows the instance, so DNS stays valid and the deploy
workflow keeps working. Miners, relay and witnesses come back with the stack
because every service is `restart: unless-stopped`; the node replays
`chain.sqlite` before it serves, so expect minutes of `502`-ish emptiness, not
seconds. Watch it rather than guessing:

```bash
ssh sccp@proofofchess.com 'cd /opt/sccp-testnet && docker compose logs -f --no-color node' | head -40
```

**2. The instance is running and SSH fails.** This is either the network path or
the host, and they need different people.

```bash
gcloud compute ssh "$VM" --zone="$ZONE" --troubleshoot
```

- `Connection refused` on 22 → `sshd` is dead or the firewall rule for the tag was
  changed. The tag is `sccp-node`; without it no rule applies, which looks exactly
  like a dead firewall rule and is more common:

  ```bash
  gcloud compute instances describe "$VM" --zone="$ZONE" --format='value(tags.items)'
  gcloud compute firewall-rules list --project=chessproofofwork \
    --filter='name~sccp' --format='table(name,direction,sourceRanges,allowed[].map().firewall_rule().list())'
  ```

- `Connection timed out` on 22 → the host is alive but not answering: memory
  pressure, a stuck kernel task, or the boot disk is full. Get in out of band and
  look before restarting:

  ```bash
  gcloud compute connect-to-serial-port "$VM" --zone="$ZONE"
  ```

  A serial console that shows a kernel OOM message or a full-disk panic is the
  answer, and it changes the fix: a plain reboot fills the disk again
  ([`DATA_LOSS.md`](DATA_LOSS.md)).

**3. SSH works, Docker does not.** The machine is fine; the container runtime is
not.

```bash
ssh sccp@proofofchess.com 'systemctl is-active docker; docker info --format "{{.ServerVersion}}"; docker ps -q | wc -l'
```

`inactive` → `sudo systemctl start docker`, then the stack comes back on its own.
`docker info` hanging is the disk or the daemon's own log; check `df -h /` and
`journalctl -u docker --no-pager | tail -50`.

**4. Docker works, the stack is not up.** Either nothing is running (a manual
`down`, a reboot before Docker was enabled) or something is crash-looping.

```bash
ssh sccp@proofofchess.com 'cd /opt/sccp-testnet && docker compose ps -a'
```

- Everything `Exited` and none restarting → `docker compose up -d` (never `-v`).
- `node` in `Restarting` with a growing restart count → read its log; the exit
  code tells you the class before the message does:

| Exit | Usually |
| --- | --- |
| `137` | SIGKILL: the host's memory killer, or `docker stop` that timed out. `dmesg -T \| grep -i oom` |
| `1` | the node refused to start: bad flag, missing genesis, `StorageError` on `chain.sqlite` |
| `143` | SIGTERM: a stop that was asked for — usually a deploy |

**5. Everything is up and the API still fails.** The last mile: the port, the
proxy, or a node that is serving something else.

```bash
ssh sccp@proofofchess.com 'ss -ltnp | grep -E "8645|8646|8080|8081|:80|:443"'
curl -fsS https://app.proofofchess.com/v1/status
```

A missing `8645` means the node container is not the process you think it is
(`docker compose ps` says `Up`, but the API can still be mid-replay, in which case
the socket is bound later). A missing `:443` is
[`TLS_AND_DNS.md`](TLS_AND_DNS.md). `502` from Caddy with `8080` listening is
nginx failing to read its release tree — [`DEPLOY_FAILURES.md`](DEPLOY_FAILURES.md).

## Restarting without losing anything

```bash
ssh sccp@proofofchess.com 'cd /opt/sccp-testnet \
  && docker compose logs --no-color --tail=200 > /tmp/before-restart.log \
  && docker compose up -d --remove-orphans \
  && docker compose ps'
```

`up -d` is safe: it recreates containers that are gone or whose image changed and
leaves the volumes alone. Then prove it, with the same check the deploy workflow
uses:

```bash
ssh sccp@proofofchess.com 'cd /opt/sccp-testnet && docker compose exec -T node \
  python /app/deploy/check_testnet_stack.py --node http://127.0.0.1:8645 --relay http://127.0.0.1:8646'
curl -fsS https://app.proofofchess.com/v1/status | jq '{height, tip_hash}'
```

The smoke test waits for the bootstrap height and the relay's attestation, so
"it exits zero" is a stronger statement than "the container is Up". It does not
wait for a game to finish, so confirm the chain is *advancing* by calling
`/v1/status` twice a minute apart — a stack can be healthy and the chain frozen,
which is a different page.

## What this page does not fix

- **The disk is full.** Every rung above looks healthy and the node still dies in
  a loop. `df -h /` first, always. Go to [`DATA_LOSS.md`](DATA_LOSS.md).
- **The chain advanced but the data is gone.** A restore or a rebuild:
  [`DATA_LOSS.md`](DATA_LOSS.md).
- **The certificate expired while the host was down.** Restarting Caddy reissues,
  but the rate limit is real: [`TLS_AND_DNS.md`](TLS_AND_DNS.md).

## Prevention, in the order it pays off

1. An external check on `https://app.proofofchess.com/v1/status` that alerts on
   two consecutive failures. Today the first person to notice is a user.
2. A disk and memory alert on the VM. Both failures present as "the API is down"
   and both are visible an hour early in `df -h` and `free -m`.
3. Leave `restart: unless-stopped` and the reserved IP alone. They are why a
   reboot is boring.
4. Do not add a second process to this VM. It already carries the chain, six
   miners, three witnesses, a relay and two web origins on two vCPUs.

