# Open a wallet

A wallet in this project is a single encrypted **keyfile** on your machine. There
is no sign-up and no server: the keyfile holds your Ed25519 keypair, and the
address printed when you create it is your public identity on the network. A
keyfile can instead hold a **hybrid** Ed25519 + ML-DSA-65 keypair, which gets
its own address version and stays spendable even if Ed25519 falls to a quantum
attacker (section 2.1).

There are three commands in this workflow:

| Step | Command | What it does |
| --- | --- | --- |
| 1 | `sccp wallet new --network testnet --out alice.key` | Generates a keypair and writes an encrypted keyfile. Add `--scheme ed25519-mldsa65` for a post-quantum keyfile. |
| 2 | `sccp wallet export-mnemonic --key alice.key` | Prints the 24-word recovery phrase. Do this immediately. |
| 3 | `sccp wallet address --key alice.key` | Prints the address without needing the passphrase. |

The examples below use `alice.key` in the repository root and the `testnet`
network. Creating, backing up and inspecting the wallet are local operations;
only the balance and faucet commands in section 5 talk to a node.

---

## 1. Prepare

Run commands from the repository root:

```bash
cd somecryptochessproject
source .venv/bin/activate       # if not already active
pip install -e ".[dev]"         # first time only
sccp --version
```

You should see the package and protocol version, for example:

```text
sccp 0.16.0 (protocol v1)
```

---

## 2. Create the keyfile

```bash
sccp wallet new --network testnet --out alice.key
```

It prompts for a passphrase twice:

```text
Passphrase:
Repeat for confirmation:
sccpt1qrvk6qhg6jc72c2evv8kgyj8cf3y29hj85d2xj0x
```

The last line is your **address**. The passphrase is not stored anywhere; it is
the only thing that decrypts the keyfile. There is no password reset.

`wallet new` does **not** print the recovery phrase. Back it up in step 3 before
you fund the wallet.

### What was written

`alice.key` is a JSON document with file mode `0600` (owner read/write only):

```text
-rw-------  1 you  staff  ...  alice.key
```

The public fields are `version`, `network`, `address`, `public_key`, `kdf`,
`cipher` and `created_at`; the private seed is present only as an AES-256-GCM
`ciphertext`. A hybrid keyfile is version `2` and adds a `scheme` field, and its
`public_key` is the 1985-byte blob rather than a 32-byte key (section 2.1). The
key is derived from your passphrase with scrypt
(`N=2**15, r=8, p=1`) and a random 16-byte salt. The header is authenticated as
associated data, so editing `address`, `network` or the KDF parameters makes
decryption fail instead of silently producing another key.

Because the header is public, `wallet address` can work without a passphrase.
Everything that *signs* (`wallet send`, `miner register`, `miner run`, ...) needs
the passphrase.

### Choose the right network

The keyfile is bound to one network, and addresses are network-specific:

| Network | Address prefix | Use |
| --- | --- | --- |
| `mainnet` | `sccp1...` | Real value; `min_bond` is 500 coins. |
| `testnet` | `sccpt1...` | Test network; faucet-funded. |
| `devnet` | `sccpt1...` | In-process development network. |

When you pass `--network`, a keyfile for another network is refused:

```text
error: keyfile belongs to network 'testnet', but --network 'devnet' was requested
```

Create a separate keyfile per network. Do not try to reuse one. Always pass
`--network` to chain-reading wallet commands (`balance`, `send`, `history`):
when it is omitted, the CLI default is `testnet`, not the network stored in the
keyfile.

### If a keyfile already exists

```text
error: keyfile already exists: alice.key (pass force=True to overwrite)
```

`--force` replaces the file, destroying the old key. Only use it on a keyfile
you are certain you no longer need.

### Non-interactive passphrases

For scripts and services, avoid typing the passphrase into a prompt:

```bash
# Preferred: a mode-0600 file whose first line is the passphrase
sccp --passphrase-file ./secrets/alice.pass wallet new \
    --network testnet --out alice.key

# Or an environment variable
export SCCP_WALLET_PASSPHRASE='correct horse battery staple'
sccp wallet new --network testnet --out alice.key
```

Order of precedence is `--passphrase-file`, then `SCCP_WALLET_PASSPHRASE`,
then the interactive prompt. `--passphrase-file` must not be readable by group
or others:

```text
error: passphrase file secrets/alice.pass is accessible by group or others; chmod 600 it
```

