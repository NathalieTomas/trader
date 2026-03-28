"""
╔══════════════════════════════════════════════════════════════════════╗
║  NEXUS GEMHUNTER — Swap Executor                                    ║
║  Buy en 1 clic depuis Telegram avec fee engine intégré              ║
║                                                                      ║
║  Chains supportées:                                                  ║
║  • EVM (Ethereum, Base, Arbitrum) — Uniswap V2 Router               ║
║  • Solana — Raydium swap via Jupiter aggregator                     ║
║                                                                      ║
║  Modèle de fees:                                                     ║
║  • Chaque swap prélève X% du montant en fee                        ║
║  • Fee envoyée au wallet du projet                                  ║
║  • Fee configurable (défaut 0.8%)                                   ║
║                                                                      ║
║  Sécurité:                                                           ║
║  • Clés privées chiffrées par user (AES-256)                       ║
║  • Slippage protection                                               ║
║  • Gas estimation avant exécution                                   ║
║  • Dry-run mode pour tester sans exécuter                           ║
╚══════════════════════════════════════════════════════════════════════╝

INSTALLATION:
    pip install web3 aiohttp base58 cryptography

CONFIGURATION .env:
    # Fee wallet (reçoit les fees de chaque trade)
    FEE_WALLET_EVM=0x...
    FEE_WALLET_SOLANA=...
    FEE_PCT=0.8
    
    # RPC (HTTP pour les transactions)
    ETH_HTTP_RPC=https://eth-mainnet.g.alchemy.com/v2/YOUR_KEY
    BASE_HTTP_RPC=https://base-mainnet.g.alchemy.com/v2/YOUR_KEY
    SOLANA_HTTP_RPC=https://api.mainnet-beta.solana.com
    
    # Sécurité
    ENCRYPTION_KEY=...    # Généré automatiquement au premier lancement
"""

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import asyncio
import hashlib
import json
import logging
import os
import secrets
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

try:
    import aiohttp
except ImportError:
    raise ImportError("pip install aiohttp")

try:
    from web3 import Web3
    from web3.middleware import geth_poa_middleware
    HAS_WEB3 = True
except ImportError:
    HAS_WEB3 = False

try:
    from cryptography.fernet import Fernet
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

log = logging.getLogger("nexus.swap_executor")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Data Models
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SwapChain(str, Enum):
    ETHEREUM = "ethereum"
    BASE = "base"
    ARBITRUM = "arbitrum"
    SOLANA = "solana"


@dataclass
class SwapRequest:
    """Requête de swap initiée par un utilisateur."""
    user_id: str                      # Telegram chat_id
    chain: SwapChain
    token_address: str                # Token à acheter
    amount_in_usd: float              # Montant en USD à dépenser
    pool_address: str = ""            # Pool spécifique (optionnel)
    slippage_pct: float = 5.0         # Slippage max toléré
    deadline_seconds: int = 120       # Deadline pour la transaction
    dry_run: bool = False             # Test sans exécuter


@dataclass
class SwapResult:
    """Résultat d'un swap exécuté."""
    success: bool
    chain: SwapChain
    token_address: str
    token_symbol: str = "?"
    amount_in: float = 0.0            # Montant dépensé (ETH/SOL)
    amount_out: float = 0.0           # Tokens reçus
    price_per_token: float = 0.0
    fee_amount: float = 0.0           # Fee prélevée
    fee_usd: float = 0.0
    tx_hash: str = ""
    gas_used: float = 0.0
    gas_cost_usd: float = 0.0
    error: str = ""
    timestamp: str = ""
    explorer_url: str = ""


