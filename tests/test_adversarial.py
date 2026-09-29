"""Adversarial tests. Every test states the secure behaviour; none documents a flaw as expected.

Attacker model: can send any SPL token (the real USDT mint included), build any transaction shape,
register any address on an invoice of their own, and time all of it against expiry, restarts and RPC faults.
"""

import asyncio
import json
from decimal import Decimal
from pathlib import Path

import pytest
import sol
from conftest import (
    ATA,
    ATA2,
    OWNER,
    OWNER2,
    SENDER,
    SENDER_ATA,
    SIG,
    USDC_MINT,
    load_usdt_wallet,
    make_tx,
    token_balance,
    usdt_transfer,
)
from genericprocessor import PR_EXPIRED, PR_PAID, PR_UNPAID

SIG2 = "5ryjBnEE4UCdikRzZCQAA4TFESZaTWDaTRrT9fTZYD1SQ8aWPL31fGo3c3MxyLnRGVvooVB8uQaDvv33SPuBNkex"
SIG3 = "3kKNbWwPsJ21LLKaqsVHEh1zw6cq7jdzgxPmwCsuBh7JvyWTRnDUvQCdBYZmTErgvvhSWuHTaqKFsEEAYdqhLvtE"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ATTACKER = USDC_MINT  # any valid pubkey the attacker holds the key of
ATTACKER_ATA = sol.derive_ata(ATTACKER, sol.USDT_MINT)
VICTIM = OWNER2
VICTIM_ATA = ATA2


async def usdt_wallet(daemon, **kwargs):
    wallet = await load_usdt_wallet(daemon, **kwargs)
    await asyncio.sleep(0)  # let the backfill task scheduled by load_wallet finish first
    daemon.BLOCK_TIME = 0  # background retries must not wait in tests
    return wallet


async def open_request(daemon, wallet, amount="1", sender=None):
    req = await daemon.add_request(amount, wallet=wallet.wallet_key)
    if sender:
        assert await daemon.setrequestaddress(req["request_id"], sender, wallet=wallet.wallet_key)
    return req["request_id"]


def land(rpc, signature, tx=None, **transfer):
    rpc.add_signature(signature)
    rpc.transactions[signature] = tx or usdt_transfer(signature=signature, **transfer)


def events(daemon, wallet):
    return [u["event"] for u in daemon.get_updates(wallet=wallet.wallet_key)]


async def settle(daemon, rounds=20):
    """Let background tasks (rescan retries) run to completion."""
    for _ in range(rounds):
        await asyncio.sleep(0)


def fail_first(rpc, method, error, times=1, match=None):
    """Make the first `times` calls of `method` (optionally only for one first parameter) fail or answer `error`."""
    original = rpc.send_request
    left = {"n": times}

    async def send_request(name, params):
        if name == method and left["n"] > 0 and (match is None or params[0] == match):
            left["n"] -= 1
            rpc.calls.append((name, params))
            if isinstance(error, BaseException):
                raise error
            return error
        return await original(name, params)

    rpc.send_request = send_request


# --- only the configured mint, only the canonical account --------------------------------


def test_token2022_lookalike_gives_no_credit():
    lookalike = sol.b58encode(bytes(range(1, 33)))
    bal = token_balance(2, OWNER, 9_000_000)
    bal["programId"] = TOKEN_2022
    tx = make_tx(keys=(ATTACKER, ATTACKER_ATA, lookalike), post=(bal,))
    assert sol.parse_token_credit(tx, OWNER, sol.USDT_MINT) == (None, None)


def test_real_usdt_to_a_non_canonical_account_of_the_merchant_gives_no_credit():
    aux = sol.b58encode(bytes(range(2, 34)))  # a second USDT account owned by the merchant, not the ATA
    tx = make_tx(
        keys=(ATTACKER, ATTACKER_ATA, aux),
        pre=(token_balance(1, ATTACKER, 5_000_000), token_balance(2, OWNER, 0)),
        post=(token_balance(1, ATTACKER, 0), token_balance(2, OWNER, 5_000_000)),
    )
    assert sol.parse_token_credit(tx, OWNER, sol.USDT_MINT) == (None, None)


def test_fake_token_beside_one_real_unit_credits_the_real_unit_only():
    fake_mint = SENDER_ATA
    fake_ata = sol.derive_ata(OWNER, fake_mint)
    tx = make_tx(
        keys=(ATTACKER, ATTACKER_ATA, ATA, fake_ata),
        pre=(token_balance(1, ATTACKER, 10), token_balance(2, OWNER, 0), token_balance(3, OWNER, 0, fake_mint)),
        post=(token_balance(1, ATTACKER, 9), token_balance(2, OWNER, 1), token_balance(3, OWNER, 10**12, fake_mint)),
    )
    assert sol.parse_token_credit(tx, OWNER, sol.USDT_MINT) == (1, ATTACKER)


def test_fake_mint_entry_sharing_the_ata_index_cannot_add_credit():
    # A provider (or a crafted reply) listing a second entry for the same index with another mint
    tx = usdt_transfer(amount=1)
    tx["meta"]["postTokenBalances"].append(token_balance(2, OWNER, 10**12, USDC_MINT))
    credit, _ = sol.parse_token_credit(tx, OWNER, sol.USDT_MINT)
    assert credit in (None, 1)


