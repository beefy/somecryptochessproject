# The faucet, and every key on the box

| | |
| --- | --- |
| Severity | SEV3 for a dry or limited faucet; SEV1 the moment a key may have been copied |
| Blast radius | the faucet is a convenience; the keys in `stack` are identities and bonds |
| First move | read `GET /v1/faucet` — the policy is published, so most "the faucet is broken" reports are the policy working |

The faucet is not a consensus feature. It is a signed transfer from the network's
published faucet key, with two in-memory limits: one grant per recipient per
cooldown, and a fixed number of grants per client per window. Everything it can do
is visible in one document, so start there instead of in the logs.

```bash
curl -fsS https://app.proofofchess.com/v1/faucet | jq
```

```text
network                testnet
address                the faucet's own address
amount                 1000      coins per default grant
max_amount             10000     the ceiling a request may ask for
cooldown_seconds       3600      per recipient
client_window_seconds  3600      per client
client_grants          5         grants a client gets in the window
```


`404` with `the faucet is disabled` means the node was started with `--no-faucet`
— the mirror is supposed to be in that state, and the primary is not.

## Is it empty, or is it limited?

```bash
FAUCET=$(curl -fsS https://app.proofofchess.com/v1/faucet | jq -r .address)
curl -fsS "https://app.proofofchess.com/v1/accounts/$FAUCET" | jq
curl -sS -X POST https://app.proofofchess.com/v1/faucet \
  -H 'content-type: application/json' -d '{"address":"<recipient>"}' | jq
```

| Response | Meaning | Fix |
| --- | --- | --- |
| a signed grant with a `txid` | it works | — |
| `429` / a cooldown message | the recipient asked recently, or the client used its five | wait; the window is an hour |
| an insufficient-balance error from the node | the faucet is dry | refill, below |
| `404` | disabled on this node | use the primary |

The limits live in memory and a restart forgets them, which is fine for a testnet
and is exactly the abuse path on anything else: the same client can drain the
faucet in bursts by bouncing the node. If the faucet is being drained, that is a
[`SECURITY_INCIDENT.md`](SECURITY_INCIDENT.md) question, not a top-up question.

## Refilling it

The faucet can be topped up like any account: send it coins from something that
has them. First confirm the balance is really the problem:

```bash
ssh sccp@proofofchess.com 'cd /opt/sccp-testnet && docker compose exec -T node \
  sccp --network testnet --data-dir /data chain account "<faucet address>"'
```

Then pay it from a funded keyfile. The container has the same CLI as a laptop:

```bash
sccp --network testnet --node https://app.proofofchess.com \
  --passphrase-file ./passphrase wallet send \
  --key ./keys/miner-1.key --to "<faucet address>" --amount 5000
```

`--node` and `--network` are not decoration: without them the CLI talks to its own
default and never checks the keyfile's network. If `wallet send` reports a nonce
conflict, the seat you spent from is mid-game and mining at the same time — wait a
block and retry rather than raising the fee.

The offline path exists for the case the HTTP endpoint cannot serve: an operator
who holds the published keyfile can pay a recipient directly with `sccp faucet
send --to <address> --amount 1000`. It bypasses the limits by design, so it is an
operator action, logged and deliberate — never the answer you give a user who hit
a cooldown.

## The key inventory

Every key the stack uses is a file in the `stack` volume, wrapped with the
passphrase in `stack/passphrase`:

| File | Identity | If it leaks |
| --- | --- | --- |
| `keys/miner-1..4.key` (plus `.nonce`) | the four mining seats and their bonds | someone else can mine as them, and equivocate |
| `keys/relay*.key`, `keys/witness-*.key` and `.pub` | matchmaking and clock attestation | forged receipts and forged timeout claims |
| the faucet key | derived from the network's published seed, not stored in the volume | the whole faucet balance |
| `passphrase` | unwraps every keyfile above | all of the above, at rest |
| `stack.json`, `synthetic-fleet.json` | addresses and the fleet roster | nothing secret, everything confusing if edited |

On testnet the passphrase is `sccp-testnet-stack` in `docker-compose.yml` and the
seeds are published in `deploy/init_testnet_stack.py`. That is the point: the stack
is reproducible, and a leaked testnet key is worth exactly nothing. On a
value-bearing network every one of those properties is a defect — generated keys,
a secret passphrase, no published seeds — and the recovery for a leak is a new key,
not a new passphrase.

## A key may have leaked

Treat "may" as "did" until you can prove otherwise, and move in this order:
contain, rotate, verify. The order matters, because rotating while the attacker
still has access just re-issues credentials to them.

**Contain.** Snapshot the evidence, then cut the paths in. Every command here is
read-only until the firewall line, and the firewall line is the one that matters:

```bash
ssh sccp@proofofchess.com 'last -20; sudo grep -iE "Accepted|Failed password" /var/log/auth.log | tail -30'
gcloud compute firewall-rules list --project=chessproofofwork --filter='name~ssh' \
  --format='table(name,sourceRanges,targetTags.list())'
```

Close every SSH rule that is not needed, then remove keys that should not be
there. A CI key reachable from `0.0.0.0/0` is a door on a public street with a
good lock: it is key-only, so nobody walks in, but the lock is the only thing
between the internet and `sshd`. Check whether such a rule exists, including the
untagged `default-allow-ssh` — a temporary rule has a way of becoming permanent,
and it can outlive the reason it was created.


**Rotate, in the order the blast radius grows.**

1. **SSH** — install the new public key and remove the old one in the same pass:

   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/sccp_deploy -C "ci-deploy-$(date +%F)"
   gcloud compute instances add-metadata "$VM" --zone="$ZONE" \
     --metadata-from-file ssh-keys=keys.txt          # new key only
   gh secret set TESTNET_SSH_KEY < ~/.ssh/sccp_deploy
   ssh -o BatchMode=yes -i ~/.ssh/<old-key> sccp@proofofchess.com true   # must fail
   ```

   Adding the new key without removing the old entry rotates nothing; the last
   line is the only proof that it worked.

2. **GHCR and workflow credentials** — the deploy authenticates with the job's own
   `GITHUB_TOKEN`, so there is no long-lived registry password in the repository to
   rotate. A personal access token someone used by hand is the exception: revoke
   it, and prefer `gh auth refresh` over storing one.

3. **The stack keys** — regenerate, re-fund and re-register the seats. There is no
   in-place rotation: the bond belongs to the old key, so plan the move as a new
   registration followed by a bond release, in that order.

4. **The faucet key** — cannot be rotated without changing the network's genesis,
   by design. On a network with value, that is an argument for not having a
   deterministic faucet key at all.

**Verify.** A rotation that was never tested is a second incident waiting: run the
old credential once and watch it fail, then run the workflow and watch it pass.

## Prevention

1. Keep SSH rules scoped: `sccp-edge-ssh-from-primary`, IAP for humans, and one
   GitHub rule as narrow as the runner's egress allows. Audit the list monthly —
   it is one command, and it must not be filtered to `name~sccp`, because
   `default-allow-ssh` is untagged and matches nothing with that name.

2. Watch the faucet balance and the grant rate. A drop with no new recipient
   addresses is a script, not users.
3. Never let a key exist in two places. The moment `stack` is copied to a second
   host, both copies are compromised, because neither can prove the other is idle.
4. Treat the passphrase as public on testnet and as a real secret everywhere else.
   `docker compose config` prints it, so never paste that output into an issue.