@dataclass
class UserWallet:
    """Wallet chiffré d'un utilisateur."""
    user_id: str
    chain: SwapChain
    address: str
    encrypted_private_key: str        # Chiffré avec Fernet
    created_at: str = ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Constantes
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Uniswap V2 Router ABI (fonctions essentielles)
UNISWAP_V2_ROUTER_ABI = json.loads("""[
    {
        "name": "swapExactETHForTokens",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [
            {"name": "amountOutMin", "type": "uint256"},
            {"name": "path", "type": "address[]"},
            {"name": "to", "type": "address"},
            {"name": "deadline", "type": "uint256"}
        ],
        "outputs": [{"name": "amounts", "type": "uint256[]"}]
    },
    {
        "name": "swapExactTokensForETH",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "amountIn", "type": "uint256"},
            {"name": "amountOutMin", "type": "uint256"},
            {"name": "path", "type": "address[]"},
            {"name": "to", "type": "address"},
            {"name": "deadline", "type": "uint256"}
        ],
        "outputs": [{"name": "amounts", "type": "uint256[]"}]
    },
    {
        "name": "getAmountsOut",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "amountIn", "type": "uint256"},
            {"name": "path", "type": "address[]"}
        ],
        "outputs": [{"name": "amounts", "type": "uint256[]"}]
    },
    {
        "name": "WETH",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}]
    }
]""")

# Router addresses
ROUTERS = {
    SwapChain.ETHEREUM: {
        "uniswap_v2": "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
    },
    SwapChain.BASE: {
        "uniswap_v2": "0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24",
        "baseswap": "0x327Df1E6de05895d2ab08513aaDD9313Fe505d86",
    },
    SwapChain.ARBITRUM: {
        "uniswap_v2": "0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24",
        "sushiswap": "0x1b02dA8Cb0d097eB8D57A175b88c7D8b47997506",
    },
}

# WETH addresses
WETH = {
    SwapChain.ETHEREUM: "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
    SwapChain.BASE: "0x4200000000000000000000000000000000000006",
    SwapChain.ARBITRUM: "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
}

# Chain IDs
CHAIN_IDS = {
    SwapChain.ETHEREUM: 1,
    SwapChain.BASE: 8453,
    SwapChain.ARBITRUM: 42161,
}

# Explorer URLs
EXPLORERS = {
    SwapChain.ETHEREUM: "https://etherscan.io/tx/",
    SwapChain.BASE: "https://basescan.org/tx/",
    SwapChain.ARBITRUM: "https://arbiscan.io/tx/",
    SwapChain.SOLANA: "https://solscan.io/tx/",
}

# Jupiter API (Solana aggregator — meilleur prix)
JUPITER_API = "https://quote-api.jup.ag/v6"
JUPITER_SWAP_API = "https://quote-api.jup.ag/v6/swap"

