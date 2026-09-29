import asyncio
import hashlib
import json
import os
import time
import traceback
from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from urllib.parse import urlparse

from aiohttp import ClientError, ClientResponseError, ClientSession, ClientTimeout
from genericprocessor import (
    NOOP_PATH,
    PR_UNPAID,
    BlockchainFeatures,
    BlockProcessorDaemon,
    WalletDB,
    daemon_ctx,
    from_wei,
    str_to_bool,
)
from genericprocessor import KeyStore as BaseKeyStore
from genericprocessor import Transaction as BaseTransaction
from genericprocessor import Wallet as BaseWallet
from logger import get_logger
from storage import Storage, StoredDBProperty, decimal_to_string
from utils import AbstractRPCProvider, MultipleProviderRPC, modify_payment_url, rpc

logger = get_logger(__name__)

USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"  # noqa: S105
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
# Only canonical mainnet USDT is accepted; symbol/name metadata is never trusted.
SPL_TOKENS = {USDT_MINT: {"symbol": "USDT", "decimals": 6}}

NOT_SUPPORTED = "Currently not supported"
INVALID_CONTRACT = "Invalid contract address or non-ERC20 token"  # eth.json maps this exact text
# Matched by text in backend/forked/solana/plugin.py, which turns it into a 422 for the checkout.
SENDER_IN_USE = "This sender address is already attached to another open invoice"

# Everything is read at "finalized": a rooted transaction can never be rolled back, so a detected
# payment is final money and the invoice completes on detection (~13 s later than "confirmed").
COMMITMENT = {"commitment": "finalized"}
FINALIZED_CONFIRMATIONS = 32
RETRY_DELAYS = (1, 2, 4)
STALL_WARN_POLLS = 5
RESCAN_RETRIES = 75  # one per poll interval, about 10 minutes

RPC_URLS = {
    "mainnet": "https://api.mainnet-beta.solana.com",
    "devnet": "https://api.devnet.solana.com",
    "testnet": "https://api.testnet.solana.com",
}

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(B58)}
ED25519_P = 2**255 - 19
ED25519_D = (-121665 * pow(121666, -1, ED25519_P)) % ED25519_P


def b58decode(s):
    n = 0
    for c in s:
        n = n * 58 + _B58_INDEX[c]
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return b"\0" * (len(s) - len(s.lstrip("1"))) + body


def b58encode(b):
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + out


def is_pubkey(s):
    try:
        return isinstance(s, str) and len(b58decode(s)) == 32
    except (KeyError, AttributeError):
        return False


