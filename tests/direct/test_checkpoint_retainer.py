"""Direct-mode tests for Checkpoint Retainer.

Groups:

  A  creation            -- the escrow is held whole and the schedule is honest
  B  release             -- two sources must both report the checkpoint done
  C  skip-forward        -- a missed checkpoint never blocks a later one
  D  finalize            -- the unearned remainder goes home, exactly once
  E  cancel              -- the payer's one clean exit
  F  invariants          -- refusals mutate nothing; no unit is stranded
  G  views               -- no float ever leaves the contract
"""

import json

import pytest

from conftest import (
    BODY_DONE,
    BODY_EMPTY,
    BODY_ERROR,
    BODY_PLAN,
    DUE1,
    DUE2,
    ESCROW,
    GRACE,
    HALF,
    PAYER,
    QUARTER,
    RECIPIENT,
    SPAN_V1,
    SPAN_V2,
    STRANGER,
    URL_A,
    URL_B,
    balance,
    contract_balance,
    cps,
    finalize,
    iso,
    open_vault,
    release,
    remock,
    vault_state,
)


# ==========================================================================
# A. creation
# ==========================================================================


def test_open_vault_escrows_the_whole_total(retainer):
    vm, c = retainer
    before = balance(vm, PAYER)
    held = contract_balance(vm)
    vid = open_vault(vm, c)
    assert balance(vm, PAYER) == before - ESCROW
    assert contract_balance(vm) == held + ESCROW
    v = vault_state(vm, c, vid)
    assert v["status"] == "OPEN"
    assert v["total"] == str(ESCROW)
    assert v["paid_total"] == "0"
    assert v["refunded"] == "0"
    assert [x["status"] for x in v["checkpoints"]] == ["PENDING", "PENDING"]


def test_open_vault_records_the_schedule_and_the_two_sources(retainer):
    vm, c = retainer
    vid = open_vault(vm, c)
    v = vault_state(vm, c, vid)
    assert v["sources"] == [URL_A, URL_B]
    assert v["grace"] == str(GRACE)
    assert v["first_due"] == str(DUE1)
    assert v["last_due"] == str(DUE2)
    assert [x["marker"] for x in v["checkpoints"]] == ["V1", "V2"]
    assert [x["amount"] for x in v["checkpoints"]] == [str(HALF), str(HALF)]


@pytest.mark.parametrize("bad", [
    "not-an-address",
    "0x1234",
    "",
    None,
])
def test_rejects_an_invalid_recipient(retainer, bad):
    vm, c = retainer
    with pytest.raises(Exception):
        open_vault(vm, c, recipient=bad)
    assert c.get_vault_count() == 0


def test_rejects_recipient_equal_to_payer(retainer):
    vm, c = retainer
    with pytest.raises(Exception):
        open_vault(vm, c, recipient=PAYER)
    assert c.get_vault_count() == 0


@pytest.mark.parametrize("sources", [
    [URL_A],
    [URL_A, URL_B, URL_A],
    [],
])
def test_requires_exactly_two_sources(retainer, sources):
    vm, c = retainer
    with pytest.raises(Exception):
        open_vault(vm, c, sources=sources)
    assert c.get_vault_count() == 0


def test_rejects_two_sources_on_one_host(retainer):
    """Two pages on one host are one hand, so they cannot corroborate."""
    vm, c = retainer
    with pytest.raises(Exception):
        open_vault(vm, c, sources=[URL_A, "https://alpha.example.com/other"])
    assert c.get_vault_count() == 0


@pytest.mark.parametrize("sources", [
    ["ftp://alpha.example.com/log", URL_B],
    ["alpha.example.com/log", URL_B],
    [123, URL_B],
])
def test_rejects_non_http_sources(retainer, sources):
    vm, c = retainer
    with pytest.raises(Exception):
        open_vault(vm, c, sources=sources)


def test_rejects_a_zero_escrow(retainer):
    vm, c = retainer
    with pytest.raises(Exception):
        open_vault(vm, c, value=0)
    assert c.get_vault_count() == 0


def test_rejects_amounts_that_do_not_sum_to_the_escrow(retainer):
    """Every unit of the escrow must have a named destination."""
    vm, c = retainer
    with pytest.raises(Exception):
        open_vault(vm, c, checkpoints=cps(amount1=QUARTER, amount2=QUARTER))
    assert c.get_vault_count() == 0


@pytest.mark.parametrize("bad", [
    cps(due1=200, due2=100),
    cps(due1=100, due2=100),
    cps(due1=0, due2=100),
])
def test_requires_strictly_increasing_due_dates(retainer, bad):
    vm, c = retainer
    with pytest.raises(Exception):
        open_vault(vm, c, checkpoints=bad)
    assert c.get_vault_count() == 0


