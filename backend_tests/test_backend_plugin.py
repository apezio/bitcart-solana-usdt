"""Runs inside bitcart/bitcart:<version> with the plugin mounted exactly as rules/90_solana_modules.py
mounts it (./backend/forked -> /app/modules/forked:ro), cwd /app. See README "Tests / lint"."""

import glob
import importlib

import pytest
from aiohttp import web
from api.plugins import load_module
from bitcart.errors import RequestError
from fastapi import HTTPException

PLUGIN_PATH = "modules/forked/solana/plugin.py"


@pytest.fixture(scope="module")
def plugin():
    # the stock loader: glob + importlib on the dotted path, next to the image's modules/__init__.py
    assert PLUGIN_PATH in glob.glob("modules/**/**/plugin.py")
    return load_module(PLUGIN_PATH)


def test_registration(plugin):
    import bitcart
    from api import constants
    from api.ext.blockexplorer import EXPLORERS

    assert bitcart.COINS["SOL"] is plugin.SOL
    assert constants.SUPPORTED_CRYPTOS["sol"] == "Solana"
    assert EXPLORERS["sol"]["mainnet"] == "https://solscan.io/tx/{}"
    assert plugin.Plugin.name == "forked_solana"


def test_network_label(plugin, monkeypatch):
    from api import constants

    monkeypatch.setenv("SOL_NETWORK", "devnet")
    try:
        importlib.reload(plugin)
        assert constants.SUPPORTED_CRYPTOS["sol"] == "Solana (devnet)"
        assert plugin.SOL.friendly_name == "Solana"  # CoinGecko name fallback must not change
    finally:
        monkeypatch.delenv("SOL_NETWORK")
        importlib.reload(plugin)
    assert constants.SUPPORTED_CRYPTOS["sol"] == "Solana"


@pytest.fixture
async def fake_daemon(unused_tcp_port):
    replies = {}

    async def handle(request):
        body = await request.json()
        return web.json_response({"jsonrpc": "2.0", "id": body["id"], **replies[body["method"]]})

    async def spec(request):
        return web.json_response({"version": "4.5.0", "exceptions": {}})

    app = web.Application()
    app.router.add_post("/", handle)
    app.router.add_get("/spec", spec)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", unused_tcp_port).start()
    yield f"http://127.0.0.1:{unused_tcp_port}", replies
    await runner.cleanup()


async def test_sender_lock_becomes_422(plugin, fake_daemon):
    url, replies = fake_daemon
    coin = plugin.SOL(rpc_url=url, xpub={"xpub": "x", "contract": "y"})
    # what the daemon sends for the refusal (fallback code -32603, message = "Exception: <text>")
    replies["setrequestaddress"] = {"error": {"code": -32603, "message": f"Exception: {plugin.SENDER_IN_USE}"}}
    with pytest.raises(HTTPException) as exc:
        await coin.server.setrequestaddress("req", "addr")
    assert exc.value.status_code == 422
    assert exc.value.detail == plugin.SENDER_IN_USE
    replies["setrequestaddress"] = {"error": {"code": -32603, "message": "Exception: something else"}}
    with pytest.raises(RequestError):
        await coin.server.setrequestaddress("req", "addr")
    replies["setrequestaddress"] = {"result": True}
    assert await coin.server.setrequestaddress("req", "addr") is True
    replies["normalizeaddress"] = {"result": "addr"}
    assert await coin.server.normalizeaddress("addr") == "addr"  # other methods untouched