async def test_fake_token_never_pays_an_invoice(daemon, rpc):
    wallet = await usdt_wallet(daemon)
    req_id = await open_request(daemon, wallet, "1", ATTACKER)
    fake_mint = SENDER_ATA
    tx = make_tx(
        signature=SIG,
        keys=(ATTACKER, ATTACKER_ATA, ATA, sol.derive_ata(OWNER, fake_mint)),
        pre=(token_balance(1, ATTACKER, 10**9, fake_mint), token_balance(3, OWNER, 0, fake_mint)),
        post=(token_balance(1, ATTACKER, 0, fake_mint), token_balance(3, OWNER, 10**9, fake_mint)),
    )
    land(rpc, SIG, tx)  # the ATA is referenced read-only so the signature shows up on the watched account
    await daemon.poll_wallet(wallet.wallet_key)
    assert wallet.get_request(req_id).status == PR_UNPAID
    assert wallet.get_request(req_id).sent_amount == 0
    assert wallet.last_signature == SIG
    assert events(daemon, wallet) == []


# --- decimals ---------------------------------------------------------------------------


def test_amount_comes_from_the_integer_field_only():
    tx = usdt_transfer(amount=1)
    for bal in tx["meta"]["postTokenBalances"]:
        bal["uiTokenAmount"].update({"decimals": 0, "uiAmount": 1e9, "uiAmountString": "1000000000"})
    assert sol.parse_token_credit(tx, OWNER, sol.USDT_MINT) == (1, SENDER)


@pytest.mark.parametrize(
    ("units", "expected"),
    [(1, "0.000001"), (999_999, "0.999999"), (10**6, "1"), (2**64 - 1, "18446744073709.551615")],
)
async def test_base_units_convert_exactly(daemon, rpc, units, expected):
    wallet = await usdt_wallet(daemon)
    tx = make_tx(
        signature=SIG,
        pre=(token_balance(1, SENDER, units), token_balance(2, OWNER, 0)),
        post=(token_balance(1, SENDER, 0), token_balance(2, OWNER, units)),
    )
    land(rpc, SIG, tx)
    await daemon.poll_wallet(wallet.wallet_key)
    update = daemon.get_updates(wallet=wallet.wallet_key)[0]
    assert Decimal(update["amount"]) == Decimal(expected)
    assert "e" not in update["amount"].lower()


async def test_one_base_unit_short_does_not_pay_and_sums_are_exact(daemon, rpc):
    wallet = await usdt_wallet(daemon)
    req_id = await open_request(daemon, wallet, "0.3", SENDER)
    land(rpc, SIG, amount=100_000)
    land(rpc, SIG2, amount=199_999, before=100_000)
    await daemon.poll_wallet(wallet.wallet_key)
    req = wallet.get_request(req_id)
    assert req.status == PR_UNPAID
    assert req.sent_amount == Decimal("0.299999")
    land(rpc, SIG3, amount=1, before=299_999)
    await daemon.poll_wallet(wallet.wallet_key)
    req = wallet.get_request(req_id)
    assert req.status == PR_PAID
    assert req.sent_amount == Decimal("0.3")


# --- outgoing / zero / net-zero / several transfers ---------------------------------------


async def test_outgoing_transfer_to_a_registered_sender_gives_no_credit(daemon, rpc):
    wallet = await usdt_wallet(daemon)
    req_id = await open_request(daemon, wallet, "1", ATTACKER)
    refund = make_tx(
        signature=SIG,
        keys=(OWNER, ATA, ATTACKER_ATA),
        pre=(token_balance(1, OWNER, 5_000_000), token_balance(2, ATTACKER, 0)),
        post=(token_balance(1, OWNER, 3_000_000), token_balance(2, ATTACKER, 2_000_000)),
    )
    land(rpc, SIG, refund)
    await daemon.poll_wallet(wallet.wallet_key)
    assert wallet.get_request(req_id).status == PR_UNPAID
    assert wallet.get_request(req_id).sent_amount == 0
    assert events(daemon, wallet) == []


async def test_zero_and_net_zero_transfers_from_the_registered_sender_change_nothing(daemon, rpc):
    wallet = await usdt_wallet(daemon)
    req_id = await open_request(daemon, wallet, "1", SENDER)
    land(rpc, SIG, amount=0, before=7)
    in_and_out = make_tx(  # 5 in and 5 out again inside one transaction
        signature=SIG2,
        keys=(SENDER, SENDER_ATA, ATA),
        pre=(token_balance(1, SENDER, 50), token_balance(2, OWNER, 7)),
        post=(token_balance(1, SENDER, 50), token_balance(2, OWNER, 7)),
    )
    land(rpc, SIG2, in_and_out)
    await daemon.poll_wallet(wallet.wallet_key)
    assert wallet.get_request(req_id).sent_amount == 0
    assert wallet.get_request(req_id).tx_hashes == []
    assert events(daemon, wallet) == []
    assert wallet.last_signature == SIG2