Never put the passphrase in the keyfile, in shell history, or in a repository.

### 2.1 Post-quantum wallets

Ed25519 signatures fall to Shor's algorithm on a large quantum computer, so a
wallet can instead hold a hybrid **Ed25519 + ML-DSA-65** key (NIST FIPS 204):

```bash
sccp wallet new --network testnet --scheme ed25519-mldsa65 --out alice-pq.key
```

The address is a version-1 bech32 program over the full 32-byte digest, so it
is longer than the Ed25519 form (65 characters instead of 46 on testnet), and a
spend needs *both* signatures, so an attacker has to break ML-DSA-65 as well. The
keyfile is version `2` and stores the 1985-byte hybrid public key; the private
key is still one 32-byte seed, so the 24-word phrase restores both halves:

```bash
sccp wallet export-mnemonic --key alice-pq.key
sccp wallet import-mnemonic --network testnet --out alice-pq.key \
    --scheme ed25519-mldsa65
```

Three differences from an Ed25519 keyfile:

- **Longer passphrase.** A hybrid keyfile needs at least 12 characters, because
  the same passphrase now protects both schemes.
- **Harder KDF.** scrypt defaults to `N=2**17` instead of `2**15`; `--kdf-n`
  overrides it.
- **No miner or witness identity.** Match certificates, clock receipts and the
  relay roster are Ed25519-only, so miner and witness commands refuse a hybrid
  keyfile: `a hybrid keyfile cannot ...: match transcripts, clock receipts and
  the relay roster are Ed25519-only`. Fund and spend with it; register with an
  Ed25519 keyfile.

Hybrid support needs a `cryptography` build whose OpenSSL exposes ML-DSA (50.0
or later on a current OpenSSL). Without it, `wallet new --scheme
ed25519-mldsa65` fails with a clear error instead of writing a half-working key,
and a node rejects hybrid signatures rather than trusting the Ed25519 half
alone.

---

## 3. Back up the recovery phrase

The keyfile is encrypted with your passphrase, so losing either the file or the
passphrase loses the funds. The recovery phrase is the backup that survives a
lost keyfile: it encodes the 32-byte seed as 24 BIP-39 words. For a hybrid
keyfile that one seed controls both halves, so the same phrase restores an
Ed25519 *and* an ML-DSA-65 key.

```bash
sccp wallet export-mnemonic --key alice.key
```

```text
Passphrase:
legal winner thank year wave sausage worth useful legal winner thank yellow
...
```

(The phrase above is truncated; the real one is exactly 24 words.)

Write it down on paper or another offline medium and store it separately from
the keyfile. Anyone who has both the words and the passphrase can spend the
wallet, so treat the phrase as the key itself.

### Prove the backup works

Restore into a second keyfile and compare addresses:

```bash
sccp wallet import-mnemonic --network testnet --out alice-restored.key

# Prompts:
# Mnemonic (24 words):
# Passphrase:
# Repeat for confirmation:

sccp wallet address --key alice-restored.key
sccp wallet address --key alice.key
```

Both commands must print the same `sccpt1...` address. If they differ, the
phrase was transcribed incorrectly. The restored keyfile has a new salt and
nonce, so the two files are not byte-identical, but they control the same key.

`wallet import-mnemonic` also accepts `--force`, `--network` and
`--passphrase-file`, exactly like `wallet new`.

---

## 4. Inspect the wallet

```bash
sccp wallet address --key alice.key
```

For a version suitable for scripts:

```bash
sccp --json wallet address --key alice.key
```

```json
{"address": "sccpt1...", "keyfile": "alice.key", "network": "testnet", "public_key": "...", "scheme": "ed25519"}
```

`wallet new`, `wallet import-mnemonic` and `wallet address` all print only the
address by default; each supports `--json` with the fields above. `scheme` is
`ed25519` or `ed25519-mldsa65`, and a hybrid `public_key` is the 3970-hex
`0x01 || ed25519 || mldsa65` blob. `wallet export-mnemonic --json` returns
`{"mnemonic", "address", "network", "scheme"}`: the phrase is only the 32-byte
seed, so the `scheme` field is what tells a restore to re-expand it into two
keypairs.

---

## 5. Check the balance and fund the wallet

Balances live on the chain, not in the keyfile. This reads the account from a node
(the default is `http://127.0.0.1:8645`), so a running node is enough:

```bash
sccp wallet balance --network testnet --key alice.key \
    --node http://127.0.0.1:8645
```

```text
sccpt1... 0.00000000 (spendable 0.00000000, bonded 0.00000000, immature 0.00000000)
```

- **spendable** — coins a transfer can use now.
- **bonded** — coins locked as a miner's registration bond.
- **immature** — mining rewards still inside `coinbase_maturity`.

On `testnet` and `devnet` the genesis faucet can fund a fresh wallet:

```bash
sccp faucet send --network testnet --to sccpt1... --amount 1000
```

The faucet needs a running node: like `wallet send`, it reads the sender's balance
and next nonce from `--node` (see [`SEND_COINS.md`](SEND_COINS.md)).

---

## 6. Common errors

| Message | Cause | Fix |
| --- | --- | --- |
| `keyfile already exists: ...` | The `--out` path is taken. | Pick another path, or pass `--force` if the old key is disposable. |
| `cannot decrypt keyfile for ...: wrong passphrase or corrupted ciphertext` | Wrong passphrase, or the file was edited. | Re-enter the passphrase; restore from the recovery phrase if it is lost. |
| `keyfile belongs to network '...', but --network '...' was requested` | The keyfile was created for a different network. | Use the keyfile's network, or create a new keyfile for the requested one. |
| `passphrase file ... is accessible by group or others; chmod 600 it` | The passphrase file is too permissive. | `chmod 600 <file>` (or `chmod 400`). |
| `passphrase file ... is empty` | The file has no first line. | Write the passphrase and a newline. |
| `expected 24 words, got N` | The imported phrase is incomplete. | Re-enter all 24 words in order. |
| `unknown network '...'; known networks: ...` | A command named a network this build does not know. | Use `mainnet`, `testnet` or `devnet`. |
| `unknown scheme '...'; use one of: ed25519, ed25519-mldsa65` | `--scheme` named a key scheme this build does not know. | Use `ed25519` or `ed25519-mldsa65`. |
| `a hybrid keyfile passphrase must be at least 12 characters` | A post-quantum keyfile was given a short passphrase. | Use 12+ characters; the same phrase protects two schemes. |
| `a hybrid keyfile cannot ...: match transcripts, clock receipts and the relay roster are Ed25519-only` | A miner or witness command was pointed at a hybrid keyfile. | Register and play with an Ed25519 keyfile; spend with the hybrid one. |
| `keyfile version 2 requires scheme 'ed25519-mldsa65', not 'ed25519'` | The header was hand-edited, or `--scheme` disagrees with the file. | Do not edit the header; recreate the keyfile. |
| `keyfile names unknown network '...' (known: ...)` | The keyfile header names a network this build does not know. | Use a current build; do not edit the header. |
| `no chain database at ...` | A chain command ran without `--data-dir` pointing at a node's data. | Start a node or pass the directory that holds `chain.sqlite`. |

Exit codes: `0` success, `1` operation refused or failed, `2` invalid usage or
unknown network.

---

## 7. Safety checklist

- [ ] The keyfile is mode `0600` and not in a git repository.
- [ ] The 24-word phrase is written down offline, in order, and stored apart
      from the keyfile.
- [ ] The passphrase is in a password manager (or a mode-0600 file for
      automation), never in the keyfile.
- [ ] You have restored the phrase once and seen the same address.
- [ ] You are using the keyfile for the network you think you are: check the
      address prefix (`sccp1` mainnet, `sccpt1` testnet/devnet).
- [ ] For a hybrid keyfile: the passphrase is at least 12 characters, the
      address is a version-1 program (65 characters on testnet), and the file
      says `"version": 2` with `"scheme": "ed25519-mldsa65"`.

---

## See also

- [`CREATE_A_TESTNET_WALLET.md`](CREATE_A_TESTNET_WALLET.md) — the short
  keyfile-plus-faucet walkthrough.
- [`SEND_COINS.md`](SEND_COINS.md) — fund the wallet and transfer coins.
- [`RUN_A_TESTNET_MINER.md`](RUN_A_TESTNET_MINER.md) — register the wallet as a
  miner.
- [`PLAY_AND_EXPORT.md`](PLAY_AND_EXPORT.md) — play a game on-chain.
- [`PROTOCOL.md`](PROTOCOL.md) — normative address and transaction formats; the
  keyfile layout itself is documented in this guide.
