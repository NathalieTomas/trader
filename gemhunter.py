"""
╔══════════════════════════════════════════════════════════════════════╗
║  NEXUS TRADER — GEM HUNTER                                         ║
║  Détecte les crypto-pépites AVANT qu'elles explosent               ║
║                                                                      ║
║  Sources:                                                            ║
║  1. Twitter/X — Influenceurs crypto, tendances émergentes           ║
║  2. On-chain — Smart money wallets, volume anormal                  ║
║  3. Listings — Annonces Binance, Coinbase, nouveaux DEX             ║
║  4. Social — Telegram, Reddit, sentiment communautaire              ║
║  5. Scoring — Combine tout en un score de "potentiel explosion"     ║
║                                                                      ║
║  Actions:                                                            ║
║  - Alerte Telegram en temps réel                                    ║
║  - Achat automatique si signal fort (configurable)                  ║
║  - Gestion de position (stop-loss, take-profit, trailing)           ║
╚══════════════════════════════════════════════════════════════════════╝

INSTALLATION:
    pip install ccxt aiohttp tweepy telethon python-telegram-bot web3

CONFIGURATION .env:
    # Twitter/X API (gratuit avec un compte développeur)
    TWITTER_BEARER_TOKEN=...
    
    # Telegram Bot (crée via @BotFather)
    TELEGRAM_BOT_TOKEN=...
    TELEGRAM_CHAT_ID=...
    
    # On-chain (optionnel, APIs gratuites dispo)
    ETHERSCAN_API_KEY=...
    MORALIS_API_KEY=...
    
    # Trading
    GEM_AUTO_BUY=false             # true = achète automatiquement
    GEM_BET_SIZE=10                # Montant par pari en $
    GEM_MAX_BETS=10                # Max 10 paris simultanés
    GEM_STOP_LOSS_PCT=30           # Stop-loss large (c'est spéculatif)
    GEM_TAKE_PROFIT_PCT=200        # Take-profit x3
    GEM_TRAILING_STOP_PCT=20       # Trailing stop après +50%
"""

import asyncio
import json
import logging
import math
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

try:
    import aiohttp
except ImportError:
    print("❌ pip install aiohttp")

try:
    import ccxt.async_support as ccxt
except ImportError:
    print("❌ pip install ccxt")

log = logging.getLogger("nexus.gemhunter")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Data Models
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class GemSignal:
    """Un signal détecté par le Gem Hunter."""
    token: str                    # Symbole (ex: "PEPE")
    pair: str                     # Paire tradable (ex: "PEPE/USDT")
    score: float                  # Score composite 0-100
    price: float
    market_cap: float
    volume_24h: float
    change_24h: float
    
    # Détails des signaux
    twitter_score: float = 0      # 0-100
    onchain_score: float = 0      # 0-100
    listing_score: float = 0      # 0-100
    social_score: float = 0       # 0-100
    volume_anomaly: float = 0     # Ratio vs moyenne
    
    signals: list = field(default_factory=list)  # Liste des raisons
    risk_level: str = "EXTREME"   # EXTREME, HIGH, MEDIUM
    recommended_bet: float = 0    # Montant recommandé en $
    timestamp: str = ""
    
    # Tracking
    detected_at_price: float = 0
    current_gain_pct: float = 0


