# Checkpoint Retainer

**A retainer that pays per checkpoint, against two independent sources, and
refunds whatever was never earned.**

A payer escrows a total and splits it into up to six **checkpoints**, each with
its own amount, due date and **marker**. Each checkpoint is judged on its due
date against **two sources the payer fixed at creation**. It pays only when both
sources report it complete. A checkpoint that misses never blocks a later one,
and at `finalize` everything unearned goes back to the payer.

**There is no evidence-filing entry anywhere in this contract.** Nothing is
filed, nobody chooses what gets judged, and no third party can touch the judged
set. It is two URLs, written once, at funding.

---

## Why that matters

Two failure modes keep getting contracts sent back:

| failure | how this contract is immune |
|---|---|
| **judged set can be narrowed or occupied** — a capped write slot that anyone can fill | there is **no write slot at all**. The judged set is `sources`, fixed by `open_vault` and immutable |
| **the interested party frames the question** | the payer chooses the sources and markers, but the payer is the one **paying**: its choice can only withhold its own escrow, never extract from anyone, and the two URLs are public from the first block |

The one thing a payer can do is name two dead URLs and pay nobody — while getting
its own escrow back. That is visible at creation, before anyone works, and it is
strictly weaker than simply not opening the vault. The recipient can read
`get_vault` and decline.

---

## Skip-forward, not dependency-gated

A dependency graph says: if node 1 fails, nothing below it can ever pay. That is
right when the parts are genuinely ordered (you cannot ship the roof before the
walls), and wrong for a retainer, where February's work is not invalidated by a
missed January.

Here each checkpoint is judged **on its own evidence, at its own date**. A miss
leaves that tranche in the vault and the schedule moves on. `finalize` is what
turns "never proven" into "refunded", and it is the only place that happens.

```
AUTHOR: transfer moves inside release; the only refunds are finalize and cancel.
```

---

## What the model does

For each source, consensus answers one narrow question: *quote the sentence that
contains MARKER and reports the checkpoint as done.* Code then requires, per
source:

```
span is non-empty
  ∧ span contains the marker (squashed)
  ∧ span really occurs in the body the contract fetched (squashed)
```

and requires **both** sources to pass. So the model can only lose credit, never
create it: a fluent sentence without the marker fails, a quote that is not in the
fetched text fails, and an empty answer fails — `""` is a substring of
everything, so emptiness is treated as failure explicitly.

Fetching and asking share one equivalence block, so there is a single consensus
point per checkpoint and the verdict is computed in code inside it. Validators
agree on `retrieved` and `ok`; the span is kept as an audit trail and may differ
in wording.

---

## Lifecycle

| vault | meaning |
|---|---|
| `OPEN` | checkpoints are being judged |
| `CLOSED` | `finalize` ran: pending checkpoints are `MISSED`, remainder refunded |
| `REFUNDED` | the payer took everything back before the first due date |

| checkpoint | meaning |
|---|---|
| `PENDING` | due date not reached, or not yet proven. **Retryable** — evidence often arrives late |
| `PAID` | both sources reported it complete; the tranche moved |
| `MISSED` | `finalize` ran and it was never proven; the tranche went home |

A miss is not a verdict: `release` on unproven evidence returns `NOT_YET` and
leaves the checkpoint `PENDING`, so a late release note still pays. Only
`finalize`, after the last due date plus the grace period, closes it.

---

## API

**Writes**

| method | note |
|---|---|
| `open_vault(recipient, sources, checkpoints, grace)` | **payable**. `sources` is exactly two `http(s)` URLs on **different hosts**; `checkpoints` is 1..6 `{label, amount, due_at, marker}` with strictly increasing `due_at` whose amounts sum to the escrow |
| `release(vault_id, index)` | permissionless. Only after `due_at`. Pays `amount` if both sources report it done, else returns `NOT_YET` |
| `finalize(vault_id)` | permissionless, after `last_due + grace`. Marks pending checkpoints `MISSED`, refunds the rest to the payer, closes the vault. **No model call** |
| `cancel(vault_id)` | payer only, before the first due date, only while nothing was paid. Refunds the whole escrow |

**Views** — JSON strings only, never a bare `list`/`dict`, because a `float` in a
return value breaks calldata encoding on chain while direct mode stays green.

