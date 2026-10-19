"""Fixtures for Checkpoint Retainer direct-mode tests.

Direct mode does not track native value flow, so two accounting hooks are
installed (mirroring what the chain does) so the tests can genuinely assert
fund movement:

1. payable value: `open_vault` moves `vm.value` sender -> contract
2. `emit_transfer` is intercepted as a PostMessage gl_call and moved
   contract -> recipient

A refused payable call restores the value, so "a refusal mutates nothing" is
testable for exactly the calls that matter.

Time is driven explicitly with `vm.warp(iso)` so checkpoint due dates are tested
deterministically.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

CONTRACT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "checkpoint_retainer.py",
)

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
EPOCH0 = int(BASE.timestamp())

ESCROW = 10 ** 18
HALF = ESCROW // 2
QUARTER = ESCROW // 4

# Absolute epochs: the contract compares due_at against the block timestamp.
DUE1 = EPOCH0 + 100
DUE2 = EPOCH0 + 200
GRACE = 50

PAYER = "0x" + "aa" * 20
RECIPIENT = "0x" + "bb" * 20
STRANGER = "0x" + "44" * 20

URL_A = "https://alpha.example.com/log"
URL_B = "https://bravo.example.com/notes"

# Both sources serve the same text: two venues, one document. What the contract
# cross-checks is that *both* venues carry it.
BODY_DONE = "Delivery log\nV1 shipped.\nV2 shipped."
BODY_PLAN = "Delivery log\nV1 planned.\nV2 planned."
BODY_EMPTY = ""
BODY_ERROR = "Internal Server Error"

SPAN_V1 = "V1 shipped."
SPAN_V2 = "V2 shipped."


def iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def balance(vm, addr):
    return vm._balances.get(vm._to_bytes(addr), 0)


def contract_balance(vm):
    return vm._balances.get(vm._contract_address, 0)


def answer(span):
    return json.dumps({"span": span})


def remock(vm, body_a=BODY_DONE, body_b=BODY_DONE, span_v1=SPAN_V1, span_v2=SPAN_V2,
           status_a=200, status_b=200):
    """Mocks are first-match-wins, so this always clears before installing.

    The model is keyed on the marker only. Whether the answer is *usable* is
    decided by the code against the fetched body, which is exactly the property
    the tests are here to pin down.
    """
    vm.clear_mocks()
    vm.mock_web(".*alpha\\.example\\.com.*", {"method": "GET", "status": status_a, "body": body_a})
    vm.mock_web(".*bravo\\.example\\.com.*", {"method": "GET", "status": status_b, "body": body_b})
    vm.mock_llm(r"MARKER: V1", answer(span_v1))
    vm.mock_llm(r"MARKER: V2", answer(span_v2))


@pytest.fixture
def retainer(direct_vm, direct_deploy):
    vm = direct_vm

    def _value_transfer_hook(vm, request):
        if "PostMessage" not in request:
            return None
        msg = request["PostMessage"]
        amount = int(msg.get("value", 0))
        if amount > 0:
            contract = vm._contract_address
            recipient = vm._to_bytes(msg["address"])
            vm._balances[contract] = vm._balances.get(contract, 0) - amount
            vm._balances[recipient] = vm._balances.get(recipient, 0) + amount
        return {"ok": None}

    vm._gl_call_hook = _value_transfer_hook

    remock(vm)
    c = direct_deploy(CONTRACT)

    def _wrap(fn):
        def inner(*args):
            paid = 0
            sender = vm._to_bytes(vm.sender)
            contract = vm._contract_address
            if vm.value > 0:
                vm._balances[sender] = vm._balances.get(sender, 0) - vm.value
                vm._balances[contract] = vm._balances.get(contract, 0) + vm.value
                paid = vm.value
            try:
                return fn(*args)
            except Exception:
                if paid:
                    vm._balances[sender] = vm._balances.get(sender, 0) + paid
                    vm._balances[contract] = vm._balances.get(contract, 0) - paid
                raise
        return inner

    c.open_vault = _wrap(c.open_vault)

    for addr in (PAYER, RECIPIENT, STRANGER):
        vm.deal(addr, 100 * ESCROW)

    vm.warp(iso(EPOCH0))
    vm.sender = PAYER
    return vm, c


def cps(amount1=HALF, amount2=HALF, due1=DUE1, due2=DUE2, m1="V1", m2="V2"):
    return [
        {"label": "Milestone 1", "amount": str(amount1), "due_at": str(due1), "marker": m1},
        {"label": "Milestone 2", "amount": str(amount2), "due_at": str(due2), "marker": m2},
    ]


def open_vault(vm, c, sources=None, checkpoints=None, grace=GRACE, value=ESCROW,
               sender=PAYER, recipient=RECIPIENT):
    vm.warp(iso(EPOCH0))
    vm.sender = sender
    vm.value = value if value is not None else ESCROW
    try:
        vid = c.open_vault(recipient,
                           [URL_A, URL_B] if sources is None else sources,
                           cps() if checkpoints is None else checkpoints,
                           grace)
    finally:
        vm.value = 0
    return vid


def release(vm, c, vid, index, t=None):
    if t is None:
        t = int(json.loads(c.get_checkpoint(vid, index))["due_at"]) + 1
    vm.warp(iso(t))
    vm.sender = STRANGER
    return c.release(vid, index)


def finalize(vm, c, vid, t=None):
    v = json.loads(c.get_vault(vid))
    if t is None:
        t = int(v["last_due"]) + int(v["grace"]) + 1
    vm.warp(iso(t))
    vm.sender = STRANGER
    return c.finalize(vid)


def vault_state(vm, c, vid):
    return json.loads(c.get_vault(vid))