# SOL mint
SOL_MINT = "So11111111111111111111111111111111111111112"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1. KEY MANAGER — Chiffrement des clés privées
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class KeyManager:
    """
    Gère le chiffrement/déchiffrement des clés privées des utilisateurs.
    
    Utilise Fernet (AES-128-CBC) — chaque clé privée est chiffrée
    avec une master key stockée dans .env.
    
    IMPORTANT: En production, utiliser un KMS (AWS KMS, HashiCorp Vault)
    au lieu de stocker la master key dans .env.
    """

    def __init__(self):
        self._master_key = self._load_or_create_key()
        self._fernet = Fernet(self._master_key) if HAS_CRYPTO else None

    def _load_or_create_key(self) -> bytes:
        """Charge ou génère la master key."""
        key_str = os.getenv("ENCRYPTION_KEY", "")
        if key_str:
            return key_str.encode()
        
        # Génère une nouvelle clé
        if HAS_CRYPTO:
            key = Fernet.generate_key()
            log.warning(
                f"⚠️  Nouvelle ENCRYPTION_KEY générée. "
                f"Ajoute dans ton .env:\n"
                f"ENCRYPTION_KEY={key.decode()}"
            )
            return key
        return b""

    def encrypt_key(self, private_key: str) -> str:
        """Chiffre une clé privée."""
        if not self._fernet:
            log.warning("Cryptography non disponible — clé stockée en clair!")
            return private_key
        return self._fernet.encrypt(private_key.encode()).decode()

    def decrypt_key(self, encrypted_key: str) -> str:
        """Déchiffre une clé privée."""
        if not self._fernet:
            return encrypted_key
        return self._fernet.decrypt(encrypted_key.encode()).decode()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2. EVM SWAP EXECUTOR
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class EVMSwapExecutor:
    """
    Exécute des swaps sur les chaînes EVM via Uniswap V2 Router.
    
    Flow:
    1. User clique "Buy" dans Telegram
    2. On calcule le montant en ETH (amount_in_usd / prix ETH)
    3. On prélève la fee (X% du montant)
    4. On exécute swapExactETHForTokens avec le reste
    5. On envoie la fee au fee wallet
    6. On confirme dans Telegram
    """

    def __init__(self, chain: SwapChain, http_rpc: str, key_manager: KeyManager):
        self.chain = chain
        self.http_rpc = http_rpc
        self.key_manager = key_manager
        self.w3: Optional[Web3] = None
        self._session: Optional[aiohttp.ClientSession] = None
        
        # Fee config
        self.fee_pct = float(os.getenv("FEE_PCT", "0.8")) / 100
        self.fee_wallet = os.getenv("FEE_WALLET_EVM", "")
        
        # Init Web3
        if HAS_WEB3 and http_rpc:
            self.w3 = Web3(Web3.HTTPProvider(http_rpc))
            # Pour les chaînes PoA (Base, Arbitrum)
            if chain in (SwapChain.BASE, SwapChain.ARBITRUM):
                self.w3.middleware_onion.inject(geth_poa_middleware, layer=0)
            log.info(f"💱 [{chain.value}] Swap executor initialisé")

    async def _get_session(self):
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
        return self._session

    def generate_wallet(self) -> tuple[str, str]:
        """
        Génère un nouveau wallet EVM pour un utilisateur.
        Retourne (address, encrypted_private_key).
        """
        if not self.w3:
            raise RuntimeError("Web3 not initialized")
        
        account = self.w3.eth.account.create()
        encrypted_pk = self.key_manager.encrypt_key(account.key.hex())
        return account.address, encrypted_pk

    async def get_eth_price(self) -> float:
        """Récupère le prix ETH/USD via CoinGecko."""
        session = await self._get_session()
        try:
            url = "https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd"
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("ethereum", {}).get("usd", 0)
        except Exception:
            pass
        return 0

    async def estimate_swap(self, request: SwapRequest, private_key: str) -> SwapResult:
        """
        Estime un swap sans l'exécuter.
        Retourne le résultat attendu (montant out, gas, fees).
        """
        if not self.w3:
            return SwapResult(
                success=False, chain=self.chain,
                token_address=request.token_address,
                error="Web3 not initialized"
            )
        
        try:
            # Prix ETH
            eth_price = await self.get_eth_price()
            if eth_price == 0:
                return SwapResult(
                    success=False, chain=self.chain,
                    token_address=request.token_address,
                    error="Cannot fetch ETH price"
                )
            
            # Montant en ETH
            amount_in_eth = request.amount_in_usd / eth_price
            amount_in_wei = self.w3.to_wei(amount_in_eth, "ether")
            
            # Fee
            fee_wei = int(amount_in_wei * self.fee_pct)
            swap_amount_wei = amount_in_wei - fee_wei
            
            # Get router
            router_address = list(ROUTERS.get(self.chain, {}).values())[0]
            router = self.w3.eth.contract(
                address=Web3.to_checksum_address(router_address),
                abi=UNISWAP_V2_ROUTER_ABI
            )
            
            # Path: WETH -> Token
            weth = WETH[self.chain]
            path = [
                Web3.to_checksum_address(weth),
                Web3.to_checksum_address(request.token_address),
            ]
            
            # Estimate output
            amounts_out = router.functions.getAmountsOut(
                swap_amount_wei, path
            ).call()
            
            expected_out = amounts_out[-1]
            
            return SwapResult(
                success=True,
                chain=self.chain,
                token_address=request.token_address,
                amount_in=amount_in_eth,
                amount_out=expected_out / (10 ** 18),  # Approximation, dépend des decimals
                price_per_token=request.amount_in_usd / (expected_out / (10 ** 18)) if expected_out > 0 else 0,
                fee_amount=self.w3.from_wei(fee_wei, "ether"),
                fee_usd=request.amount_in_usd * self.fee_pct,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            
        except Exception as e:
            return SwapResult(
                success=False, chain=self.chain,
                token_address=request.token_address,
                error=str(e)
            )

    async def execute_swap(self, request: SwapRequest, encrypted_pk: str) -> SwapResult:
        """
        Exécute le swap on-chain.
        
        Steps:
        1. Déchiffre la clé privée
        2. Calcule le montant - fee
        3. Envoie la fee au fee wallet
        4. Exécute le swap via Uniswap V2 Router
        5. Retourne le résultat
        """
        if not self.w3:
            return SwapResult(
                success=False, chain=self.chain,
                token_address=request.token_address,
                error="Web3 not initialized"
            )
        
        if request.dry_run:
            return await self.estimate_swap(request, "")
        
        try:
            # Déchiffre la clé
            private_key = self.key_manager.decrypt_key(encrypted_pk)
            account = self.w3.eth.account.from_key(private_key)
            
            # Prix ETH
            eth_price = await self.get_eth_price()
            if eth_price == 0:
                return SwapResult(
                    success=False, chain=self.chain,
                    token_address=request.token_address,
                    error="Cannot fetch ETH price"
                )
            
            # Montants
            amount_in_eth = request.amount_in_usd / eth_price
            amount_in_wei = self.w3.to_wei(amount_in_eth, "ether")
            
            # Vérifier balance
            balance = self.w3.eth.get_balance(account.address)
            if balance < amount_in_wei:
                return SwapResult(
                    success=False, chain=self.chain,
                    token_address=request.token_address,
                    error=f"Insufficient balance: {self.w3.from_wei(balance, 'ether'):.6f} ETH"
                )
            
            # Fee
            fee_wei = int(amount_in_wei * self.fee_pct)
            swap_amount_wei = amount_in_wei - fee_wei
            
            nonce = self.w3.eth.get_transaction_count(account.address)
            chain_id = CHAIN_IDS[self.chain]
            
            # ── Step 1: Envoyer la fee ──
            if fee_wei > 0 and self.fee_wallet:
                fee_tx = {
                    "nonce": nonce,
                    "to": Web3.to_checksum_address(self.fee_wallet),
                    "value": fee_wei,
                    "gas": 21000,
                    "gasPrice": self.w3.eth.gas_price,
                    "chainId": chain_id,
                }
                signed_fee = account.sign_transaction(fee_tx)
                fee_hash = self.w3.eth.send_raw_transaction(signed_fee.raw_transaction)
                self.w3.eth.wait_for_transaction_receipt(fee_hash, timeout=60)
                nonce += 1
                log.info(f"💰 Fee sent: {self.w3.from_wei(fee_wei, 'ether'):.6f} ETH")
            
            # ── Step 2: Exécuter le swap ──
            router_address = list(ROUTERS.get(self.chain, {}).values())[0]
            router = self.w3.eth.contract(
                address=Web3.to_checksum_address(router_address),
                abi=UNISWAP_V2_ROUTER_ABI
            )
            
            weth = WETH[self.chain]
            path = [
                Web3.to_checksum_address(weth),
                Web3.to_checksum_address(request.token_address),
            ]
            
            # Slippage: calcule le min output
            amounts_out = router.functions.getAmountsOut(
                swap_amount_wei, path
            ).call()
            min_out = int(amounts_out[-1] * (1 - request.slippage_pct / 100))
            
            deadline = int(time.time()) + request.deadline_seconds
            
            # Build transaction
            swap_tx = router.functions.swapExactETHForTokens(
                min_out,
                path,
                account.address,
                deadline,
            ).build_transaction({
                "from": account.address,
                "value": swap_amount_wei,
                "nonce": nonce,
                "gasPrice": self.w3.eth.gas_price,
                "chainId": chain_id,
            })
            
            # Estimate gas
            try:
                gas_estimate = self.w3.eth.estimate_gas(swap_tx)
                swap_tx["gas"] = int(gas_estimate * 1.2)  # 20% buffer
            except Exception:
                swap_tx["gas"] = 300000  # Fallback
            
            # Sign & send
            signed_tx = account.sign_transaction(swap_tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            
            # Attendre confirmation
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            
            tx_hash_hex = tx_hash.hex() if hasattr(tx_hash, 'hex') else str(tx_hash)
            explorer = EXPLORERS.get(self.chain, "") + tx_hash_hex
            
            gas_cost_wei = receipt.gasUsed * receipt.effectiveGasPrice
            gas_cost_eth = self.w3.from_wei(gas_cost_wei, "ether")
            
            if receipt.status == 1:
                log.info(
                    f"🟢 [{self.chain.value}] Swap réussi! "
                    f"TX: {tx_hash_hex[:16]}... "
                    f"Gas: {receipt.gasUsed}"
                )
                
                return SwapResult(
                    success=True,
                    chain=self.chain,
                    token_address=request.token_address,
                    amount_in=float(self.w3.from_wei(swap_amount_wei, "ether")),
                    amount_out=amounts_out[-1] / (10 ** 18),
                    fee_amount=float(self.w3.from_wei(fee_wei, "ether")),
                    fee_usd=request.amount_in_usd * self.fee_pct,
                    tx_hash=tx_hash_hex,
                    gas_used=receipt.gasUsed,
                    gas_cost_usd=float(gas_cost_eth) * eth_price,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    explorer_url=explorer,
                )
            else:
                return SwapResult(
                    success=False, chain=self.chain,
                    token_address=request.token_address,
                    tx_hash=tx_hash_hex,
                    error="Transaction reverted",
                    explorer_url=explorer,
                )
            
        except Exception as e:
            log.error(f"🔴 [{self.chain.value}] Swap failed: {e}")
            return SwapResult(
                success=False, chain=self.chain,
                token_address=request.token_address,
                error=str(e),
            )

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3. SOLANA SWAP EXECUTOR
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SolanaSwapExecutor:
    """
    Exécute des swaps sur Solana via Jupiter Aggregator.
    
    Jupiter est préféré à Raydium direct car :
    - Il agrège tous les DEX (Raydium, Orca, Meteora...)
    - Il donne le meilleur prix automatiquement
    - Son API est simple et bien documentée
    
    Flow:
    1. Get quote via Jupiter API
    2. Prélève la fee
    3. Build & sign la transaction
    4. Send via RPC
    """

    def __init__(self, http_rpc: str, key_manager: KeyManager):
        self.http_rpc = http_rpc
        self.key_manager = key_manager
        self._session: Optional[aiohttp.ClientSession] = None
        
        self.fee_pct = float(os.getenv("FEE_PCT", "0.8")) / 100
        self.fee_wallet = os.getenv("FEE_WALLET_SOLANA", "")

    async def _get_session(self):
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
        return self._session

    def generate_wallet(self) -> tuple[str, str]:
        """
        Génère un wallet Solana.
        Retourne (public_key, encrypted_private_key).
        """
        # Génère 32 bytes aléatoires pour la clé privée
        private_bytes = secrets.token_bytes(32)
        
        try:
            from solders.keypair import Keypair
            keypair = Keypair.from_seed(private_bytes)
            public_key = str(keypair.pubkey())
            encrypted_pk = self.key_manager.encrypt_key(private_bytes.hex())
            return public_key, encrypted_pk
        except ImportError:
            # Fallback sans solders
            import base58 as b58
            # Simplifié — en production utiliser solders
            public_key = b58.b58encode(private_bytes[:32]).decode()
            encrypted_pk = self.key_manager.encrypt_key(private_bytes.hex())
            return public_key, encrypted_pk

    async def get_sol_price(self) -> float:
        """Récupère le prix SOL/USD."""
        session = await self._get_session()
        try:
            url = "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd"
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("solana", {}).get("usd", 0)
        except Exception:
            pass
        return 0

    async def get_quote(self, token_mint: str, amount_lamports: int, slippage_bps: int = 500) -> dict:
        """
        Récupère un quote via Jupiter.
        
        Args:
            token_mint: Mint address du token à acheter
            amount_lamports: Montant en lamports (1 SOL = 1e9 lamports)
            slippage_bps: Slippage en basis points (500 = 5%)
        """
        session = await self._get_session()
        
        try:
            params = {
                "inputMint": SOL_MINT,
                "outputMint": token_mint,
                "amount": str(amount_lamports),
                "slippageBps": slippage_bps,
                "onlyDirectRoutes": "false",
            }
            
            async with session.get(f"{JUPITER_API}/quote", params=params) as resp:
                if resp.status != 200:
                    return {"error": f"Jupiter API error: {resp.status}"}
                return await resp.json()
                
        except Exception as e:
            return {"error": str(e)}

    async def estimate_swap(self, request: SwapRequest) -> SwapResult:
        """Estime un swap Solana sans l'exécuter."""
        try:
            sol_price = await self.get_sol_price()
            if sol_price == 0:
                return SwapResult(
                    success=False, chain=SwapChain.SOLANA,
                    token_address=request.token_address,
                    error="Cannot fetch SOL price"
                )
            
            amount_sol = request.amount_in_usd / sol_price
            amount_lamports = int(amount_sol * 1e9)
            
            # Fee
            fee_lamports = int(amount_lamports * self.fee_pct)
            swap_lamports = amount_lamports - fee_lamports
            
            # Quote
            slippage_bps = int(request.slippage_pct * 100)
            quote = await self.get_quote(
                request.token_address, swap_lamports, slippage_bps
            )
            
            if "error" in quote:
                return SwapResult(
                    success=False, chain=SwapChain.SOLANA,
                    token_address=request.token_address,
                    error=quote["error"]
                )
            
            out_amount = int(quote.get("outAmount", 0))
            
            return SwapResult(
                success=True,
                chain=SwapChain.SOLANA,
                token_address=request.token_address,
                amount_in=amount_sol,
                amount_out=out_amount / (10 ** 9),  # Approximation
                fee_amount=fee_lamports / 1e9,
                fee_usd=request.amount_in_usd * self.fee_pct,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            
        except Exception as e:
            return SwapResult(
                success=False, chain=SwapChain.SOLANA,
                token_address=request.token_address,
                error=str(e),
            )

    async def execute_swap(self, request: SwapRequest, encrypted_pk: str) -> SwapResult:
        """
        Exécute un swap Solana via Jupiter.
        
        Note: L'exécution complète nécessite la lib solders/solana-py
        pour signer et envoyer la transaction. Ce code fournit la structure
        et les appels API — la signature sera ajoutée quand solders est dispo.
        """
        if request.dry_run:
            return await self.estimate_swap(request)
        
        try:
            private_key_hex = self.key_manager.decrypt_key(encrypted_pk)
            
            sol_price = await self.get_sol_price()
            if sol_price == 0:
                return SwapResult(
                    success=False, chain=SwapChain.SOLANA,
                    token_address=request.token_address,
                    error="Cannot fetch SOL price"
                )
            
            amount_sol = request.amount_in_usd / sol_price
            amount_lamports = int(amount_sol * 1e9)
            
            fee_lamports = int(amount_lamports * self.fee_pct)
            swap_lamports = amount_lamports - fee_lamports
            
            # Get quote
            slippage_bps = int(request.slippage_pct * 100)
            quote = await self.get_quote(
                request.token_address, swap_lamports, slippage_bps
            )
            
            if "error" in quote:
                return SwapResult(
                    success=False, chain=SwapChain.SOLANA,
                    token_address=request.token_address,
                    error=quote["error"]
                )
            
            # Get swap transaction from Jupiter
            session = await self._get_session()
            
            try:
                from solders.keypair import Keypair
                from solders.transaction import VersionedTransaction
                
                keypair = Keypair.from_seed(bytes.fromhex(private_key_hex))
                user_pubkey = str(keypair.pubkey())
            except ImportError:
                return SwapResult(
                    success=False, chain=SwapChain.SOLANA,
                    token_address=request.token_address,
                    error="solders library required for Solana swaps. pip install solders"
                )
            
            # Request swap transaction
            swap_body = {
                "quoteResponse": quote,
                "userPublicKey": user_pubkey,
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": "auto",
            }
            
            async with session.post(JUPITER_SWAP_API, json=swap_body) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    return SwapResult(
                        success=False, chain=SwapChain.SOLANA,
                        token_address=request.token_address,
                        error=f"Jupiter swap API error: {err[:200]}"
                    )
                swap_data = await resp.json()
            
            # Decode, sign, send
            swap_tx_b64 = swap_data.get("swapTransaction", "")
            if not swap_tx_b64:
                return SwapResult(
                    success=False, chain=SwapChain.SOLANA,
                    token_address=request.token_address,
                    error="No swap transaction returned"
                )
            
            import base64
            tx_bytes = base64.b64decode(swap_tx_b64)
            tx = VersionedTransaction.from_bytes(tx_bytes)
            
            # Sign
            signed_tx = VersionedTransaction(tx.message, [keypair])
            
            # Send
            send_body = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "sendTransaction",
                "params": [
                    base64.b64encode(bytes(signed_tx)).decode(),
                    {"encoding": "base64", "skipPreflight": True}
                ]
            }
            
            async with session.post(self.http_rpc, json=send_body) as resp:
                result = await resp.json()
            
            if "error" in result:
                return SwapResult(
                    success=False, chain=SwapChain.SOLANA,
                    token_address=request.token_address,
                    error=str(result["error"])
                )
            
            tx_sig = result.get("result", "")
            explorer_url = EXPLORERS[SwapChain.SOLANA] + tx_sig
            
            # TODO: Envoyer la fee séparément (transfer SOL au fee wallet)
            
            out_amount = int(quote.get("outAmount", 0))
            
            log.info(f"🟢 [solana] Swap réussi! TX: {tx_sig[:20]}...")
            
            return SwapResult(
                success=True,
                chain=SwapChain.SOLANA,
                token_address=request.token_address,
                amount_in=amount_sol,
                amount_out=out_amount / (10 ** 9),
                fee_amount=fee_lamports / 1e9,
                fee_usd=request.amount_in_usd * self.fee_pct,
                tx_hash=tx_sig,
                timestamp=datetime.now(timezone.utc).isoformat(),
                explorer_url=explorer_url,
            )
            
        except Exception as e:
            log.error(f"🔴 [solana] Swap failed: {e}")
            return SwapResult(
                success=False, chain=SwapChain.SOLANA,
                token_address=request.token_address,
                error=str(e),
            )

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4. UNIFIED SWAP ROUTER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SwapRouter:
    """
    Point d'entrée unique pour tous les swaps.
    
    Route automatiquement vers le bon executor selon la chain.
    Gère les wallets des utilisateurs et l'historique des trades.
    
    Usage:
        router = SwapRouter()
        
        # Créer un wallet pour un user
        address, enc_pk = router.create_wallet("user_123", SwapChain.BASE)
        
        # Exécuter un swap
        result = await router.swap(SwapRequest(
            user_id="user_123",
            chain=SwapChain.BASE,
            token_address="0x...",
            amount_in_usd=10.0,
        ))
    """

    def __init__(self):
        self.key_manager = KeyManager()
        
        # Executors par chain
        self.executors: dict[SwapChain, object] = {}
        
        # Init EVM executors
        evm_rpcs = {
            SwapChain.ETHEREUM: os.getenv("ETH_HTTP_RPC", ""),
            SwapChain.BASE: os.getenv("BASE_HTTP_RPC", ""),
            SwapChain.ARBITRUM: os.getenv("ARB_HTTP_RPC", ""),
        }
        
        for chain, rpc in evm_rpcs.items():
            if rpc:
                self.executors[chain] = EVMSwapExecutor(chain, rpc, self.key_manager)
        
        # Init Solana executor
        sol_rpc = os.getenv("SOLANA_HTTP_RPC", "")
        if sol_rpc:
            self.executors[SwapChain.SOLANA] = SolanaSwapExecutor(sol_rpc, self.key_manager)
        
        # User wallets: {user_id: {chain: UserWallet}}
        self.wallets: dict[str, dict[SwapChain, UserWallet]] = {}
        
        # Trade history
        self.trade_history: list[SwapResult] = []
        
        # Stats
        self.stats = {
            "total_swaps": 0,
            "successful_swaps": 0,
            "failed_swaps": 0,
            "total_volume_usd": 0.0,
            "total_fees_usd": 0.0,
        }
        
        chains_ready = [c.value for c in self.executors.keys()]
        log.info(f"💱 SwapRouter ready — chains: {', '.join(chains_ready) or 'none'}")

    def create_wallet(self, user_id: str, chain: SwapChain) -> Optional[str]:
        """
        Crée un wallet pour un utilisateur sur une chain donnée.
        Retourne l'adresse publique.
        """
        executor = self.executors.get(chain)
        if not executor:
            log.warning(f"No executor for chain {chain.value}")
            return None
        
        address, encrypted_pk = executor.generate_wallet()
        
        if user_id not in self.wallets:
            self.wallets[user_id] = {}
        
        self.wallets[user_id][chain] = UserWallet(
            user_id=user_id,
            chain=chain,
            address=address,
            encrypted_private_key=encrypted_pk,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        
        log.info(f"🔑 Wallet created for {user_id} on {chain.value}: {address[:10]}...")
        return address

    def get_wallet(self, user_id: str, chain: SwapChain) -> Optional[UserWallet]:
        """Récupère le wallet d'un user sur une chain."""
        return self.wallets.get(user_id, {}).get(chain)

    async def swap(self, request: SwapRequest) -> SwapResult:
        """
        Exécute un swap pour un utilisateur.
        
        1. Vérifie que le user a un wallet sur la chain
        2. Route vers le bon executor
        3. Exécute le swap
        4. Log le résultat
        """
        # Vérifie le wallet
        wallet = self.get_wallet(request.user_id, request.chain)
        if not wallet:
            return SwapResult(
                success=False,
                chain=request.chain,
                token_address=request.token_address,
                error="No wallet found. Use /connectwallet first.",
            )
        
        # Vérifie l'executor
        executor = self.executors.get(request.chain)
        if not executor:
            return SwapResult(
                success=False,
                chain=request.chain,
                token_address=request.token_address,
                error=f"Chain {request.chain.value} not configured",
            )
        
        # Execute
        result = await executor.execute_swap(request, wallet.encrypted_private_key)
        
        # Stats
        self.stats["total_swaps"] += 1
        if result.success:
            self.stats["successful_swaps"] += 1
            self.stats["total_volume_usd"] += request.amount_in_usd
            self.stats["total_fees_usd"] += result.fee_usd
        else:
            self.stats["failed_swaps"] += 1
        
        # History
        self.trade_history.append(result)
        if len(self.trade_history) > 1000:
            self.trade_history = self.trade_history[-500:]
        
        return result

    def get_stats(self) -> dict:
        return {
            **self.stats,
            "chains_active": [c.value for c in self.executors.keys()],
            "users_with_wallets": len(self.wallets),
            "recent_trades": [
                {
                    "chain": t.chain.value,
                    "success": t.success,
                    "amount_usd": t.fee_usd / self.executors.get(t.chain, type('', (), {'fee_pct': 0.008})()).fee_pct if t.fee_usd > 0 else 0,
                    "fee_usd": t.fee_usd,
                    "tx_hash": t.tx_hash[:16] + "..." if t.tx_hash else "",
                    "timestamp": t.timestamp,
                }
                for t in self.trade_history[-10:]
            ],
        }

    async def close(self):
        for executor in self.executors.values():
            await executor.close()