| method | returns |
|---|---|
| `get_vault(vault_id)` | config, the two sources, totals, and every checkpoint with its own status, span and reason |
| `get_checkpoint(vault_id, index)` | one checkpoint record |
| `get_vault_count()` | for deriving the next id from a count delta |

**Bounds** — 6 checkpoints, amounts sum to the escrow, label ≤ 80, marker ≤ 80,
url ≤ 220, grace ≥ 1.

---

## Testing

**54 direct-mode tests**, in seven groups:

```
A  creation             the escrow is held whole; the schedule is validated
B  release              both sources must report it done; every trick that isn't
C  skip-forward         a miss never blocks the next checkpoint
D  finalize             the remainder goes home, exactly once
E  cancel               the payer's one clean exit
F  invariants           refusals mutate nothing; no unit is stranded
G  views                no float ever leaves the contract
```

The ones worth reading first:

```
test_one_source_reporting_nothing_is_not_enough
test_a_span_without_the_marker_does_not_count
test_a_span_that_is_not_in_the_body_does_not_count
test_an_empty_span_never_counted
test_an_unreachable_source_fails_closed
test_a_late_checkpoint_can_be_retried
test_a_missed_checkpoint_does_not_block_the_next_one
test_every_refusal_leaves_the_vault_byte_identical
test_every_unit_is_accounted_for
```

Direct mode does not track native value, so `conftest.py` installs two accounting
hooks — payable value into the contract, `emit_transfer` out of it — and restores
the value when a payable call is refused, so a refusal really is testable as
"nothing moved".

```
python -m pytest tests/direct -q        # 54 passed
```

---

## Live on chain

Deployed on Bradbury at `0xA3c223197D5c691392ba4500A11aFcEC78664Aa6`.

`open_vault` was exercised against the live contract with 1 GEN and two
checkpoints, judged against two sources on different hosts:

```
open_vault: FINISHED_WITH_RETURN / vote=1
vault 1: total=1000000000000000000  paid=0  status=OPEN
sources: raw.githubusercontent.com/qlxjcj/genlayer-checkpoint-retainer/master/fixtures/delivery-log.txt
         cdn.jsdelivr.net/gh/qlxjcj/genlayer-checkpoint-retainer@master/fixtures/delivery-log.txt

release(vault 1, 0) before the due date
  -> REFUSED: Checkpoint is not due yet
```

The delivery log served at those two URLs says:

```
Checkpoint 1: V1 shipped on 2026-01-05.
Checkpoint 2: V2 planned for March, not shipped yet.
```

so checkpoint 1 is provable and checkpoint 2 is not — precisely the case the
skip-forward rule exists for. Both URLs return the file with both markers, and
they are on different hosts, which `open_vault` enforces.

The settlement run then finished the vault:

```
ck0  PAID     400000000000000000
     span   "Checkpoint 1: V1 shipped on 2026-01-05."
     reason both sources report the checkpoint complete

ck1  MISSED   600000000000000000
     reason deadline passed without proof from both sources

status        CLOSED
total         1000000000000000000
paid_total    400000000000000000
refunded      600000000000000000
paid + refunded == total : true
```

`ck0` is the whole claim of this contract in one line: two independent hosts were
fetched, consensus quoted the sentence that names the checkpoint and reports it
done, code proved that sentence occurs in **both** bodies and carries the marker,
and only then did 0.4 GEN move.

`ck1` is worth describing honestly. Its `release` was submitted, but the Bradbury
RPC dropped the connection during the consensus round and the transaction never
landed (`attempts` is still 0), so **`finalize` closed it by deadline rather than
by a judgement**. That is a fair outcome — the document says `V2 planned for
March, not shipped yet` — but it is the deadline path, not the evidence path.
The `NOT_YET` path itself is covered by `test_a_late_checkpoint_can_be_retried`
and the five fail-closed tests, which do not depend on the network.

What the run does show end to end is the part that matters for the money:
**a checkpoint paid only on two-source corroboration, a checkpoint that was never
proven paid nobody, and every unit ended up in exactly one place.**

## Source integrity

Repository and deployed source are byte identical:

```
sha256  8ee67fb1c248ecb7f75c52f215b4ca12d4067451069b9d986c7a2fe71a05fa5c
bytes   15052
```

Reproduce with
`genlayer code 0xA3c223197D5c691392ba4500A11aFcEC78664Aa6 --rpc https://rpc-bradbury.genlayer.com`
and compare against `checkpoint_retainer.py`.
