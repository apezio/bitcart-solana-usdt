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
    FakeRPC,
    load_fixture,
    load_usdt_wallet,
    make_tx,
    token_balance,
    usdt_transfer,
)
from genericprocessor import PR_EXPIRED, PR_PAID, PR_UNPAID

SIG2 = "5ryjBnEE4UCdikRzZCQAA4TFESZaTWDaTRrT9fTZYD1SQ8aWPL31fGo3c3MxyLnRGVvooVB8uQaDvv33SPuBNkex"
SIG3 = "3kKNbWwPsJ21LLKaqsVHEh1zw6cq7jdzgxPmwCsuBh7JvyWTRnDUvQCdBYZmTErgvvhSWuHTaqKFsEEAYdqhLvtE"
SIG0 = "2" + SIG[1:]


# --- addresses / ATA -----------------------------------------------------------------


def test_valid_address():
    assert sol.is_pubkey(OWNER)
    assert sol.is_pubkey(sol.USDT_MINT)


@pytest.mark.parametrize("bad", ["", "0OIl", OWNER[:20], OWNER + "11", "not base58!", "1" * 44, None, 5])
def test_invalid_address(bad):
    assert not sol.is_pubkey(bad)


def test_ata_matches_mainnet_vectors():
    assert sol.derive_ata(OWNER, sol.USDT_MINT) == ATA
    assert sol.derive_ata(OWNER2, sol.USDT_MINT) == ATA2


def test_ata_depends_on_mint():
    assert sol.derive_ata(OWNER, USDC_MINT) != ATA
    assert not sol.is_on_curve(sol.b58decode(ATA))
    assert sol.is_on_curve(sol.b58decode(OWNER))


def test_base58_roundtrip():
    raw = sol.b58decode(OWNER)
    assert len(raw) == 32
    assert sol.b58encode(raw) == OWNER
    assert sol.b58encode(b"\0\0\x01") == "112"


# --- amounts / URI --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "expected"),
    [("1", "1"), ("0.000001", "0.000001"), ("2.50", "2.5"), ("123456789.123456", "123456789.123456"), ("0", "0")],
)
def test_format_amount(amount, expected):
    assert sol.format_amount(Decimal(amount), 6) == expected


def test_units():
    assert sol.from_wei(1_000_000, 6) == Decimal("1")
    assert sol.from_wei(1, 6) == Decimal("0.000001")
    big = 10**18 + 1
    assert sol.from_wei(big, 6) == Decimal(big) / Decimal(10**6)
    assert isinstance(sol.from_wei(5, 6), Decimal)


async def test_payment_uri(daemon):
    uri = await daemon.coin.get_payment_uri(OWNER, Decimal("2.5"), 6, contract=sol.USDT_MINT)
    assert uri == f"solana:{OWNER}?amount=2.5&spl-token={sol.USDT_MINT}"
    assert ATA not in uri
    native = await daemon.coin.get_payment_uri(OWNER, Decimal("0.1"), 9)
    assert native == f"solana:{OWNER}?amount=0.1"


async def test_modify_payment_url(daemon):
    uri = await daemon.coin.get_payment_uri(OWNER, Decimal("2.5"), 6, contract=sol.USDT_MINT)
    assert (
        await daemon.modifypaymenturl(uri, Decimal("1.234567"), 6)
        == f"solana:{OWNER}?amount=1.234567&spl-token={sol.USDT_MINT}"
    )


# --- transaction parsing --------------------------------------------------------------


def test_transfer_detected():
    credit, sender = sol.parse_token_credit(usdt_transfer(amount=1_500_000, before=7), OWNER, sol.USDT_MINT)
    assert credit == 1_500_000
    assert sender == SENDER


def test_live_incoming_fixture():
    data = load_fixture("live_tx_in.json")
    credit, sender = sol.parse_token_credit(data, OWNER2, sol.USDT_MINT)
    assert credit == 257_173_943_747_000
    assert sol.is_pubkey(sender)
    assert sender != OWNER2


def test_live_outgoing_fixture_ignored():
    data = load_fixture("live_tx0.json")
    assert sol.parse_token_credit(data, OWNER, sol.USDT_MINT) == (None, None)


def test_transfer_to_other_recipient_ignored():
    tx = usdt_transfer()
    tx["transaction"]["message"]["accountKeys"][2]["pubkey"] = ATA2
    assert sol.parse_token_credit(tx, OWNER, sol.USDT_MINT) == (None, None)


def test_spoof_token_same_symbol_ignored():
    fake_mint = OWNER2  # any pubkey that is not the canonical USDT mint
    fake_ata = sol.derive_ata(OWNER, fake_mint)
    tx = make_tx(
        keys=(SENDER, SENDER_ATA, fake_ata),
        pre=(token_balance(2, OWNER, 0, fake_mint),),
        post=(token_balance(2, OWNER, 5_000_000, fake_mint),),
    )
    assert sol.parse_token_credit(tx, OWNER, sol.USDT_MINT) == (None, None)


def test_wrong_mint_label_on_real_ata_ignored():
    tx = usdt_transfer()
    for bal in tx["meta"]["postTokenBalances"]:
        bal["mint"] = USDC_MINT
    assert sol.parse_token_credit(tx, OWNER, sol.USDT_MINT) == (None, None)


def test_net_zero_and_outgoing_ignored():
    assert sol.parse_token_credit(usdt_transfer(amount=0, before=5), OWNER, sol.USDT_MINT) == (None, None)
    tx = make_tx(pre=(token_balance(2, OWNER, 9),), post=(token_balance(2, OWNER, 4),))
    assert sol.parse_token_credit(tx, OWNER, sol.USDT_MINT) == (None, None)


def test_native_sol_tx_ignored_for_token_wallet():
    tx = make_tx(keys=(SENDER, OWNER), pre_lamports=[10, 0], post_lamports=[5, 5])
    assert sol.parse_token_credit(tx, OWNER, sol.USDT_MINT) == (None, None)
    assert sol.parse_native_credit(tx, OWNER) == (5, SENDER)