@pytest.mark.parametrize("bad", [
    [{"label": "M1", "amount": str(ESCROW), "due_at": "100", "marker": ""}],
    [{"label": "M1", "amount": str(ESCROW), "due_at": "100", "marker": "   "}],
    [{"label": "", "amount": str(ESCROW), "due_at": "100", "marker": "V1"}],
    [{"label": "M1", "amount": "0", "due_at": "100", "marker": "V1"}],
    [{"label": "M1", "amount": str(ESCROW), "due_at": "100", "marker": "x" * 81}],
])
def test_rejects_a_malformed_checkpoint(retainer, bad):
    vm, c = retainer
    with pytest.raises(Exception):
        open_vault(vm, c, checkpoints=bad)
    assert c.get_vault_count() == 0


def test_rejects_more_than_six_checkpoints(retainer):
    vm, c = retainer
    each = ESCROW // 7
    seven = [{"label": "M%d" % i, "amount": str(each), "due_at": str(100 * (i + 1)),
              "marker": "V%d" % i} for i in range(7)]
    with pytest.raises(Exception):
        open_vault(vm, c, checkpoints=seven)


def test_rejects_a_grace_shorter_than_one(retainer):
    vm, c = retainer
    with pytest.raises(Exception):
        open_vault(vm, c, grace=0)
    assert c.get_vault_count() == 0


# ==========================================================================
# B. release -- two sources must both report it done
# ==========================================================================


def test_release_pays_when_both_sources_report_it_done(retainer):
    vm, c = retainer
    vid = open_vault(vm, c)
    before = balance(vm, RECIPIENT)
    held = contract_balance(vm)
    assert release(vm, c, vid, 0) == "PAID"
    assert balance(vm, RECIPIENT) == before + HALF
    assert contract_balance(vm) == held - HALF
    ck = json.loads(c.get_checkpoint(vid, 0))
    assert ck["status"] == "PAID"
    assert ck["span"] == SPAN_V1
    assert ck["attempts"] == "1"
    assert vault_state(vm, c, vid)["paid_total"] == str(HALF)


def test_release_is_permissionless(retainer):
    vm, c = retainer
    vid = open_vault(vm, c)
    vm.warp(iso(DUE1 + 1))
    vm.sender = STRANGER
    assert c.release(vid, 0) == "PAID"


def test_release_before_the_due_date_is_refused(retainer):
    vm, c = retainer
    vid = open_vault(vm, c)
    before = json.dumps(vault_state(vm, c, vid), sort_keys=True)
    vm.warp(iso(DUE1 - 1))
    vm.sender = STRANGER
    with pytest.raises(Exception):
        c.release(vid, 0)
    assert json.dumps(vault_state(vm, c, vid), sort_keys=True) == before


def test_one_source_reporting_nothing_is_not_enough(retainer):
    """The whole point of two sources: one of them alone cannot pay."""
    vm, c = retainer
    remock(vm, body_b=BODY_PLAN, span_v1=SPAN_V1, span_v2=SPAN_V2)
    vid = open_vault(vm, c)
    before = balance(vm, RECIPIENT)
    assert release(vm, c, vid, 0) == "NOT_YET"
    assert balance(vm, RECIPIENT) == before
    ck = json.loads(c.get_checkpoint(vid, 0))
    assert ck["status"] == "PENDING"
    assert ck["attempts"] == "1"
    assert ck["reason"] == "not reported complete by both sources"


def test_a_span_without_the_marker_does_not_count(retainer):
    """A fluent sentence is worthless if it does not name the checkpoint."""
    vm, c = retainer
    remock(vm, span_v1="shipped.")
    vid = open_vault(vm, c)
    assert release(vm, c, vid, 0) == "NOT_YET"


def test_a_span_that_is_not_in_the_body_does_not_count(retainer):
    """The model cannot quote text the contract never fetched."""
    vm, c = retainer
    remock(vm, span_v1="V1 shipped in a document that does not exist.")
    vid = open_vault(vm, c)
    assert release(vm, c, vid, 0) == "NOT_YET"


def test_an_empty_span_never_counted(retainer):
    """The empty-string trap: '' is a substring of everything."""
    vm, c = retainer
    remock(vm, span_v1="")
    vid = open_vault(vm, c)
    assert release(vm, c, vid, 0) == "NOT_YET"


def test_an_unreachable_source_fails_closed(retainer):
    vm, c = retainer
    remock(vm, body_b=BODY_EMPTY)
    vid = open_vault(vm, c)
    assert release(vm, c, vid, 0) == "NOT_YET"
    assert json.loads(c.get_checkpoint(vid, 0))["reason"] == "a source was unreachable"


