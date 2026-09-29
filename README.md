# USDT on Solana for Bitcart

A [Bitcart](https://github.com/bitcart/bitcart) **docker plugin**. It adds the coin `sol` with the SPL token USDT (mint `Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB`, 6 decimals) as a watch-only payment rail. No Bitcart core, SDK, database or frontend change.

Built and tested against **Bitcart 0.10.3.0** (docker deployment, `bitcart-docker`). Other versions: read "Upgrade check" first.

This is an independent plugin. It is not part of the Bitcart project and the Bitcart maintainers do not support it.

## Parts

All of this repository goes to `compose/plugins/docker/solana/` in the `bitcart-docker` folder of the Bitcart host.

- `daemon/` — the Solana daemon image (`forked/bitcart-sol:0.10.3.0`, built `FROM bitcart/bitcart-xmr:0.10.3.0`, adds `sol.py` to Bitcart's `daemons/`). Port 5012. Pure Python, no extra packages. `pull_policy: build`, so `./update.sh` (which runs `docker compose pull`) skips it and `./start.sh` builds it locally.
- `backend/forked/solana/plugin.py` — registers the `SOL` coin class in the backend and worker, and turns the daemon's sender-lock refusal into a readable 422. `rules/90_solana_modules.py` bind-mounts only this package, read-only, at `/app/modules/forked` (next to the image's own `/app/modules/__init__.py`, the same layout the stock `backend-plugins.Dockerfile` produces). The stock backend-plugin image build is not used because it needs `bitcart/bitcart:stable`, which a `BITCART_VERSION`-pinned install does not have.
- `components/solana.yml` — the compose service, `SOL_HOST` and `SOL_NETWORK` for backend/worker, the `solana_datadir` volume.
- `tests/` — daemon suite (mocked RPC plus two recorded mainnet transactions), runs in the daemon image. `backend_tests/` — backend plugin suite, runs in the backend image with the real mount layout.

## How it works

- Wallet = a normal Solana owner address (watch-only, no private key ever). Token wallet = owner + USDT mint. The daemon derives the associated token account (ATA) itself and polls `getSignaturesForAddress` on it (owner address for a native SOL wallet). No block scanning.
- A payment = a successful transaction whose post − pre token balance on the canonical ATA, for exactly the USDT mint, is positive. Symbol/name metadata is never trusted; other mints, failed transactions, outgoing and net-zero transfers are ignored. Amounts are integer base units → `Decimal`.
- Finality: every read uses the `finalized` commitment. A finalized (rooted) transaction cannot be rolled back, so a detected payment is final money. The cost is speed: finalization trails `confirmed` by about 32 slots (≈13 s), plus up to one poll interval (8 s). Expect the invoice to turn paid about 15–30 s after the customer's wallet shows the transfer as sent. Confirmations are reported as 32 for every detected payment, so the invoice completes on detection whatever the store's transaction speed.
- Invoice matching is Bitcart's stock sender-address matching (the customer enters the address that sends the USDT), like TRX/ETH/Polygon USDT. The sender is the owner of a same-mint token account that lost balance **and** signed the transaction.
- Paid before entering the address: when the address is attached (`setrequestaddress`), the daemon rescans the wallet's signatures back to the request's creation slot and credits that sender's transfers. The poller had already passed them while no request was mapped to that sender. If the provider cannot serve the rescan (error, or a listed transaction not served yet), the address stays set and the rescan is repeated once per poll interval, up to 75 times (about 10 minutes), while the request is open. Open requests with a sender are also rescanned when the wallet is loaded, so a daemon restart does not lose a transfer paid before the address was entered.
- Replay guard: a transfer finalized in a slot before the request was created never pays that request (poller and rescan alike).
- One transfer, one request: a transaction already recorded on any request of the wallet never pays a second request (poller, rescan and replay alike).
- Sender lock: one sender address can be attached to only one open invoice per wallet, so a stranger cannot redirect detection away from someone else's invoice. A second attach is refused with `This sender address is already attached to another open invoice` (see "Customer-visible sender-lock message" below). The wallet's own address is refused as a sender (`Invalid address`).
- Cursor: the newest fully processed signature is stored per wallet (`last_signature` in the wallet file). Restart or RPC hiccups replay from the cursor; Bitcart's `tx_hashes` dedupe stops double credits. First load scans `SOL_MAX_SYNC_HOURS` (default 24) back.
- Page cap (200 pages × 1000 signatures; about 125 MB while a full walk is held): on an existing cursor a cap hit raises and the cursor does not move, so nothing is ever skipped (raise `MAX_SIGNATURE_PAGES` or check that the RPC honours `until`). A hit is reported in `getinfo` as `signature_cap_hits` {account: time}. On the first load of a brand-new wallet (no cursor), the daemon logs a WARNING (`First load of …: more than 200000 signatures …; starting from the newest`) and starts at the newest signature. It keeps only signatures an open request could still match (slot ≥ the oldest open request's creation slot), instead of failing on every tick.
- Balance: `getAccountInfo` on the ATA. An ATA that does not exist yet (the owner never received USDT) reads as 0 and is not counted as a provider failure.
- Payment URI: `solana:<owner>?amount=<usdt>&spl-token=<mint>` (Solana Pay; recipient is the owner, never the ATA).
- Not supported (raise "Currently not supported" or a clear NotImplementedError): payouts/`payto`, `broadcast`, seed creation, private keys, network-fee estimation (do not enable "include network fee" on a store for this wallet), `rescan_blocks` (there is no slot scanning to rescan).

## Known limitations

Read these before you take real payments.

- **No proof of ownership for the sender address.** An address is registered on an invoice without a signature. The first open invoice that registers a payer's address gets that payer's transfers. If a customer pays first and enters the address later, another person who sees the transfer on chain can register that address on an invoice of their own first. Tell customers to enter the address **before** they pay, and show the sender form before the pay-to address in your checkout. A real fix needs a design change (signed message, unique amounts, or login-bound addresses). Stock Bitcart sender matching for TRX/ETH/Polygon tokens has the same limit.
- **Exchange and custodial payments cannot be matched.** See the next section.
- **Signature flood.** More than 200 000 transactions that name the watched account between two successful polls stop detection for that wallet until an operator raises `MAX_SIGNATURE_PAGES`. The daemon fails closed: it never skips a payment. Monitor `signature_cap_hits` in `getinfo`.
- **Rescan cost.** Each address attach starts a rescan that fetches every transaction on the account since the invoice was created. Many invoices can use up the RPC quota and delay detection. There is no false credit.
- **No logs with `SOL_DEBUG=false`.** The Bitcart daemon base disables all logging when debug is off. Error, stall and credit lines are written nowhere. Read the daemon state through `getinfo`, not through `docker logs`.
- **Late payments.** The Bitcart backend expires invoices on its own clock. A payment finalized before expiry but seen after it (daemon down) is not credited.
- **Only mainnet USDT** (legacy SPL token program) is accepted. No Token-2022, no other tokens, no payouts.

The daemon trusts the RPC provider for these facts:

- `getSignaturesForAddress` at `finalized` is complete and newest-first, and its `err` field is correct.
- `preTokenBalances` / `postTokenBalances` are correct and list every token account of the transaction. The `owner` labels and the `signer` flags are correct.
- `getSlot` at `finalized` is not far behind.
- The provider honours `until`. If not, the daemon replays up to 24 h on every poll (safe, slow) and hits the page cap on a busy account.
- The provider serves every listed finalized transaction with `maxSupportedTransactionVersion: 0`. A transaction it never serves stalls that wallet until an operator acts.

Use an RPC provider you trust. The public default endpoint is rate-limited.

## Sender / exchange limitation

Detection needs the address that actually signs the transfer. Payments from exchanges and custodial wallets (Binance, Coinbase, OKX, a hosted wallet, a payment processor) are sent from the provider's own hot wallet, not from the customer's deposit address. They cannot be attributed to the invoice, even though the USDT arrives in the merchant's wallet. The customer must pay from a self-custody wallet (Phantom, Solflare, Backpack, a hardware wallet, and so on) and enter that wallet's address. Transfers signed by a program or multisig vault instead of the owner also carry no attributable sender. Unattributed transfers are visible in the daemon's `docker logs` (`USDT credit …`) **only with `SOL_DEBUG=true`**. They have to be reconciled by hand from the chain (Solscan, the wallet's token account).

## Customer-visible sender-lock message

The daemon refuses a second attach with the text above. The stock invoice service (`api/services/crud/invoices.py` `update_payment_details`) only catches `normalizeaddress` errors and maps a `False` result to 422 `Invalid address`, so an SDK error would become a 500. `plugin.py` therefore wraps `setrequestaddress` on the SOL coin and turns that SDK error (`UnknownError: Unknown error from server: Exception: This sender address …`) into HTTP 422 with `detail` = `This sender address is already attached to another open invoice`.

- API clients that display `detail` show the text verbatim.
- The stock Bitcart checkout page (bitcart-admin 0.10.3.0, `components/TabbedCheckout.vue` `updatePaymentDetails`) only shows an inline error when `detail` is exactly `Invalid address`. For any other detail it shows nothing: the address is simply not accepted. A clearer message on that page is not possible without a frontend change. To get the old (misleading but visible) `Invalid address` on the stock page instead, remove the `__init__` override in `plugin.py` and make `Wallet.set_request_address` in `sol.py` return `None` again.

## Environment

- `SOL_SERVER` (default `https://api.mainnet-beta.solana.com`): comma list = failover (stock `MultipleProviderRPC`); keyed URLs are redacted in logs and in `getservers`.
- `SOL_NETWORK` (default `mainnet`): `devnet` / `testnet` switch the daemon's default RPC. The same value goes to backend and worker, where the stock `CoinService` uses it to pick the Solscan explorer link for that cluster, and the admin coin list shows `Solana (<network>)`. Only the mainnet USDT mint is accepted on any network.
- `SOL_MAX_SYNC_HOURS` (default `24`): backfill window on first load / after long downtime.
- `SOL_DEBUG` (default `false`).

Where to set them (checked against bitcart-docker `update.sh` / `helpers.sh`):

- `./update.sh` and `./setup.sh` call `bitcart_update_docker_env`, which **rewrites** `bitcart-docker/.env` from the shell environment. It writes a fixed list (`BITCART_CRYPTOS`, the stock `<COIN>_NETWORK`, …) plus any variable matching `*_SERVER`, `*_DEBUG`, `*_LIGHTNING_GOSSIP` and `BITCART_*_PORT/_EXPOSE/_SCALE/_ROOTPATH`. That environment comes from `load_env`, which sources `/etc/profile.d/bitcart-env.sh`, which exports every line of `.env`.
- Result: `SOL_SERVER` and `SOL_DEBUG` survive in `.env`. `SOL_NETWORK` and `SOL_MAX_SYNC_HOURS` are silently dropped from `.env` by the next update, and the compose defaults (`mainnet`, `24`) apply again.
- So: put `SOL_SERVER` / `SOL_DEBUG` in `.env`. For a non-default `SOL_NETWORK` / `SOL_MAX_SYNC_HOURS`, add an `export` line to `/etc/profile.d/bitcart-env.sh`. It is sourced before every `docker compose up` by `start.sh`, `update.sh` and the systemd unit, and compose takes shell variables over `.env`. Re-add the line after any `setup.sh` rerun (setup.sh regenerates that file).
- After changing any `SOL_*`, run `./start.sh` so the daemon **and** backend/worker get the same value.

## Install on an existing Bitcart docker install

As root on the Bitcart host. The examples use `/root/bitcart-docker`; change the path if yours is different.

```
cd /root/bitcart-docker
cp -a .env .env.pre-sol
git clone https://github.com/apezio/bitcart-solana-usdt.git \
  compose/plugins/docker/solana
sed -i 's/^BITCART_CRYPTOS=.*/&,sol/' .env
# regenerates compose, builds the daemon, restarts backend+worker
./start.sh
```

`BITCART_CRYPTOS` is on `update.sh`'s fixed list, so `.env` is the live setting. If you keep a separate environment file as the input for `setup.sh`, add `sol` there too.

Check: `curl https://<your-bitcart-host>/api/cryptos` lists `sol`; `/api/cryptos/tokens/sol` → `["USDT"]`; the daemon's `getinfo` reports connected and synchronized; no `.plugins-failed` in the `bitcart_datadir` volume.

Then in the admin panel: Wallets → create, currency **Solana**, Address = the owner address, contract = **USDT**.

## Tests / lint

You need Docker. Both suites run in the pinned Bitcart images, so they use the same Python and the same Bitcart code as production.

Daemon suite, pytest + ruff, from the root of this repository:

```
docker run --rm -v "$PWD":/plug bitcart/bitcart-xmr:0.10.3.0 \
  sh -c "uv pip install -q --python /home/electrum/.venv/bin/python \
  pytest pytest-asyncio; cd /plug && python -m pytest -q && \
  uvx -q ruff check . && uvx -q ruff format --check ."
```

Backend plugin suite, in the pinned backend image with the production mount layout:

```
docker run --rm -v "$PWD":/plug \
  -v "$PWD"/backend/forked:/app/modules/forked:ro -w /app \
  --entrypoint sh bitcart/bitcart:0.10.3.0 -c \
  "uv pip install -q --python /app/.venv/bin/python pytest pytest-asyncio \
  && python -m pytest -q -p no:cacheprovider /plug/backend_tests"
```

Two daemon tests are strict expected failures. They reproduce open items from "Known limitations".

Compose generator dry-run without touching production: copy the `bitcart-docker` folder to `/tmp/bd-dry`, put this tree in `/tmp/bd-dry/compose/plugins/docker/solana`, source `.deploy` and `/etc/profile.d/bitcart-env.sh`, then run the generator image directly (`docker run --rm -v /tmp/bd-dry/compose:/app/compose --env-file <(env | grep '^BITCART_') --env-file <(env | grep '^REVERSEPROXY_') --env NAME="$NAME" bitcart/docker-compose-generator:local`). Run it from a script file, not `bash -c`, or the command string leaks into `env`. Do not use `build.sh` for this: with the `:local` generator it rebuilds the shared image tag. Diff `compose/generated.yml` against production, then `rm -rf /tmp/bd-dry` (it holds a copy of `.deploy` secrets).

## Manual mainnet smoke test

1. Create the SOL/USDT wallet (owner address), attach it to a store.
2. Create a small invoice (≈1 USD) in that store; the checkout shows the owner address, the exact USDT amount and the `solana:` QR.
3. Enter the sending wallet's address in the invoice. Then send ≥ the amount in USDT from that self-custody Solana wallet.
4. Expect: paid and complete about 15–30 s after the transfer is sent (finalized + one poll); the transaction link points to Solscan.

## Rollback

```
# on the Bitcart host, as root
cd /root/bitcart-docker
sed -i 's/,sol$//' .env
rm -rf compose/plugins/docker/solana
./start.sh
docker volume rm compose_solana_datadir   # optional
```

Other coins are untouched; existing SOL wallets/invoices in the database simply stop being served.

## Upgrade check (when the pinned Bitcart image changes)

Bump the tag in `daemon/Dockerfile` (`FROM`), `components/solana.yml` (`image`) and both test recipes. Run both suites and the generator dry-run against the new images before `./start.sh`. Then re-verify by reading the new source:

- Daemon base class API (`daemons/genericprocessor.py`, `daemons/base.py`, `daemons/utils.py`): `BlockProcessorDaemon` (`load_wallet`, `process_pending`, `process_transaction`, `trigger_event`, `get_error_code`/spec loading, `execute_method` awaiting async RPC methods), `Wallet` (`set_request_address`, `remove_from_detection_dict`, `process_new_payment`, `set_request_status`), `KeyStore`, `Invoice.height` (= chain height at creation; the slot guard and the rescan floor depend on it), the stock `setrequestaddress` and `rescan_blocks` signatures (both overridden here), `MultipleProviderRPC` failure accounting, `AbstractRPCProvider`, the `rpc` decorator.
- genericprocessor semantics the payment path relies on: `PR_PAID` stays immutable, `tx_hashes` dedupe in `process_new_payment`, requests leave the detection dict on PAID/EXPIRED, `get_confirmations` is used for the confirmation count. Also check that the sender-lock text still maps to no code in `daemons/spec/eth.json` (a spec code would replace the text with a fixed docstring; a test asserts this).
- `/app/modules` loading (`api/plugins.py` `load_plugins`): still `glob("modules/**/**/plugin.py")` from `/app`, imported as `modules.forked.solana.plugin`; the image still ships `/app/modules/__init__.py`. The backend suite checks this with the real mount.
- Backend call sites: `update_payment_details` still calls `coin.server.setrequestaddress` and only catches `normalizeaddress` errors; `CoinService` still reads `SOL_HOST` / `SOL_NETWORK` via `settings.config`; `bitcart.errors.UnknownError` still carries the daemon's message; the checkout's `detail` handling in bitcart-admin `TabbedCheckout.vue`.
- `plugin.py` touches `bitcart.COINS`, `api.constants.SUPPORTED_CRYPTOS`, `api.ext.blockexplorer.EXPLORERS`, `api.ext.exchanges.coinrules`, `api.ext.moneyformat.currency_table`.
- bitcart-docker: `bitcart_update_docker_env` variable list, the generator's plugin component/rules loading (`get_plugin_components`, `get_plugin_rules`), and compose `pull_policy: build` still skipped by `compose pull`.
- Port 5012 must stay free of upstream daemons.
- If upstream ever ships `bitcart/bitcart:stable` on your host, the module could move to the stock `compose/plugins/backend` path and the mount rule be dropped.

## License

MIT. See `LICENSE`. Bitcart is MIT licensed, Copyright (c) 2019 MrNaif2018.