def test_ata_created_in_same_tx():
    tx = make_tx(post=(token_balance(2, OWNER, 3_000_000),))
    assert sol.parse_token_credit(tx, OWNER, sol.USDT_MINT) == (3_000_000, None)


def test_multi_instruction_and_cpi_credit_from_balances():
    tx = usdt_transfer(amount=2_000_000)
    tx["meta"]["innerInstructions"] = [{"index": 3, "instructions": [{"program": "spl-token"}]}]
    tx["transaction"]["message"]["accountKeys"] += [
        {"pubkey": USDC_MINT, "signer": False, "source": "transaction", "writable": False}
    ]
    assert sol.parse_token_credit(tx, OWNER, sol.USDT_MINT) == (2_000_000, SENDER)


def test_versioned_tx_lookup_table_keys():
    tx = make_tx(
        keys=(SENDER, SENDER_ATA), post=(token_balance(2, OWNER, 4_000_000),), loaded={"writable": [ATA], "readonly": []}
    )
    assert sol.parse_token_credit(tx, OWNER, sol.USDT_MINT) == (4_000_000, None)
    inline = make_tx(keys=(SENDER, SENDER_ATA, ATA), post=(token_balance(2, OWNER, 4_000_000),), loaded={"writable": [ATA]})
    inline["transaction"]["message"]["accountKeys"][2]["source"] = "lookupTable"
    assert sol.parse_token_credit(inline, OWNER, sol.USDT_MINT) == (4_000_000, None)


def test_missing_balance_metadata_gives_no_amount():
    tx = usdt_transfer()
    tx["meta"]["postTokenBalances"] = None
    assert sol.parse_token_credit(tx, OWNER, sol.USDT_MINT) == (None, None)
    del tx["meta"]["preTokenBalances"]
    with pytest.raises(KeyError):
        sol.parse_token_credit(tx, OWNER, sol.USDT_MINT)


async def test_malformed_tx_does_not_pay(daemon, rpc):
    wallet = await load_usdt_wallet(daemon)
    req = await daemon.add_request("1", wallet=wallet.wallet_key)
    await daemon.setrequestaddress(req["request_id"], SENDER, wallet=wallet.wallet_key)
    rpc.add_signature(SIG)
    rpc.transactions[SIG] = {"slot": 1, "meta": {"err": None}, "transaction": {"signatures": [SIG], "message": {}}}
    await daemon.poll_wallet(wallet.wallet_key)
    assert wallet.get_request(req["request_id"]).status == PR_UNPAID
    assert wallet.last_signature is None  # a reply we cannot read is never skipped over


# --- wallet + payment pipeline --------------------------------------------------------


async def test_wallet_load_and_keys(daemon):
    wallet = await load_usdt_wallet(daemon)
    assert wallet.address == OWNER
    assert wallet.watch_address == ATA
    assert wallet.symbol == "USDT"
    assert wallet.divisibility == 6
    assert wallet.wallet_key == f"{OWNER}_{sol.USDT_MINT}"
    assert daemon.validatecontract(sol.USDT_MINT)
    assert not daemon.validatecontract(USDC_MINT)
    assert daemon.get_tokens() == {"USDT": sol.USDT_MINT}
    assert await daemon.readcontract(sol.USDT_MINT, "symbol") == "USDT"
    assert await daemon.readcontract(sol.USDT_MINT, "decimals") == 6
    with pytest.raises(Exception, match="Invalid contract"):
        await daemon.load_wallet(OWNER2, USDC_MINT, diskless=True)


async def test_validatekey_rejects_private_material(daemon):
    assert daemon.validatekey(OWNER)
    assert not daemon.validatekey("bad")
    with pytest.raises(NotImplementedError):
        daemon.make_seed()
    wallet = await load_usdt_wallet(daemon)
    assert wallet.is_watching_only()
    with pytest.raises(Exception, match="not supported"):
        daemon.importprivkey("x" * 64, wallet=wallet.wallet_key)


async def test_full_payment_flow(daemon, rpc):
    wallet = await load_usdt_wallet(daemon)
    req = await daemon.add_request("2.5", wallet=wallet.wallet_key)
    assert req["URI"] == f"solana:{OWNER}?amount=2.5&spl-token={sol.USDT_MINT}"
    assert Decimal(req["amount_USDT"]) == Decimal("2.5")
    assert await daemon.setrequestaddress(req["request_id"], SENDER, wallet=wallet.wallet_key)
    rpc.add_signature(SIG)
    rpc.transactions[SIG] = usdt_transfer(amount=2_500_000)
    rpc.statuses[SIG] = {"confirmationStatus": "confirmed", "confirmations": 3, "err": None}
    await daemon.poll_wallet(wallet.wallet_key)
    got = await daemon.getrequest(req["request_id"], wallet=wallet.wallet_key)
    assert got["status"] == PR_PAID
    assert Decimal(got["sent_amount"]) == Decimal("2.5")
    assert got["tx_hashes"] == [SIG]
    assert got["confirmations"] == 32  # recorded on the request, so known final without asking the RPC
    events = [u["event"] for u in daemon.get_updates(wallet=wallet.wallet_key)]
    assert events == ["new_transaction", "new_payment"]
    # duplicate poll of the same signature must not credit twice
    rpc.signatures.insert(0, dict(rpc.signatures[0]))
    wallet.last_signature = None
    await daemon.poll_wallet(wallet.wallet_key)
    assert Decimal((await daemon.getrequest(req["request_id"], wallet=wallet.wallet_key))["sent_amount"]) == Decimal("2.5")