def test_a_provider_error_fails_closed(retainer):
    vm, c = retainer
    remock(vm, status_b=500, body_b=BODY_ERROR)
    vid = open_vault(vm, c)
    assert release(vm, c, vid, 0) == "NOT_YET"


def test_a_late_checkpoint_can_be_retried(retainer):
    """Evidence often arrives late, so a miss is not a verdict."""
    vm, c = retainer
    remock(vm, body_b=BODY_PLAN)
    vid = open_vault(vm, c)
    assert release(vm, c, vid, 0) == "NOT_YET"
    remock(vm, body_b=BODY_DONE)
    assert release(vm, c, vid, 0, t=DUE1 + 5) == "PAID"
    ck = json.loads(c.get_checkpoint(vid, 0))
    assert ck["status"] == "PAID"
    assert ck["attempts"] == "2"


def test_a_paid_checkpoint_cannot_be_paid_twice(retainer):
    vm, c = retainer
    vid = open_vault(vm, c)
    assert release(vm, c, vid, 0) == "PAID"
    before = balance(vm, RECIPIENT)
    with pytest.raises(Exception):
        release(vm, c, vid, 0, t=DUE1 + 2)
    assert balance(vm, RECIPIENT) == before


def test_an_unknown_checkpoint_index_is_refused(retainer):
    vm, c = retainer
    vid = open_vault(vm, c)
    for bad in (2, 99, -1):
        with pytest.raises(Exception):
            release(vm, c, vid, bad)


def test_an_unknown_vault_is_refused(retainer):
    vm, c = retainer
    with pytest.raises(Exception):
        release(vm, c, "999", 0)


# ==========================================================================
# C. skip-forward -- a missed checkpoint never blocks a later one
# ==========================================================================


def test_a_missed_checkpoint_does_not_block_the_next_one(retainer):
    """This is the structural difference from a dependency-gated escrow: the
    schedule advances, the unearned tranche stays behind, and the next
    checkpoint is judged on its own evidence."""
    vm, c = retainer
    remock(vm, body_a=BODY_PLAN, body_b=BODY_PLAN)
    vid = open_vault(vm, c)
    assert release(vm, c, vid, 0) == "NOT_YET"

    # checkpoint 1 uses a different marker and fresh evidence
    vm.warp(iso(DUE2 + 1))
    remock(vm)
    vm.sender = STRANGER
    assert c.release(vid, 1) == "PAID"

    v = vault_state(vm, c, vid)
    assert [x["status"] for x in v["checkpoints"]] == ["PENDING", "PAID"]
    assert v["paid_total"] == str(HALF)


def test_finalize_turns_the_missed_checkpoint_into_a_refund(retainer):
    vm, c = retainer
    remock(vm, body_a=BODY_PLAN, body_b=BODY_PLAN)
    vid = open_vault(vm, c)
    assert release(vm, c, vid, 0) == "NOT_YET"
    remock(vm)
    assert release(vm, c, vid, 1, t=DUE2 + 1) == "PAID"

    payer_before = balance(vm, PAYER)
    assert finalize(vm, c, vid) == "CLOSED"
    v = vault_state(vm, c, vid)
    assert [x["status"] for x in v["checkpoints"]] == ["MISSED", "PAID"]
    assert v["refunded"] == str(HALF)
    assert balance(vm, PAYER) == payer_before + HALF


# ==========================================================================
# D. finalize
# ==========================================================================


def test_finalize_before_the_grace_ends_is_refused(retainer):
    vm, c = retainer
    vid = open_vault(vm, c)
    before = json.dumps(vault_state(vm, c, vid), sort_keys=True)
    vm.warp(iso(DUE2 + GRACE - 1))
    vm.sender = STRANGER
    with pytest.raises(Exception):
        c.finalize(vid)
    assert json.dumps(vault_state(vm, c, vid), sort_keys=True) == before


def test_finalize_refunds_everything_when_nothing_was_earned(retainer):
    vm, c = retainer
    remock(vm, body_a=BODY_PLAN, body_b=BODY_PLAN)
    vid = open_vault(vm, c)
    payer_before = balance(vm, PAYER)
    recipient_before = balance(vm, RECIPIENT)
    assert finalize(vm, c, vid) == "CLOSED"
    v = vault_state(vm, c, vid)
    assert v["status"] == "CLOSED"
    assert v["refunded"] == str(ESCROW)
    assert v["paid_total"] == "0"
    assert balance(vm, PAYER) == payer_before + ESCROW
    assert balance(vm, RECIPIENT) == recipient_before


def test_finalize_cannot_run_twice(retainer):
    vm, c = retainer
    vid = open_vault(vm, c)
    finalize(vm, c, vid)
    payer_before = balance(vm, PAYER)
    with pytest.raises(Exception):
        finalize(vm, c, vid)
    assert balance(vm, PAYER) == payer_before