def is_on_curve(point):
    y = int.from_bytes(point, "little") & ((1 << 255) - 1)
    if y >= ED25519_P:
        return False
    x2 = (y * y - 1) * pow(ED25519_D * y * y + 1, -1, ED25519_P) % ED25519_P
    x = pow(x2, (ED25519_P + 3) // 8, ED25519_P)
    if (x * x - x2) % ED25519_P != 0:
        x = x * pow(2, (ED25519_P - 1) // 4, ED25519_P) % ED25519_P
    return (x * x - x2) % ED25519_P == 0


@lru_cache(maxsize=1024)
def derive_ata(owner, mint):
    seeds = b58decode(owner) + b58decode(TOKEN_PROGRAM) + b58decode(mint)
    program = b58decode(ATA_PROGRAM)
    for bump in range(255, -1, -1):
        candidate = hashlib.sha256(seeds + bytes([bump]) + program + b"ProgramDerivedAddress").digest()
        if not is_on_curve(candidate):
            return b58encode(candidate)
    raise ValueError("Unable to find a viable program address bump seed")


def format_amount(amount, divisibility):
    return f"{Decimal(amount):.{divisibility}f}".rstrip("0").rstrip(".")


def redact_url(url):
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.hostname}"


class SolanaRPCError(Exception):
    pass


class SolanaTransientError(SolanaRPCError):
    pass


class SolanaRPCProvider(AbstractRPCProvider):
    def __init__(self, url):
        self.url = url
        self.session = None

    async def send_single_request(self, method, params):
        for attempt, delay in enumerate((*RETRY_DELAYS, None)):
            try:
                return await self._post(method, params)
            except SolanaTransientError as e:
                if delay is None:
                    raise
                logger.warning(f"{redact_url(self.url)} {method}: {e}; retry {attempt + 1} in {delay}s")
                await asyncio.sleep(delay)

    async def _post(self, method, params):
        if self.session is None:
            self.session = ClientSession(timeout=ClientTimeout(total=30))
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        # aiohttp exceptions carry the full request URL (API key included); never let them escape
        try:
            async with self.session.post(self.url, json=payload) as response:
                if response.status == 429 or response.status >= 500:
                    raise SolanaTransientError(f"HTTP {response.status}")
                if response.status != 200:
                    raise SolanaRPCError(f"HTTP {response.status} for {method}")
                data = await response.json()
        except ClientResponseError as e:
            raise SolanaTransientError(f"Malformed HTTP response for {method}: {e.message}") from None
        except (ClientError, TimeoutError, OSError) as e:
            raise SolanaTransientError(f"{type(e).__name__} for {method}") from None
        if not isinstance(data, dict) or ("result" not in data and "error" not in data):
            raise SolanaTransientError(f"Malformed RPC response for {method}")
        if "error" in data:
            code = data["error"].get("code") if isinstance(data["error"], dict) else None
            if code in (429, -32005, -32016):  # rate limited / node behind / node unhealthy
                raise SolanaTransientError(data["error"])
            raise SolanaRPCError(data["error"])
        return data["result"]

    async def send_ping_request(self):
        return await self.send_single_request("getHealth", [])

    async def close(self):
        if self.session is not None:
            await self.session.close()


class SOLFeatures(BlockchainFeatures):
    def __init__(self, rpc):
        self.rpc = rpc

    async def request(self, method, *params):
        return await self.rpc.send_request(method, list(params))

    async def get_block_number(self):
        return await self.request("getSlot", COMMITMENT)

    async def is_connected(self):
        try:
            return await self.request("getHealth") == "ok"
        except Exception:
            return False

    async def get_gas_price(self):
        return 5000  # base fee in lamports per signature; only shown in getinfo

    async def get_transaction(self, tx):
        return await self.request(
            "getTransaction", tx, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, **COMMITMENT}
        )

    async def get_tx_receipt(self, tx):
        data = await self.get_transaction(tx)
        if data is None:
            raise Exception("Transaction not found")
        return {"slot": data["slot"], "blockTime": data.get("blockTime"), "err": data["meta"]["err"]}

    async def get_confirmations(self, tx_hash, data=None) -> int:
        # A hash we recorded on a request was seen finalized; no RPC (or a lagging node) can undo that.
        if daemon_ctx.get().is_recorded(tx_hash):
            return FINALIZED_CONFIRMATIONS
        statuses = await self.request("getSignatureStatuses", [tx_hash], {"searchTransactionHistory": True})
        status = statuses["value"][0]
        if not status or status.get("err") is not None:
            return 0
        if status.get("confirmationStatus") == "finalized":
            return FINALIZED_CONFIRMATIONS
        if status.get("confirmationStatus") == "confirmed":
            return 1
        return 0

    async def get_balance(self, address):
        result = await self.request("getBalance", address, COMMITMENT)
        return from_wei(int(result["value"]), SOLDaemon.DIVISIBILITY)

    async def get_token_balance(self, owner, mint):
        # getAccountInfo answers value=null for an ATA that does not exist yet (nothing received so far).
        # getTokenAccountBalance raises there instead, which MultipleProviderRPC counts as a provider failure.
        ata = derive_ata(owner, mint)
        result = await self.request("getAccountInfo", ata, {"encoding": "jsonParsed", **COMMITMENT})
        if result["value"] is None:
            return Decimal(0)
        try:
            amount = result["value"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]
        except (KeyError, TypeError):
            raise SolanaRPCError(f"Unexpected account data for {ata}") from None
        return from_wei(int(amount), SPL_TOKENS[mint]["decimals"])

    async def get_block(self, block, *args, **kwargs):
        raise NotImplementedError(NOT_SUPPORTED)

    async def get_block_txes(self, block):
        raise NotImplementedError(NOT_SUPPORTED)

    def is_address(self, address):
        return is_pubkey(address)

    def normalize_address(self, address):
        if not is_pubkey(address):
            raise Exception("Invalid address")
        return address

    async def get_payment_uri(self, address, amount, divisibility, contract=None):
        # Solana Pay: recipient is the owner; the wallet derives the token account itself
        url = f"solana:{address}"
        params = []
        if amount:
            params.append(f"amount={format_amount(amount, divisibility)}")
        if contract:
            params.append(f"spl-token={contract}")
        if params:
            url += "?" + "&".join(params)
        return url

    async def process_tx_data(self, data):
        raise NotImplementedError(NOT_SUPPORTED)

    def get_tx_hash(self, tx_data):
        return tx_data["transaction"]["signatures"][0]

    def get_wallet_key(self, xpub, contract=None, **extra_params):
        return f"{xpub}_{contract}" if contract else xpub

    def to_dict(self, obj):
        return json.loads(json.dumps(obj, default=str))

    def current_server(self):
        return redact_url(self.rpc.current_rpc.url)