async def test_several_transfers_in_one_transaction_credit_the_net_once(daemon, rpc):
    wallet = await usdt_wallet(daemon)
    req_id = await open_request(daemon, wallet, "10", SENDER)
    tx = make_tx(  # three transfers of 1 USDT in, one of 0.5 back out: the balances only show the net
        signature=SIG,
        pre=(token_balance(1, SENDER, 9_000_000), token_balance(2, OWNER, 4)),
        post=(token_balance(1, SENDER, 6_500_000), token_balance(2, OWNER, 2_500_004)),
    )
    land(rpc, SIG, tx)
    await daemon.poll_wallet(wallet.wallet_key)
    req = wallet.get_request(req_id)
    assert req.sent_amount == Decimal("2.5")
    assert req.tx_hashes == [SIG]


# --- failed transactions ------------------------------------------------------------------


async def test_failed_transaction_with_credit_shaped_balances_never_pays(daemon, rpc):
    wallet = await usdt_wallet(daemon)
    req_id = await open_request(daemon, wallet, "1", SENDER)
    # the list says success, the transaction itself says failed (provider inconsistency)
    land(rpc, SIG, err={"InstructionError": [0, {"Custom": 1}]})
    # the list says failed: never fetched
    rpc.add_signature(SIG2, err={"InstructionError": [0, {"Custom": 1}]})
    rpc.transactions[SIG2] = usdt_transfer(signature=SIG2)
    await daemon.poll_wallet(wallet.wallet_key)
    assert wallet.get_request(req_id).status == PR_UNPAID
    assert wallet.get_request(req_id).sent_amount == 0
    assert [p[0] for m, p in rpc.calls if m == "getTransaction"] == [SIG]
    assert wallet.last_signature == SIG2


# --- sender attribution -------------------------------------------------------------------


def test_sender_is_never_an_owner_who_did_not_sign():
    # The attacker moves the victim's USDT as a delegate: the victim's account falls, the victim did not sign
    tx = make_tx(
        keys=(ATTACKER, VICTIM_ATA, ATA),
        pre=(token_balance(1, VICTIM, 5_000_000), token_balance(2, OWNER, 0)),
        post=(token_balance(1, VICTIM, 0), token_balance(2, OWNER, 5_000_000)),
    )
    assert sol.parse_token_credit(tx, OWNER, sol.USDT_MINT) == (5_000_000, None)


def test_forged_owner_label_on_the_merchant_account_is_not_a_sender():
    tx = usdt_transfer()
    tx["meta"]["preTokenBalances"][1]["owner"] = VICTIM
    tx["meta"]["postTokenBalances"][1]["owner"] = VICTIM
    assert sol.parse_token_credit(tx, OWNER, sol.USDT_MINT) == (1_000_000, SENDER)


async def test_payment_from_another_sender_does_not_pay_the_invoice(daemon, rpc):
    wallet = await usdt_wallet(daemon)
    req_id = await open_request(daemon, wallet, "1", VICTIM)
    land(rpc, SIG, sender=ATTACKER, sender_ata=ATTACKER_ATA)
    await daemon.poll_wallet(wallet.wallet_key)
    assert wallet.get_request(req_id).status == PR_UNPAID
    assert events(daemon, wallet) == ["new_transaction"]


# --- one payment, one credit --------------------------------------------------------------


async def test_one_payment_cannot_pay_two_invoices_open_at_the_same_time(daemon, rpc):
    """Attacker opens A and B, pays A once, then attaches the same sender to B: the rescan finds the same transfer."""
    wallet = await usdt_wallet(daemon)
    a = await open_request(daemon, wallet, "1", ATTACKER)
    b = await open_request(daemon, wallet, "1")
    land(rpc, SIG, sender=ATTACKER, sender_ata=ATTACKER_ATA)
    await daemon.poll_wallet(wallet.wallet_key)
    assert wallet.get_request(a).status == PR_PAID
    assert await daemon.setrequestaddress(b, ATTACKER, wallet=wallet.wallet_key)
    await settle(daemon)
    assert wallet.get_request(b).status == PR_UNPAID
    assert wallet.get_request(b).sent_amount == 0
    assert wallet.get_request(b).tx_hashes == []


async def test_replayed_payment_cannot_pay_a_second_invoice(daemon, rpc):
    """Same as above through the poller: the cursor is lost (restart, provider ignoring `until`) after A is paid."""
    wallet = await usdt_wallet(daemon)
    a = await open_request(daemon, wallet, "1", ATTACKER)
    b = await open_request(daemon, wallet, "1")
    land(rpc, SIG, sender=ATTACKER, sender_ata=ATTACKER_ATA)
    await daemon.poll_wallet(wallet.wallet_key)
    assert wallet.get_request(a).status == PR_PAID
    fail_first(rpc, "getSignaturesForAddress", TimeoutError())  # keep the rescan out of this test
    assert await daemon.setrequestaddress(b, ATTACKER, wallet=wallet.wallet_key)
    daemon.BLOCK_TIME = 3600  # no background retry either
    wallet.last_signature = None
    await daemon.poll_wallet(wallet.wallet_key)
    assert wallet.get_request(b).status == PR_UNPAID
    assert wallet.get_request(b).sent_amount == 0


