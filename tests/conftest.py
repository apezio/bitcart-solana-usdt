import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

DAEMONS_DIR = os.environ.get("BITCART_DAEMONS_DIR", "/home/electrum/site/daemons")
sys.path.insert(0, DAEMONS_DIR)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "daemon"))

import sol  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"

OWNER = "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9"
ATA = "CyBjGpte4Npi5zNkdtWumPxVW4kpMR8BuFSbA587xZES"  # verified on mainnet via getTokenAccountsByOwner
OWNER2 = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
ATA2 = "TB5FCqbNsnuLQgEjUuPaT9qtVPTT4U1A8rvi7qzEj2M"
SENDER = "6XTtLubirAzv7Z3HN3EbhUcTuuVwfpD1receteNGBncK"
SENDER_ATA = "28BCL6ygj1DkqyN7goCt7yjHXb8V3XMT82t9QQzadkmQ"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SIG = "3Gj8QFqitaVK8nknmCKc3tE8DsXvETe9jtuHvuq5mip7zZPNQQmhokE93d1qh2kb9LgXpsp8GSMtCdfNvE7X5ae3"


def load_fixture(name):
    with open(FIXTURES / name) as f:
        return json.load(f)


def token_balance(index, account_owner, amount, mint=sol.USDT_MINT, decimals=6):
    return {
        "accountIndex": index,
        "mint": mint,
        "owner": account_owner,
        "programId": sol.TOKEN_PROGRAM,
        "uiTokenAmount": {"amount": str(amount), "decimals": decimals, "uiAmount": None, "uiAmountString": str(amount)},
    }


def make_tx(
    signature=SIG,
    keys=(SENDER, SENDER_ATA, ATA),
    pre=(),
    post=(),
    err=None,
    pre_lamports=None,
    post_lamports=None,
    signers=1,
    loaded=None,
    slot=449610391,
):
    keys = list(keys)
    account_keys = [
        {"pubkey": k, "signer": i < signers, "source": "transaction", "writable": True} for i, k in enumerate(keys)
    ]
    return {
        "slot": slot,
        "blockTime": 1790141871,
        "meta": {
            "err": err,
            "fee": 5000,
            "preBalances": list(pre_lamports or [0] * len(keys)),
            "postBalances": list(post_lamports or [0] * len(keys)),
            "preTokenBalances": list(pre),
            "postTokenBalances": list(post),
            "loadedAddresses": loaded or {"readonly": [], "writable": []},
            "innerInstructions": [],
        },
        "transaction": {"message": {"accountKeys": account_keys}, "signatures": [signature]},
    }


def usdt_transfer(
    signature=SIG,
    amount=1_000_000,
    before=0,
    sender=SENDER,
    sender_ata=SENDER_ATA,
    mint=sol.USDT_MINT,
    err=None,
    slot=449610391,
):
    return make_tx(
        signature=signature,
        err=err,
        slot=slot,
        keys=(sender, sender_ata, ATA),
        pre=(token_balance(1, sender, 10 * amount, mint), token_balance(2, OWNER, before, mint)),
        post=(token_balance(1, sender, 9 * amount, mint), token_balance(2, OWNER, before + amount, mint)),
    )


class FakeRPC:
    """Stands in for MultipleProviderRPC: canned responses keyed by method, recorded calls."""

    def __init__(self):
        self.slot = 449610000  # below every fixture slot, so requests never postdate the transactions
        self.signatures = []  # newest first, as the RPC returns them
        self.transactions = {}
        self.statuses = {}
        self.token_balances = {}
        self.failures = {}
        self.calls = []
        self.current_rpc = type("P", (), {"url": "https://rpc.example/key123"})()
        self.providers = []

    async def send_request(self, method, params):
        self.calls.append((method, params))
        if method in self.failures:
            raise self.failures[method]
        if method == "getSlot":
            return self.slot
        if method == "getHealth":
            return "ok"
        if method == "getSignaturesForAddress":
            return self._signatures(params[0], params[1])
        if method == "getTransaction":
            return self.transactions.get(params[0])
        if method == "getSignatureStatuses":
            return {"value": [self.statuses.get(sig) for sig in params[0]]}
        if method == "getAccountInfo":
            if params[0] not in self.token_balances:
                return {"context": {"slot": self.slot}, "value": None}  # account does not exist
            info = {"mint": sol.USDT_MINT, "tokenAmount": {"amount": self.token_balances[params[0]], "decimals": 6}}
            return {"value": {"owner": sol.TOKEN_PROGRAM, "data": {"program": "spl-token", "parsed": {"info": info}}}}
        if method == "getBalance":
            return {"value": self.token_balances.get(params[0], 0)}
        raise AssertionError(f"unexpected RPC {method}")

    def _signatures(self, address, opts):
        entries = [e for e in self.signatures if e.get("address", ATA) == address]
        if "before" in opts:
            idx = next(i for i, e in enumerate(entries) if e["signature"] == opts["before"])
            entries = entries[idx + 1 :]
        if "until" in opts:
            cut = [i for i, e in enumerate(entries) if e["signature"] == opts["until"]]
            if cut:
                entries = entries[: cut[0]]
        return [{k: v for k, v in e.items() if k != "address"} for e in entries[: opts["limit"]]]

    def add_signature(self, signature, err=None, block_time=None, address=ATA, slot=None):
        # default blockTime is "now": a fixed date falls out of the MAX_SYNC_HOURS window as the suite ages
        entry = {"signature": signature, "err": err, "slot": slot or self.slot, "blockTime": block_time or int(time.time())}
        self.signatures.insert(0, {**entry, "address": address})

    async def stop(self):
        pass


@pytest.fixture
def rpc():
    return FakeRPC()


@pytest.fixture
async def daemon(tmp_path, monkeypatch, rpc):
    monkeypatch.setenv("SOL_DATA_PATH", str(tmp_path))
    monkeypatch.setenv("SOL_NETWORK", "mainnet")
    monkeypatch.setenv("SOL_SERVER", "https://rpc.example/key123")
    monkeypatch.setenv("SOL_POLLING_CAP", "100")
    monkeypatch.chdir(Path(DAEMONS_DIR).parent)  # spec files are opened relative to the bitcart root
    d = sol.SOLDaemon()
    d.coin = sol.SOLFeatures(rpc)
    d.loop = asyncio.get_running_loop()
    return d


async def load_usdt_wallet(daemon, owner=OWNER, diskless=True):
    return await daemon.load_wallet(owner, sol.USDT_MINT, diskless=diskless)