async def test_partial_then_full(daemon, rpc):
    wallet = await load_usdt_wallet(daemon)
    req = await daemon.add_request("3", wallet=wallet.wallet_key)
    await daemon.setrequestaddress(req["request_id"], SENDER, wallet=wallet.wallet_key)
    rpc.add_signature(SIG)
    rpc.transactions[SIG] = usdt_transfer(amount=1_000_000)
    await daemon.poll_wallet(wallet.wallet_key)
    assert wallet.get_request(req["request_id"]).status == PR_UNPAID
    assert wallet.get_request(req["request_id"]).sent_amount == Decimal("1")
    rpc.add_signature(SIG2)
    rpc.transactions[SIG2] = usdt_transfer(signature=SIG2, amount=2_000_000, before=1_000_000)
    await daemon.poll_wallet(wallet.wallet_key)
    assert wallet.get_request(req["request_id"]).status == PR_PAID
    assert wallet.get_request(req["request_id"]).tx_hashes == [SIG, SIG2]


async def test_spoof_token_leaves_invoice_unpaid(daemon, rpc):
    wallet = await load_usdt_wallet(daemon)
    req = await daemon.add_request("5", wallet=wallet.wallet_key)
    await daemon.setrequestaddress(req["request_id"], SENDER, wallet=wallet.wallet_key)
    fake_mint = OWNER2
    fake_ata = sol.derive_ata(OWNER, fake_mint)
    rpc.add_signature(SIG, address=ATA)
    rpc.transactions[SIG] = make_tx(
        keys=(SENDER, SENDER_ATA, fake_ata, ATA),
        pre=(token_balance(1, SENDER, 9_000_000, fake_mint), token_balance(3, OWNER, 0)),
        post=(
            token_balance(1, SENDER, 4_000_000, fake_mint),
            token_balance(2, OWNER, 5_000_000, fake_mint),
            token_balance(3, OWNER, 0),
        ),
    )
    await daemon.poll_wallet(wallet.wallet_key)
    assert wallet.get_request(req["request_id"]).status == PR_UNPAID
    assert daemon.get_updates(wallet=wallet.wallet_key) == []


async def test_failed_tx_and_unknown_sender_ignored(daemon, rpc):
    wallet = await load_usdt_wallet(daemon)
    req = await daemon.add_request("1", wallet=wallet.wallet_key)
    await daemon.setrequestaddress(req["request_id"], SENDER, wallet=wallet.wallet_key)
    rpc.add_signature(SIG, err={"InstructionError": [0, "Custom"]})
    rpc.transactions[SIG] = usdt_transfer()
    rpc.add_signature(SIG2)
    rpc.transactions[SIG2] = usdt_transfer(signature=SIG2, err={"InstructionError": [0, "Custom"]})
    rpc.add_signature(SIG3)
    rpc.transactions[SIG3] = usdt_transfer(signature=SIG3, sender=OWNER2, sender_ata=ATA2)
    await daemon.poll_wallet(wallet.wallet_key)
    assert wallet.get_request(req["request_id"]).status == PR_UNPAID
    assert [u["event"] for u in daemon.get_updates(wallet=wallet.wallet_key)] == ["new_transaction"]
    assert wallet.last_signature == SIG3
    fetched = [p[0] for m, p in rpc.calls if m == "getTransaction"]
    assert fetched == [SIG2, SIG3]  # a signature already flagged err by the RPC is never fetched


async def test_wallets_are_independent(daemon, rpc):
    w1 = await load_usdt_wallet(daemon)
    w2 = await load_usdt_wallet(daemon, OWNER2)
    r1 = await daemon.add_request("1", wallet=w1.wallet_key)
    r2 = await daemon.add_request("1", wallet=w2.wallet_key)
    await daemon.setrequestaddress(r1["request_id"], SENDER, wallet=w1.wallet_key)
    await daemon.setrequestaddress(r2["request_id"], SENDER, wallet=w2.wallet_key)
    rpc.add_signature(SIG, address=ATA)
    rpc.transactions[SIG] = usdt_transfer()
    await daemon.poll_wallet(w1.wallet_key)
    await daemon.poll_wallet(w2.wallet_key)
    assert w1.get_request(r1["request_id"]).status == PR_PAID
    assert w2.get_request(r2["request_id"]).status == PR_UNPAID