async def test_partial_payment_of_an_expired_invoice_is_not_credited_again(daemon, rpc):
    wallet = await usdt_wallet(daemon)
    a = await open_request(daemon, wallet, "2", ATTACKER)
    b = await open_request(daemon, wallet, "1")
    land(rpc, SIG, sender=ATTACKER, sender_ata=ATTACKER_ATA)
    await daemon.poll_wallet(wallet.wallet_key)
    assert wallet.get_request(a).sent_amount == 1
    wallet.set_request_status(a, PR_EXPIRED)
    assert await daemon.setrequestaddress(b, ATTACKER, wallet=wallet.wallet_key)
    await settle(daemon)
    assert wallet.get_request(b).status == PR_UNPAID
    assert wallet.get_request(a).tx_hashes == [SIG]


async def test_signature_listed_twice_credits_once(daemon, rpc):
    wallet = await usdt_wallet(daemon)
    req_id = await open_request(daemon, wallet, "5", SENDER)
    land(rpc, SIG)
    rpc.signatures.insert(0, dict(rpc.signatures[0]))  # duplicate observation in one page
    await daemon.poll_wallet(wallet.wallet_key)
    await daemon.poll_wallet(wallet.wallet_key)
    req = wallet.get_request(req_id)
    assert req.sent_amount == 1
    assert req.tx_hashes == [SIG]
    assert events(daemon, wallet).count("new_payment") == 1


async def test_provider_ignoring_until_replays_without_second_credit(daemon, rpc):
    wallet = await usdt_wallet(daemon)
    req_id = await open_request(daemon, wallet, "5", SENDER)
    land(rpc, SIG)
    await daemon.poll_wallet(wallet.wallet_key)
    original = rpc._signatures
    rpc._signatures = lambda address, opts: original(address, {k: v for k, v in opts.items() if k != "until"})
    for _ in range(3):
        await daemon.poll_wallet(wallet.wallet_key)
    assert wallet.get_request(req_id).sent_amount == 1
    assert events(daemon, wallet).count("new_payment") == 1


async def test_attach_retry_by_the_backend_credits_once(daemon, rpc):
    wallet = await usdt_wallet(daemon)
    req_id = await open_request(daemon, wallet, "2")
    land(rpc, SIG)
    await daemon.poll_wallet(wallet.wallet_key)
    for _ in range(3):  # worker retry / double click
        assert await daemon.setrequestaddress(req_id, SENDER, wallet=wallet.wallet_key)
    await asyncio.gather(*(daemon.setrequestaddress(req_id, SENDER, wallet=wallet.wallet_key) for _ in range(3)))
    await settle(daemon)
    req = wallet.get_request(req_id)
    assert req.sent_amount == 1
    assert req.tx_hashes == [SIG]
    assert events(daemon, wallet).count("new_payment") == 1


async def test_poll_and_rescan_at_the_same_time_credit_once(daemon, rpc):
    wallet = await usdt_wallet(daemon)
    req_id = await open_request(daemon, wallet, "5")
    land(rpc, SIG)
    await asyncio.gather(
        daemon.poll_wallet(wallet.wallet_key),
        daemon.setrequestaddress(req_id, SENDER, wallet=wallet.wallet_key),
        daemon.poll_wallet(wallet.wallet_key),
    )
    await settle(daemon)
    assert wallet.get_request(req_id).sent_amount == 1
    assert events(daemon, wallet).count("new_payment") == 1


# --- restarts -----------------------------------------------------------------------------


async def test_crash_after_credit_before_cursor_write_credits_once(daemon, rpc):
    wallet = await usdt_wallet(daemon, diskless=False)
    key = wallet.wallet_key
    req_id = await open_request(daemon, wallet, "5", SENDER)
    land(rpc, SIG)
    await daemon.poll_wallet(key)
    path = Path(wallet.storage.path)
    stored = json.loads(path.read_text())
    assert stored["last_signature"] == SIG
    del stored["last_signature"]  # the state a crash between the two writes leaves behind
    await daemon.close_wallet_impl(key)
    path.write_text(json.dumps(stored))
    wallet = await usdt_wallet(daemon, diskless=False)  # its backfill replays SIG from the start
    await daemon.poll_wallet(key)
    assert [p[0] for m, p in rpc.calls if m == "getTransaction"].count(SIG) >= 2
    req = wallet.get_request(req_id)
    assert req.sent_amount == 1
    assert req.tx_hashes == [SIG]
    assert wallet.last_signature == SIG


