import os
from functools import partial

import bitcart
from api import constants
from api.ext.blockexplorer import EXPLORERS
from api.ext.exchanges import coinrules
from api.ext.moneyformat import currency_table
from api.plugins import BasePlugin
from bitcart import ETH
from bitcart.errors import RequestError
from fastapi import HTTPException
from universalasync import wrap

# Same variable the daemon gets (components/solana.yml). The stock CoinService reads it too and picks
# the matching EXPLORERS entry below; here it only labels a non-mainnet coin in the admin panel.
SOL_NETWORK = os.environ.get("SOL_NETWORK", "mainnet")
# Must match daemon/sol.py SENDER_IN_USE.
SENDER_IN_USE = "This sender address is already attached to another open invoice"


async def setrequestaddress(call, *args, **kwargs):
    # The stock invoice service only catches normalizeaddress errors, so the daemon's refusal would be a 500.
    # The SDK carries the daemon's text in UnknownError ("Unknown error from server: ..."): turn it into a 422.
    try:
        return await call(*args, **kwargs)
    except RequestError as e:
        if SENDER_IN_USE.lower() in str(e).lower():
            raise HTTPException(422, SENDER_IN_USE) from None
        raise


class SOL(ETH):
    coin_name = "SOL"
    friendly_name = "Solana"
    RPC_URL = "http://localhost:5012"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.server.setrequestaddress = partial(setrequestaddress, self.server.setrequestaddress)


class SOLRules:
    coingecko_id = "solana"


# Registration happens at import: CoinService reads bitcart.COINS[...] for every entry of
# BITCART_CRYPTOS as soon as the DI container is built, before any plugin hook runs.
wrap(SOL)
bitcart.COINS["SOL"] = SOL
# friendly_name stays "Solana" (CoinGecko name fallback); only the admin label shows the network
constants.SUPPORTED_CRYPTOS["sol"] = SOL.friendly_name if SOL_NETWORK == "mainnet" else f"Solana ({SOL_NETWORK})"
EXPLORERS["sol"] = {
    "mainnet": "https://solscan.io/tx/{}",
    "devnet": "https://solscan.io/tx/{}?cluster=devnet",
    "testnet": "https://solscan.io/tx/{}?cluster=testnet",
}
coinrules.SOL = SOLRules
currency_table.data["SOL"] = {"name": "Solana", "divisibility": 8, "symbol": None, "crypto": True}


class Plugin(BasePlugin):
    name = "forked_solana"

    def setup_app(self, app):
        pass

    async def startup(self):
        pass

    async def shutdown(self):
        pass

    async def worker_setup(self):
        pass