@dataclass
class GemPosition:
    """Position ouverte sur un gem."""
    token: str
    pair: str
    entry_price: float
    amount: float
    bet_size_usd: float
    entry_time: str
    stop_loss: float
    take_profit: float
    trailing_activated: bool = False
    highest_price: float = 0
    score_at_entry: float = 0
    signals_at_entry: list = field(default_factory=list)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1. TWITTER/X SCANNER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TwitterScanner:
    """
    Scanne Twitter/X pour détecter les tokens qui buzzent.
    
    Stratégie:
    - Surveille une liste d'influenceurs crypto connus
    - Détecte quand plusieurs influenceurs mentionnent le même token
    - Analyse le sentiment des tweets (bullish/bearish)
    - Détecte les tendances émergentes avant le pic
    """

    # Influenceurs crypto à suivre (par catégorie)
    INFLUENCERS = {
        "alpha_callers": [
            # Comptes connus pour trouver des gems tôt
            "CryptoKaleo", "CryptoCobain", "HsakaTrades",
            "GiganticRebirth", "CryptoGodJohn", "blaboratory",
            "DegenSpartan", "inversebrah", "CryptoCapo_",
        ],
        "analysts": [
            "MessariCrypto", "theaboredape", "Tradermayne",
            "CryptoHornHairs", "pentaborhood", "ColdBloodShill",
        ],
        "whales_watchers": [
            "whale_alert", "WhaleChart", "lookonchain",
        ],
        "news": [
            "CoinDesk", "Cointelegraph", "TheBlock__",
            "WatcherGuru", "tier10k",
        ],
    }

    # Patterns pour extraire les tickers des tweets
    TICKER_PATTERNS = [
        r'\$([A-Z]{2,10})',           # $PEPE, $SOL
        r'#([A-Z]{2,10})',            # #PEPE
        r'\b([A-Z]{2,6})/USDT\b',    # PEPE/USDT
        r'\b([A-Z]{2,6})/USD\b',     # PEPE/USD
    ]

    # Mots bullish
    BULLISH_WORDS = {
        "moon": 3, "100x": 5, "gem": 4, "alpha": 3, "bullish": 2,
        "breakout": 3, "pump": 2, "buy": 1, "long": 1, "send it": 3,
        "accumulate": 3, "undervalued": 3, "next leg up": 3,
        "ape": 2, "degen play": 2, "early": 3, "low cap": 3,
        "just listed": 4, "new listing": 4, "launching": 3,
    }

    def __init__(self):
        self.bearer_token = os.getenv("TWITTER_BEARER_TOKEN", "")
        self._session: Optional[aiohttp.ClientSession] = None
        self._mention_tracker: dict[str, list] = defaultdict(list)  # token -> [{time, user, sentiment}]
        self._cache_time = 0
        self._scan_interval = 120  # 2 minutes

    async def _get_session(self):
        if not self._session or self._session.closed:
            headers = {}
            if self.bearer_token:
                headers["Authorization"] = f"Bearer {self.bearer_token}"
            self._session = aiohttp.ClientSession(
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15)
            )
        return self._session

    async def scan(self) -> dict[str, dict]:
        """
        Scanne les tweets récents et retourne un score par token.
        
        Retourne: {
            "PEPE": {"score": 78, "mentions": 5, "influencers": ["CryptoKaleo", ...], "sentiment": 0.8},
            ...
        }
        """
        now = time.time()
        if now - self._cache_time < self._scan_interval:
            return self._aggregate_mentions()

        results = {}

        if self.bearer_token:
            # ── API Twitter v2 ──
            results = await self._scan_twitter_api()
        else:
            # ── Fallback: APIs de sentiment crypto gratuites ──
            results = await self._scan_crypto_sentiment_apis()

        self._cache_time = now
        return results

    async def _scan_twitter_api(self) -> dict:
        """Scanne via l'API Twitter v2 (nécessite bearer token)."""
        session = await self._get_session()
        all_influencers = []
        for group in self.INFLUENCERS.values():
            all_influencers.extend(group)

        for username in all_influencers[:20]:  # Rate limit
            try:
                # Recherche les tweets récents de cet utilisateur
                url = "https://api.twitter.com/2/tweets/search/recent"
                params = {
                    "query": f"from:{username} -is:retweet",
                    "max_results": 10,
                    "tweet.fields": "created_at,public_metrics",
                }
                async with session.get(url, params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for tweet in data.get("data", []):
                            self._process_tweet(tweet["text"], username, tweet.get("created_at", ""))
                    elif resp.status == 429:
                        log.debug("Twitter rate limit hit")
                        break
            except Exception as e:
                log.debug(f"Twitter scan error for {username}: {e}")
            
            await asyncio.sleep(0.5)  # Rate limit

        return self._aggregate_mentions()

    async def _scan_crypto_sentiment_apis(self) -> dict:
        """
        Fallback sans API Twitter — utilise des APIs de sentiment crypto gratuites.
        LunarCrush, CoinGecko trending, etc.
        """
        session = await self._get_session()
        results = {}

        # ── CoinGecko Trending ──
        try:
            async with session.get("https://api.coingecko.com/api/v3/search/trending") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for i, coin in enumerate(data.get("coins", [])[:10]):
                        item = coin.get("item", {})
                        symbol = item.get("symbol", "").upper()
                        if symbol:
                            self._mention_tracker[symbol].append({
                                "time": time.time(),
                                "source": "coingecko_trending",
                                "rank": i + 1,
                                "sentiment": 0.6,  # Trending = modérément bullish
                            })
        except Exception as e:
            log.debug(f"CoinGecko trending error: {e}")

        # ── CoinGecko Recently Added ──
        try:
            async with session.get("https://api.coingecko.com/api/v3/coins/list?include_platform=true") as resp:
                if resp.status == 200:
                    pass  # On pourrait filtrer les plus récents
        except Exception:
            pass

        # ── CryptoCompare Social Stats ──
        try:
            url = "https://min-api.cryptocompare.com/data/top/totalvolfull?limit=50&tsym=USD"
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for coin in data.get("Data", []):
                        info = coin.get("CoinInfo", {})
                        symbol = info.get("Name", "").upper()
                        raw = coin.get("RAW", {}).get("USD", {})
                        
                        if raw:
                            volume = raw.get("TOTALVOLUME24HTO", 0)
                            change = raw.get("CHANGEPCT24HOUR", 0)
                            
                            # Détecte les anomalies de volume
                            if change > 10 and volume > 1000000:
                                self._mention_tracker[symbol].append({
                                    "time": time.time(),
                                    "source": "volume_spike",
                                    "change_24h": change,
                                    "volume": volume,
                                    "sentiment": min(change / 50, 1.0),
                                })
        except Exception as e:
            log.debug(f"CryptoCompare error: {e}")

        return self._aggregate_mentions()

    def _process_tweet(self, text: str, username: str, created_at: str):
        """Analyse un tweet et extrait les tickers mentionnés."""
        text_upper = text.upper()

        # Extraire les tickers
        tickers = set()
        for pattern in self.TICKER_PATTERNS:
            matches = re.findall(pattern, text_upper)
            tickers.update(matches)

        # Filtrer les faux positifs courants
        noise = {"THE", "FOR", "AND", "NOT", "BUT", "ALL", "NFT", "ETF", "CEO", "CTO", "USD", "API"}
        tickers -= noise

        # Calculer le sentiment
        sentiment = 0
        text_lower = text.lower()
        for word, weight in self.BULLISH_WORDS.items():
            if word in text_lower:
                sentiment += weight

        sentiment = min(sentiment / 10, 1.0)  # Normalise 0-1

        # Enregistrer
        for ticker in tickers:
            self._mention_tracker[ticker].append({
                "time": time.time(),
                "source": f"twitter:{username}",
                "sentiment": sentiment,
                "user_category": self._get_user_category(username),
            })

    def _get_user_category(self, username: str) -> str:
        for category, users in self.INFLUENCERS.items():
            if username in users:
                return category
        return "unknown"

    def _aggregate_mentions(self) -> dict:
        """Agrège les mentions en score par token."""
        results = {}
        now = time.time()
        window = 3600  # Dernière heure

        for token, mentions in self._mention_tracker.items():
            recent = [m for m in mentions if now - m["time"] < window]
            if not recent:
                continue

            # Score basé sur: nombre de mentions, diversité des sources, sentiment moyen
            unique_sources = set(m["source"] for m in recent)
            avg_sentiment = sum(m["sentiment"] for m in recent) / len(recent)

            # Les mentions d'alpha_callers comptent double
            alpha_mentions = sum(1 for m in recent if m.get("user_category") == "alpha_callers")

            score = (
                min(len(recent) * 10, 40) +          # Nombre de mentions (max 40)
                min(len(unique_sources) * 15, 30) +   # Diversité sources (max 30)
                avg_sentiment * 20 +                   # Sentiment (max 20)
                min(alpha_mentions * 10, 10)           # Bonus alpha callers (max 10)
            )

            results[token] = {
                "score": round(min(score, 100), 1),
                "mentions": len(recent),
                "unique_sources": len(unique_sources),
                "sentiment": round(avg_sentiment, 2),
                "alpha_mentions": alpha_mentions,
                "sources": list(unique_sources)[:5],
            }

        return results

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2. ON-CHAIN TRACKER (Smart Money)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class OnChainTracker:
    """
    Suit les wallets "smart money" — les adresses qui ont un historique
    d'achats précoces sur des tokens qui ont ensuite explosé.
    
    Quand plusieurs smart money wallets achètent le même token
    dans un court laps de temps, c'est un signal très fort.
    """

    # APIs on-chain gratuites
    DEXSCREENER_API = "https://api.dexscreener.com/latest/dex"
    BIRDEYE_API = "https://public-api.birdeye.so"

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: dict = {}
        self._cache_time = 0
        self._scan_interval = 180  # 3 minutes

    async def _get_session(self):
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        return self._session

    async def scan_new_pairs(self) -> list[dict]:
        """
        Scanne les nouvelles paires sur les DEX pour trouver des tokens
        qui viennent d'être listés avec un volume anormal.
        """
        session = await self._get_session()
        gems = []

        # ── DexScreener: nouvelles paires avec volume ──
        try:
            # Paires récentes sur plusieurs chaînes
            for chain in ["solana", "ethereum", "bsc"]:
                url = f"{self.DEXSCREENER_API}/search?q=USDT&chain={chain}"
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for pair in data.get("pairs", [])[:20]:
                            age_hours = self._pair_age_hours(pair)
                            volume = float(pair.get("volume", {}).get("h24", 0) or 0)
                            liquidity = float(pair.get("liquidity", {}).get("usd", 0) or 0)
                            price_change = float(pair.get("priceChange", {}).get("h24", 0) or 0)
                            mcap = float(pair.get("fdv", 0) or 0)

                            # Critères de gem: jeune, volume élevé vs mcap, en hausse
                            if (
                                age_hours < 72 and         # Moins de 3 jours
                                volume > 50000 and          # Volume minimum $50k
                                liquidity > 10000 and       # Liquidité minimum $10k
                                mcap < 10000000 and         # Market cap < $10M (micro-cap)
                                mcap > 10000 and            # Pas un total scam
                                price_change > 0            # En hausse
                            ):
                                score = self._score_new_pair(pair, age_hours, volume, mcap, price_change)
                                gems.append({
                                    "token": pair.get("baseToken", {}).get("symbol", "?"),
                                    "pair": pair.get("pairAddress", ""),
                                    "chain": chain,
                                    "price": float(pair.get("priceUsd", 0) or 0),
                                    "mcap": mcap,
                                    "volume_24h": volume,
                                    "liquidity": liquidity,
                                    "change_24h": price_change,
                                    "age_hours": age_hours,
                                    "score": score,
                                    "url": pair.get("url", ""),
                                    "dex": pair.get("dexId", ""),
                                })
                
                await asyncio.sleep(0.3)

        except Exception as e:
            log.warning(f"DexScreener scan error: {e}")

        # Trie par score
        gems.sort(key=lambda g: g["score"], reverse=True)
        
        if gems:
            log.info(f"💎 {len(gems)} gems détectés — Top: {gems[0]['token']} (score: {gems[0]['score']:.0f})")
        
        return gems[:20]

    async def scan_volume_anomalies(self) -> list[dict]:
        """
        Détecte les tokens avec un volume anormalement élevé
        par rapport à leur moyenne — signe que quelque chose se passe.
        """
        session = await self._get_session()
        anomalies = []

        try:
            # Top gainers sur DexScreener
            url = f"{self.DEXSCREENER_API}/tokens/trending"
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for token in data.get("tokens", data if isinstance(data, list) else [])[:30]:
                        if isinstance(token, dict):
                            volume = float(token.get("volume", {}).get("h24", 0) or 0)
                            volume_6h = float(token.get("volume", {}).get("h6", 0) or 0)
                            
                            # Si le volume des 6 dernières heures > 60% du volume 24h
                            # = accélération récente
                            if volume > 0 and volume_6h > volume * 0.6:
                                anomalies.append({
                                    "token": token.get("baseToken", {}).get("symbol", "?"),
                                    "volume_24h": volume,
                                    "volume_acceleration": round(volume_6h / max(volume * 0.25, 1), 2),
                                    "type": "volume_acceleration",
                                })
        except Exception as e:
            log.debug(f"Volume anomaly scan error: {e}")

        return anomalies

    async def check_smart_money(self, token_address: str, chain: str = "ethereum") -> dict:
        """
        Vérifie si des wallets "smart money" ont récemment acheté ce token.
        Utilise les APIs publiques de Etherscan/BSCScan.
        """
        # En prod, utilise Moralis, Nansen, ou Arkham pour le vrai tracking smart money
        # Ici on fait une version simplifiée avec les APIs gratuites
        
        etherscan_key = os.getenv("ETHERSCAN_API_KEY", "")
        if not etherscan_key:
            return {"smart_money_detected": False, "reason": "Pas de clé Etherscan"}

        session = await self._get_session()
        
        try:
            # Vérifie les gros achats récents
            base_url = {
                "ethereum": "https://api.etherscan.io/api",
                "bsc": "https://api.bscscan.com/api",
            }.get(chain, "https://api.etherscan.io/api")

            params = {
                "module": "token",
                "action": "tokentx",
                "contractaddress": token_address,
                "sort": "desc",
                "page": 1,
                "offset": 50,
                "apikey": etherscan_key,
            }

            async with session.get(base_url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    txs = data.get("result", [])
                    
                    if not isinstance(txs, list):
                        return {"smart_money_detected": False}

                    # Analyse les transactions
                    large_buys = 0
                    unique_buyers = set()
                    
                    for tx in txs:
                        value = int(tx.get("value", 0))
                        to_addr = tx.get("to", "").lower()
                        
                        # Compte les gros achats (en valeur token)
                        if value > 0:
                            unique_buyers.add(to_addr)
                            # Heuristique: beaucoup d'acheteurs uniques = intérêt croissant
                    
                    buyer_ratio = len(unique_buyers) / max(len(txs), 1)
                    
                    return {
                        "smart_money_detected": buyer_ratio > 0.6,
                        "unique_buyers": len(unique_buyers),
                        "total_txs": len(txs),
                        "buyer_ratio": round(buyer_ratio, 2),
                        "score": round(buyer_ratio * 100, 1),
                    }

        except Exception as e:
            log.debug(f"Smart money check error: {e}")

        return {"smart_money_detected": False}

    def _pair_age_hours(self, pair: dict) -> float:
        """Calcule l'âge d'une paire en heures."""
        created = pair.get("pairCreatedAt", 0)
        if created:
            return (time.time() * 1000 - created) / 3600000
        return 999

    def _score_new_pair(self, pair, age_hours, volume, mcap, price_change) -> float:
        """Score une nouvelle paire détectée."""
        score = 0

        # Plus c'est jeune, plus c'est intéressant
        if age_hours < 6: score += 30
        elif age_hours < 24: score += 20
        elif age_hours < 48: score += 10

        # Volume vs Market Cap (ratio élevé = fort intérêt)
        if mcap > 0:
            vol_ratio = volume / mcap
            if vol_ratio > 2: score += 25
            elif vol_ratio > 1: score += 20
            elif vol_ratio > 0.5: score += 15
            elif vol_ratio > 0.1: score += 10

        # Hausse modérée (pas trop = pas déjà pumpé, assez = momentum)
        if 10 < price_change < 50: score += 15
        elif 50 < price_change < 200: score += 10
        elif price_change > 200: score += 5  # Peut-être déjà trop tard

        # Liquidité
        liquidity = float(pair.get("liquidity", {}).get("usd", 0) or 0)
        if liquidity > 100000: score += 10
        elif liquidity > 50000: score += 7
        elif liquidity > 10000: score += 5

        # Nombre de transactions
        txns = pair.get("txns", {}).get("h24", {})
        if isinstance(txns, dict):
            buys = txns.get("buys", 0)
            sells = txns.get("sells", 0)
            if buys > sells * 2: score += 10  # Plus d'achats que de ventes
            if buys > 100: score += 5

        return min(score, 100)

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3. LISTING DETECTOR
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ListingDetector:
    """
    Détecte les annonces de listing sur les gros exchanges.
    Un listing Binance = souvent x2-x5 dans les heures qui suivent.
    """

    # URLs des pages d'annonces
    BINANCE_ANNOUNCEMENTS = "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
    
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._known_listings: set = set()  # Listings déjà détectés
        self._cache_time = 0

    async def _get_session(self):
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        return self._session

    async def check_new_listings(self) -> list[dict]:
        """Vérifie les nouvelles annonces de listing."""
        session = await self._get_session()
        listings = []

        # ── Binance Announcements ──
        try:
            params = {
                "type": 1,
                "pageNo": 1,
                "pageSize": 10,
            }
            async with session.get(self.BINANCE_ANNOUNCEMENTS, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    articles = data.get("data", {}).get("catalogs", [{}])[0].get("articles", [])
                    
                    for article in articles:
                        title = article.get("title", "").lower()
                        article_id = article.get("id", "")
                        
                        # Détecte les annonces de listing
                        if any(kw in title for kw in ["will list", "lists", "new listing", "adds"]):
                            # Extraire le ticker
                            tokens = re.findall(r'\(([A-Z]{2,10})\)', article.get("title", ""))
                            
                            for token in tokens:
                                if token not in self._known_listings:
                                    self._known_listings.add(token)
                                    listings.append({
                                        "token": token,
                                        "exchange": "Binance",
                                        "title": article.get("title", ""),
                                        "url": f"https://www.binance.com/en/support/announcement/{article_id}",
                                        "score": 90,  # Listing Binance = signal très fort
                                        "timestamp": datetime.now(timezone.utc).isoformat(),
                                    })
                                    log.warning(f"🚨 NOUVEAU LISTING BINANCE: {token}")
        except Exception as e:
            log.debug(f"Binance listing check error: {e}")

        # ── CoinGecko New Coins ──
        try:
            url = "https://api.coingecko.com/api/v3/coins/list?include_platform=false"
            async with session.get(url) as resp:
                if resp.status == 200:
                    # On pourrait tracker les nouveaux ajouts
                    pass
        except Exception:
            pass

        return listings

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4. TELEGRAM ALERTER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TelegramAlerter:
    """
    Envoie des alertes en temps réel sur Telegram.
    
    Setup:
    1. Parle à @BotFather sur Telegram → /newbot → copie le token
    2. Envoie un message à ton bot
    3. Va sur https://api.telegram.org/bot<TOKEN>/getUpdates → copie le chat_id
    4. Mets TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID dans .env
    """

    def __init__(self):
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self._session: Optional[aiohttp.ClientSession] = None
        self.enabled = bool(self.bot_token and self.chat_id)

        if self.enabled:
            log.info("📱 Telegram alerter activé")
        else:
            log.info("📱 Telegram alerter désactivé (configure TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID)")

    async def _get_session(self):
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        return self._session

    async def send_alert(self, message: str, parse_mode: str = "HTML"):
        """Envoie un message Telegram."""
        if not self.enabled:
            log.info(f"📱 [TELEGRAM DISABLED] {message[:100]}...")
            return

        session = await self._get_session()
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    log.warning(f"Telegram send error: {resp.status}")
        except Exception as e:
            log.warning(f"Telegram error: {e}")

    async def send_gem_alert(self, gem: GemSignal):
        """Envoie une alerte formatée pour un gem détecté."""
        risk_emoji = {"EXTREME": "🔴", "HIGH": "🟠", "MEDIUM": "🟡"}.get(gem.risk_level, "⚪")
        
        signals_text = "\n".join(f"  • {s}" for s in gem.signals[:5])
        
        message = f"""
💎 <b>GEM HUNTER ALERT</b> 💎

<b>Token:</b> {gem.token}
<b>Prix:</b> ${gem.price:.8f}
<b>Market Cap:</b> ${gem.market_cap:,.0f}
<b>Volume 24h:</b> ${gem.volume_24h:,.0f}
<b>Change 24h:</b> {gem.change_24h:+.1f}%

📊 <b>Score:</b> {gem.score:.0f}/100
{risk_emoji} <b>Risque:</b> {gem.risk_level}

<b>Signaux détectés:</b>
{signals_text}

💰 <b>Mise recommandée:</b> ${gem.recommended_bet:.0f}
🎯 <b>Take-profit:</b> x3 (${gem.price * 3:.8f})
⛔ <b>Stop-loss:</b> -30% (${gem.price * 0.7:.8f})

⏰ {gem.timestamp}
"""
        await self.send_alert(message.strip())

    async def send_trade_alert(self, action: str, token: str, price: float, amount: float, reason: str):
        """Alerte pour un trade exécuté."""
        emoji = "🟢" if action == "BUY" else "🔴"
        message = f"""
{emoji} <b>{action}</b> — {token}

Prix: ${price:.8f}
Montant: {amount:.4f}
Raison: {reason}
"""
        await self.send_alert(message.strip())

    async def send_pnl_alert(self, token: str, pnl_pct: float, pnl_usd: float):
        """Alerte pour un P&L."""
        emoji = "🎯" if pnl_pct > 0 else "💀"
        message = f"{emoji} <b>{token}</b> — P&L: {pnl_pct:+.1f}% (${pnl_usd:+.2f})"
        await self.send_alert(message)

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 5. GEM HUNTER ENGINE — Orchestre tout
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class GemHunter:
    """
    Moteur principal du Gem Hunter.
    
    Cycle (toutes les 2-5 minutes):
    1. Scanne Twitter/X pour les tokens qui buzzent
    2. Scanne les DEX pour les nouvelles paires prometteuses
    3. Vérifie les annonces de listing
    4. Combine les scores de toutes les sources
    5. Si score > seuil → alerte Telegram + achat auto si activé
    6. Gère les positions ouvertes (trailing stop, TP, SL)
    
    Usage:
        hunter = GemHunter()
        await hunter.initialize()
        await hunter.run()  # Boucle infinie
    """

    # Seuils
    ALERT_THRESHOLD = 60           # Score minimum pour alerter
    AUTO_BUY_THRESHOLD = 75        # Score minimum pour achat auto
    SCAN_INTERVAL = 120            # Secondes entre chaque scan

    # Pondération des sources
    WEIGHTS = {
        "twitter": 0.30,
        "onchain": 0.30,
        "listing": 0.25,
        "social": 0.15,
    }

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.twitter = TwitterScanner()
        self.onchain = OnChainTracker()
        self.listings = ListingDetector()
        self.telegram = TelegramAlerter()
        
        self.exchange: Optional[ccxt.binance] = None
        self.positions: list[GemPosition] = []
        self.detected_gems: list[GemSignal] = []
        self.gem_history: list[dict] = []  # Historique des détections
        
        # Config trading
        self.auto_buy = os.getenv("GEM_AUTO_BUY", "false").lower() == "true"
        self.bet_size = float(os.getenv("GEM_BET_SIZE", "10"))
        self.max_bets = int(os.getenv("GEM_MAX_BETS", "10"))
        self.stop_loss_pct = float(os.getenv("GEM_STOP_LOSS_PCT", "30"))
        self.take_profit_pct = float(os.getenv("GEM_TAKE_PROFIT_PCT", "200"))
        self.trailing_stop_pct = float(os.getenv("GEM_TRAILING_STOP_PCT", "20"))

        self._running = False

    async def initialize(self, api_key: str = "", api_secret: str = ""):
        """Initialise les connexions."""
        params = {"enableRateLimit": True, "options": {"defaultType": "spot"}}
        if api_key:
            params["apiKey"] = api_key
            params["secret"] = api_secret
        
        self.exchange = ccxt.binance(params)
        
        try:
            await self.exchange.load_markets()
            log.info(f"💎 Gem Hunter initialisé — {len(self.exchange.markets)} paires sur Binance")
        except Exception as e:
            log.warning(f"Exchange connection warning: {e}")

        # Message de démarrage Telegram
        await self.telegram.send_alert(
            "💎 <b>GEM HUNTER ACTIVÉ</b>\n\n"
            f"Mode: {'AUTO-BUY' if self.auto_buy else 'ALERTES SEULEMENT'}\n"
            f"Mise par pari: ${self.bet_size}\n"
            f"Max paris simultanés: {self.max_bets}\n"
            f"Stop-loss: -{self.stop_loss_pct}%\n"
            f"Take-profit: +{self.take_profit_pct}%\n\n"
            "🔍 Scan en cours..."
        )

    async def run(self):
        """Boucle principale du Gem Hunter."""
        self._running = True
        log.info("💎 Gem Hunter démarré — Scan toutes les 2 minutes")

        while self._running:
            try:
                await self._scan_cycle()
                await asyncio.sleep(self.SCAN_INTERVAL)
            except Exception as e:
                log.error(f"Gem Hunter error: {e}")
                await asyncio.sleep(10)

    async def _scan_cycle(self):
        """Un cycle complet de scan."""
        
        # ── 1. Collecte les signaux de toutes les sources ──
        twitter_data, onchain_gems, new_listings = await asyncio.gather(
            self.twitter.scan(),
            self.onchain.scan_new_pairs(),
            self.listings.check_new_listings(),
        )

        # ── 2. Fusionne et score les candidats ──
        candidates = self._merge_signals(twitter_data, onchain_gems, new_listings)

        # ── 3. Filtre et alerte ──
        for gem in candidates:
            if gem.score >= self.ALERT_THRESHOLD:
                # Vérifie qu'on n'a pas déjà alerté récemment pour ce token
                if not self._recently_alerted(gem.token):
                    self.detected_gems.append(gem)
                    self.gem_history.append({
                        "token": gem.token,
                        "score": gem.score,
                        "price": gem.price,
                        "time": datetime.now(timezone.utc).isoformat(),
                    })
                    
                    # Alerte Telegram
                    await self.telegram.send_gem_alert(gem)
                    log.info(f"💎 GEM: {gem.token} — Score {gem.score:.0f} — ${gem.price:.8f}")

                    # Auto-buy si activé et score suffisant
                    if (
                        self.auto_buy
                        and gem.score >= self.AUTO_BUY_THRESHOLD
                        and len(self.positions) < self.max_bets
                    ):
                        await self._auto_buy(gem)

        # ── 4. Gère les positions ouvertes ──
        await self._manage_positions()

    def _merge_signals(
        self,
        twitter_data: dict,
        onchain_gems: list[dict],
        new_listings: list[dict],
    ) -> list[GemSignal]:
        """Fusionne les signaux de toutes les sources en GemSignals."""
        merged: dict[str, GemSignal] = {}

        # ── Twitter signals ──
        for token, data in twitter_data.items():
            if token not in merged:
                merged[token] = GemSignal(
                    token=token,
                    pair=f"{token}/USDT",
                    score=0, price=0, market_cap=0,
                    volume_24h=0, change_24h=0,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )
            merged[token].twitter_score = data["score"]
            merged[token].signals.append(
                f"Twitter: {data['mentions']} mentions, sentiment {data['sentiment']:.0%}"
            )
            if data.get("alpha_mentions", 0) > 0:
                merged[token].signals.append(
                    f"🔥 {data['alpha_mentions']} alpha caller(s) mentionnent ce token"
                )

        # ── On-chain signals ──
        for gem in onchain_gems:
            token = gem["token"]
            if token not in merged:
                merged[token] = GemSignal(
                    token=token,
                    pair=f"{token}/USDT",
                    score=0, price=gem.get("price", 0),
                    market_cap=gem.get("mcap", 0),
                    volume_24h=gem.get("volume_24h", 0),
                    change_24h=gem.get("change_24h", 0),
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )
            merged[token].onchain_score = gem["score"]
            merged[token].price = gem.get("price", merged[token].price)
            merged[token].market_cap = gem.get("mcap", merged[token].market_cap)
            merged[token].volume_24h = gem.get("volume_24h", merged[token].volume_24h)
            merged[token].change_24h = gem.get("change_24h", merged[token].change_24h)
            merged[token].signals.append(
                f"DEX: Paire de {gem['age_hours']:.0f}h, vol ${gem['volume_24h']:,.0f}, "
                f"change {gem['change_24h']:+.0f}%"
            )

        # ── Listing signals ──
        for listing in new_listings:
            token = listing["token"]
            if token not in merged:
                merged[token] = GemSignal(
                    token=token,
                    pair=f"{token}/USDT",
                    score=0, price=0, market_cap=0,
                    volume_24h=0, change_24h=0,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )
            merged[token].listing_score = listing["score"]
            merged[token].signals.append(
                f"🚨 LISTING {listing['exchange']}: {listing['title']}"
            )

        # ── Calcul du score composite ──
        for token, gem in merged.items():
            gem.score = (
                gem.twitter_score * self.WEIGHTS["twitter"]
                + gem.onchain_score * self.WEIGHTS["onchain"]
                + gem.listing_score * self.WEIGHTS["listing"]
                + gem.social_score * self.WEIGHTS["social"]
            )

            # Bonus si plusieurs sources convergent
            active_sources = sum([
                1 if gem.twitter_score > 20 else 0,
                1 if gem.onchain_score > 20 else 0,
                1 if gem.listing_score > 20 else 0,
            ])
            if active_sources >= 2:
                gem.score *= 1.3  # Bonus 30% pour convergence
                gem.signals.append(f"✨ {active_sources} sources convergent")
            if active_sources >= 3:
                gem.score *= 1.2  # Bonus supplémentaire
                gem.signals.append("🔥🔥 TOUTES LES SOURCES CONVERGENT")

            gem.score = min(gem.score, 100)

            # Risk level
            if gem.market_cap < 100000: gem.risk_level = "EXTREME"
            elif gem.market_cap < 1000000: gem.risk_level = "HIGH"
            else: gem.risk_level = "MEDIUM"

            # Bet sizing
            risk_mult = {"EXTREME": 0.5, "HIGH": 0.75, "MEDIUM": 1.0}
            gem.recommended_bet = round(self.bet_size * risk_mult.get(gem.risk_level, 0.5), 2)
            gem.detected_at_price = gem.price

        # Trie et retourne
        results = sorted(merged.values(), key=lambda g: g.score, reverse=True)
        return results

    def _recently_alerted(self, token: str, window: int = 3600) -> bool:
        """Vérifie si on a déjà alerté pour ce token récemment."""
        now = time.time()
        for gem in self.detected_gems:
            if gem.token == token:
                try:
                    t = datetime.fromisoformat(gem.timestamp).timestamp()
                    if now - t < window:
                        return True
                except:
                    pass
        return False

    async def _auto_buy(self, gem: GemSignal):
        """Achète automatiquement un gem détecté."""
        if not self.exchange:
            return

        pair = f"{gem.token}/USDT"
        
        # Vérifie que la paire existe sur Binance
        if pair not in self.exchange.symbols:
            log.debug(f"Paire {pair} non disponible sur Binance")
            return

        try:
            ticker = await self.exchange.fetch_ticker(pair)
            price = ticker["last"]
            amount = self.bet_size / price

            # Place l'ordre
            order = await self.exchange.create_market_order(pair, "buy", amount)
            
            if order:
                position = GemPosition(
                    token=gem.token,
                    pair=pair,
                    entry_price=price,
                    amount=amount,
                    bet_size_usd=self.bet_size,
                    entry_time=datetime.now(timezone.utc).isoformat(),
                    stop_loss=price * (1 - self.stop_loss_pct / 100),
                    take_profit=price * (1 + self.take_profit_pct / 100),
                    highest_price=price,
                    score_at_entry=gem.score,
                    signals_at_entry=gem.signals[:3],
                )
                self.positions.append(position)

                await self.telegram.send_trade_alert(
                    "BUY", gem.token, price, amount,
                    f"Score {gem.score:.0f} — Auto-buy"
                )
                log.info(f"🟢 AUTO-BUY {gem.token} @ ${price:.8f} — ${self.bet_size}")

        except Exception as e:
            log.error(f"Auto-buy error for {gem.token}: {e}")

    async def _manage_positions(self):
        """Gère les positions ouvertes — SL, TP, trailing stop."""
        if not self.exchange:
            return

        for pos in self.positions[:]:
            try:
                ticker = await self.exchange.fetch_ticker(pos.pair)
                price = ticker["last"]
            except:
                continue

            pnl_pct = (price - pos.entry_price) / pos.entry_price * 100

            # Update highest price
            if price > pos.highest_price:
                pos.highest_price = price

            # ── Take Profit ──
            if price >= pos.take_profit:
                await self._close_position(pos, price, "TAKE_PROFIT", pnl_pct)
                continue

            # ── Trailing Stop (activé après +50%) ──
            if pnl_pct > 50:
                pos.trailing_activated = True
                trailing_stop = pos.highest_price * (1 - self.trailing_stop_pct / 100)
                if price <= trailing_stop:
                    await self._close_position(pos, price, "TRAILING_STOP", pnl_pct)
                    continue

            # ── Stop Loss ──
            if price <= pos.stop_loss:
                await self._close_position(pos, price, "STOP_LOSS", pnl_pct)
                continue

    async def _close_position(self, pos: GemPosition, price: float, reason: str, pnl_pct: float):
        """Ferme une position."""
        try:
            order = await self.exchange.create_market_order(pos.pair, "sell", pos.amount)
            pnl_usd = pos.bet_size_usd * pnl_pct / 100

            await self.telegram.send_trade_alert(
                "SELL", pos.token, price, pos.amount,
                f"{reason} — P&L: {pnl_pct:+.1f}% (${pnl_usd:+.2f})"
            )
            await self.telegram.send_pnl_alert(pos.token, pnl_pct, pnl_usd)

            emoji = "🎯" if pnl_pct > 0 else "💀"
            log.info(f"{emoji} {reason} {pos.token} @ ${price:.8f} — P&L: {pnl_pct:+.1f}%")

            self.positions.remove(pos)

        except Exception as e:
            log.error(f"Close position error: {e}")

    def get_status(self) -> dict:
        """Retourne l'état actuel du Gem Hunter."""
        return {
            "running": self._running,
            "auto_buy": self.auto_buy,
            "bet_size": self.bet_size,
            "open_positions": len(self.positions),
            "max_positions": self.max_bets,
            "gems_detected_today": len([
                g for g in self.gem_history
                if g["time"][:10] == datetime.now(timezone.utc).strftime("%Y-%m-%d")
            ]),
            "positions": [
                {
                    "token": p.token,
                    "entry_price": p.entry_price,
                    "bet_size": p.bet_size_usd,
                    "score": p.score_at_entry,
                }
                for p in self.positions
            ],
            "recent_gems": self.gem_history[-10:],
        }

    async def stop(self):
        self._running = False
        await self.twitter.close()
        await self.onchain.close()
        await self.listings.close()
        await self.telegram.close()
        if self.exchange:
            await self.exchange.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Test standalone
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def test():
    """Test le Gem Hunter en mode scan uniquement."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s │ %(message)s")

    print("""
    ╔══════════════════════════════════════════════════════════════╗
    ║  💎 NEXUS TRADER — Gem Hunter Test                          ║
    ╚══════════════════════════════════════════════════════════════╝
    """)

    hunter = GemHunter()

    # Test Twitter Scanner
    print("📡 [1/3] Scan Twitter/Sentiment...")
    twitter_data = await hunter.twitter.scan()
    if twitter_data:
        print(f"   {len(twitter_data)} tokens détectés sur Twitter/Sentiment")
        for token, data in sorted(twitter_data.items(), key=lambda x: x[1]["score"], reverse=True)[:5]:
            print(f"   • {token}: score {data['score']:.0f}, {data['mentions']} mentions, sentiment {data['sentiment']:.0%}")
    else:
        print("   Aucun signal Twitter (configure TWITTER_BEARER_TOKEN pour plus de données)")

    # Test On-Chain
    print("\n📡 [2/3] Scan On-Chain (DEX)...")
    gems = await hunter.onchain.scan_new_pairs()
    if gems:
        print(f"   {len(gems)} gems détectés sur les DEX")
        for g in gems[:5]:
            print(f"   • {g['token']} ({g['chain']}): score {g['score']:.0f}, "
                  f"mcap ${g['mcap']:,.0f}, vol ${g['volume_24h']:,.0f}, "
                  f"change {g['change_24h']:+.0f}%, âge {g['age_hours']:.0f}h")
    else:
        print("   Aucun gem DEX détecté (critères stricts)")

    # Test Listings
    print("\n📡 [3/3] Check nouveaux listings...")
    listings = await hunter.listings.check_new_listings()
    if listings:
        for l in listings:
            print(f"   🚨 LISTING {l['exchange']}: {l['token']} — {l['title']}")
    else:
        print("   Aucun nouveau listing détecté")

    # Merge
    print("\n📊 Fusion des signaux...")
    candidates = hunter._merge_signals(twitter_data, gems, listings)
    top = [c for c in candidates if c.score > 30]

    if top:
        print(f"\n{'═'*70}")
        print(f"  💎 TOP GEMS DÉTECTÉS")
        print(f"{'═'*70}")
        print(f"  {'Token':<10} {'Score':>6} {'Prix':>14} {'MCap':>12} {'Vol 24h':>12} {'Signaux'}")
        print(f"  {'─'*10} {'─'*6} {'─'*14} {'─'*12} {'─'*12} {'─'*20}")
        
        for g in top[:10]:
            print(f"  {g.token:<10} {g.score:>5.0f} ${g.price:>13.8f} ${g.market_cap:>11,.0f} ${g.volume_24h:>11,.0f} {len(g.signals)} signaux")
            for s in g.signals[:2]:
                print(f"{'':>12} → {s}")
        print(f"{'═'*70}")
    else:
        print("   Aucun candidat avec un score suffisant pour le moment")
        print("   (Le scan s'améliore avec le temps et plus de sources de données)")

    await hunter.stop()
    print("\n✅ Test terminé\n")


if __name__ == "__main__":
    asyncio.run(test())