async def test_restart_in_the_middle_of_a_batch_loses_and_doubles_nothing(daemon, rpc):
    wallet = await usdt_wallet(daemon, diskless=False)
    key = wallet.wallet_key
    req_id = await open_request(daemon, wallet, "3", SENDER)
    land(rpc, SIG)
    land(rpc, SIG2, before=1_000_000)
    land(rpc, SIG3, before=2_000_000)
    fail_first(rpc, "getTransaction", ConnectionError(), match=SIG2)  # the daemon dies here
    await daemon.poll_wallet(key)
    assert wallet.last_signature == SIG
    await daemon.close_wallet_impl(key)
    wallet = await usdt_wallet(daemon, diskless=False)
    await daemon.poll_wallet(key)
    req = wallet.get_request(req_id)
    assert req.status == PR_PAID
    assert req.tx_hashes == [SIG, SIG2, SIG3]
    assert req.sent_amount == 3


async def test_payment_before_address_survives_a_restart_before_the_rescan_finished(daemon, rpc):
    wallet = await usdt_wallet(daemon, diskless=False)
    key = wallet.wallet_key
    req_id = await open_request(daemon, wallet, "1")
    land(rpc, SIG)
    await daemon.poll_wallet(key)
    rpc.failures["getSignaturesForAddress"] = TimeoutError()
    daemon.BLOCK_TIME = 3600  # the retry never gets its turn: the daemon goes down first
    assert await daemon.setrequestaddress(req_id, SENDER, wallet=key)
    await daemon.close_wallet_impl(key)
    rpc.failures = {}
    wallet = await usdt_wallet(daemon, diskless=False)
    await settle(daemon)
    await daemon.poll_wallet(key)
    await settle(daemon)
    assert wallet.get_request(req_id).status == PR_PAID
    assert wallet.get_request(req_id).tx_hashes == [SIG]


# --- expiry / sync-window boundaries ------------------------------------------------------


async def test_payment_after_expiry_is_not_credited_and_not_carried_to_a_later_invoice(daemon, rpc):
    wallet = await usdt_wallet(daemon)
    a = await open_request(daemon, wallet, "1", SENDER)
    wallet.set_request_status(a, PR_EXPIRED)
    land(rpc, SIG)
    await daemon.poll_wallet(wallet.wallet_key)
    assert wallet.get_request(a).status == PR_EXPIRED
    assert wallet.get_request(a).sent_amount == 0
    rpc.slot += 1000  # a later invoice: the old transfer predates it
    b = await open_request(daemon, wallet, "1", SENDER)
    wallet.last_signature = None
    await daemon.poll_wallet(wallet.wallet_key)
    await settle(daemon)
    assert wallet.get_request(b).status == PR_UNPAID


async def test_slot_boundary_of_the_replay_guard(daemon, rpc):
    wallet = await usdt_wallet(daemon)
    req_id = await open_request(daemon, wallet, "2", SENDER)
    height = wallet.get_request(req_id).height
    land(rpc, SIG, slot=height - 1)
    land(rpc, SIG2, slot=height, before=1_000_000)
    await daemon.poll_wallet(wallet.wallet_key)
    assert wallet.get_request(req_id).tx_hashes == [SIG2]


async def test_time_window_boundary_keeps_the_edge_and_unknown_block_times(daemon, rpc, monkeypatch):
    now = 1_800_000_000
    monkeypatch.setattr(sol.time, "time", lambda: now)
    daemon.MAX_SYNC_SECONDS = 3600
    wallet = await usdt_wallet(daemon)
    rpc.add_signature(SIG, block_time=now - 3601)  # outside
    rpc.add_signature(SIG2, block_time=now - 3600)  # exactly on the edge
    rpc.add_signature(SIG3)
    rpc.signatures[0]["blockTime"] = None  # provider without a block time
    for s in (SIG, SIG2, SIG3):
        rpc.transactions[s] = usdt_transfer(signature=s)
    await daemon.poll_wallet(wallet.wallet_key)
    assert [p[0] for m, p in rpc.calls if m == "getTransaction"] == [SIG2, SIG3]


# --- sender lock --------------------------------------------------------------------------


async def test_sender_lock_holds_through_partial_payment_and_detours(daemon, rpc):
    wallet = await usdt_wallet(daemon)
    a = await open_request(daemon, wallet, "2", VICTIM)
    b = await open_request(daemon, wallet, "1")
    land(rpc, SIG, sender=VICTIM, sender_ata=VICTIM_ATA)
    await daemon.poll_wallet(wallet.wallet_key)
    assert wallet.get_request(a).status == PR_UNPAID  # partly paid, still open
    for _ in range(2):
        with pytest.raises(Exception, match=sol.SENDER_IN_USE):
            await daemon.setrequestaddress(b, VICTIM, wallet=wallet.wallet_key)
        assert await daemon.setrequestaddress(b, ATTACKER, wallet=wallet.wallet_key)  # detour over another address
    assert wallet.request_addresses[VICTIM] == a
    assert wallet.get_request(b).payment_address == ATTACKER
    land(rpc, SIG2, sender=VICTIM, sender_ata=VICTIM_ATA, before=1_000_000)
    await daemon.poll_wallet(wallet.wallet_key)
    assert wallet.get_request(a).status == PR_PAID
    assert wallet.get_request(b).sent_amount == 0