async def test_native_wallet_receives_sol(daemon, rpc):
    wallet = await daemon.load_wallet(OWNER, None, diskless=True)
    assert wallet.watch_address == OWNER
    assert wallet.divisibility == 9
    req = await daemon.add_request("0.5", wallet=wallet.wallet_key)
    await daemon.setrequestaddress(req["request_id"], SENDER, wallet=wallet.wallet_key)
    rpc.add_signature(SIG, address=OWNER)
    rpc.transactions[SIG] = make_tx(keys=(SENDER, OWNER), pre_lamports=[10**9, 0], post_lamports=[10**9 // 2, 10**9 // 2])
    await daemon.poll_wallet(wallet.wallet_key)
    assert wallet.get_request(req["request_id"]).status == PR_PAID


# --- cursor / restart -----------------------------------------------------------------


async def test_multiple_signatures_processed_oldest_first_with_pagination(daemon, rpc):
    daemon.SIGNATURE_PAGE_SIZE = 2
    wallet = await load_usdt_wallet(daemon)
    req = await daemon.add_request("6", wallet=wallet.wallet_key)
    await daemon.setrequestaddress(req["request_id"], SENDER, wallet=wallet.wallet_key)
    sigs = [SIG, SIG2, SIG3, "4" + SIG[1:], "5" + SIG[1:]]
    before = 0
    for s in sigs:
        rpc.add_signature(s)
        rpc.transactions[s] = usdt_transfer(signature=s, amount=2_000_000, before=before)
        before += 2_000_000
    await daemon.poll_wallet(wallet.wallet_key)
    fetched = [p[0] for m, p in rpc.calls if m == "getTransaction"]
    assert fetched == sigs
    assert wallet.get_request(req["request_id"]).tx_hashes == sigs[:3]
    assert wallet.get_request(req["request_id"]).status == PR_PAID
    assert wallet.last_signature == sigs[-1]


async def test_transaction_not_yet_available_is_retried(daemon, rpc):
    wallet = await load_usdt_wallet(daemon)
    await asyncio.sleep(0)  # let the backfill task scheduled by load_wallet finish first
    req = await daemon.add_request("2", wallet=wallet.wallet_key)
    await daemon.setrequestaddress(req["request_id"], SENDER, wallet=wallet.wallet_key)
    rpc.calls.clear()  # the backfill and the address rescan each listed signatures once
    rpc.add_signature(SIG)
    rpc.transactions[SIG] = usdt_transfer(amount=1_000_000)
    rpc.add_signature(SIG2)  # newer, not served yet
    await daemon.poll_wallet(wallet.wallet_key)
    assert wallet.last_signature == SIG
    rpc.transactions[SIG2] = usdt_transfer(signature=SIG2, amount=1_000_000, before=1_000_000)
    await daemon.poll_wallet(wallet.wallet_key)
    assert wallet.last_signature == SIG2
    assert wallet.get_request(req["request_id"]).status == PR_PAID
    until = [p[1].get("until") for m, p in rpc.calls if m == "getSignaturesForAddress"]
    assert until == [None, SIG]


async def test_restart_resumes_from_cursor_without_gap_or_double_credit(daemon, rpc, tmp_path):
    wallet = await load_usdt_wallet(daemon, diskless=False)
    req = await daemon.add_request("2", wallet=wallet.wallet_key)
    await daemon.setrequestaddress(req["request_id"], SENDER, wallet=wallet.wallet_key)
    rpc.add_signature(SIG)
    rpc.transactions[SIG] = usdt_transfer(amount=1_000_000)
    await daemon.poll_wallet(wallet.wallet_key)
    key = wallet.wallet_key
    # "restart": drop the in-memory wallet, a payment lands meanwhile, wallet is loaded again from disk
    await daemon.close_wallet_impl(key)
    rpc.add_signature(SIG2)
    rpc.transactions[SIG2] = usdt_transfer(signature=SIG2, amount=1_000_000, before=1_000_000)
    wallet = await load_usdt_wallet(daemon, diskless=False)
    assert wallet.last_signature == SIG
    await asyncio.sleep(0.05)  # backfill task scheduled by load_wallet
    await daemon.poll_wallet(key)
    got = wallet.get_request(req["request_id"])
    assert got.status == PR_PAID
    assert got.tx_hashes == [SIG, SIG2]
    assert got.sent_amount == Decimal("2")
    assert wallet.last_signature == SIG2
    assert json.loads(Path(wallet.storage.path).read_text())["last_signature"] == SIG2


async def test_first_load_bounded_by_time_window(daemon, rpc, monkeypatch):
    daemon.MAX_SYNC_SECONDS = 3600
    monkeypatch.setattr(sol.time, "time", lambda: 1790141871 + 100)
    wallet = await load_usdt_wallet(daemon)
    rpc.add_signature(SIG, block_time=1790141871 - 7200)
    rpc.add_signature(SIG2, block_time=1790141871)
    rpc.transactions[SIG2] = usdt_transfer(signature=SIG2)
    await daemon.poll_wallet(wallet.wallet_key)
    fetched = [p[0] for m, p in rpc.calls if m == "getTransaction"]
    assert fetched == [SIG2]
    assert wallet.last_signature == SIG2


async def test_expired_request_not_paid(daemon, rpc):
    wallet = await load_usdt_wallet(daemon)
    req = await daemon.add_request("1", expiration=1, wallet=wallet.wallet_key)
    await daemon.setrequestaddress(req["request_id"], SENDER, wallet=wallet.wallet_key)
    wallet.set_request_status(req["request_id"], PR_EXPIRED)
    rpc.add_signature(SIG)
    rpc.transactions[SIG] = usdt_transfer()
    await daemon.poll_wallet(wallet.wallet_key)
    assert wallet.get_request(req["request_id"]).status == PR_EXPIRED


# --- balance / confirmations / RPC failures ------------------------------------------


async def test_balance(daemon, rpc):
    wallet = await load_usdt_wallet(daemon)
    assert await wallet.balance() == Decimal(0)
    rpc.token_balances[ATA] = "12345678"
    assert await wallet.balance() == Decimal("12.345678")
    assert (await daemon.getbalance(wallet=wallet.wallet_key))["confirmed"] == "12.345678"
    assert await daemon.readcontract(sol.USDT_MINT, "balanceOf", OWNER) == 12345678
    rpc.failures["getAccountInfo"] = sol.SolanaRPCError({"code": 429, "message": "Too many requests"})
    with pytest.raises(sol.SolanaRPCError):
        await wallet.balance()


async def test_confirmations_mapping(daemon, rpc):
    rpc.statuses[SIG] = None
    assert await daemon.coin.get_confirmations(SIG) == 0
    rpc.statuses[SIG] = {"confirmationStatus": "processed", "confirmations": 0, "err": None}
    assert await daemon.coin.get_confirmations(SIG) == 0
    rpc.statuses[SIG] = {"confirmationStatus": "confirmed", "confirmations": 0, "err": None}
    assert await daemon.coin.get_confirmations(SIG) == 1
    rpc.statuses[SIG] = {"confirmationStatus": "finalized", "confirmations": None, "err": None}
    assert await daemon.coin.get_confirmations(SIG) == 32
    rpc.statuses[SIG] = {"confirmationStatus": "finalized", "confirmations": None, "err": {"x": 1}}
    assert await daemon.coin.get_confirmations(SIG) == 0


async def test_rpc_failure_keeps_state(daemon, rpc):
    wallet = await load_usdt_wallet(daemon)
    req = await daemon.add_request("1", wallet=wallet.wallet_key)
    await daemon.setrequestaddress(req["request_id"], SENDER, wallet=wallet.wallet_key)
    rpc.add_signature(SIG)
    rpc.transactions[SIG] = usdt_transfer()
    rpc.failures["getSignaturesForAddress"] = TimeoutError()
    await daemon.poll_wallet(wallet.wallet_key)
    assert wallet.last_signature is None
    rpc.failures = {"getTransaction": sol.SolanaRPCError({"code": -32005, "message": "Node is behind"})}
    await daemon.poll_wallet(wallet.wallet_key)
    assert wallet.last_signature is None
    rpc.failures = {}
    await daemon.poll_wallet(wallet.wallet_key)
    assert wallet.get_request(req["request_id"]).status == PR_PAID


async def test_process_pending_survives_errors(daemon, rpc):
    daemon.BLOCK_TIME = 0
    rpc.failures["getSlot"] = ConnectionError()
    daemon.running = True

    async def stop_soon():
        await asyncio.sleep(0.02)
        daemon.running = False

    await asyncio.gather(daemon.process_pending(), stop_soon())
    assert daemon.synchronized is False


async def test_getinfo_redacts_rpc_key(daemon):
    info = await daemon.getinfo()
    assert info["server"] == "https://rpc.example"
    assert "key123" not in str(info)
    assert info["connected"] is True


async def test_http_provider_failover(unused_tcp_port_factory, monkeypatch):
    from aiohttp import web

    monkeypatch.setattr(sol, "RETRY_DELAYS", (0,))

    calls = {"bad": 0, "good": 0}

    async def bad(request):
        calls["bad"] += 1
        return web.Response(status=503)

    async def good(request):
        calls["good"] += 1
        body = await request.json()
        if body["method"] == "getHealth":
            return web.json_response({"jsonrpc": "2.0", "id": 1, "result": "ok"})
        return web.json_response({"jsonrpc": "2.0", "id": 1, "result": 42})

    async def malformed(request):
        return web.Response(text="<html>rate limited</html>", content_type="text/html")

    ports = [unused_tcp_port_factory() for _ in range(3)]
    runners = []
    for port, handler in zip(ports, (bad, malformed, good), strict=True):
        app = web.Application()
        app.router.add_post("/", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        runners.append(runner)
    providers = [sol.SolanaRPCProvider(f"http://127.0.0.1:{p}/") for p in ports]
    multi = sol.MultipleProviderRPC(providers)
    coin = sol.SOLFeatures(multi)
    try:
        assert await coin.get_block_number() == 42
        assert calls == {"bad": 2, "good": 1}  # one bounded retry, then the next provider
        for _ in range(6):
            await coin.get_block_number()
        assert coin.current_server() == "http://127.0.0.1"
        assert multi.current_rpc_idx == 2
    finally:
        for p in providers:
            await p.close()
        for r in runners:
            await r.cleanup()


class TestFakeRPCSanity:
    def test_pagination_helper(self):
        rpc = FakeRPC()
        for s in (SIG, SIG2, SIG3):
            rpc.add_signature(s)
        page = rpc._signatures(ATA, {"limit": 2})
        assert [e["signature"] for e in page] == [SIG3, SIG2]
        page = rpc._signatures(ATA, {"limit": 2, "before": SIG2})
        assert [e["signature"] for e in page] == [SIG]
        page = rpc._signatures(ATA, {"limit": 10, "until": SIG})
        assert [e["signature"] for e in page] == [SIG3, SIG2]


# --- audit regressions ------------------------------------------------------------------


def test_sender_is_signing_owner_not_token_account():
    credit, sender = sol.parse_token_credit(usdt_transfer(), OWNER, sol.USDT_MINT)
    assert sender == SENDER
    assert sender != SENDER_ATA


def test_sender_from_non_canonical_source_account():
    tx = usdt_transfer(sender_ata=OWNER2)  # any token account the payer owns, not their ATA
    assert sol.parse_token_credit(tx, OWNER, sol.USDT_MINT) == (1_000_000, SENDER)


def test_pool_that_lost_balance_is_not_a_sender():
    pool = OWNER2
    tx = make_tx(
        keys=(SENDER, ATA2, ATA),
        pre=(token_balance(1, pool, 9_000_000),),
        post=(token_balance(1, pool, 8_000_000), token_balance(2, OWNER, 1_000_000)),
    )
    assert sol.parse_token_credit(tx, OWNER, sol.USDT_MINT) == (1_000_000, None)


def test_sender_found_when_source_account_closed():
    tx = make_tx(pre=(token_balance(1, SENDER, 1_000_000),), post=(token_balance(2, OWNER, 1_000_000),))
    assert sol.parse_token_credit(tx, OWNER, sol.USDT_MINT) == (1_000_000, SENDER)


def test_two_incoming_and_incoming_plus_outgoing_net():
    tx = make_tx(
        keys=(SENDER, SENDER_ATA, ATA, OWNER2, ATA2),
        signers=1,
        pre=(token_balance(1, SENDER, 5), token_balance(2, OWNER, 10), token_balance(4, OWNER2, 5)),
        post=(token_balance(1, SENDER, 2), token_balance(2, OWNER, 15), token_balance(4, OWNER2, 3)),
    )
    assert sol.parse_token_credit(tx, OWNER, sol.USDT_MINT) == (5, SENDER)
    mixed = make_tx(
        pre=(token_balance(1, SENDER, 9), token_balance(2, OWNER, 10)),
        post=(token_balance(1, SENDER, 5), token_balance(2, OWNER, 12)),
    )
    assert sol.parse_token_credit(mixed, OWNER, sol.USDT_MINT) == (2, SENDER)


def test_owner_label_absent_or_wrong_does_not_matter_address_binds():
    tx = usdt_transfer()
    del tx["meta"]["postTokenBalances"][1]["owner"]
    assert sol.parse_token_credit(tx, OWNER, sol.USDT_MINT) == (1_000_000, SENDER)
    tx = make_tx(keys=(SENDER, SENDER_ATA, ATA2), post=(token_balance(2, OWNER, 5),))  # owner label lies, account is not ours
    assert sol.parse_token_credit(tx, OWNER, sol.USDT_MINT) == (None, None)


def test_negative_account_index_ignored():
    tx = make_tx(post=(token_balance(-1, OWNER, 5), token_balance(2, OWNER, 0)))
    assert sol.parse_token_credit(tx, OWNER, sol.USDT_MINT) == (None, None)


def test_native_sender_is_signer_who_paid():
    tx = make_tx(keys=(SENDER, OWNER2, OWNER), signers=2, pre_lamports=[10, 100, 0], post_lamports=[9, 50, 50])
    assert sol.parse_native_credit(tx, OWNER) == (50, OWNER2)


async def test_sender_address_cannot_be_hijacked(daemon):
    wallet = await load_usdt_wallet(daemon)
    a = await daemon.add_request("1", wallet=wallet.wallet_key)
    b = await daemon.add_request("2", wallet=wallet.wallet_key)
    assert await daemon.setrequestaddress(a["request_id"], SENDER, wallet=wallet.wallet_key)
    with pytest.raises(Exception, match=sol.SENDER_IN_USE):
        await daemon.setrequestaddress(b["request_id"], SENDER, wallet=wallet.wallet_key)
    assert wallet.request_addresses[SENDER] == a["request_id"]
    wallet.set_request_status(a["request_id"], PR_EXPIRED)
    assert SENDER not in wallet.request_addresses
    assert await daemon.setrequestaddress(b["request_id"], SENDER, wallet=wallet.wallet_key)
    wallet.set_request_status(a["request_id"], PR_UNPAID)
    wallet.set_request_status(a["request_id"], PR_EXPIRED)  # a stale request expiring must not unmap b
    assert wallet.request_addresses[SENDER] == b["request_id"]


async def test_transaction_before_request_never_pays_it(daemon, rpc):
    wallet = await load_usdt_wallet(daemon)
    req = await daemon.add_request("1", wallet=wallet.wallet_key)
    await daemon.setrequestaddress(req["request_id"], SENDER, wallet=wallet.wallet_key)
    rpc.add_signature(SIG)
    rpc.transactions[SIG] = usdt_transfer(slot=rpc.slot - 1)
    await daemon.poll_wallet(wallet.wallet_key)
    assert wallet.get_request(req["request_id"]).status == PR_UNPAID
    assert [u["event"] for u in daemon.get_updates(wallet=wallet.wallet_key)] == ["new_transaction"]
    rpc.add_signature(SIG2)
    rpc.transactions[SIG2] = usdt_transfer(signature=SIG2, slot=rpc.slot)
    await daemon.poll_wallet(wallet.wallet_key)
    assert wallet.get_request(req["request_id"]).status == PR_PAID


async def test_replay_after_lost_cursor_does_not_pay_new_request(daemon, rpc):
    wallet = await load_usdt_wallet(daemon)
    a = await daemon.add_request("1", wallet=wallet.wallet_key)
    await daemon.setrequestaddress(a["request_id"], SENDER, wallet=wallet.wallet_key)
    rpc.add_signature(SIG)
    rpc.transactions[SIG] = usdt_transfer()
    await daemon.poll_wallet(wallet.wallet_key)
    assert wallet.get_request(a["request_id"]).status == PR_PAID
    rpc.slot += 1000
    b = await daemon.add_request("1", wallet=wallet.wallet_key)
    await daemon.setrequestaddress(b["request_id"], SENDER, wallet=wallet.wallet_key)
    wallet.last_signature = None  # provider that does not know the cursor replays history
    await daemon.poll_wallet(wallet.wallet_key)
    assert wallet.get_request(b["request_id"]).status == PR_UNPAID


async def test_pagination_cap_never_skips(daemon, rpc):
    daemon.SIGNATURE_PAGE_SIZE = 2
    daemon.MAX_SIGNATURE_PAGES = 1
    wallet = await load_usdt_wallet(daemon)
    await asyncio.sleep(0)  # let the backfill task scheduled by load_wallet finish first
    rpc.add_signature(SIG0)
    wallet.last_signature = SIG0  # an existing cursor: the gap after it must never be skipped
    rpc.calls.clear()
    for s in (SIG, SIG2, SIG3):
        rpc.add_signature(s)
        rpc.transactions[s] = usdt_transfer(signature=s)
    await daemon.poll_wallet(wallet.wallet_key)
    assert wallet.last_signature == SIG0
    assert not [c for c in rpc.calls if c[0] == "getTransaction"]
    daemon.MAX_SIGNATURE_PAGES = 5
    await daemon.poll_wallet(wallet.wallet_key)
    assert wallet.last_signature == SIG3


async def test_hash_mismatch_stalls_without_skip(daemon, rpc):
    wallet = await load_usdt_wallet(daemon)
    rpc.add_signature(SIG)
    rpc.transactions[SIG] = usdt_transfer(signature=SIG2)
    for _ in range(sol.STALL_WARN_POLLS):
        await daemon.poll_wallet(wallet.wallet_key)
    assert wallet.last_signature is None
    assert daemon.stalled[wallet.wallet_key] == (SIG, sol.STALL_WARN_POLLS)


async def test_getservers_redacted(daemon):
    assert daemon.getservers() == ["https://rpc.example"]


async def test_finalized_commitment_everywhere(daemon, rpc):
    wallet = await load_usdt_wallet(daemon)
    await wallet.balance()
    await daemon.coin.get_block_number()
    rpc.add_signature(SIG)
    rpc.transactions[SIG] = usdt_transfer()
    await daemon.poll_wallet(wallet.wallet_key)
    for method, params in rpc.calls:
        if method in ("getSlot", "getAccountInfo", "getSignaturesForAddress", "getTransaction"):
            assert params[-1]["commitment"] == "finalized", method


async def test_429_then_success_and_no_secret_in_logs(unused_tcp_port_factory, monkeypatch, caplog):
    from aiohttp import web

    monkeypatch.setattr(sol, "RETRY_DELAYS", (0, 0))
    hits = []

    async def flaky(request):
        hits.append(1)
        if len(hits) == 1:
            return web.Response(status=429)
        if len(hits) == 2:
            return web.Response(text="<html>", content_type="text/html")
        return web.json_response({"jsonrpc": "2.0", "id": 1, "result": 7})

    async def dead(request):
        return web.Response(status=503)

    port, port2 = unused_tcp_port_factory(), unused_tcp_port_factory()
    runners = []
    for p, h in ((port, flaky), (port2, dead)):
        app = web.Application()
        app.router.add_post("/{tail:.*}", h)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", p).start()
        runners.append(runner)
    provider = sol.SolanaRPCProvider(f"http://127.0.0.1:{port}/?api-key=SECRET123")
    dead_provider = sol.SolanaRPCProvider(f"http://127.0.0.1:{port2}/v1/SECRET456")
    try:
        with caplog.at_level("DEBUG"):
            assert await provider.send_single_request("getSlot", []) == 7
            with pytest.raises(sol.SolanaTransientError) as exc:
                await dead_provider.send_single_request("getSlot", [])
        assert len(hits) == 3
        assert "SECRET456" not in str(exc.value) and "SECRET456" not in repr(exc.value)
        ours = "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith("aiohttp"))
        assert "SECRET123" not in ours and "SECRET456" not in ours
        assert "retry 1" in ours
    finally:
        await provider.close()
        await dead_provider.close()
        for r in runners:
            await r.cleanup()


async def test_unreachable_host_error_has_no_url(unused_tcp_port_factory):
    provider = sol.SolanaRPCProvider(f"http://127.0.0.1:{unused_tcp_port_factory()}/?api-key=SECRET789")
    with pytest.raises(sol.SolanaTransientError) as exc:
        await provider._post("getSlot", [])
    assert "SECRET789" not in str(exc.value)
    await provider.close()


async def test_confirmations_for_recorded_hash_need_no_rpc(daemon, rpc):
    wallet = await load_usdt_wallet(daemon)
    req = await daemon.add_request("1", wallet=wallet.wallet_key)
    await daemon.setrequestaddress(req["request_id"], SENDER, wallet=wallet.wallet_key)
    rpc.add_signature(SIG)
    rpc.transactions[SIG] = usdt_transfer()
    await daemon.poll_wallet(wallet.wallet_key)
    rpc.failures["getSignatureStatuses"] = TimeoutError()
    assert (await daemon.get_tx_status(SIG))["confirmations"] == 32
    assert (await daemon.getrequest(req["request_id"], wallet=wallet.wallet_key))["confirmations"] == 32


async def test_no_custody_paths(daemon):
    wallet = await load_usdt_wallet(daemon)
    for call in (
        lambda: daemon.payto(OWNER2, "1", wallet=wallet.wallet_key),
        lambda: daemon.transfer(sol.USDT_MINT, OWNER2, "1", wallet=wallet.wallet_key),
        lambda: daemon.broadcast("00", wallet=wallet.wallet_key),
        lambda: daemon.get_default_fee("00", wallet=wallet.wallet_key),
    ):
        with pytest.raises(NotImplementedError, match="Currently not supported"):
            await call()
    for sync_call in (
        lambda: daemon.signmessage(OWNER, "m", wallet=wallet.wallet_key),
        lambda: daemon.signtransaction("00", wallet=wallet.wallet_key),
        lambda: daemon.make_seed(),
    ):
        with pytest.raises(NotImplementedError, match="Currently not supported"):
            sync_call()
    with pytest.raises(Exception, match="watching-only"):
        daemon.getprivatekeys(wallet=wallet.wallet_key)


# --- review round 2 ---------------------------------------------------------------------


async def paid_before_address(daemon, rpc, **transfer):
    """Invoice created, customer pays, the poller passes the transfer (no sender mapped yet)."""
    wallet = await load_usdt_wallet(daemon)
    await asyncio.sleep(0)  # let the backfill task scheduled by load_wallet finish first
    req = await daemon.add_request("1", wallet=wallet.wallet_key)
    rpc.add_signature(SIG)
    rpc.transactions[SIG] = usdt_transfer(**transfer)
    await daemon.poll_wallet(wallet.wallet_key)
    assert wallet.last_signature == SIG
    assert wallet.get_request(req["request_id"]).status == PR_UNPAID
    return wallet, req["request_id"]


async def test_payment_before_address_is_credited_on_setrequestaddress(daemon, rpc):
    wallet, req_id = await paid_before_address(daemon, rpc)
    assert await daemon.setrequestaddress(req_id, SENDER, wallet=wallet.wallet_key)
    got = wallet.get_request(req_id)
    assert got.status == PR_PAID
    assert got.tx_hashes == [SIG]
    assert [u["event"] for u in daemon.get_updates(wallet=wallet.wallet_key)] == ["new_transaction", "new_payment"]
    # the poller seeing the same signature again (lost cursor) must not credit twice
    wallet.last_signature = None
    await daemon.poll_wallet(wallet.wallet_key)
    assert wallet.get_request(req_id).sent_amount == Decimal("1")


async def test_rescan_ignores_other_senders(daemon, rpc):
    wallet, req_id = await paid_before_address(daemon, rpc, sender=OWNER2, sender_ata=ATA2)
    assert await daemon.setrequestaddress(req_id, SENDER, wallet=wallet.wallet_key)
    assert wallet.get_request(req_id).status == PR_UNPAID


async def test_rescan_keeps_slot_replay_guard(daemon, rpc):
    wallet = await load_usdt_wallet(daemon)
    await asyncio.sleep(0)
    rpc.add_signature(SIG)
    rpc.transactions[SIG] = usdt_transfer(slot=rpc.slot)
    rpc.slot += 100
    req = await daemon.add_request("1", wallet=wallet.wallet_key)
    rpc.calls.clear()
    assert await daemon.setrequestaddress(req["request_id"], SENDER, wallet=wallet.wallet_key)
    assert [p[0] for m, p in rpc.calls if m == "getTransaction"] == []  # the walk stops at the request's slot
    assert wallet.get_request(req["request_id"]).status == PR_UNPAID
    rpc.add_signature(SIG2)  # listed at the request's slot, but the transaction itself predates it
    rpc.transactions[SIG2] = usdt_transfer(signature=SIG2, slot=rpc.slot - 1)
    assert await daemon.setrequestaddress(req["request_id"], SENDER, wallet=wallet.wallet_key)  # same request again
    assert [p[0] for m, p in rpc.calls if m == "getTransaction"] == [SIG2]
    assert wallet.get_request(req["request_id"]).status == PR_UNPAID


async def test_rescan_failure_still_sets_address(daemon, rpc):
    wallet, req_id = await paid_before_address(daemon, rpc)
    rpc.failures["getSignaturesForAddress"] = TimeoutError()
    assert await daemon.setrequestaddress(req_id, SENDER, wallet=wallet.wallet_key)
    assert wallet.request_addresses[SENDER] == req_id
    assert wallet.get_request(req_id).status == PR_UNPAID
    rpc.failures = {}
    rpc.add_signature(SIG2)  # a later transfer is still matched by the poller
    rpc.transactions[SIG2] = usdt_transfer(signature=SIG2, before=1_000_000)
    await daemon.poll_wallet(wallet.wallet_key)
    assert wallet.get_request(req_id).status == PR_PAID


async def test_sender_lock_error_reaches_backend_as_text(daemon):
    wallet = await load_usdt_wallet(daemon)
    a = await daemon.add_request("1", wallet=wallet.wallet_key)
    b = await daemon.add_request("1", wallet=wallet.wallet_key)
    await daemon.setrequestaddress(a["request_id"], SENDER, wallet=wallet.wallet_key)
    with pytest.raises(Exception, match=sol.SENDER_IN_USE) as exc:
        await daemon.setrequestaddress(b["request_id"], SENDER, wallet=wallet.wallet_key)
    # No spec code may claim this text: the SDK then raises UnknownError carrying the daemon's message,
    # which backend/forked/solana/plugin.py matches. A spec code would replace it with a fixed docstring.
    message = daemon.get_exception_message(exc.value)
    assert str(daemon.get_error_code(message)) not in daemon.spec["exceptions"]
    assert b["request_id"] != wallet.request_addresses[SENDER]


async def test_rescan_blocks_not_supported(daemon, rpc):
    with pytest.raises(NotImplementedError, match="not supported for Solana"):
        await daemon.rescan_blocks(1, 10)
    assert rpc.calls == []  # no new_block events, no slot fetches


async def test_first_load_over_page_cap_starts_at_newest(daemon, rpc, caplog):
    daemon.SIGNATURE_PAGE_SIZE = 2
    daemon.MAX_SIGNATURE_PAGES = 1
    for s in (SIG, SIG2, SIG3):
        rpc.add_signature(s)
    with caplog.at_level("WARNING"):
        wallet = await load_usdt_wallet(daemon)
        await asyncio.sleep(0)  # the first-load poll scheduled by load_wallet
    assert wallet.last_signature == SIG3
    assert not [c for c in rpc.calls if c[0] == "getTransaction"]
    assert "starting from the newest" in caplog.text
    rpc.calls.clear()
    await daemon.poll_wallet(wallet.wallet_key)  # later ticks resume from the cursor
    assert [p[1].get("until") for m, p in rpc.calls if m == "getSignaturesForAddress"] == [SIG3]


async def test_first_load_over_page_cap_keeps_open_request_window(daemon, rpc):
    wallet = await load_usdt_wallet(daemon)
    await asyncio.sleep(0)  # nothing on the account yet: no cursor
    assert wallet.last_signature is None
    req = await daemon.add_request("2", wallet=wallet.wallet_key)
    await daemon.setrequestaddress(req["request_id"], SENDER, wallet=wallet.wallet_key)
    daemon.SIGNATURE_PAGE_SIZE = 3
    daemon.MAX_SIGNATURE_PAGES = 1
    rpc.add_signature(SIG0)
    rpc.add_signature(SIG)
    rpc.signatures[0]["slot"] = rpc.slot - 1  # before the request
    rpc.add_signature(SIG2)
    rpc.add_signature(SIG3)
    rpc.transactions[SIG2] = usdt_transfer(signature=SIG2)
    rpc.transactions[SIG3] = usdt_transfer(signature=SIG3, before=1_000_000)
    rpc.calls.clear()
    await daemon.poll_wallet(wallet.wallet_key)
    assert [p[0] for m, p in rpc.calls if m == "getTransaction"] == [SIG2, SIG3]
    assert wallet.last_signature == SIG3
    assert wallet.get_request(req["request_id"]).status == PR_PAID


async def test_missing_ata_balance_is_zero_without_provider_failure(daemon, rpc):
    class Provider:
        url = "https://rpc.example"

        async def send_single_request(self, method, params):
            return await rpc.send_request(method, params)

    multi = sol.MultipleProviderRPC([Provider(), Provider()])
    daemon.coin = sol.SOLFeatures(multi)
    wallet = await load_usdt_wallet(daemon)
    assert await wallet.balance() == Decimal(0)
    assert await daemon.readcontract(sol.USDT_MINT, "balanceOf", OWNER) == 0
    assert multi.failed_stats == [0, 0]
    assert multi.current_rpc_idx == 0
    assert "getTokenAccountBalance" not in [m for m, _ in rpc.calls]


async def test_unexpected_account_data_is_an_error(daemon, rpc):
    wallet = await load_usdt_wallet(daemon)
    original = rpc.send_request

    async def odd(method, params):
        if method == "getAccountInfo":
            return {"value": {"owner": sol.TOKEN_PROGRAM, "data": ["AAAA", "base64"]}}
        return await original(method, params)

    rpc.send_request = odd
    with pytest.raises(sol.SolanaRPCError, match="Unexpected account data"):
        await wallet.balance()
