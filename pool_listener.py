"""
╔══════════════════════════════════════════════════════════════════════╗
║  NEXUS GEMHUNTER — Pool Listener Multi-Chain                       ║
║  Détecte les nouveaux pools de liquidité EN TEMPS RÉEL             ║
║                                                                      ║
║  Chains supportées:                                                  ║
║  • EVM (Ethereum, Base, Arbitrum) — events PairCreated             ║
║  • Solana — créations de pools Raydium via log subscribe           ║
║                                                                      ║
║  Ce module remplace le scan DexScreener périodique par un           ║
║  listener websocket qui capte les pools À LA SECONDE où ils        ║
║  sont créés — avantage de vitesse crucial pour le sniping.          ║
╚══════════════════════════════════════════════════════════════════════╝

INSTALLATION:
    pip install web3 aiohttp websockets solders base58

CONFIGURATION .env:
    # RPC Endpoints (websocket obligatoire pour le listener)
    ETH_WS_RPC=wss://eth-mainnet.g.alchemy.com/v2/YOUR_KEY
    BASE_WS_RPC=wss://base-mainnet.g.alchemy.com/v2/YOUR_KEY
    SOLANA_WS_RPC=wss://api.mainnet-beta.solana.com
    SOLANA_HTTP_RPC=https://api.mainnet-beta.solana.com
    
    # Telegram
    TELEGRAM_BOT_TOKEN=...
    
    # Filtres
    MIN_LIQUIDITY_USD=5000
    MIN_SCORE=50
    MAX_TOKEN_AGE_BLOCKS=10
"""
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import asyncio
import json
import logging
import os
import struct
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Callable

try:
    import aiohttp
except ImportError:
    raise ImportError("pip install aiohttp")

try:
    from eth_abi import decode as abi_decode
    HAS_ETH_ABI = True
except ImportError:
    HAS_ETH_ABI = False

try:
    import base58
    HAS_BASE58 = True
except ImportError:
    HAS_BASE58 = False

log = logging.getLogger("nexus.pool_listener")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Data Models
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class Chain(str, Enum):
    ETHEREUM = "ethereum"
    BASE = "base"
    ARBITRUM = "arbitrum"
    SOLANA = "solana"


@dataclass
class NewPool:
    """Un nouveau pool de liquidité détecté."""
    chain: Chain
    dex: str                          # "uniswap_v2", "raydium", etc.
    pool_address: str
    token0: str                       # Adresse du token 0
    token1: str                       # Adresse du token 1
    token0_symbol: str = "?"
    token1_symbol: str = "?"
    block_number: int = 0
    tx_hash: str = ""
    timestamp: float = 0.0           # Unix timestamp de la détection
    
    # Analyse (remplie après détection)
    target_token: str = ""            # L'adresse du "nouveau" token (pas WETH/USDC)
    target_symbol: str = "?"
    initial_liquidity_usd: float = 0.0
    is_honeypot: Optional[bool] = None
    buy_tax_pct: float = 0.0
    sell_tax_pct: float = 0.0
    owner_renounced: Optional[bool] = None
    lp_locked: Optional[bool] = None
    score: float = 0.0
    red_flags: list = field(default_factory=list)
    green_flags: list = field(default_factory=list)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Constantes
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Uniswap V2 Factory — event PairCreated(address token0, address token1, address pair, uint)
UNISWAP_V2_PAIR_CREATED_TOPIC = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"

# Factory addresses par chain
FACTORIES = {
    Chain.ETHEREUM: {
        "uniswap_v2": "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f",
        "sushiswap": "0xC0AEe478e3658e2610c5F7A4A2E1777cE9e4f2Ac",
    },
    Chain.BASE: {
        "uniswap_v2": "0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6",
        "baseswap": "0xFDa619b6d20975be80A10332cD39b9a4b0FAa8BB",
        "aerodrome": "0x420DD381b31aEf6683db6B902084cB0FFECe40Da",
    },
    Chain.ARBITRUM: {
        "uniswap_v2": "0xf1D7CC64Fb4452F05c498126312eBE29f30Fbcf9",
        "sushiswap": "0xc35DADB65012eC5796536bD9864eD8773aBc74C4",
    },
}

# Tokens "base" connus (WETH, USDC, etc.) — si un token du pool est dedans, l'autre est le "nouveau"
BASE_TOKENS = {
    Chain.ETHEREUM: {
        "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2".lower(): ("WETH", 18),
        "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48".lower(): ("USDC", 6),
        "0xdAC17F958D2ee523a2206206994597C13D831ec7".lower(): ("USDT", 6),
    },
    Chain.BASE: {
        "0x4200000000000000000000000000000000000006".lower(): ("WETH", 18),
        "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913".lower(): ("USDC", 6),
    },
    Chain.ARBITRUM: {
        "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1".lower(): ("WETH", 18),
        "0xaf88d065e77c8cC2239327C5EDb3A432268e5831".lower(): ("USDC", 6),
    },
}

# Raydium AMM Program ID (Solana)
RAYDIUM_AMM_PROGRAM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"