async def test_sender_lock_survives_a_restart(daemon):
    wallet = await usdt_wallet(daemon, diskless=False)
    key = wallet.wallet_key
    a = await open_request(daemon, wallet, "1", VICTIM)
    b = await open_request(daemon, wallet, "1")
    await daemon.close_wallet_impl(key)
    wallet = await usdt_wallet(daemon, diskless=False)
    with pytest.raises(Exception, match=sol.SENDER_IN_USE):
        await daemon.setrequestaddress(b, VICTIM, wallet=key)
    assert wallet.request_addresses[VICTIM] == a


async def test_merchant_address_cannot_be_registered_as_a_sender(daemon, rpc):
    """The merchant moving their own USDT into the ATA must never pay a stranger's invoice."""
    wallet = await usdt_wallet(daemon)
    req_id = await open_request(daemon, wallet, "1")
    assert not await daemon.setrequestaddress(req_id, OWNER, wallet=wallet.wallet_key)
    aux = sol.b58encode(bytes(range(2, 34)))
    consolidation = make_tx(
        signature=SIG,
        keys=(OWNER, aux, ATA),
        pre=(token_balance(1, OWNER, 5_000_000), token_balance(2, OWNER, 0)),
        post=(token_balance(1, OWNER, 0), token_balance(2, OWNER, 5_000_000)),
    )
    land(rpc, SIG, consolidation)
    await daemon.poll_wallet(wallet.wallet_key)
    await settle(daemon)
    assert wallet.get_request(req_id).status == PR_UNPAID
    assert wallet.get_request(req_id).sent_amount == 0


# --- RPC faults ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        None,
        {"unexpected": "object"},
        "rate limited",
        [{"err": None, "slot": 1}],  # entry without a signature
        [None],
    ],
)
async def test_malformed_signature_list_moves_nothing(daemon, rpc, reply):
    wallet = await usdt_wallet(daemon)
    req_id = await open_request(daemon, wallet, "1", SENDER)
    land(rpc, SIG)
    fail_first(rpc, "getSignaturesForAddress", reply)
    await daemon.poll_wallet(wallet.wallet_key)  # must not raise: other wallets share the tick
    assert wallet.last_signature is None
    assert wallet.get_request(req_id).sent_amount == 0
    await daemon.poll_wallet(wallet.wallet_key)
    assert wallet.get_request(req_id).status == PR_PAID


@pytest.mark.parametrize(
    "reply",
    [
        {},
        {"slot": 1, "transaction": {"signatures": [SIG]}},  # no meta
        {"slot": 1, "meta": None, "transaction": {"signatures": [SIG]}},
        {"slot": 1, "meta": {"err": None}, "transaction": {"signatures": []}},
        "not an object",
        TimeoutError(),
        sol.SolanaTransientError("HTTP 429"),
    ],
)
async def test_bad_transaction_reply_stalls_then_credits_once(daemon, rpc, reply):
    wallet = await usdt_wallet(daemon)
    req_id = await open_request(daemon, wallet, "2", SENDER)
    land(rpc, SIG)
    land(rpc, SIG2, before=1_000_000)
    fail_first(rpc, "getTransaction", reply, times=2, match=SIG)
    for _ in range(2):
        await daemon.poll_wallet(wallet.wallet_key)
        assert wallet.last_signature is None  # nothing is skipped, SIG2 waits behind SIG
        assert wallet.get_request(req_id).sent_amount == 0
    await daemon.poll_wallet(wallet.wallet_key)
    req = wallet.get_request(req_id)
    assert req.status == PR_PAID
    assert req.tx_hashes == [SIG, SIG2]


async def test_one_wallet_in_trouble_does_not_stop_the_other(daemon, rpc):
    wallet = await usdt_wallet(daemon)
    other = await load_usdt_wallet(daemon, owner=OWNER2)
    await asyncio.sleep(0)
    req_id = await open_request(daemon, other, "1", SENDER)
    rpc.add_signature(SIG)  # on the first wallet, never served
    rpc.add_signature(SIG2, address=ATA2)
    tx = make_tx(
        signature=SIG2,
        keys=(SENDER, SENDER_ATA, ATA2),
        pre=(token_balance(1, SENDER, 1_000_000), token_balance(2, OWNER2, 0)),
        post=(token_balance(1, SENDER, 0), token_balance(2, OWNER2, 1_000_000)),
    )
    rpc.transactions[SIG2] = tx
    await asyncio.gather(*(daemon.poll_wallet(key) for key in list(daemon.wallets)))
    assert wallet.last_signature is None
    assert other.get_request(req_id).status == PR_PAID


async def test_transaction_missing_during_the_rescan_is_credited_later(daemon, rpc):
    """Paid before the address was entered; the provider does not serve the transaction at attach time."""
    wallet = await usdt_wallet(daemon)
    req_id = await open_request(daemon, wallet, "1")
    land(rpc, SIG)
    await daemon.poll_wallet(wallet.wallet_key)
    assert wallet.last_signature == SIG  # the poller is past it: only the rescan can still find it
    fail_first(rpc, "getTransaction", None, times=2, match=SIG)
    assert await daemon.setrequestaddress(req_id, SENDER, wallet=wallet.wallet_key)
    await settle(daemon)
    req = wallet.get_request(req_id)
    assert req.status == PR_PAID
    assert req.tx_hashes == [SIG]