@dataclass
class Transaction(BaseTransaction):
    slot: int = None


def account_keys(data):
    message = data["transaction"]["message"]
    keys = [key["pubkey"] for key in message["accountKeys"]]
    loaded = data["meta"].get("loadedAddresses") or {}
    # jsonParsed lists lookup-table keys inline; append them only if this node did not
    if not any(key.get("source") == "lookupTable" for key in message["accountKeys"]):
        keys += loaded.get("writable", []) + loaded.get("readonly", [])
    return keys


def signers(data):
    return [key["pubkey"] for key in data["transaction"]["message"]["accountKeys"] if key.get("signer")]


def raw_amount(balance):
    return int(balance["uiTokenAmount"]["amount"]) if balance else 0


def parse_token_credit(data, owner, mint):
    """Net credit to the owner's canonical ATA for `mint`, measured from balance deltas so every
    transfer shape (Transfer, TransferChecked, CPI, multiple instructions) is covered without
    trusting instruction data. The account is bound by its address (owner + mint + token program),
    never by the `owner` label on the balance entry."""
    keys = account_keys(data)
    ata = derive_ata(owner, mint)
    pre = {b["accountIndex"]: b for b in data["meta"]["preTokenBalances"] or []}
    post = {b["accountIndex"]: b for b in data["meta"]["postTokenBalances"] or []}
    credit = 0
    for idx, bal in post.items():
        if 0 <= idx < len(keys) and keys[idx] == ata and bal["mint"] == mint:
            credit = raw_amount(bal) - raw_amount(pre.get(idx))
    if credit <= 0:
        return None, None
    # The payer is the owner of a same-mint account that lost balance AND signed the transaction;
    # a pool/PDA that lost balance in a swap is not a payer anyone can legitimately register.
    sender = None
    tx_signers = signers(data)
    for idx, bal in pre.items():
        if bal["mint"] == mint and raw_amount(post.get(idx)) < raw_amount(bal) and bal.get("owner") in tx_signers:
            sender = bal["owner"]
            break
    return credit, sender


def parse_native_credit(data, owner):
    keys = account_keys(data)
    if owner not in keys:
        return None, None
    idx = keys.index(owner)
    credit = int(data["meta"]["postBalances"][idx]) - int(data["meta"]["preBalances"][idx])
    if credit <= 0:
        return None, None
    for signer in signers(data):
        i = keys.index(signer)
        if int(data["meta"]["preBalances"][i]) - int(data["meta"]["postBalances"][i]) >= credit:
            return credit, signer
    return credit, None