def test_release_after_finalize_is_refused(retainer):
    vm, c = retainer
    vid = open_vault(vm, c)
    finalize(vm, c, vid)
    with pytest.raises(Exception):
        release(vm, c, vid, 0, t=DUE1 + 1)


# ==========================================================================
# E. cancel -- the payer's one clean exit
# ==========================================================================


def test_cancel_refunds_the_payer_whole(retainer):
    vm, c = retainer
    vid = open_vault(vm, c)
    before = balance(vm, PAYER)
    held = contract_balance(vm)
    vm.warp(iso(DUE1 - 1))
    vm.sender = PAYER
    assert c.cancel(vid) == "REFUNDED"
    v = vault_state(vm, c, vid)
    assert v["status"] == "REFUNDED"
    assert v["refunded"] == str(ESCROW)
    assert balance(vm, PAYER) == before + ESCROW
    assert contract_balance(vm) == held - ESCROW


def test_cancel_is_payer_only(retainer):
    vm, c = retainer
    vid = open_vault(vm, c)
    for who in (STRANGER, RECIPIENT, ""):
        vm.warp(iso(DUE1 - 1))
        vm.sender = who
        with pytest.raises(Exception):
            c.cancel(vid)
    assert vault_state(vm, c, vid)["status"] == "OPEN"


def test_cancel_is_refused_once_the_first_checkpoint_is_due(retainer):
    vm, c = retainer
    vid = open_vault(vm, c)
    vm.warp(iso(DUE1))
    vm.sender = PAYER
    with pytest.raises(Exception):
        c.cancel(vid)
    assert vault_state(vm, c, vid)["status"] == "OPEN"


def test_cancel_is_refused_after_a_payout(retainer):
    vm, c = retainer
    vid = open_vault(vm, c)
    assert release(vm, c, vid, 0) == "PAID"
    vm.sender = PAYER
    with pytest.raises(Exception):
        c.cancel(vid)


# ==========================================================================
# F. invariants
# ==========================================================================


def test_every_refusal_leaves_the_vault_byte_identical(retainer):
    vm, c = retainer
    vid = open_vault(vm, c)
    snapshot = json.dumps(vault_state(vm, c, vid), sort_keys=True)

    attempts = [
        (lambda: c.release(vid, 7), STRANGER, DUE1 + 1),
        (lambda: c.finalize(vid), STRANGER, 0),
        (lambda: c.cancel(vid), STRANGER, 0),
        (lambda: c.release("999", 0), STRANGER, DUE1 + 1),
        (lambda: c.finalize("999"), STRANGER, 0),
    ]
    for fn, who, t in attempts:
        vm.warp(iso(t))
        vm.sender = who
        with pytest.raises(Exception):
            fn()
        assert json.dumps(vault_state(vm, c, vid), sort_keys=True) == snapshot


def test_every_unit_is_accounted_for(retainer):
    """paid + refunded == total, on every path that ends the vault."""
    vm, c = retainer
    remock(vm, body_a=BODY_PLAN, body_b=BODY_PLAN)
    vid = open_vault(vm, c)
    release(vm, c, vid, 0)
    remock(vm)
    release(vm, c, vid, 1, t=DUE2 + 1)
    finalize(vm, c, vid)
    v = vault_state(vm, c, vid)
    assert int(v["paid_total"]) + int(v["refunded"]) == int(v["total"])
    assert v["status"] == "CLOSED"


def test_a_cancelled_vault_keeps_every_unit(retainer):
    vm, c = retainer
    vid = open_vault(vm, c)
    vm.warp(iso(DUE1 - 1))
    vm.sender = PAYER
    c.cancel(vid)
    v = vault_state(vm, c, vid)
    assert int(v["paid_total"]) + int(v["refunded"]) == int(v["total"])
    assert contract_balance(vm) == 0


# ==========================================================================
# G. views
# ==========================================================================


def test_views_return_json_strings_and_never_a_float(retainer):
    vm, c = retainer
    vid = open_vault(vm, c)
    raw = c.get_vault(vid)
    assert isinstance(raw, str)
    for name in ("total", "paid_total", "refunded", "grace", "first_due", "last_due"):
        assert isinstance(json.loads(raw)[name], str)

    def walk(node):
        if isinstance(node, float):
            raise AssertionError("float in a return value")
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(json.loads(raw))
    walk(json.loads(c.get_checkpoint(vid, 0)))
    assert c.get_vault_count() == 1


def test_the_vault_id_is_derivable_from_the_count(retainer):
    vm, c = retainer
    before = c.get_vault_count()
    vid = open_vault(vm, c)
    assert c.get_vault_count() == before + 1
    assert vid == str(before + 1)