@pytest.mark.parametrize("method", ["getSignaturesForAddress", "getTransaction"])
async def test_rpc_timeout_during_the_rescan_is_credited_later(daemon, rpc, method):
    wallet = await usdt_wallet(daemon)
    req_id = await open_request(daemon, wallet, "1")
    land(rpc, SIG)
    await daemon.poll_wallet(wallet.wallet_key)
    fail_first(rpc, method, TimeoutError(), times=2)
    assert await daemon.setrequestaddress(req_id, SENDER, wallet=wallet.wallet_key)
    await settle(daemon)
    assert wallet.get_request(req_id).status == PR_PAID
    assert events(daemon, wallet).count("new_payment") == 1


async def test_rescan_retry_stops_when_the_request_is_closed(daemon, rpc):
    wallet = await usdt_wallet(daemon)
    req_id = await open_request(daemon, wallet, "1")
    land(rpc, SIG)
    await daemon.poll_wallet(wallet.wallet_key)
    rpc.failures["getSignaturesForAddress"] = TimeoutError()
    assert await daemon.setrequestaddress(req_id, SENDER, wallet=wallet.wallet_key)
    wallet.set_request_status(req_id, PR_EXPIRED)
    rpc.failures = {}
    rpc.calls.clear()
    await settle(daemon)
    assert rpc.calls == []
    assert wallet.get_request(req_id).status == PR_EXPIRED
    assert wallet.get_request(req_id).sent_amount == 0


# --- HTTP-level faults, through the real provider and failover classes ----------------------


async def _status(request, status):
    from aiohttp import web

    return web.Response(status=status)


async def _body(request, text, content_type):
    from aiohttp import web

    return web.Response(text=text, content_type=content_type)


async def _slow(request):
    from aiohttp import web

    await asyncio.sleep(0.3)
    return web.json_response({"jsonrpc": "2.0", "id": 1, "result": None})


async def _drop(request):
    from aiohttp import web

    request.transport.abort()
    return web.Response()


HTTP_FAULTS = {
    "http 429": lambda r: _status(r, 429),
    "http 503": lambda r: _status(r, 503),
    "http 403": lambda r: _status(r, 403),
    "html page": lambda r: _body(r, "<html>rate limited</html>", "text/html"),
    "broken json": lambda r: _body(r, '{"jsonrpc": "2.0", "resu', "application/json"),
    "json array": lambda r: _body(r, "[1, 2]", "application/json"),
    "json without result": lambda r: _body(r, '{"jsonrpc": "2.0", "id": 1}', "application/json"),
    "error as text": lambda r: _body(r, '{"error": "boom"}', "application/json"),
    "rate limit error": lambda r: _body(r, '{"error": {"code": 429, "message": "Too many requests"}}', "application/json"),
    "node behind": lambda r: _body(r, '{"error": {"code": -32005, "message": "Node is behind"}}', "application/json"),
    "null result": lambda r: _body(r, '{"jsonrpc": "2.0", "id": 1, "result": null}', "application/json"),
    "empty object result": lambda r: _body(r, '{"jsonrpc": "2.0", "id": 1, "result": {}}', "application/json"),
    "timeout": _slow,
    "connection drop": _drop,
}


