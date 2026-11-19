# Incident response

The scenario pages say what is wrong and what to do about it. This page is the
part around them: who decides, in what order, and what has to be written down so
the next incident is cheaper. It is deliberately short enough to read while
something is on fire.

## Declare, then work

An incident starts when someone says "this is an incident" out loud, in the
channel, with a severity from [`README.md`](README.md#severity). Until then it is
a person looking at a graph, and nobody knows whether a change is safe.

The declaration is three lines:

```text
INCIDENT — SEV2 — primary API is down, chain height unknown
Declared 14:05 UTC by <name>
One-sentence guess: node container is restarting after the 13:58 deploy
```

The guess can be wrong. What matters is that it is written down, because the
first guess and the actual cause are different things more often than not, and
the gap between them is the most useful part of the postmortem.

## Roles

Two people is enough. One person is not, for any SEV1 or SEV2 — the operator
cannot debug and watch the clock at the same time, and nobody remembers what they
did afterwards.

| Role | Does | Does not |
| --- | --- | --- |
| **Operator** | runs the commands, one change at a time, narrating each | answer questions about timing or scope; talk to users |
| **Commander** | sets severity, picks the runbook, decides when to roll back, keeps the timeline, says stop | touch the box while the operator is in it |

For SEV3 and SEV4 the same person does both. Above SEV2, the commander's most
valuable sentence is "what does the user see right now?" — it reframes every
proposal against impact instead of curiosity.

## The order of operations

1. **Stop the bleeding.** Whatever makes the site lie or makes the chain
   unreachable goes first: take the mirror out of DNS, roll the bundle back,
   restart the node. Mitigation is allowed to be crude, and it is allowed to lose
   data that a redeploy can rebuild — but never the `stack` volume.
2. **Preserve evidence before you fix the cause.** Copy the logs, the container
   states, the tip hash and the disk report somewhere that survives the restart:
   `docker compose logs --no-color > /tmp/incident-$(date +%s).log`, `docker
   compose ps -a`, `df -h`, `curl /v1/status`. A restart erases the buffer you
   needed.
3. **Find the cause.** One hypothesis at a time, each with the command that
   distinguishes it. If you cannot name what the output would look like if you
   were wrong, you are guessing.
4. **Fix the cause**, after the mitigation is holding.
5. **Verify with the same check that failed**, then watch one full cycle —
   two blocks, not one; a certificate renewal, not a page load.
6. **Write it down** while it is fresh. The postmortem is part of the incident,
   not a chore after it.

## Decisions that always come up

**Restart or investigate?** If users cannot read the chain, restart first and
investigate on the copy of the logs. If the chain is advancing but something
reads wrong, investigate first — a restart destroys the only witness.

**Roll back or roll forward?** Roll back the *bundle* freely; it holds no state
([`DEPLOY_FAILURES.md`](DEPLOY_FAILURES.md)). Do not roll back the *stack*: the
workflow deploys with `down -v`, so "roll back" on this network means "reset the
chain to the bootstrap", which is a data-loss operation and needs the same
approval as one. Until there is a chain-preserving path, the honest options at a
bad deploy are fix-forward or accept the reset.

**Reset or restore?** A reset is minutes and destroys history. A restore is an
hour and depends on a copy existing — which, today, means the mirror or a manual
backup. On a network anybody uses, history is the product; see
[`DATA_LOSS.md`](DATA_LOSS.md) before reaching for `-v`.

**Restore to an older tip?** Then the network will reorganise away the newer
blocks once the restored node is behind its peers, and anything built on them is
gone. Say it explicitly in the incident log, and tell anyone who mined or spent
in the discarded window.

## Change freezes

During a SEV1 nothing deploys except the fix. Concretely: no merges to `main`
(that triggers the deploy workflow), no manual `docker compose` runs that are not
the runbook's, and no second person "just trying something". A second change
during an incident is how one incident becomes two, and the timeline stops being
readable.

## Communication

Nobody outside the box needs a cause. They need: what is affected, what still
works, and when the next update is.

```text
14:05 UTC — investigating: the public API is not answering. The mirror is still
serving reads as of height 4021. Next update 14:35 or sooner.
14:31 UTC — mitigated: the API is back and the chain is advancing. Reads that
failed between 13:58 and 14:29 may have been lost; we are checking. Next update
after verification.
15:10 UTC — resolved: cause was <one sentence>. Fix is <one sentence>. Postmortem
by <date>.
```

Say what is *not* known. "We do not know yet whether any block was lost" is a
complete status update; a confident wrong one costs trust that the fix does not
buy back.

## The postmortem

Write it for the next operator, in this order, within two days:

1. **Timeline**, in UTC, of observed facts and actions, each with the command or
   the log line that showed it. The gap between "first symptom" and "declared"
   is the number that improves fastest.
2. **What users lost**, precisely: a time window, a height range, a set of
   addresses — or "nothing".
3. **Cause.** The trigger is rarely the cause: the deploy was the trigger, the
   missing health check was the cause.
4. **Why it was not caught**, and what would have caught it. If the answer is a
   check that already exists but did not run, that is a different fix than a
   check that does not exist.
5. **Actions**, each with an owner and a date, each one a small verifiable
   change: a test, an alert, a firewall rule, a line in a runbook. Add the
   runbook line in the same pull request as the fix, while the detail is fresh.

The test of a good postmortem is whether a new operator could handle the same
incident from it without asking anyone. If it names a person instead of a check,
it is a story, not a fix.

## After it is over

- re-run the smoke test that should have caught it, and confirm it now fails on
  the old condition — otherwise you fixed the symptom, not the detection;
- delete the temporary rules you added: an open SSH range, a debug port, a
  disabled check. Write their removal in the incident log so a forgotten one is
  visible;
- if the incident touched a key, do the rotation in
  [`FAUCET_AND_KEYS.md`](FAUCET_AND_KEYS.md), not a partial version of it.