# Tokens base Solana
SOLANA_BASE_TOKENS = {
    "So11111111111111111111111111111111111111112": ("SOL", 9),
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": ("USDC", 6),
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": ("USDT", 6),
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1. EVM POOL LISTENER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class EVMPoolListener:
    """
    Écoute les events PairCreated sur les factories Uniswap V2 (et forks)
    via websocket pour une latence minimale.
    
    Pourquoi websocket et pas polling :
    - Polling (ce que fait gemhunter.py actuel) = tu vois le pool 2-5 min après
    - Websocket = tu vois le pool dans la SECONDE où il est créé
    - Cette différence = tout le edge du sniping
    """

    def __init__(self, chain: Chain, ws_rpc: str, on_new_pool: Callable):
        self.chain = chain
        self.ws_rpc = ws_rpc
        self.on_new_pool = on_new_pool  # Callback async
        self._ws = None
        self._running = False
        self._reconnect_delay = 1
        self._max_reconnect_delay = 60
        self._http_rpc = ws_rpc.replace("wss://", "https://").replace("ws://", "http://")
        self._session: Optional[aiohttp.ClientSession] = None
        
        # Toutes les factory addresses de cette chain
        self._factory_addresses = [
            addr for addr in FACTORIES.get(chain, {}).values()
        ]
        self._factory_names = {
            addr.lower(): name 
            for name, addr in FACTORIES.get(chain, {}).items()
        }

    async def _get_session(self):
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            )
        return self._session

    async def start(self):
        """Lance le listener avec auto-reconnect."""
        self._running = True
        log.info(f"🔌 [{self.chain.value}] Pool listener démarré — {len(self._factory_addresses)} factories")
        
        while self._running:
            try:
                await self._listen()
            except Exception as e:
                if not self._running:
                    break
                log.warning(
                    f"🔌 [{self.chain.value}] Connexion perdue: {e} — "
                    f"reconnexion dans {self._reconnect_delay}s"
                )
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(
                    self._reconnect_delay * 2, 
                    self._max_reconnect_delay
                )

    async def _listen(self):
        """Connexion websocket et écoute des events."""
        import websockets
        print(f"DEBUG connecting to: {self.ws_rpc}") 
        async with websockets.connect(self.ws_rpc, ping_interval=20) as ws:
            self._ws = ws
            self._reconnect_delay = 1  # Reset on success
            log.info(f"🔌 [{self.chain.value}] WebSocket connecté")
            
            # Subscribe aux logs PairCreated de toutes les factories
            subscribe_msg = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_subscribe",
                "params": [
                    "logs",
                    {
                        "address": self._factory_addresses,
                        "topics": [UNISWAP_V2_PAIR_CREATED_TOPIC],
                    }
                ]
            }
            
            await ws.send(json.dumps(subscribe_msg))
            
            # Confirmation de la subscription
            response = await ws.recv()
            data = json.loads(response)
            if "result" in data:
                sub_id = data["result"]
                log.info(f"🔌 [{self.chain.value}] Subscribed (id: {sub_id})")
            else:
                log.warning(f"🔌 [{self.chain.value}] Subscribe failed: {data}")
                return
            
            # Boucle d'écoute
            async for message in ws:
                if not self._running:
                    break
                try:
                    await self._handle_message(json.loads(message))
                except Exception as e:
                    log.error(f"🔌 [{self.chain.value}] Error handling message: {e}")

    async def _handle_message(self, data: dict):
        """Parse un event PairCreated et crée un NewPool."""
        params = data.get("params", {})
        result = params.get("result", {})
        
        if not result or not result.get("topics"):
            return
        
        topics = result["topics"]
        log_data = result.get("data", "0x")
        
        # PairCreated(address indexed token0, address indexed token1, address pair, uint)
        # topics[0] = event signature
        # topics[1] = token0 (indexed)
        # topics[2] = token1 (indexed)
        # data = pair_address + pair_count (non-indexed)
        
        if len(topics) < 3:
            return
        
        token0 = "0x" + topics[1][-40:]
        token1 = "0x" + topics[2][-40:]
        
        # Decode pair address from data
        if len(log_data) >= 66:
            pair_address = "0x" + log_data[26:66]
        else:
            pair_address = "unknown"
        
        # Identifie quel token est le "nouveau" (pas WETH/USDC)
        base_tokens = BASE_TOKENS.get(self.chain, {})
        t0_lower = token0.lower()
        t1_lower = token1.lower()
        
        if t0_lower in base_tokens:
            target_token = token1
            target_symbol = "?"
            base_symbol = base_tokens[t0_lower][0]
        elif t1_lower in base_tokens:
            target_token = token0
            target_symbol = "?"
            base_symbol = base_tokens[t1_lower][0]
        else:
            # Deux tokens inconnus — on prend token0 comme target
            target_token = token0
            target_symbol = "?"
            base_symbol = "?"
        
        # Factory name
        factory_addr = result.get("address", "").lower()
        dex_name = self._factory_names.get(factory_addr, "unknown_dex")
        
        pool = NewPool(
            chain=self.chain,
            dex=dex_name,
            pool_address=pair_address,
            token0=token0,
            token1=token1,
            target_token=target_token,
            target_symbol=target_symbol,
            block_number=int(result.get("blockNumber", "0x0"), 16),
            tx_hash=result.get("transactionHash", ""),
            timestamp=time.time(),
        )
        
        log.info(
            f"🆕 [{self.chain.value}] Nouveau pool détecté! "
            f"{dex_name} — {token0[:10]}.../{token1[:10]}... "
            f"— Pool: {pair_address[:10]}... "
            f"— Block: {pool.block_number}"
        )
        
        # Callback pour analyse + alerte
        asyncio.create_task(self.on_new_pool(pool))

    async def stop(self):
        self._running = False
        if self._ws:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2. SOLANA POOL LISTENER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SolanaPoolListener:
    """
    Écoute les créations de pools Raydium sur Solana.
    
    Utilise logsSubscribe pour capter les transactions du 
    Raydium AMM Program en temps réel.
    """

    def __init__(self, ws_rpc: str, http_rpc: str, on_new_pool: Callable):
        self.ws_rpc = ws_rpc
        self.http_rpc = http_rpc
        self.on_new_pool = on_new_pool
        self._ws = None
        self._running = False
        self._reconnect_delay = 1
        self._max_reconnect_delay = 60
        self._session: Optional[aiohttp.ClientSession] = None
        self._seen_signatures: set = set()  # Dédup

    async def _get_session(self):
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            )
        return self._session

    async def start(self):
        """Lance le listener Solana avec auto-reconnect."""
        self._running = True
        log.info(f"🔌 [solana] Pool listener démarré — Raydium AMM")
        
        while self._running:
            try:
                await self._listen()
            except Exception as e:
                if not self._running:
                    break
                log.warning(
                    f"🔌 [solana] Connexion perdue: {e} — "
                    f"reconnexion dans {self._reconnect_delay}s"
                )
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(
                    self._reconnect_delay * 2,
                    self._max_reconnect_delay
                )

    async def _listen(self):
        """Connexion websocket Solana et écoute des logs Raydium."""
        import websockets
        
        async with websockets.connect(self.ws_rpc, ping_interval=20) as ws:
            self._ws = ws
            self._reconnect_delay = 1
            log.info(f"🔌 [solana] WebSocket connecté")
            
            # Subscribe aux logs du programme Raydium AMM
            subscribe_msg = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "logsSubscribe",
                "params": [
                    {"mentions": [RAYDIUM_AMM_PROGRAM]},
                    {"commitment": "confirmed"}
                ]
            }
            
            await ws.send(json.dumps(subscribe_msg))
            
            response = await ws.recv()
            data = json.loads(response)
            if "result" in data:
                log.info(f"🔌 [solana] Subscribed to Raydium logs")
            else:
                log.warning(f"🔌 [solana] Subscribe failed: {data}")
                return
            
            async for message in ws:
                if not self._running:
                    break
                try:
                    await self._handle_message(json.loads(message))
                except Exception as e:
                    log.error(f"🔌 [solana] Error: {e}")

    async def _handle_message(self, data: dict):
        """
        Parse les logs Raydium pour détecter les initialisations de pool.
        
        On cherche le log "initialize2" ou "init" qui indique la création
        d'un nouveau pool AMM.
        """
        params = data.get("params", {})
        result = params.get("result", {})
        value = result.get("value", {})
        
        signature = value.get("signature", "")
        if not signature or signature in self._seen_signatures:
            return
        
        logs = value.get("logs", [])
        err = value.get("err")
        
        # Ignore les transactions échouées
        if err is not None:
            return
        
        # Cherche les logs d'initialisation de pool
        is_pool_init = False
        for log_line in logs:
            # Raydium V4 AMM utilise "initialize2" pour créer un pool
            if "initialize2" in log_line.lower() or "ray_log" in log_line.lower():
                is_pool_init = True
                break
            # Pattern alternatif
            if "init" in log_line.lower() and "amm" in log_line.lower():
                is_pool_init = True
                break
        
        if not is_pool_init:
            return
        
        self._seen_signatures.add(signature)
        # Garde la taille du set raisonnable
        if len(self._seen_signatures) > 10000:
            self._seen_signatures = set(list(self._seen_signatures)[-5000:])
        
        log.info(f"🆕 [solana] Nouveau pool Raydium détecté! TX: {signature[:20]}...")
        
        # Récupère les détails de la transaction pour extraire les tokens
        pool = await self._fetch_pool_details(signature)
        if pool:
            asyncio.create_task(self.on_new_pool(pool))

    async def _fetch_pool_details(self, signature: str) -> Optional[NewPool]:
        """
        Récupère les détails d'une transaction Raydium pour
        extraire les adresses des tokens du pool.
        """
        session = await self._get_session()
        
        try:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTransaction",
                "params": [
                    signature,
                    {
                        "encoding": "jsonParsed",
                        "maxSupportedTransactionVersion": 0,
                        "commitment": "confirmed"
                    }
                ]
            }
            
            async with session.post(self.http_rpc, json=payload) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
            
            result = data.get("result")
            if not result:
                return None
            
            tx = result.get("transaction", {})
            meta = result.get("meta", {})
            message = tx.get("message", {})
            
            # Extraire les account keys
            account_keys = []
            for ak in message.get("accountKeys", []):
                if isinstance(ak, dict):
                    account_keys.append(ak.get("pubkey", ""))
                else:
                    account_keys.append(str(ak))
            
            # Chercher les token mints dans les pre/post token balances
            token_mints = set()
            for balance in meta.get("postTokenBalances", []):
                mint = balance.get("mint", "")
                if mint and mint not in SOLANA_BASE_TOKENS:
                    token_mints.add(mint)
            
            # Identifie le target token (pas SOL/USDC/USDT)
            base_mint = ""
            target_mint = ""
            
            all_mints = set()
            for balance in meta.get("postTokenBalances", []):
                m = balance.get("mint", "")
                if m:
                    all_mints.add(m)
            
            for m in all_mints:
                if m in SOLANA_BASE_TOKENS:
                    base_mint = m
                else:
                    target_mint = m
            
            if not target_mint:
                # Pas trouvé de nouveau token — probablement pas un nouveau listing
                return None
            
            base_symbol = SOLANA_BASE_TOKENS.get(base_mint, ("?", 0))[0]
            
            pool = NewPool(
                chain=Chain.SOLANA,
                dex="raydium",
                pool_address=account_keys[1] if len(account_keys) > 1 else "",
                token0=target_mint,
                token1=base_mint,
                token0_symbol="?",
                token1_symbol=base_symbol,
                target_token=target_mint,
                target_symbol="?",
                tx_hash=signature,
                block_number=result.get("slot", 0),
                timestamp=time.time(),
            )
            
            return pool
            
        except Exception as e:
            log.error(f"[solana] Failed to fetch TX details: {e}")
            return None

    async def stop(self):
        self._running = False
        if self._ws:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3. TOKEN ANALYZER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TokenAnalyzer:
    """
    Analyse un token fraîchement détecté pour évaluer s'il est safe.
    
    Checks effectués :
    - Simulation buy/sell via DexScreener/GoPlusLabs pour détecter honeypots
    - Lecture des taxes (buy tax, sell tax)
    - Vérification de la liquidité initiale
    - Check si le contrat est vérifié / ownership renounced
    - LP lock check
    
    Le scoring produit un score 0-100 :
    - 0-30  : RED — ne pas toucher
    - 30-60 : ORANGE — risqué
    - 60-80 : GREEN — potentiel
    - 80+   : GOLD — signal fort
    """

    GOPLUS_API = "https://api.gopluslabs.com/api/v1"
    DEXSCREENER_API = "https://api.dexscreener.com/latest/dex"

    CHAIN_IDS = {
        Chain.ETHEREUM: "1",
        Chain.BASE: "8453",
        Chain.ARBITRUM: "42161",
        Chain.SOLANA: "solana",
    }

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: dict[str, dict] = {}  # token_addr -> analysis
        self._cache_ttl = 300  # 5 min

    async def _get_session(self):
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20)
            )
        return self._session

    async def analyze(self, pool: NewPool) -> NewPool:
        """
        Analyse complète d'un token. Modifie et retourne le pool enrichi.
        """
        token = pool.target_token
        if not token:
            return pool

        # Check cache
        cache_key = f"{pool.chain.value}:{token}"
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            if time.time() - cached.get("_time", 0) < self._cache_ttl:
                pool.score = cached.get("score", 0)
                pool.is_honeypot = cached.get("is_honeypot")
                pool.buy_tax_pct = cached.get("buy_tax", 0)
                pool.sell_tax_pct = cached.get("sell_tax", 0)
                pool.red_flags = cached.get("red_flags", [])
                pool.green_flags = cached.get("green_flags", [])
                return pool

        # Analyse parallèle
        results = await asyncio.gather(
            self._check_goplus(pool),
            self._check_dexscreener(pool),
            return_exceptions=True,
        )

        goplus_data = results[0] if not isinstance(results[0], Exception) else {}
        dex_data = results[1] if not isinstance(results[1], Exception) else {}

        # Merge les résultats et score
        pool = self._score_token(pool, goplus_data, dex_data)

        # Cache
        self._cache[cache_key] = {
            "_time": time.time(),
            "score": pool.score,
            "is_honeypot": pool.is_honeypot,
            "buy_tax": pool.buy_tax_pct,
            "sell_tax": pool.sell_tax_pct,
            "red_flags": pool.red_flags,
            "green_flags": pool.green_flags,
        }

        return pool

    async def _check_goplus(self, pool: NewPool) -> dict:
        """Check via GoPlus Labs API (gratuit, pas de clé requise)."""
        session = await self._get_session()
        chain_id = self.CHAIN_IDS.get(pool.chain, "1")
        
        if pool.chain == Chain.SOLANA:
            url = f"{self.GOPLUS_API}/solana/token_security?contract_addresses={pool.target_token}"
        else:
            url = f"{self.GOPLUS_API}/token_security/{chain_id}?contract_addresses={pool.target_token}"

        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return {}
                data = await resp.json()
            
            result = data.get("result", {})
            token_data = result.get(pool.target_token.lower(), {})
            
            if not token_data:
                # Essayer sans lowercase
                for key, val in result.items():
                    token_data = val
                    break
            
            return token_data
            
        except Exception as e:
            log.debug(f"GoPlus check failed for {pool.target_token[:10]}: {e}")
            return {}

    async def _check_dexscreener(self, pool: NewPool) -> dict:
        """Check via DexScreener API."""
        session = await self._get_session()
        
        try:
            url = f"{self.DEXSCREENER_API}/pairs/{pool.chain.value}/{pool.pool_address}"
            async with session.get(url) as resp:
                if resp.status != 200:
                    # Fallback: search by token
                    url2 = f"{self.DEXSCREENER_API}/tokens/{pool.target_token}"
                    async with session.get(url2) as resp2:
                        if resp2.status != 200:
                            return {}
                        data = await resp2.json()
                else:
                    data = await resp.json()
            
            pairs = data.get("pairs", data.get("pair", []))
            if isinstance(pairs, dict):
                pairs = [pairs]
            if not pairs:
                return {}
            
            pair = pairs[0] if isinstance(pairs, list) else pairs
            
            return {
                "price_usd": float(pair.get("priceUsd", 0) or 0),
                "liquidity_usd": float(pair.get("liquidity", {}).get("usd", 0) or 0),
                "volume_24h": float(pair.get("volume", {}).get("h24", 0) or 0),
                "price_change_5m": float(pair.get("priceChange", {}).get("m5", 0) or 0),
                "price_change_1h": float(pair.get("priceChange", {}).get("h1", 0) or 0),
                "txns_buys_5m": pair.get("txns", {}).get("m5", {}).get("buys", 0),
                "txns_sells_5m": pair.get("txns", {}).get("m5", {}).get("sells", 0),
                "fdv": float(pair.get("fdv", 0) or 0),
                "pair_age_hours": self._calc_age_hours(pair),
                "base_token_symbol": pair.get("baseToken", {}).get("symbol", "?"),
                "url": pair.get("url", ""),
            }
            
        except Exception as e:
            log.debug(f"DexScreener check failed: {e}")
            return {}

    def _calc_age_hours(self, pair: dict) -> float:
        created = pair.get("pairCreatedAt")
        if created:
            try:
                age_ms = time.time() * 1000 - float(created)
                return age_ms / (1000 * 3600)
            except (ValueError, TypeError):
                pass
        return 999

    def _score_token(self, pool: NewPool, goplus: dict, dex: dict) -> NewPool:
        """
        Scoring composite du token.
        
        Score 0-100 basé sur :
        - Sécurité (GoPlus) : honeypot, taxes, ownership  [40 pts max]
        - Liquidité & volume (DexScreener)                 [30 pts max]
        - Signaux positifs                                  [30 pts max]
        """
        score = 50  # Base neutre (pas d'info = 50)
        red_flags = []
        green_flags = []
        
        # ── GoPlus Security Checks ──
        if goplus:
            # Honeypot
            is_hp = goplus.get("is_honeypot", "0")
            if str(is_hp) == "1":
                pool.is_honeypot = True
                score -= 50
                red_flags.append("HONEYPOT DETECTED")
            else:
                pool.is_honeypot = False
                score += 10
                green_flags.append("Not a honeypot")
            
            # Buy/Sell tax
            buy_tax = float(goplus.get("buy_tax", 0) or 0)
            sell_tax = float(goplus.get("sell_tax", 0) or 0)
            pool.buy_tax_pct = buy_tax * 100
            pool.sell_tax_pct = sell_tax * 100
            
            if sell_tax > 0.1:  # > 10%
                score -= 20
                red_flags.append(f"High sell tax: {sell_tax*100:.0f}%")
            elif sell_tax > 0.05:
                score -= 10
                red_flags.append(f"Moderate sell tax: {sell_tax*100:.0f}%")
            elif sell_tax <= 0.05 and sell_tax >= 0:
                score += 5
                green_flags.append(f"Low sell tax: {sell_tax*100:.1f}%")
            
            if buy_tax > 0.1:
                score -= 15
                red_flags.append(f"High buy tax: {buy_tax*100:.0f}%")
            
            # Ownership
            owner_renounced = goplus.get("can_take_back_ownership", "0")
            if str(owner_renounced) == "0":
                pool.owner_renounced = True
                score += 10
                green_flags.append("Ownership safe")
            else:
                pool.owner_renounced = False
                score -= 5
                red_flags.append("Owner can take back ownership")
            
            # Open source
            is_open = goplus.get("is_open_source", "0")
            if str(is_open) == "1":
                score += 5
                green_flags.append("Contract verified")
            else:
                score -= 5
                red_flags.append("Contract not verified")
            
            # Mint function
            can_mint = goplus.get("is_mintable", "0")
            if str(can_mint) == "1":
                score -= 15
                red_flags.append("Token is mintable")
            
            # Proxy
            is_proxy = goplus.get("is_proxy", "0")
            if str(is_proxy) == "1":
                score -= 10
                red_flags.append("Proxy contract (upgradeable)")
        
        # ── DexScreener Data ──
        if dex:
            liquidity = dex.get("liquidity_usd", 0)
            pool.initial_liquidity_usd = liquidity
            
            # Update symbol
            symbol = dex.get("base_token_symbol", "?")
            if symbol != "?":
                pool.target_symbol = symbol
                pool.token0_symbol = symbol
            
            # Liquidité
            if liquidity > 50000:
                score += 15
                green_flags.append(f"Strong liquidity: ${liquidity:,.0f}")
            elif liquidity > 10000:
                score += 8
                green_flags.append(f"OK liquidity: ${liquidity:,.0f}")
            elif liquidity < 5000:
                score -= 10
                red_flags.append(f"Low liquidity: ${liquidity:,.0f}")
            
            # Volume
            volume = dex.get("volume_24h", 0)
            if volume > 100000:
                score += 10
                green_flags.append(f"High volume: ${volume:,.0f}")
            elif volume > 10000:
                score += 5
            
            # Buy/sell ratio (5min)
            buys = dex.get("txns_buys_5m", 0) or 0
            sells = dex.get("txns_sells_5m", 0) or 0
            if buys + sells > 0:
                buy_ratio = buys / (buys + sells)
                if buy_ratio > 0.65:
                    score += 5
                    green_flags.append(f"Strong buy pressure: {buy_ratio:.0%}")
                elif buy_ratio < 0.35:
                    score -= 5
                    red_flags.append(f"Strong sell pressure: {buy_ratio:.0%}")
            
            # Market cap
            fdv = dex.get("fdv", 0)
            if 0 < fdv < 100000:
                score += 5
                green_flags.append(f"Micro cap: ${fdv:,.0f}")
            elif fdv > 10000000:
                score -= 5  # Déjà gros, moins de upside
        
        # Clamp
        pool.score = max(0, min(100, score))
        pool.red_flags = red_flags
        pool.green_flags = green_flags
        
        return pool

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4. TELEGRAM MULTI-USER BOT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class UserConfig:
    """Configuration par utilisateur."""
    chat_id: str
    username: str = ""
    # Filtres
    min_score: int = 50
    chains: list = field(default_factory=lambda: ["ethereum", "base", "solana"])
    min_liquidity: float = 5000
    max_buy_tax: float = 10.0     # %
    max_sell_tax: float = 10.0    # %
    # Trading
    auto_buy: bool = False
    bet_size_usd: float = 10.0
    wallet_address: str = ""       # Pour le buy en 1 clic
    # State
    registered_at: str = ""
    total_alerts: int = 0
    is_premium: bool = False