class KeyStore(BaseKeyStore):
    def load_account_from_key(self):
        if not is_pubkey(self.key):
            raise Exception("Error loading wallet: invalid address")
        self.address = self.key

    def add_privkey(self, privkey):
        raise Exception(NOT_SUPPORTED)

    @classmethod
    def load(cls, db):
        return cls(key=db.get("key", ""), contract=db.get("contract", None))

    def dump(self):
        return {"key": self.key, "contract": self.contract}


@dataclass
class Wallet(BaseWallet):
    contract: str = None

    last_signature = StoredDBProperty("last_signature", None)

    def __post_init__(self):
        super().__post_init__()
        self.poll_lock = asyncio.Lock()

    @property
    def contract_addr(self):
        return self.contract

    @property
    def watch_address(self):
        # token transfers reference the ATA, not the owner, so that is the address to poll
        return derive_ata(self.address, self.contract) if self.contract else self.address

    async def balance(self):
        if self.contract:
            return await self.coin.get_token_balance(self.address, self.contract)
        return await self.coin.get_balance(self.address)

    def parse_credit(self, data):
        if self.contract:
            return parse_token_credit(data, self.address, self.contract)
        return parse_native_credit(data, self.address)

    def set_request_address(self, key, address):
        req = self.get_request(key)
        other = self.receive_requests.get(self.request_addresses.get(address))
        # One sender address can only be watched for one open request; the endpoint that sets it
        # needs no login, so a later caller must not be able to steal detection from an earlier one.
        if req and other and other.id != req.id and other.status == PR_UNPAID:
            raise Exception(SENDER_IN_USE)
        return super().set_request_address(key, address)

    def remove_from_detection_dict(self, req):
        if self.request_addresses.get(req.payment_address) == req.id:
            self.request_addresses.pop(req.payment_address, None)

    async def process_new_payment(self, lookup_field, tx, amount, wallet):
        req = self.get_request(lookup_field)
        # A transfer finalized before the request existed cannot be its payment: this closes the
        # replay of an old transaction against a newer request from the same sender (lost cursor,
        # provider ignoring `until`, first load). req.height is the finalized slot at creation.
        if req and tx.slot is not None and tx.slot < req.height:
            logger.info(f"Ignoring {tx.hash}: slot {tx.slot} predates request {req.id}")
            return
        # One transfer pays one request. The stock dedupe is per request, so without this a sender could
        # pay request A, then attach the same address to request B (open at that time) and have the rescan
        # or a replay credit the same transfer again.
        if req and any(tx.hash in other.tx_hashes for other in self.receive_requests.values() if other.id != req.id):
            logger.info(f"Ignoring {tx.hash} for request {req.id}: already credited to another request")
            return
        await super().process_new_payment(lookup_field, tx, amount, wallet)