@pytest.mark.parametrize("method", ["getSignaturesForAddress", "getTransaction"])
@pytest.mark.parametrize("fault", list(HTTP_FAULTS))
async def test_http_fault_delays_but_never_loses_or_doubles(daemon, rpc, monkeypatch, unused_tcp_port_factory, fault, method):
    from aiohttp import ClientTimeout, web

    monkeypatch.setattr(sol, "RETRY_DELAYS", (0,))
    monkeypatch.setattr(sol, "ClientTimeout", lambda total: ClientTimeout(total=0.1))
    left = {"n": 3}

    async def handler(request):
        body = await request.json()
        if body["method"] == method and left["n"] > 0:
            left["n"] -= 1
            return await HTTP_FAULTS[fault](request)
        result = await rpc.send_request(body["method"], body["params"])
        return web.json_response({"jsonrpc": "2.0", "id": 1, "result": result})

    app = web.Application()
    app.router.add_post("/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    port = unused_tcp_port_factory()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    provider = sol.SolanaRPCProvider(f"http://127.0.0.1:{port}/?api-key=SECRET")
    daemon.coin = sol.SOLFeatures(sol.MultipleProviderRPC([provider]))
    try:
        wallet = await usdt_wallet(daemon)
        req_id = await open_request(daemon, wallet, "2", SENDER)
        land(rpc, SIG)
        land(rpc, SIG2, before=1_000_000)
        for _ in range(5):
            await daemon.poll_wallet(wallet.wallet_key)
            req = wallet.get_request(req_id)
            assert req.tx_hashes in ([], [SIG], [SIG, SIG2])  # in order, nothing skipped
            assert req.sent_amount == len(req.tx_hashes)
        assert left["n"] == 0
        assert req.status == PR_PAID
        assert req.tx_hashes == [SIG, SIG2]
        assert events(daemon, wallet).count("new_payment") == 2
    finally:
        await provider.close()
        await runner.cleanup()


# --- open items: reproduced, not fixed (see README, "Known limitations") ---------------


@pytest.mark.xfail(strict=True, reason="design limit: an address is registered without proof of ownership")
async def test_open_stranger_registers_the_payers_address_first(daemon, rpc):
    """The victim pays before entering the address; the attacker sees the transfer on chain and
    attaches the victim's address to an invoice of their own before the victim does."""
    wallet = await usdt_wallet(daemon)
    victims = await open_request(daemon, wallet, "1")
    attackers = await open_request(daemon, wallet, "1")
    land(rpc, SIG, sender=VICTIM, sender_ata=VICTIM_ATA)
    await daemon.poll_wallet(wallet.wallet_key)
    assert await daemon.setrequestaddress(attackers, VICTIM, wallet=wallet.wallet_key)
    assert await daemon.setrequestaddress(victims, VICTIM, wallet=wallet.wallet_key)
    await settle(daemon)
    assert wallet.get_request(attackers).status == PR_UNPAID
    assert wallet.get_request(victims).status == PR_PAID


@pytest.mark.xfail(strict=True, reason="fail-closed by design above the cap (200 pages); reported in getinfo")
async def test_open_signature_flood_behind_the_cursor_stops_detection(daemon, rpc):
    """Any transaction that names the watched account is listed. More than the page cap of them
    between two polls (an outage helps the attacker) and detection waits for the operator."""
    daemon.SIGNATURE_PAGE_SIZE = 2
    daemon.MAX_SIGNATURE_PAGES = 2
    wallet = await usdt_wallet(daemon)
    req_id = await open_request(daemon, wallet, "1", SENDER)
    rpc.add_signature("2" + SIG[1:])
    wallet.last_signature = "2" + SIG[1:]
    land(rpc, SIG)
    for i in range(4, 9):
        rpc.add_signature(f"{i}" + SIG[1:], err={"InstructionError": [0, "Custom"]})  # cheap failed spam
    for _ in range(3):
        await daemon.poll_wallet(wallet.wallet_key)
    assert wallet.get_request(req_id).status == PR_PAID


async def test_page_cap_hit_is_reported_in_getinfo(daemon, rpc, monkeypatch):
    """Logging is off in production (SOL_DEBUG=false), so the health probe reads the cap hit from getinfo."""
    assert sol.SOLDaemon.MAX_SIGNATURE_PAGES * sol.SOLDaemon.SIGNATURE_PAGE_SIZE == 200_000
    daemon.SIGNATURE_PAGE_SIZE = 2
    daemon.MAX_SIGNATURE_PAGES = 1
    wallet = await usdt_wallet(daemon)
    assert (await daemon.getinfo())["signature_cap_hits"] == {}
    rpc.add_signature("2" + SIG[1:])
    wallet.last_signature = "2" + SIG[1:]
    for s in (SIG, SIG2, SIG3):
        land(rpc, s)
    now = int(sol.time.time())
    await daemon.poll_wallet(wallet.wallet_key)
    hits = (await daemon.getinfo())["signature_cap_hits"]
    assert list(hits) == [ATA]
    assert now <= hits[ATA] <= now + 5
    assert wallet.last_signature == "2" + SIG[1:]  # still fail-closed
    daemon.MAX_SIGNATURE_PAGES = 5  # operator raises the cap: detection resumes, the old hit ages out in the probe
    await daemon.poll_wallet(wallet.wallet_key)
    assert wallet.last_signature == SIG3
    assert (await daemon.getinfo())["signature_cap_hits"] == hits


async def test_first_load_page_cap_hit_is_reported_in_getinfo(daemon, rpc):
    daemon.SIGNATURE_PAGE_SIZE = 2
    daemon.MAX_SIGNATURE_PAGES = 1
    for s in (SIG, SIG2, SIG3):
        land(rpc, s)
    await usdt_wallet(daemon)
    assert list((await daemon.getinfo())["signature_cap_hits"]) == [ATA]


# --- watch-only ---------------------------------------------------------------------------


async def test_wallet_file_and_key_handling_hold_no_private_material(daemon, caplog):
    secret = sol.b58encode(bytes(range(64)))  # the 64-byte keypair format wallets export
    assert not daemon.validatekey(secret)
    with pytest.raises(Exception, match="Invalid key|invalid address") as exc:
        await daemon.load_wallet(secret, sol.USDT_MINT, diskless=True)
    assert secret not in str(exc.value)
    assert secret not in caplog.text
    wallet = await usdt_wallet(daemon, diskless=False)
    stored = json.loads(Path(wallet.storage.path).read_text())
    assert stored["keystore"] == {"key": OWNER, "contract": sol.USDT_MINT}
    assert wallet.is_watching_only()
    assert not wallet.keystore.has_seed()
    for leak in ("getprivatekeys", "getseed"):
        with pytest.raises(Exception, match=r"watching-only|seed|not supported"):
            getattr(daemon, leak)(wallet=wallet.wallet_key)