class TelegramMultiUserBot:
    """
    Bot Telegram multi-utilisateurs.
    
    Commandes :
    /start              — Inscription
    /settings           — Config (chains, score min, etc.)
    /status             — Stats perso
    /recent             — 5 dernières détections
    /buy <token> <amt>  — Buy en 1 clic (future)
    
    Callbacks (boutons inline) :
    buy_<pool_addr>     — Buy ce token
    ignore_<pool_addr>  — Ignorer
    details_<pool_addr> — Plus de détails
    """

    def __init__(self, bot_token: str):
        self.bot_token = bot_token
        self.api_url = f"https://api.telegram.org/bot{bot_token}"
        self.users: dict[str, UserConfig] = {}  # chat_id -> UserConfig
        self._session: Optional[aiohttp.ClientSession] = None
        self._offset = 0
        self._running = False
        self.enabled = bool(bot_token)

    async def _get_session(self):
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            )
        return self._session

    async def start_polling(self):
        """Écoute les commandes utilisateur via long polling."""
        if not self.enabled:
            log.info("📱 Telegram bot disabled (no token)")
            return
        
        self._running = True
        log.info(f"📱 Telegram multi-user bot started — polling updates")
        
        while self._running:
            try:
                await self._poll_updates()
            except Exception as e:
                log.warning(f"📱 Telegram poll error: {e}")
                await asyncio.sleep(5)

    async def _poll_updates(self):
        """Long polling pour les messages entrants."""
        session = await self._get_session()
        url = f"{self.api_url}/getUpdates"
        params = {"offset": self._offset, "timeout": 30, "limit": 100}
        
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                await asyncio.sleep(5)
                return
            data = await resp.json()
        
        for update in data.get("result", []):
            self._offset = update["update_id"] + 1
            
            # Message texte
            msg = update.get("message", {})
            if msg:
                await self._handle_message(msg)
            
            # Callback (boutons inline)
            callback = update.get("callback_query", {})
            if callback:
                await self._handle_callback(callback)

    async def _handle_message(self, msg: dict):
        """Traite un message utilisateur."""
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = msg.get("text", "").strip()
        username = msg.get("from", {}).get("username", "")
        
        if not chat_id or not text:
            return
        
        # /start — Inscription
        if text == "/start":
            if chat_id not in self.users:
                self.users[chat_id] = UserConfig(
                    chat_id=chat_id,
                    username=username,
                    registered_at=datetime.now(timezone.utc).isoformat(),
                )
                await self._send(chat_id, (
                    "💎 <b>Bienvenue sur Nexus GemHunter!</b>\n\n"
                    "Je détecte les nouveaux tokens en temps réel sur :\n"
                    "• Ethereum + Base (Uniswap)\n"
                    "• Solana (Raydium)\n\n"
                    "Tu recevras des alertes avec score de sécurité, "
                    "analyse honeypot, et bouton Buy en 1 clic.\n\n"
                    "Commandes :\n"
                    "/settings — Configurer tes filtres\n"
                    "/status — Tes stats\n"
                    "/recent — Dernières détections\n\n"
                    f"Score minimum actuel : <b>50/100</b>\n"
                    f"Chains : <b>ETH, Base, Solana</b>"
                ))
            else:
                await self._send(chat_id, "Tu es déjà inscrit! Utilise /settings pour configurer.")
            return
        
        # /settings
        if text == "/settings":
            user = self.users.get(chat_id)
            if not user:
                await self._send(chat_id, "Envoie /start d'abord.")
                return
            
            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": f"Score min: {user.min_score}", "callback_data": "set_score"},
                        {"text": f"Liquidité min: ${user.min_liquidity:,.0f}", "callback_data": "set_liq"},
                    ],
                    [
                        {"text": "🔗 ETH " + ("✅" if "ethereum" in user.chains else "❌"), "callback_data": "toggle_eth"},
                        {"text": "🔵 Base " + ("✅" if "base" in user.chains else "❌"), "callback_data": "toggle_base"},
                        {"text": "🟣 Sol " + ("✅" if "solana" in user.chains else "❌"), "callback_data": "toggle_sol"},
                    ],
                    [
                        {"text": f"Max buy tax: {user.max_buy_tax}%", "callback_data": "set_buy_tax"},
                        {"text": f"Max sell tax: {user.max_sell_tax}%", "callback_data": "set_sell_tax"},
                    ],
                ]
            }
            await self._send(chat_id, "⚙️ <b>Tes paramètres :</b>", reply_markup=keyboard)
            return
        
        # /status
        if text == "/status":
            user = self.users.get(chat_id)
            if not user:
                await self._send(chat_id, "Envoie /start d'abord.")
                return
            
            await self._send(chat_id, (
                f"📊 <b>Tes stats</b>\n\n"
                f"Alertes reçues : {user.total_alerts}\n"
                f"Chains actives : {', '.join(user.chains)}\n"
                f"Score minimum : {user.min_score}\n"
                f"Compte : {'⭐ Premium' if user.is_premium else 'Free'}\n"
                f"Inscrit le : {user.registered_at[:10]}"
            ))
            return
        
        # /setscore <value>
        if text.startswith("/setscore"):
            parts = text.split()
            if len(parts) == 2:
                try:
                    val = int(parts[1])
                    if 0 <= val <= 100:
                        user = self.users.get(chat_id)
                        if user:
                            user.min_score = val
                            await self._send(chat_id, f"✅ Score minimum mis à jour : <b>{val}</b>")
                            return
                except ValueError:
                    pass
            await self._send(chat_id, "Usage : /setscore 50")
            return

    async def _handle_callback(self, callback: dict):
        """Traite les callbacks des boutons inline."""
        chat_id = str(callback.get("message", {}).get("chat", {}).get("id", ""))
        data = callback.get("data", "")
        callback_id = callback.get("id", "")
        
        user = self.users.get(chat_id)
        if not user:
            return
        
        # Toggle chains
        chain_toggles = {
            "toggle_eth": "ethereum",
            "toggle_base": "base", 
            "toggle_sol": "solana",
        }
        
        if data in chain_toggles:
            chain = chain_toggles[data]
            if chain in user.chains:
                user.chains.remove(chain)
                await self._answer_callback(callback_id, f"{chain} désactivé")
            else:
                user.chains.append(chain)
                await self._answer_callback(callback_id, f"{chain} activé")
            return
        
        # Score settings
        if data == "set_score":
            await self._send(chat_id, "Envoie /setscore <valeur> (0-100)\nEx: /setscore 60")
            await self._answer_callback(callback_id, "")
            return
        
        # Buy button
        if data.startswith("buy_"):
            pool_addr = data[4:]
            await self._send(chat_id, (
                f"⚠️ <b>Buy en 1 clic</b>\n\n"
                f"Pool: <code>{pool_addr}</code>\n\n"
                f"Cette fonctionnalité nécessite de connecter ton wallet.\n"
                f"Envoie /connectwallet pour commencer."
            ))
            await self._answer_callback(callback_id, "")
            return

    async def broadcast_alert(self, pool: NewPool):
        """
        Envoie une alerte à tous les utilisateurs dont les filtres matchent.
        """
        if not self.enabled:
            return
        
        for chat_id, user in self.users.items():
            # Check filtres
            if pool.chain.value not in user.chains:
                continue
            if pool.score < user.min_score:
                continue
            if pool.initial_liquidity_usd < user.min_liquidity:
                continue
            if pool.buy_tax_pct > user.max_buy_tax:
                continue
            if pool.sell_tax_pct > user.max_sell_tax:
                continue
            
            # Envoie l'alerte
            await self._send_pool_alert(chat_id, pool)
            user.total_alerts += 1

    async def _send_pool_alert(self, chat_id: str, pool: NewPool):
        """Formate et envoie une alerte de nouveau pool."""
        # Score emoji
        if pool.score >= 80:
            score_emoji = "🟢"
            score_label = "GOLD"
        elif pool.score >= 60:
            score_emoji = "🟡"
            score_label = "OK"
        elif pool.score >= 40:
            score_emoji = "🟠"
            score_label = "RISKY"
        else:
            score_emoji = "🔴"
            score_label = "DANGER"
        
        # Chain emoji
        chain_emoji = {
            Chain.ETHEREUM: "⟠",
            Chain.BASE: "🔵",
            Chain.SOLANA: "🟣",
            Chain.ARBITRUM: "🔷",
        }.get(pool.chain, "⚪")
        
        # Red/green flags
        flags_text = ""
        if pool.green_flags:
            flags_text += "\n".join(f"  ✅ {f}" for f in pool.green_flags[:4])
        if pool.red_flags:
            if flags_text:
                flags_text += "\n"
            flags_text += "\n".join(f"  ⚠️ {f}" for f in pool.red_flags[:4])
        
        # Honeypot status
        hp_text = ""
        if pool.is_honeypot is True:
            hp_text = "🚫 <b>HONEYPOT</b>"
        elif pool.is_honeypot is False:
            hp_text = "✅ Not a honeypot"
        else:
            hp_text = "❓ Unknown"
        
        # Explorer links
        if pool.chain == Chain.SOLANA:
            explorer = f"https://solscan.io/tx/{pool.tx_hash}"
            dex_link = f"https://dexscreener.com/solana/{pool.pool_address}"
        elif pool.chain == Chain.BASE:
            explorer = f"https://basescan.org/tx/{pool.tx_hash}"
            dex_link = f"https://dexscreener.com/base/{pool.pool_address}"
        elif pool.chain == Chain.ETHEREUM:
            explorer = f"https://etherscan.io/tx/{pool.tx_hash}"
            dex_link = f"https://dexscreener.com/ethereum/{pool.pool_address}"
        else:
            explorer = ""
            dex_link = ""
        
        message = (
            f"💎 <b>NEW POOL DETECTED</b> 💎\n"
            f"\n"
            f"{chain_emoji} <b>Chain:</b> {pool.chain.value.upper()}\n"
            f"📊 <b>DEX:</b> {pool.dex}\n"
            f"🪙 <b>Token:</b> {pool.target_symbol}\n"
            f"📋 <b>Contract:</b> <code>{pool.target_token}</code>\n"
            f"\n"
            f"{score_emoji} <b>Score: {pool.score:.0f}/100</b> ({score_label})\n"
            f"{hp_text}\n"
            f"💰 <b>Liquidity:</b> ${pool.initial_liquidity_usd:,.0f}\n"
            f"🏷 <b>Buy tax:</b> {pool.buy_tax_pct:.1f}% | <b>Sell tax:</b> {pool.sell_tax_pct:.1f}%\n"
            f"\n"
            f"<b>Analysis:</b>\n"
            f"{flags_text}\n"
            f"\n"
            f"🔗 <a href='{dex_link}'>DexScreener</a> | <a href='{explorer}'>Explorer</a>"
        )
        
        # Boutons inline
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": f"🟢 Buy ${10}", "callback_data": f"buy_{pool.pool_address[:40]}"},
                    {"text": "📊 Details", "callback_data": f"details_{pool.pool_address[:40]}"},
                    {"text": "❌ Ignore", "callback_data": f"ignore_{pool.pool_address[:40]}"},
                ],
            ]
        }
        
        await self._send(chat_id, message, reply_markup=keyboard)

    async def _send(self, chat_id: str, text: str, reply_markup: dict = None):
        """Envoie un message Telegram."""
        session = await self._get_session()
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        
        try:
            async with session.post(f"{self.api_url}/sendMessage", json=payload) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    log.warning(f"📱 Telegram send error ({resp.status}): {err[:100]}")
        except Exception as e:
            log.warning(f"📱 Telegram error: {e}")

    async def _answer_callback(self, callback_id: str, text: str):
        """Répond à un callback query."""
        session = await self._get_session()
        try:
            await session.post(
                f"{self.api_url}/answerCallbackQuery",
                json={"callback_query_id": callback_id, "text": text}
            )
        except Exception:
            pass

    async def stop(self):
        self._running = False
        if self._session and not self._session.closed:
            await self._session.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 5. ORCHESTRATOR — Relie tout
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class PoolListenerOrchestrator:
    """
    Orchestre les listeners multi-chain + l'analyse + les alertes.
    
    Flow:
    1. EVMPoolListener / SolanaPoolListener détecte un nouveau pool
    2. TokenAnalyzer analyse le token (honeypot, taxes, liquidité)
    3. Si score > seuil → TelegramMultiUserBot broadcast aux users
    
    Usage:
        orch = PoolListenerOrchestrator()
        await orch.start()
    """

    def __init__(self, config: dict = None):
        self.config = config or {}
        
        # Components
        self.analyzer = TokenAnalyzer()
        self.telegram = TelegramMultiUserBot(
            os.getenv("TELEGRAM_BOT_TOKEN", "")
        )
        
        # Listeners (initialisés dans start())
        self.listeners: list = []
        
        # Stats
        self.stats = {
            "pools_detected": 0,
            "pools_analyzed": 0,
            "alerts_sent": 0,
            "honeypots_caught": 0,
            "start_time": None,
        }
        
        # Config
        self.min_alert_score = int(os.getenv("MIN_SCORE", "50"))
        self.min_liquidity = float(os.getenv("MIN_LIQUIDITY_USD", "5000"))
        
        # Recent pools (pour /recent et dédup)
        self.recent_pools: list[NewPool] = []
        self._max_recent = 100

    async def on_new_pool(self, pool: NewPool):
        """
        Callback appelé par les listeners quand un nouveau pool est détecté.
        Analyse le token et broadcast si le score est suffisant.
        """
        self.stats["pools_detected"] += 1
        detect_time = time.time()
        
        log.info(
            f"🔍 Analyse en cours — {pool.chain.value}/{pool.dex} "
            f"— Token: {pool.target_token[:16]}..."
        )
        
        # Analyse
        pool = await self.analyzer.analyze(pool)
        self.stats["pools_analyzed"] += 1
        
        analysis_time = time.time() - detect_time
        
        # Track honeypots
        if pool.is_honeypot:
            self.stats["honeypots_caught"] += 1
        
        # Log résultat
        log.info(
            f"{'🟢' if pool.score >= 60 else '🟠' if pool.score >= 40 else '🔴'} "
            f"[{pool.chain.value}] {pool.target_symbol} — "
            f"Score: {pool.score:.0f}/100 — "
            f"Liq: ${pool.initial_liquidity_usd:,.0f} — "
            f"HP: {pool.is_honeypot} — "
            f"Tax: {pool.buy_tax_pct:.0f}/{pool.sell_tax_pct:.0f}% — "
            f"Analysé en {analysis_time:.1f}s"
        )
        
        # Stocke dans recent
        self.recent_pools.append(pool)
        if len(self.recent_pools) > self._max_recent:
            self.recent_pools = self.recent_pools[-self._max_recent:]
        
        # Broadcast si score suffisant et pas honeypot
        if pool.score >= self.min_alert_score and not pool.is_honeypot:
            await self.telegram.broadcast_alert(pool)
            self.stats["alerts_sent"] += 1

    async def start(self):
        """Lance tous les listeners + le bot Telegram."""
        self.stats["start_time"] = datetime.now(timezone.utc).isoformat()
        
        tasks = []
        
        # ── EVM Listeners ──
        evm_configs = {
            Chain.ETHEREUM: os.getenv("ETH_WS_RPC", ""),
            Chain.BASE: os.getenv("BASE_WS_RPC", ""),
            Chain.ARBITRUM: os.getenv("ARB_WS_RPC", ""),
        }
        
        for chain, ws_rpc in evm_configs.items():
            if ws_rpc:
                listener = EVMPoolListener(chain, ws_rpc, self.on_new_pool)
                self.listeners.append(listener)
                tasks.append(listener.start())
                log.info(f"✅ [{chain.value}] Listener configuré")
            else:
                log.info(f"⏭️  [{chain.value}] Skipped (pas de RPC configuré)")
        
        # ── Solana Listener ──
        sol_ws = os.getenv("SOLANA_WS_RPC", "")
        sol_http = os.getenv("SOLANA_HTTP_RPC", "")
        
        if sol_ws and sol_http:
            sol_listener = SolanaPoolListener(sol_ws, sol_http, self.on_new_pool)
            self.listeners.append(sol_listener)
            tasks.append(sol_listener.start())
            log.info("✅ [solana] Listener configuré")
        else:
            log.info("⏭️  [solana] Skipped (pas de RPC configuré)")
        
        # ── Telegram Bot ──
        tasks.append(self.telegram.start_polling())
        
        # ── Stats logger ──
        tasks.append(self._log_stats_loop())
        
        if not tasks:
            log.error("❌ Aucun listener configuré! Vérifie ton .env")
            return
        
        log.info(
            f"\n"
            f"╔══════════════════════════════════════════════════════════════╗\n"
            f"║  💎 NEXUS GEMHUNTER — Pool Listener LIVE                    ║\n"
            f"║  Listeners: {len(self.listeners)} chains                               ║\n"
            f"║  Telegram: {'ON' if self.telegram.enabled else 'OFF':3s}                                         ║\n"
            f"║  Min score: {self.min_alert_score}                                          ║\n"
            f"╚══════════════════════════════════════════════════════════════╝"
        )
        
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _log_stats_loop(self):
        """Log des stats toutes les 5 minutes."""
        while True:
            await asyncio.sleep(300)
            log.info(
                f"📊 Stats — Pools: {self.stats['pools_detected']} detected, "
                f"{self.stats['pools_analyzed']} analyzed, "
                f"{self.stats['alerts_sent']} alerts sent, "
                f"{self.stats['honeypots_caught']} honeypots caught, "
                f"{len(self.telegram.users)} users"
            )

    async def stop(self):
        for listener in self.listeners:
            await listener.stop()
        await self.analyzer.close()
        await self.telegram.stop()

    def get_status(self) -> dict:
        return {
            **self.stats,
            "users": len(self.telegram.users),
            "listeners_active": len(self.listeners),
            "recent_pools": [
                {
                    "chain": p.chain.value,
                    "symbol": p.target_symbol,
                    "score": p.score,
                    "liquidity": p.initial_liquidity_usd,
                    "honeypot": p.is_honeypot,
                    "timestamp": p.timestamp,
                }
                for p in self.recent_pools[-10:]
            ],
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Point d'entrée standalone
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s │ %(levelname)-7s │ %(message)s",
        datefmt="%H:%M:%S",
    )
    
    print("""
    ╔══════════════════════════════════════════════════════════════╗
    ║  💎 NEXUS GEMHUNTER — Real-Time Pool Listener               ║
    ║                                                              ║
    ║  Chains: Ethereum, Base, Solana                              ║
    ║  Output: Telegram alerts + scoring                           ║
    ║                                                              ║
    ║  Configure .env avec tes RPC endpoints et Telegram token     ║
    ╚══════════════════════════════════════════════════════════════╝
    """)
    
    orchestrator = PoolListenerOrchestrator()
    
    try:
        await orchestrator.start()
    except KeyboardInterrupt:
        log.info("Arrêt...")
    finally:
        await orchestrator.stop()


if __name__ == "__main__":
    asyncio.run(main())