class SOLDaemon(BlockProcessorDaemon):
    name = "SOL"
    BASE_SPEC_FILE = "daemons/spec/eth.json"
    DEFAULT_PORT = 5012

    DIVISIBILITY = 9
    BLOCK_TIME = 8
    DEFAULT_MAX_SYNC_BLOCKS = 1  # block scanning is replaced by per-address signature polling
    # 200 000 signatures per walk: about 125 MB while they are held. A cap hit is reported in getinfo
    # (`signature_cap_hits`), which the bitcart-health probe reads: logging is off unless SOL_DEBUG is set.
    MAX_SIGNATURE_PAGES = 200
    SIGNATURE_PAGE_SIZE = 1000

    UNIT = "lamport"

    KEYSTORE_CLASS = KeyStore
    WALLET_CLASS = Wallet

    TOKENS = {info["symbol"]: mint for mint, info in SPL_TOKENS.items()}

    def __init__(self):
        super().__init__()
        if self.NET not in RPC_URLS:
            raise ValueError(f"Invalid network passed: {self.NET}. Valid choices are {', '.join(RPC_URLS)}.")
        self.stalled = {}  # wallet key -> (signature, consecutive polls it blocked)
        self.cap_hits = {}  # watched account -> time of the last signature page cap hit

    def load_env(self):
        super().load_env()
        self.MAX_SYNC_SECONDS = self.env("MAX_SYNC_HOURS", cast=int, default=24) * 3600

    def get_default_server_url(self):
        return RPC_URLS.get(self.NET, RPC_URLS["mainnet"])

    async def create_coin(self, archive=False):
        multi_provider = MultipleProviderRPC([SolanaRPCProvider(server) for server in self.SERVER])
        await multi_provider.start()
        self.coin = SOLFeatures(multi_provider)
        logger.info(f"Using RPC {self.coin.current_server()}")

    async def shutdown_coin(self, final=False, archive_only=False):
        if not hasattr(self, "coin"):
            return
        await self.coin.rpc.stop()
        for provider in self.coin.rpc.providers:
            await provider.close()

    def is_recorded(self, tx_hash):
        return any(tx_hash in req.tx_hashes for w in self.wallets.values() for req in w.receive_requests.values())

    async def load_wallet(self, xpub, contract, diskless=False, extra_params=None):
        wallet_key = self.coin.get_wallet_key(xpub, contract)
        if wallet_key in self.wallets:
            return self.wallets[wallet_key]
        if not xpub:
            return None
        if contract and contract not in SPL_TOKENS:
            raise Exception(INVALID_CONTRACT)
        if diskless:
            wallet = self.restore_wallet_from_text(xpub, contract, path=NOOP_PATH)
        else:
            wallet_path = os.path.join(self.get_wallet_path(), wallet_key)
            if not os.path.exists(wallet_path):
                self.restore(xpub, wallet_path=wallet_path, contract=contract)
            storage = Storage(wallet_path)
            db = WalletDB(storage.read())
            wallet = self.WALLET_CLASS(self.coin, db, storage)
        wallet.contract = contract
        if contract:
            wallet.symbol = SPL_TOKENS[contract]["symbol"]
            wallet.divisibility = SPL_TOKENS[contract]["decimals"]
        self.wallets[wallet_key] = wallet
        self.wallets_updates[wallet_key] = deque(maxlen=self.POLLING_CAP)
        self.addresses[wallet.address].add(wallet_key)
        await wallet.start(self.latest_blocks.copy())
        self.loop.create_task(self.poll_wallet(wallet_key))
        # A rescan cut short by a restart is done again: the poller's cursor is already past those transfers
        for req in wallet.receive_requests.values():
            if req.status == PR_UNPAID and req.payment_address:
                self.loop.create_task(self.rescan_until_done(wallet, req.id, req.payment_address, wait=False))
        return wallet

    async def process_pending(self):
        while self.running:
            try:
                self.latest_height = await self.coin.get_block_number()
                await asyncio.gather(*(self.poll_wallet(key) for key in list(self.wallets)))
                self.synchronized = True
            except Exception:
                logger.error("Error polling wallets:")
                logger.error(traceback.format_exc())
            await asyncio.sleep(self.BLOCK_TIME)

    async def collect_signatures(self, wallet, keep, until=None):
        """Walk the watched account's signatures newest first while `keep(entry)` holds.
        Returns (entries oldest first, complete); complete is False when the page cap stopped the walk."""
        found = []
        before = None
        for _ in range(self.MAX_SIGNATURE_PAGES):
            opts = {"limit": self.SIGNATURE_PAGE_SIZE, **COMMITMENT}
            if before:
                opts["before"] = before
            if until:
                opts["until"] = until
            page = await self.coin.request("getSignaturesForAddress", wallet.watch_address, opts)
            kept = [entry for entry in page if keep(entry)]
            found.extend(kept)
            if len(page) < self.SIGNATURE_PAGE_SIZE or len(kept) < len(page):
                return found[::-1], True
            before = page[-1]["signature"]
        return found[::-1], False

    async def fetch_new_signatures(self, wallet):
        min_time = time.time() - self.MAX_SYNC_SECONDS
        found, complete = await self.collect_signatures(
            wallet, lambda entry: (entry.get("blockTime") or min_time) >= min_time, until=wallet.last_signature
        )
        if complete:
            return found
        cap = self.MAX_SIGNATURE_PAGES * self.SIGNATURE_PAGE_SIZE
        self.cap_hits[wallet.watch_address] = int(time.time())
        if wallet.last_signature:
            # Never advance past a gap: dropping the oldest pages would silently lose payments.
            raise SolanaRPCError(
                f"more than {cap} new signatures on {wallet.watch_address};"
                " raise MAX_SIGNATURE_PAGES or check that the RPC honours `until`"
            )
        # First load of a busy account: older history cannot pay any request (slot guard), so start at the
        # newest signature, keeping only what an open request could still match, instead of failing every tick.
        floor = min((req.height for req in wallet.receive_requests.values() if req.status == PR_UNPAID), default=None)
        kept = [entry for entry in found if floor is not None and entry["slot"] >= floor]
        logger.warning(
            f"First load of {wallet.watch_address}: more than {cap} signatures in the last"
            f" {self.MAX_SYNC_SECONDS // 3600} h; starting from the newest ({len(kept)} kept for open requests)"
        )
        if not kept:
            wallet.last_signature = found[-1]["signature"]
        return kept

    async def poll_wallet(self, wallet_key):
        wallet = self.wallets.get(wallet_key)
        if wallet is None:
            return
        async with wallet.poll_lock:
            try:
                for entry in await self.fetch_new_signatures(wallet):
                    signature = entry["signature"]
                    if entry.get("err") is None:
                        data = await self.coin.get_transaction(signature)
                        if data is None or self.coin.get_tx_hash(data) != signature:
                            self.stall(
                                wallet_key, signature, "not served as finalized yet" if data is None else "hash mismatch"
                            )
                            return  # cursor stays behind it; retried next poll
                        if data["meta"]["err"] is None:
                            await self.process_signature(signature, data, wallet)
                        else:
                            logger.debug(f"Ignoring failed transaction {signature}")
                    # cursor moves only past fully handled signatures, so a crash mid-batch replays them
                    wallet.last_signature = signature
                self.stalled.pop(wallet_key, None)
            except Exception as e:
                # RPC trouble delays detection; the cursor is untouched so nothing is lost or invented
                logger.error(f"Polling {wallet.watch_address} failed: {type(e).__name__}: {e}")

    def stall(self, wallet_key, signature, reason):
        previous, count = self.stalled.get(wallet_key, (None, 0))
        count = count + 1 if previous == signature else 1
        self.stalled[wallet_key] = (signature, count)
        log = logger.error if count >= STALL_WARN_POLLS else logger.info
        log(f"{signature} {reason} (poll {count}); detection on {wallet_key} waits for it, check SOL_SERVER")

    async def process_signature(self, signature, data, wallet):
        # A reply this parser cannot read propagates: the poll stops before the cursor moves,
        # so a provider glitch stalls loudly instead of silently skipping a payment.
        credit, sender = wallet.parse_credit(data)
        if credit is None:
            logger.debug(f"Ignoring {signature}: no credit to {wallet.watch_address}")
            return
        tx = self.make_transaction(signature, sender, credit, data, wallet)
        amount = decimal_to_string(from_wei(credit, wallet.divisibility), wallet.divisibility)
        logger.info(f"{wallet.symbol} credit {amount} to {wallet.address} in {signature}")
        await self.process_transaction(tx)

    @staticmethod
    def make_transaction(signature, sender, credit, data, wallet):
        return Transaction(
            signature,
            sender,
            wallet.address,
            credit,
            contract=wallet.contract,
            divisibility=wallet.divisibility,
            slot=data["slot"],
        )

    async def rescan_request(self, wallet, req):
        """Credit transfers from the request's sender made after the request was created but before the
        sender address was attached: the poller saw them while no request was mapped to that sender.
        Returns False when the provider could not serve everything, so the caller can try again."""
        address = req.payment_address
        done = True
        try:
            async with wallet.poll_lock:
                entries, complete = await self.collect_signatures(wallet, lambda entry: entry["slot"] >= req.height)
                if not complete:
                    logger.warning(f"Rescan for request {req.id} hit the page cap; older transfers were not checked")
                for entry in entries:
                    signature = entry["signature"]
                    if entry.get("err") is not None:
                        continue
                    data = await self.coin.get_transaction(signature)
                    if data is None or self.coin.get_tx_hash(data) != signature:
                        logger.warning(f"Rescan for request {req.id}: {signature} not served as finalized, retry later")
                        done = False
                        continue
                    if data["meta"]["err"] is not None:
                        continue
                    credit, sender = wallet.parse_credit(data)
                    if credit is None or sender != address:
                        continue
                    tx = self.make_transaction(signature, sender, credit, data, wallet)
                    # same entry point as the poller: slot guard, tx_hashes dedupe and PR_PAID immutability apply
                    await wallet.process_new_payment(address, tx, from_wei(credit, wallet.divisibility), wallet.wallet_key)
        except Exception as e:
            logger.error(f"Rescan for request {req.id} failed: {type(e).__name__}: {e}")
            return False
        return done

    async def rescan_until_done(self, wallet, req_id, address, wait=True):
        """Repeat a rescan the provider could not complete. It stops when the request is no longer open
        with this sender, the wallet was closed, or after RESCAN_RETRIES attempts."""
        for attempt in range(RESCAN_RETRIES):
            if wait or attempt:
                await asyncio.sleep(self.BLOCK_TIME)
            req = wallet.get_request(req_id)
            if self.wallets.get(wallet.wallet_key) is not wallet or req is None:
                return
            if req.status != PR_UNPAID or req.payment_address != address:
                return
            if await self.rescan_request(wallet, req):
                return
        logger.error(f"Rescan for request {req_id} gave up after {RESCAN_RETRIES} attempts; check SOL_SERVER")

    async def process_transaction(self, tx):
        # Same as the generic version, but the payment is awaited so the cursor only advances
        # after the request state (and its tx_hashes) is on disk.
        amount = from_wei(tx.value, tx.divisibility)
        for wallet in self.addresses.get(tx.to, ()):
            if tx.contract != self.wallets[wallet].contract_addr:
                continue
            await self.trigger_event(
                {
                    "event": "new_transaction",
                    "tx": tx.hash,
                    "from_address": tx.from_addr,
                    "to": tx.to,
                    "amount": decimal_to_string(amount, tx.divisibility),
                    "contract": tx.contract,
                },
                wallet,
            )
            if tx.from_addr in self.wallets[wallet].request_addresses:
                await self.wallets[wallet].process_new_payment(tx.from_addr, tx, amount, wallet)

    @rpc
    async def getinfo(self, wallet=None):
        info = await super().getinfo(wallet=wallet)
        info["signature_cap_hits"] = dict(self.cap_hits)
        return info

    @rpc
    def getservers(self, wallet=None):
        return [redact_url(server) for server in self.SERVER]

    @rpc(requires_wallet=True)
    async def setrequestaddress(self, key, address, wallet):
        if not self.validateaddress(address):
            return False
        wallet_obj = self.wallets[wallet]
        address = self.normalizeaddress(address)
        # The merchant is never their own customer: moving own funds into the watched account must not pay anyone
        if address == wallet_obj.address:
            return False
        req = wallet_obj.set_request_address(key, address)
        if not req:
            return False
        # The customer may have paid before entering the address; the poller has already passed that transfer.
        if req.status == PR_UNPAID and not await self.rescan_request(wallet_obj, req):
            self.loop.create_task(self.rescan_until_done(wallet_obj, req.id, address))
        return True

    @rpc(requires_network=True)
    async def rescan_blocks(self, start_block, end_block=None, wallet=None):
        # The inherited version emits new_block for every slot and then fails on get_block_txes per slot.
        raise NotImplementedError(
            "rescan_blocks is not supported for Solana: payments are found per wallet from its token account's"
            " signatures, not by scanning slots"
        )

    @rpc(requires_network=True)
    async def add_peer(self, url, wallet=None):
        raise NotImplementedError(NOT_SUPPORTED)

    @rpc(requires_network=True)
    async def broadcast(self, tx, wallet=None):
        raise NotImplementedError(NOT_SUPPORTED)

    @rpc(requires_network=True)
    async def get_default_fee(self, tx, wallet=None):
        raise NotImplementedError(NOT_SUPPORTED)

    @rpc
    def get_tx_hash(self, tx_data, wallet=None):
        return self.coin.get_tx_hash(tx_data)

    @rpc
    def get_tx_size(self, tx_data, wallet=None):
        raise NotImplementedError(NOT_SUPPORTED)

    @rpc(requires_network=True)
    async def get_used_fee(self, tx_hash, wallet=None):
        data = await self.coin.get_transaction(tx_hash)
        if data is None:
            raise Exception("Transaction not found")
        return self.coin.to_dict(from_wei(int(data["meta"]["fee"]), self.DIVISIBILITY))

    @rpc(requires_network=True)
    async def gettransaction(self, tx, wallet=None):
        data = await self.coin.get_transaction(tx)
        if data is None:
            raise Exception("Transaction not found")
        return {
            "slot": data["slot"],
            "blockTime": data.get("blockTime"),
            "err": data["meta"]["err"],
            "fee": data["meta"]["fee"],
            "confirmations": await self.coin.get_confirmations(tx),
        }

    @rpc(requires_wallet=True)
    async def listaddresses(self, unused=False, funded=False, balance=False, wallet=None):
        unused, funded, balance = str_to_bool(unused), str_to_bool(funded), str_to_bool(balance)
        wallet_obj = self.wallets[wallet]
        addr_balance = await wallet_obj.balance()
        if (unused and addr_balance > 0) or (funded and addr_balance == 0):
            return []
        if balance:
            return [(wallet_obj.address, self.coin.to_dict(addr_balance))]
        return [wallet_obj.address]

    @rpc
    def make_seed(self, nbits=128, language="english", full_info=False, wallet=None):
        raise NotImplementedError(NOT_SUPPORTED)

    @rpc(requires_wallet=True, requires_network=True)
    async def payto(self, destination, amount, fee=None, feerate=None, gas=None, unsigned=False, wallet=None, *args, **kwargs):
        raise NotImplementedError(NOT_SUPPORTED)

    @rpc(requires_wallet=True)
    def signmessage(self, address=None, message=None, wallet=None):
        raise NotImplementedError(NOT_SUPPORTED)

    def _sign_transaction(self, tx, private_key):
        raise NotImplementedError(NOT_SUPPORTED)

    @rpc
    def verifymessage(self, address, signature, message, wallet=None):
        raise NotImplementedError(NOT_SUPPORTED)

    @rpc
    async def modifypaymenturl(self, url, amount, divisibility=None, wallet=None):
        return modify_payment_url("amount", url, format_amount(amount, divisibility or SPL_TOKENS[USDT_MINT]["decimals"]))

    @rpc
    def get_tokens(self, wallet=None):
        return self.TOKENS

    @rpc
    def validatecontract(self, address, wallet=None):
        return address in SPL_TOKENS

    @rpc(requires_network=True)
    async def readcontract(self, address, function, *args, wallet=None, **kwargs):
        if address not in SPL_TOKENS:
            raise Exception(INVALID_CONTRACT)
        if function == "symbol":
            return SPL_TOKENS[address]["symbol"]
        if function == "decimals":
            return SPL_TOKENS[address]["decimals"]
        if function == "balanceOf":
            return int(await self.coin.get_token_balance(args[0], address) * 10 ** SPL_TOKENS[address]["decimals"])
        raise Exception(f"Unsupported contract function: {function}")

    @rpc(requires_network=True)
    async def transfer(self, *args, wallet=None, **kwargs):
        raise NotImplementedError(NOT_SUPPORTED)


if __name__ == "__main__":
    daemon = SOLDaemon()
    daemon.start()
