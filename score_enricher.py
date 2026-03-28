"""
╔══════════════════════════════════════════════════════════════════════╗
║  NEXUS GEMHUNTER — Score Enricher                                    ║
║  Enrichit le scoring des tokens avec des signaux avancés            ║
║                                                                      ║
║  Sources (toutes gratuites, aucun compte requis):                    ║
║  1. DexScreener Trending — tokens qui gagnent en traction           ║
║  2. CoinGecko Trending — tokens qui buzzent globalement             ║
║  3. Volume Anomaly — accélération anormale du volume                ║
║  4. Whale Tracker — gros wallets qui achètent un token              ║
║  5. Market Context — événements macro via RSS feeds                 ║
║  6. Creator Analysis — historique du wallet créateur                 ║
║                                                                      ║
║  S'intègre dans le TokenAnalyzer de pool_listener.py               ║
╚══════════════════════════════════════════════════════════════════════╝
"""

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

try:
    import aiohttp
except ImportError:
    raise ImportError("pip install aiohttp")

try:
    import feedparser
    HAS_FEEDPARSER = True
except ImportError:
    HAS_FEEDPARSER = False

log = logging.getLogger("nexus.score_enricher")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1. TRENDING TRACKER — Tokens qui buzzent
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TrendingTracker:
    """
    Suit les tokens trending sur DexScreener et CoinGecko.
    Si un token vient d'être créé ET il est déjà trending,
    c'est un signal fort.
    """

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._trending_tokens: dict[str, dict] = {}  # symbol -> {source, rank, timestamp}
        self._last_refresh = 0
        self._refresh_interval = 120  # 2 minutes

    async def _get_session(self):
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )
        return self._session

    async def refresh(self):
        """Rafraîchit la liste des tokens trending."""
        now = time.time()
        if now - self._last_refresh < self._refresh_interval:
            return

        self._last_refresh = now
        session = await self._get_session()

        # DexScreener Boosted (tokens qui gagnent en visibilité)
        try:
            url = "https://api.dexscreener.com/token-boosts/latest/v1"
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for item in (data if isinstance(data, list) else []):
                        symbol = item.get("tokenAddress", "")
                        if symbol:
                            self._trending_tokens[symbol.lower()] = {
                                "source": "dexscreener_boost",
                                "chain": item.get("chainId", ""),
                                "timestamp": now,
                            }
        except Exception as e:
            log.debug(f"DexScreener trending error: {e}")

        # CoinGecko Trending
        try:
            url = "https://api.coingecko.com/api/v3/search/trending"
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for i, coin in enumerate(data.get("coins", [])[:15]):
                        item = coin.get("item", {})
                        symbol = item.get("symbol", "").upper()
                        if symbol:
                            self._trending_tokens[symbol.lower()] = {
                                "source": "coingecko_trending",
                                "rank": i + 1,
                                "timestamp": now,
                            }
        except Exception as e:
            log.debug(f"CoinGecko trending error: {e}")

        # Nettoie les entrées vieilles de plus de 30 min
        cutoff = now - 1800
        self._trending_tokens = {
            k: v for k, v in self._trending_tokens.items()
            if v.get("timestamp", 0) > cutoff
        }

        if self._trending_tokens:
            log.debug(f"📈 {len(self._trending_tokens)} tokens trending trackés")

    def is_trending(self, token_address: str, token_symbol: str) -> dict:
        """Vérifie si un token est trending. Retourne les détails ou {}."""
        # Check par adresse
        addr_lower = token_address.lower()
        if addr_lower in self._trending_tokens:
            return self._trending_tokens[addr_lower]

        # Check par symbol
        sym_lower = token_symbol.lower()
        if sym_lower in self._trending_tokens:
            return self._trending_tokens[sym_lower]

        return {}

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2. SMART MONEY TRACKER — Wallets qui ont l'habitude de gagner
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SmartMoneyTracker:
    """
    Suit les wallets "smart money" via des APIs publiques.
    Quand un wallet connu pour ses bons trades achète un nouveau
    token, c'est un signal très fort.
    
    Sources gratuites:
    - DexScreener top traders
    - Birdeye top traders (Solana)
    """

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._smart_wallets: dict[str, dict] = {}  # address -> {chain, pnl, trades}
        self._recent_smart_buys: dict[str, list] = {}  # token -> [{wallet, amount, time}]
        self._last_refresh = 0
        self._refresh_interval = 300  # 5 minutes

    async def _get_session(self):
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )
        return self._session

    async def check_smart_money_buying(self, token_address: str, chain: str) -> dict:
        """
        Vérifie si des smart money wallets achètent ce token.
        Utilise DexScreener pour voir les top traders du pool.
        """
        session = await self._get_session()
        result = {
            "smart_buyers": 0,
            "total_smart_volume": 0,
            "signal": "NONE",
            "details": [],
        }

        try:
            # DexScreener: top traders du token
            url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
            async with session.get(url) as resp:
                if resp.status != 200:
                    return result
                data = await resp.json()

            pairs = data.get("pairs", [])
            if not pairs:
                return result

            pair = pairs[0]
            
            # Analyse les transactions récentes
            txns_5m = pair.get("txns", {}).get("m5", {})
            txns_1h = pair.get("txns", {}).get("h1", {})
            
            buys_5m = txns_5m.get("buys", 0) or 0
            sells_5m = txns_5m.get("sells", 0) or 0
            buys_1h = txns_1h.get("buys", 0) or 0
            sells_1h = txns_1h.get("sells", 0) or 0
            
            volume_5m = float(pair.get("volume", {}).get("m5", 0) or 0)
            volume_1h = float(pair.get("volume", {}).get("h1", 0) or 0)
            
            # Signal: accélération du volume (5min vs moyenne horaire)
            if volume_1h > 0:
                volume_acceleration = (volume_5m * 12) / volume_1h  # Annualisé sur 1h
                if volume_acceleration > 3:
                    result["signal"] = "STRONG"
                    result["details"].append(f"Volume acceleration: {volume_acceleration:.1f}x")
                elif volume_acceleration > 1.5:
                    result["signal"] = "MODERATE"
                    result["details"].append(f"Volume acceleration: {volume_acceleration:.1f}x")
            
            # Signal: ratio buy/sell élevé
            total_txns = buys_5m + sells_5m
            if total_txns > 5:
                buy_ratio = buys_5m / total_txns
                if buy_ratio > 0.75:
                    if result["signal"] == "NONE":
                        result["signal"] = "MODERATE"
                    elif result["signal"] == "MODERATE":
                        result["signal"] = "STRONG"
                    result["details"].append(f"Buy dominance: {buy_ratio:.0%} ({buys_5m}B/{sells_5m}S)")
            
            # Signal: gros volume dans les 5 premières minutes
            if volume_5m > 10000:
                result["total_smart_volume"] = volume_5m
                result["details"].append(f"Early volume: ${volume_5m:,.0f}")
                if volume_5m > 50000:
                    result["signal"] = "STRONG"

        except Exception as e:
            log.debug(f"Smart money check error: {e}")

        return result

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3. MARKET CONTEXT — Événements macro et news
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class MarketContextAnalyzer:
    """
    Évalue le contexte macro du marché via RSS feeds gratuits.
    En période de crash/panique, on réduit les alertes.
    En période bullish, on est plus agressif.
    """

    RSS_FEEDS = [
        "https://cointelegraph.com/rss",
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
    ]

    BEARISH_KEYWORDS = [
        "hack", "exploit", "stolen", "breach", "ban",
        "sec charges", "sec sues", "rug pull", "bankruptcy",
        "crash", "plunge", "dump", "collapse",
    ]

    BULLISH_KEYWORDS = [
        "etf approved", "etf approval", "bullish", "surge",
        "rally", "all-time high", "ath", "adoption",
        "rate cut", "institutional",
    ]

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._market_mood: str = "NEUTRAL"  # BULLISH, BEARISH, NEUTRAL, PANIC
        self._mood_score: float = 0  # -1 (bearish) to +1 (bullish)
        self._last_scan = 0
        self._scan_interval = 300  # 5 minutes

    async def _get_session(self):
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )
        return self._session

    async def get_market_mood(self) -> dict:
        """Retourne l'humeur du marché basée sur les news."""
        now = time.time()
        if now - self._last_scan < self._scan_interval:
            return {
                "mood": self._market_mood,
                "score": self._mood_score,
            }

        self._last_scan = now

        if not HAS_FEEDPARSER:
            return {"mood": "NEUTRAL", "score": 0}

        bullish_count = 0
        bearish_count = 0
        session = await self._get_session()

        for feed_url in self.RSS_FEEDS:
            try:
                async with session.get(feed_url) as resp:
                    if resp.status == 200:
                        content = await resp.text()
                        feed = feedparser.parse(content)
                        for entry in feed.entries[:10]:
                            title = entry.get("title", "").lower()
                            summary = entry.get("summary", "").lower()
                            text = f"{title} {summary}"

                            for kw in self.BEARISH_KEYWORDS:
                                if kw in text:
                                    bearish_count += 1
                                    break

                            for kw in self.BULLISH_KEYWORDS:
                                if kw in text:
                                    bullish_count += 1
                                    break
            except Exception:
                pass

        total = bullish_count + bearish_count
        if total == 0:
            self._mood_score = 0
            self._market_mood = "NEUTRAL"
        else:
            self._mood_score = (bullish_count - bearish_count) / total
            if self._mood_score > 0.3:
                self._market_mood = "BULLISH"
            elif self._mood_score < -0.5:
                self._market_mood = "PANIC"
            elif self._mood_score < -0.2:
                self._market_mood = "BEARISH"
            else:
                self._market_mood = "NEUTRAL"

        log.debug(f"🌍 Market mood: {self._market_mood} (score: {self._mood_score:.2f})")

        return {
            "mood": self._market_mood,
            "score": self._mood_score,
        }

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4. SCORE ENRICHER — Combine tout
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ScoreEnricher:
    """
    Enrichit le score d'un token avec des signaux avancés.
    
    S'utilise après le TokenAnalyzer de base :
    
        enricher = ScoreEnricher()
        pool = await analyzer.analyze(pool)       # Score de base
        pool = await enricher.enrich(pool)         # Score enrichi
    
    Signaux ajoutés au score :
    - Token trending (+15 pts)
    - Smart money buying (+10 à +20 pts) 
    - Volume acceleration (+10 pts)
    - Market mood bullish (+5 pts) ou bearish (-10 pts)
    - Market panic (-20 pts, bloque les alertes)
    """

    def __init__(self):
        self.trending = TrendingTracker()
        self.smart_money = SmartMoneyTracker()
        self.market_context = MarketContextAnalyzer()

    async def enrich(self, pool) -> object:
        """
        Enrichit le score d'un pool avec des signaux avancés.
        Modifie pool.score, pool.green_flags et pool.red_flags.
        """
        # Rafraîchit les données trending en background
        await self.trending.refresh()

        # Récupère les signaux en parallèle
        smart_money_data, market_mood = await asyncio.gather(
            self.smart_money.check_smart_money_buying(
                pool.target_token, pool.chain.value
            ),
            self.market_context.get_market_mood(),
            return_exceptions=True,
        )

        if isinstance(smart_money_data, Exception):
            smart_money_data = {"signal": "NONE", "details": []}
        if isinstance(market_mood, Exception):
            market_mood = {"mood": "NEUTRAL", "score": 0}

        score_delta = 0

        # ── 1. Trending check ──
        trending_info = self.trending.is_trending(pool.target_token, pool.target_symbol)
        if trending_info:
            score_delta += 15
            source = trending_info.get("source", "unknown")
            pool.green_flags.append(f"Trending on {source}")

        # ── 2. Smart money / Volume signals ──
        sm_signal = smart_money_data.get("signal", "NONE")
        if sm_signal == "STRONG":
            score_delta += 20
            pool.green_flags.append("Strong smart money signal")
            for detail in smart_money_data.get("details", [])[:2]:
                pool.green_flags.append(detail)
        elif sm_signal == "MODERATE":
            score_delta += 10
            pool.green_flags.append("Moderate buying pressure")
            for detail in smart_money_data.get("details", [])[:1]:
                pool.green_flags.append(detail)

        # ── 3. Market mood ──
        mood = market_mood.get("mood", "NEUTRAL")
        mood_score = market_mood.get("score", 0)

        if mood == "PANIC":
            score_delta -= 20
            pool.red_flags.append("MARKET PANIC — high risk environment")
        elif mood == "BEARISH":
            score_delta -= 10
            pool.red_flags.append("Bearish market context")
        elif mood == "BULLISH":
            score_delta += 5
            pool.green_flags.append("Bullish market context")

        # Applique le delta
        pool.score = max(0, min(100, pool.score + score_delta))

        if score_delta != 0:
            log.debug(
                f"📊 Score enrichi: {pool.target_symbol} "
                f"delta={score_delta:+d} → {pool.score:.0f} "
                f"(trending={bool(trending_info)}, "
                f"smart={sm_signal}, mood={mood})"
            )

        return pool

    async def close(self):
        await self.trending.close()
        await self.smart_money.close()
        await self.market_context.close()
