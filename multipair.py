"""
╔══════════════════════════════════════════════════════════════════════╗
║  NEXUS TRADER — Module Multi-Pair + Smart Scanner                  ║
║  Scanne le marché, identifie les meilleures opportunités,          ║
║  et alloue le capital de manière dynamique                         ║
╚══════════════════════════════════════════════════════════════════════╝

FONCTIONNEMENT:
    1. Scanne 20-50 paires crypto toutes les X minutes
    2. Score chaque paire sur: momentum, volume, volatilité, tendance
    3. Classe et sélectionne les top N cryptos les plus prometteuses
    4. Alloue le capital proportionnellement aux scores
    5. Gère la corrélation pour éviter la surexposition
    6. Rééquilibre automatiquement le portfolio

INSTALLATION:
    pip install ccxt aiohttp numpy
"""

import asyncio
import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

try:
    import ccxt.async_support as ccxt
except ImportError:
    print("❌ pip install ccxt")

log = logging.getLogger("nexus.multipair")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Indicateurs rapides (version optimisée pour scanner beaucoup de paires)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fast_rsi(closes, period=14):
    if len(closes) < period + 1: return 50
    g = l = 0
    for i in range(len(closes) - period, len(closes)):
        d = closes[i] - closes[i-1]
        if d > 0: g += d
        else: l -= d
    if l == 0: return 100
    return round(100 - 100 / (1 + g / l), 2)

def fast_ema(closes, period):
    if len(closes) < period: return None
    k = 2 / (period + 1)
    e = sum(closes[:period]) / period
    for p in closes[period:]: e = p * k + e * (1 - k)
    return e

def fast_atr(candles, period=14):
    if len(candles) < period + 1: return None
    trs = []
    for i in range(1, len(candles)):
        c = candles[i]; pc = candles[i-1]["close"]
        trs.append(max(c["high"] - c["low"], abs(c["high"] - pc), abs(c["low"] - pc)))
    return sum(trs[-period:]) / period

def volume_sma(candles, period=20):
    if len(candles) < period: return None
    return sum(c["volume"] for c in candles[-period:]) / period


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Pair Scorer — Note chaque crypto
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class PairScore:
    pair: str
    price: float
    score: float                  # Score composite 0-100
    momentum_score: float         # Force du mouvement
    volume_score: float           # Volume relatif
    trend_score: float            # Direction de la tendance
    volatility_score: float       # Volatilité (opportunité)
    rsi: float
    signal: str                   # BUY, SELL, NEUTRAL
    change_24h: float             # Variation 24h en %
    volume_24h: float
    market_cap_rank: int          # Rang par market cap (si dispo)
    details: str


class PairScorer:
    """
    Score chaque paire crypto sur plusieurs dimensions.
    Un score élevé = plus prometteur pour le trading.
    """

    # Pondération des critères
    WEIGHTS = {
        "momentum": 0.30,       # Le plus important: le prix bouge-t-il dans la bonne direction ?
        "volume": 0.25,         # Un mouvement sans volume n'est pas fiable
        "trend": 0.25,          # La tendance de fond
        "volatility": 0.20,     # Assez de mouvement pour trader, mais pas trop
    }

    def score_pair(self, candles: list[dict], pair: str, market_info: dict = None) -> PairScore:
        """Score une paire sur tous les critères."""
        if len(candles) < 50:
            return PairScore(pair=pair, price=0, score=0, momentum_score=0,
                           volume_score=0, trend_score=0, volatility_score=0,
                           rsi=50, signal="NEUTRAL", change_24h=0, volume_24h=0,
                           market_cap_rank=999, details="Données insuffisantes")

        closes = [c["close"] for c in candles]
        price = closes[-1]

        # ── Momentum Score (0-100) ──
        # ROC multi-périodes + position vs EMAs
        roc_5 = (closes[-1] - closes[-5]) / closes[-5] * 100 if len(closes) >= 5 else 0
        roc_10 = (closes[-1] - closes[-10]) / closes[-10] * 100 if len(closes) >= 10 else 0
        roc_20 = (closes[-1] - closes[-20]) / closes[-20] * 100 if len(closes) >= 20 else 0

        # Combine les ROC (court terme pèse plus)
        raw_momentum = roc_5 * 0.5 + roc_10 * 0.3 + roc_20 * 0.2
        momentum_score = self._normalize(raw_momentum, -10, 10) * 100

        # ── Volume Score (0-100) ──
        vol_avg = volume_sma(candles, 20)
        current_vol = candles[-1]["volume"]
        if vol_avg and vol_avg > 0:
            vol_ratio = current_vol / vol_avg
            volume_score = self._normalize(vol_ratio, 0.5, 3.0) * 100
        else:
            volume_score = 50

        # ── Trend Score (0-100) ──
        ema9 = fast_ema(closes, 9)
        ema21 = fast_ema(closes, 21)
        ema50 = fast_ema(closes, 50) if len(closes) >= 50 else ema21

        if ema9 and ema21 and ema50:
            # Prix au-dessus des 3 EMAs = tendance forte
            above_count = sum([
                1 if price > ema9 else -1,
                1 if price > ema21 else -1,
                1 if price > ema50 else -1,
                1 if ema9 > ema21 else -1,
                1 if ema21 > ema50 else -1,
            ])
            trend_score = self._normalize(above_count, -5, 5) * 100
        else:
            trend_score = 50

        # ── Volatility Score (0-100) ──
        # On veut de la volatilité (opportunité) mais pas trop (risque)
        atr = fast_atr(candles)
        if atr and price > 0:
            atr_pct = (atr / price) * 100
            # Sweet spot: 1-4% ATR
            if atr_pct < 0.5:
                volatility_score = 20  # Trop calme
            elif atr_pct < 1:
                volatility_score = 50
            elif atr_pct < 2:
                volatility_score = 80  # Idéal
            elif atr_pct < 4:
                volatility_score = 90  # Bon
            elif atr_pct < 6:
                volatility_score = 60  # Risqué
            else:
                volatility_score = 30  # Trop volatile
        else:
            volatility_score = 50

        # ── Score composite ──
        composite = (
            momentum_score * self.WEIGHTS["momentum"]
            + volume_score * self.WEIGHTS["volume"]
            + trend_score * self.WEIGHTS["trend"]
            + volatility_score * self.WEIGHTS["volatility"]
        )

        # ── RSI & Signal ──
        rsi = fast_rsi(closes)
        
        # Signal basé sur le score composite + RSI
        if composite > 65 and rsi < 70 and momentum_score > 60:
            signal = "BUY"
        elif composite < 35 or rsi > 75:
            signal = "SELL"
        else:
            signal = "NEUTRAL"

        # Change 24h
        change_24h = 0
        if len(candles) >= 24:
            change_24h = (closes[-1] - closes[-24]) / closes[-24] * 100

        volume_24h = sum(c["volume"] * c["close"] for c in candles[-24:]) if len(candles) >= 24 else 0

        details = (
            f"Mom:{momentum_score:.0f} Vol:{volume_score:.0f} "
            f"Trend:{trend_score:.0f} Volat:{volatility_score:.0f} "
            f"RSI:{rsi} ROC5:{roc_5:+.1f}%"
        )

        return PairScore(
            pair=pair,
            price=round(price, 8),
            score=round(composite, 1),
            momentum_score=round(momentum_score, 1),
            volume_score=round(volume_score, 1),
            trend_score=round(trend_score, 1),
            volatility_score=round(volatility_score, 1),
            rsi=rsi,
            signal=signal,
            change_24h=round(change_24h, 2),
            volume_24h=round(volume_24h, 2),
            market_cap_rank=market_info.get("rank", 999) if market_info else 999,
            details=details,
        )

    def _normalize(self, value, min_val, max_val) -> float:
        """Normalise entre 0 et 1."""
        return max(0, min(1, (value - min_val) / (max_val - min_val)))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Market Scanner
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class MarketScanner:
    """
    Scanne le marché crypto pour trouver les meilleures opportunités.
    Filtre par liquidité, market cap, et score technique.
    """

    # Paires à scanner (top cryptos par market cap + altcoins prometteurs)
    DEFAULT_PAIRS = [
        # Blue chips
        "BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT",
        "ADA/USDT", "AVAX/USDT", "DOT/USDT", "LINK/USDT", "MATIC/USDT",
        # Layer 1
        "NEAR/USDT", "APT/USDT", "SUI/USDT", "SEI/USDT", "INJ/USDT",
        # DeFi
        "UNI/USDT", "AAVE/USDT", "MKR/USDT", "CRV/USDT", "SNX/USDT",
        # Layer 2
        "ARB/USDT", "OP/USDT", "IMX/USDT",
        # AI & Data
        "FET/USDT", "RNDR/USDT", "TAO/USDT",
        # Meme (haute volatilité = opportunités)
        "DOGE/USDT", "SHIB/USDT", "PEPE/USDT", "WIF/USDT",
        # Divers
        "FIL/USDT", "ATOM/USDT", "ALGO/USDT", "HBAR/USDT",
    ]

    def __init__(self, pairs: list[str] = None):
        self.pairs = pairs or self.DEFAULT_PAIRS
        self.scorer = PairScorer()
        self.exchange: Optional[ccxt.binance] = None
        self._scan_cache: list[PairScore] = []
        self._cache_time = 0
        self._cache_ttl = 180  # 3 minutes

    async def connect(self, api_key: str = "", api_secret: str = ""):
        """Connexion à Binance."""
        params = {"enableRateLimit": True, "options": {"defaultType": "spot"}}
        if api_key:
            params["apiKey"] = api_key
            params["secret"] = api_secret
        self.exchange = ccxt.binance(params)
        await self.exchange.load_markets()

        # Filtre les paires qui existent sur Binance
        available = set(self.exchange.symbols)
        self.pairs = [p for p in self.pairs if p in available]
        log.info(f"📡 Scanner initialisé: {len(self.pairs)} paires disponibles")

    async def scan(self) -> list[PairScore]:
        """Scanne toutes les paires et retourne les scores triés."""
        now = time.time()
        if self._scan_cache and (now - self._cache_time < self._cache_ttl):
            return self._scan_cache

        log.info(f"🔍 Scan de {len(self.pairs)} paires en cours...")
        scores = []

        # Fetch en parallèle par batches (pour respecter le rate limit)
        batch_size = 5
        for i in range(0, len(self.pairs), batch_size):
            batch = self.pairs[i:i + batch_size]
            tasks = [self._score_pair(pair) for pair in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for pair, result in zip(batch, results):
                if isinstance(result, PairScore):
                    scores.append(result)
                else:
                    log.debug(f"Erreur scan {pair}: {result}")

            await asyncio.sleep(0.2)  # Rate limit

        # Trie par score décroissant
        scores.sort(key=lambda s: s.score, reverse=True)
        self._scan_cache = scores
        self._cache_time = now

        log.info(f"✅ Scan terminé — Top 5: {', '.join(s.pair + f'({s.score:.0f})' for s in scores[:5])}")
        return scores

    async def _score_pair(self, pair: str) -> PairScore:
        """Score une paire individuelle."""
        try:
            ohlcv = await self.exchange.fetch_ohlcv(pair, "1h", limit=100)
            candles = [
                {"time": c[0], "open": c[1], "high": c[2], "low": c[3], "close": c[4], "volume": c[5]}
                for c in ohlcv
            ]
            return self.scorer.score_pair(candles, pair)
        except Exception as e:
            return PairScore(pair=pair, price=0, score=0, momentum_score=0,
                           volume_score=0, trend_score=0, volatility_score=0,
                           rsi=50, signal="NEUTRAL", change_24h=0, volume_24h=0,
                           market_cap_rank=999, details=f"Erreur: {e}")

    async def get_top_opportunities(self, n: int = 5, min_score: float = 55) -> list[PairScore]:
        """Retourne les N meilleures opportunités d'achat."""
        scores = await self.scan()
        return [s for s in scores if s.score >= min_score and s.signal == "BUY"][:n]

    async def close(self):
        if self.exchange:
            await self.exchange.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Correlation Manager — Évite la surexposition
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class CorrelationManager:
    """
    Gère les corrélations entre les cryptos.
    Si tu trades BTC et ETH en même temps, tu es surexposé car ils
    bougent ensemble. Ce module réduit l'allocation si trop corrélé.
    """

    # Corrélations estimées (en prod, calcule dynamiquement)
    CORRELATION_GROUPS = {
        "btc_correlated": ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT"],
        "defi": ["UNI/USDT", "AAVE/USDT", "MKR/USDT", "CRV/USDT", "SNX/USDT"],
        "layer1": ["ADA/USDT", "AVAX/USDT", "DOT/USDT", "NEAR/USDT", "APT/USDT", "SUI/USDT"],
        "layer2": ["ARB/USDT", "OP/USDT", "MATIC/USDT", "IMX/USDT"],
        "meme": ["DOGE/USDT", "SHIB/USDT", "PEPE/USDT", "WIF/USDT"],
        "ai": ["FET/USDT", "RNDR/USDT", "TAO/USDT"],
    }

    MAX_PER_GROUP = 2           # Max 2 positions dans le même groupe
    MAX_GROUP_ALLOCATION = 0.4  # Max 40% du portfolio par groupe

    def check_diversification(self, current_positions: list[str], new_pair: str) -> dict:
        """
        Vérifie si ajouter cette paire est bon pour la diversification.
        Retourne un multiplicateur d'allocation (0 = bloqué, 1 = normal).
        """
        # Trouve le groupe de la nouvelle paire
        new_group = None
        for group, pairs in self.CORRELATION_GROUPS.items():
            if new_pair in pairs:
                new_group = group
                break

        if not new_group:
            return {"allowed": True, "allocation_mult": 1.0, "reason": "Paire non corrélée"}

        # Compte les positions dans le même groupe
        same_group_count = sum(
            1 for pos in current_positions
            if pos in self.CORRELATION_GROUPS.get(new_group, [])
        )

        if same_group_count >= self.MAX_PER_GROUP:
            return {
                "allowed": False,
                "allocation_mult": 0,
                "reason": f"Max {self.MAX_PER_GROUP} positions dans le groupe '{new_group}' atteint",
            }

        # Réduit l'allocation si déjà exposé au groupe
        if same_group_count > 0:
            mult = 1.0 / (same_group_count + 1)
            return {
                "allowed": True,
                "allocation_mult": mult,
                "reason": f"Réduction allocation ({mult:.0%}): {same_group_count} position(s) dans '{new_group}'",
            }

        return {"allowed": True, "allocation_mult": 1.0, "reason": "Bonne diversification"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Portfolio Allocator — Kelly Criterion + Score-based
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class PortfolioAllocator:
    """
    Alloue le capital entre les paires sélectionnées.
    
    Méthodes:
    - Equal weight: même montant sur chaque paire
    - Score weighted: proportionnel au score de chaque paire
    - Kelly criterion: taille optimale basée sur le win rate
    """

    def __init__(self, method: str = "kelly_adjusted"):
        self.method = method
        self.correlation_mgr = CorrelationManager()
        # Historique pour le Kelly
        self._win_rates: dict[str, float] = {}
        self._avg_win_loss_ratio: dict[str, float] = {}

    def allocate(
        self,
        portfolio_value: float,
        opportunities: list[PairScore],
        current_positions: list[str] = None,
        max_positions: int = 5,
        max_single_allocation_pct: float = 25,
        min_allocation_usd: float = 50,
    ) -> list[dict]:
        """
        Calcule l'allocation optimale pour chaque opportunité.
        
        Retourne: [{"pair": "BTC/USDT", "allocation_usd": 500, "allocation_pct": 5.0, "reason": "..."}]
        """
        current_positions = current_positions or []

        if not opportunities:
            return []

        # Limite au max_positions
        candidates = opportunities[:max_positions]

        allocations = []
        remaining_budget = portfolio_value * 0.9  # Garde 10% en cash

        for opp in candidates:
            # Check diversification
            div = self.correlation_mgr.check_diversification(
                current_positions + [a["pair"] for a in allocations],
                opp.pair,
            )
            if not div["allowed"]:
                log.debug(f"⏭ {opp.pair} bloqué: {div['reason']}")
                continue

            # Calcule l'allocation de base
            if self.method == "equal":
                base_alloc_pct = 100 / max_positions
            elif self.method == "score_weighted":
                total_score = sum(o.score for o in candidates)
                base_alloc_pct = (opp.score / total_score * 100) if total_score > 0 else 0
            elif self.method == "kelly_adjusted":
                base_alloc_pct = self._kelly_allocation(opp)
            else:
                base_alloc_pct = 100 / max_positions

            # Applique les limites
            alloc_pct = min(base_alloc_pct, max_single_allocation_pct)
            alloc_pct *= div["allocation_mult"]  # Ajustement corrélation
            alloc_usd = min(remaining_budget, portfolio_value * alloc_pct / 100)

            if alloc_usd < min_allocation_usd:
                continue

            remaining_budget -= alloc_usd

            allocations.append({
                "pair": opp.pair,
                "allocation_usd": round(alloc_usd, 2),
                "allocation_pct": round(alloc_pct, 2),
                "score": opp.score,
                "signal": opp.signal,
                "reason": f"Score {opp.score:.0f} | {opp.details} | {div['reason']}",
            })

        return allocations

    def _kelly_allocation(self, opp: PairScore) -> float:
        """
        Kelly Criterion: f* = (p * b - q) / b
        
        Où:
        - p = probabilité de gagner (win rate)
        - q = 1 - p
        - b = ratio gain moyen / perte moyenne
        
        On utilise un "demi-Kelly" car le Kelly pur est trop agressif.
        """
        # Estime le win rate basé sur le score
        # Score 70+ → ~60% win rate estimé
        # Score 50 → ~50%
        # Score 30 → ~40%
        estimated_win_rate = 0.4 + (opp.score / 100) * 0.3  # 40-70%

        # Ratio gain/perte (basé sur un TP:SL de 2:1 typique)
        avg_win_loss_ratio = 2.0

        p = estimated_win_rate
        q = 1 - p
        b = avg_win_loss_ratio

        kelly = (p * b - q) / b

        # Demi-Kelly (plus conservateur)
        half_kelly = max(0, kelly / 2) * 100  # en %

        # Limites: entre 2% et 20%
        return max(2, min(20, half_kelly))

    def update_stats(self, pair: str, won: bool, win_amount: float = 0, loss_amount: float = 0):
        """Met à jour les stats pour le Kelly criterion."""
        # Simple exponential moving average du win rate
        alpha = 0.1
        prev = self._win_rates.get(pair, 0.5)
        self._win_rates[pair] = prev * (1 - alpha) + (1.0 if won else 0.0) * alpha

        if won and win_amount > 0 and loss_amount > 0:
            prev_ratio = self._avg_win_loss_ratio.get(pair, 2.0)
            self._avg_win_loss_ratio[pair] = prev_ratio * (1 - alpha) + (win_amount / loss_amount) * alpha


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Smart Order Manager — Ordres Limit intelligents
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SmartOrderManager:
    """
    Gère les ordres de manière intelligente:
    - Ordres limit au lieu de market quand possible (économise les frais)
    - Iceberg orders pour les gros montants (ne pas révéler la taille)
    - TWAP (Time Weighted Average Price) pour lisser l'entrée
    - Fallback en market si le limit n'est pas exécuté à temps
    """

    def __init__(self, exchange):
        self.exchange = exchange
        self.pending_orders: dict = {}

    async def smart_buy(
        self,
        pair: str,
        amount: float,
        current_price: float,
        urgency: str = "normal",  # "high", "normal", "low"
    ) -> Optional[dict]:
        """
        Place un ordre d'achat intelligent.
        
        urgency="high" → ordre market (exécution immédiate)
        urgency="normal" → limit légèrement sous le prix (économise ~0.05%)
        urgency="low" → limit plus bas (attend un meilleur prix)
        """
        if urgency == "high":
            # Market order — exécution garantie
            try:
                order = await self.exchange.create_market_order(pair, "buy", amount)
                log.info(f"⚡ MARKET BUY {pair}: {amount} @ market")
                return order
            except Exception as e:
                log.error(f"❌ Erreur market buy: {e}")
                return None

        elif urgency == "normal":
            # Limit légèrement sous le prix actuel
            # On place à -0.1% du prix actuel (souvent exécuté en quelques secondes)
            limit_price = round(current_price * 0.999, self._get_price_precision(pair))
            timeout = 60  # 1 minute avant fallback market

        elif urgency == "low":
            # Limit plus agressif — attend un pullback
            limit_price = round(current_price * 0.995, self._get_price_precision(pair))
            timeout = 300  # 5 minutes

        else:
            limit_price = current_price
            timeout = 60

        try:
            order = await self.exchange.create_limit_order(pair, "buy", amount, limit_price)
            order_id = order["id"]
            log.info(f"📋 LIMIT BUY {pair}: {amount} @ ${limit_price} (timeout: {timeout}s)")

            # Attend l'exécution ou timeout
            filled = await self._wait_for_fill(order_id, pair, timeout)

            if filled:
                log.info(f"✅ Limit order exécuté pour {pair}")
                return filled
            else:
                # Annule et passe en market
                await self.exchange.cancel_order(order_id, pair)
                log.info(f"⏰ Timeout limit order {pair} → fallback market")
                return await self.exchange.create_market_order(pair, "buy", amount)

        except Exception as e:
            log.error(f"❌ Erreur smart buy: {e}")
            # Fallback ultime: market
            try:
                return await self.exchange.create_market_order(pair, "buy", amount)
            except Exception as e2:
                log.error(f"❌ Fallback market aussi échoué: {e2}")
                return None

    async def smart_sell(
        self,
        pair: str,
        amount: float,
        current_price: float,
        urgency: str = "normal",
    ) -> Optional[dict]:
        """Place un ordre de vente intelligent."""
        if urgency == "high":
            try:
                return await self.exchange.create_market_order(pair, "sell", amount)
            except Exception as e:
                log.error(f"❌ Erreur market sell: {e}")
                return None

        # Limit légèrement au-dessus
        offset = 0.999 if urgency == "low" else 1.001
        limit_price = round(current_price * offset, self._get_price_precision(pair))
        timeout = 300 if urgency == "low" else 60

        try:
            order = await self.exchange.create_limit_order(pair, "sell", amount, limit_price)
            filled = await self._wait_for_fill(order["id"], pair, timeout)

            if filled:
                return filled
            else:
                await self.exchange.cancel_order(order["id"], pair)
                return await self.exchange.create_market_order(pair, "sell", amount)
        except Exception as e:
            log.error(f"❌ Erreur smart sell: {e}")
            try:
                return await self.exchange.create_market_order(pair, "sell", amount)
            except:
                return None

    async def twap_buy(
        self,
        pair: str,
        total_amount: float,
        current_price: float,
        num_slices: int = 5,
        interval_seconds: int = 30,
    ) -> list[dict]:
        """
        TWAP — achète en plusieurs tranches espacées dans le temps.
        Réduit l'impact de marché pour les gros ordres.
        """
        slice_amount = total_amount / num_slices
        orders = []

        log.info(f"🔄 TWAP BUY {pair}: {total_amount} en {num_slices} tranches de {slice_amount}")

        for i in range(num_slices):
            order = await self.smart_buy(pair, slice_amount, current_price, urgency="normal")
            if order:
                orders.append(order)

            if i < num_slices - 1:
                await asyncio.sleep(interval_seconds)
                # Rafraîchit le prix
                try:
                    ticker = await self.exchange.fetch_ticker(pair)
                    current_price = ticker["last"]
                except:
                    pass

        log.info(f"✅ TWAP terminé: {len(orders)}/{num_slices} tranches exécutées")
        return orders

    async def _wait_for_fill(self, order_id: str, pair: str, timeout: int) -> Optional[dict]:
        """Attend qu'un ordre soit rempli ou timeout."""
        start = time.time()
        while time.time() - start < timeout:
            try:
                order = await self.exchange.fetch_order(order_id, pair)
                if order["status"] == "closed":
                    return order
                if order["status"] == "canceled":
                    return None
            except Exception:
                pass
            await asyncio.sleep(2)
        return None

    def _get_price_precision(self, pair: str) -> int:
        """Retourne la précision de prix pour une paire."""
        try:
            market = self.exchange.market(pair)
            return market.get("precision", {}).get("price", 2)
        except:
            return 2


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Multi-Pair Trading Engine — Orchestre tout
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class MultiPairPosition:
    pair: str
    entry_price: float
    amount: float
    entry_time: str
    stop_loss: float
    take_profit: float
    highest_price: float
    allocation_pct: float
    score_at_entry: float


class MultiPairEngine:
    """
    Moteur de trading multi-paires.
    
    Cycle:
    1. Scan du marché → identifie les opportunités
    2. Allocation → répartit le capital
    3. Ordres → place les ordres intelligemment
    4. Gestion → trailing stops, rééquilibrage
    5. Repeat
    
    Usage dans bot.py:
    
        from multipair import MultiPairEngine
        
        self.multi = MultiPairEngine()
        await self.multi.initialize(api_key, api_secret)
        
        # Dans la boucle principale:
        actions = await self.multi.tick()
        for action in actions:
            # Exécute les actions (buy/sell/rebalance)
    """

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.scanner = MarketScanner()
        self.allocator = PortfolioAllocator(method="kelly_adjusted")
        self.order_manager: Optional[SmartOrderManager] = None
        self.positions: list[MultiPairPosition] = []
        self.max_positions = self.config.get("max_positions", 5)
        self.stop_loss_pct = self.config.get("stop_loss_pct", 3.0)
        self.take_profit_pct = self.config.get("take_profit_pct", 6.0)
        self.trailing_stop_pct = self.config.get("trailing_stop_pct", 2.5)
        self.rebalance_interval = self.config.get("rebalance_interval", 3600)  # 1h
        self._last_rebalance = 0
        self._portfolio_value = 0

    async def initialize(self, api_key: str = "", api_secret: str = ""):
        """Initialise les connexions."""
        await self.scanner.connect(api_key, api_secret)
        self.order_manager = SmartOrderManager(self.scanner.exchange)
        log.info("🚀 Multi-Pair Engine initialisé")

    async def tick(self, portfolio_value: float) -> list[dict]:
        """
        Un cycle du moteur multi-paires.
        Retourne une liste d'actions à exécuter.
        """
        self._portfolio_value = portfolio_value
        actions = []

        # ── 1. Vérifie les stop-loss / take-profit ──
        exit_actions = await self._check_exits()
        actions.extend(exit_actions)

        # ── 2. Scanne le marché ──
        opportunities = await self.scanner.get_top_opportunities(
            n=self.max_positions * 2,
            min_score=55,
        )

        # ── 3. Vérifie si on doit rééquilibrer ──
        now = time.time()
        should_rebalance = (now - self._last_rebalance) >= self.rebalance_interval

        if should_rebalance or len(self.positions) < self.max_positions:
            # Alloue le capital
            current_pairs = [p.pair for p in self.positions]
            allocations = self.allocator.allocate(
                portfolio_value=portfolio_value,
                opportunities=opportunities,
                current_positions=current_pairs,
                max_positions=self.max_positions,
            )

            # Nouvelles positions à ouvrir
            for alloc in allocations:
                if alloc["pair"] not in current_pairs and alloc["signal"] == "BUY":
                    actions.append({
                        "type": "BUY",
                        "pair": alloc["pair"],
                        "amount_usd": alloc["allocation_usd"],
                        "reason": alloc["reason"],
                        "score": alloc["score"],
                    })

            if should_rebalance:
                self._last_rebalance = now

                # Vérifie les positions existantes qui ne sont plus dans le top
                top_pairs = {o.pair for o in opportunities[:self.max_positions]}
                for pos in self.positions[:]:
                    if pos.pair not in top_pairs:
                        # La paire n'est plus prometteuse → envisage de vendre
                        current_score = next(
                            (o.score for o in opportunities if o.pair == pos.pair),
                            0
                        )
                        if current_score < 40:  # Score devenu mauvais
                            actions.append({
                                "type": "SELL",
                                "pair": pos.pair,
                                "reason": f"Score tombé à {current_score:.0f} — remplacement",
                                "urgency": "normal",
                            })

        return actions

    async def _check_exits(self) -> list[dict]:
        """Vérifie les conditions de sortie pour chaque position."""
        actions = []

        for pos in self.positions[:]:
            try:
                ticker = await self.scanner.exchange.fetch_ticker(pos.pair)
                price = ticker["last"]
            except:
                continue

            # Update trailing stop
            if price > pos.highest_price:
                pos.highest_price = price

            trailing_stop = pos.highest_price * (1 - self.trailing_stop_pct / 100)
            pnl_pct = (price - pos.entry_price) / pos.entry_price * 100

            # Stop-loss
            if price <= pos.stop_loss or price <= trailing_stop:
                actions.append({
                    "type": "STOP_LOSS",
                    "pair": pos.pair,
                    "pnl_pct": round(pnl_pct, 2),
                    "reason": f"Stop-loss @ ${price:.2f} (entry: ${pos.entry_price:.2f})",
                    "urgency": "high",
                })

            # Take-profit
            elif price >= pos.take_profit:
                actions.append({
                    "type": "TAKE_PROFIT",
                    "pair": pos.pair,
                    "pnl_pct": round(pnl_pct, 2),
                    "reason": f"Take-profit @ ${price:.2f} (+{pnl_pct:.1f}%)",
                    "urgency": "normal",
                })

        return actions

    def add_position(self, pair: str, entry_price: float, amount: float,
                     allocation_pct: float, score: float):
        """Enregistre une nouvelle position."""
        pos = MultiPairPosition(
            pair=pair,
            entry_price=entry_price,
            amount=amount,
            entry_time=datetime.now(timezone.utc).isoformat(),
            stop_loss=round(entry_price * (1 - self.stop_loss_pct / 100), 8),
            take_profit=round(entry_price * (1 + self.take_profit_pct / 100), 8),
            highest_price=entry_price,
            allocation_pct=allocation_pct,
            score_at_entry=score,
        )
        self.positions.append(pos)
        log.info(
            f"📊 Position ouverte: {pair} @ ${entry_price:.4f} "
            f"(alloc: {allocation_pct:.1f}%, score: {score:.0f})"
        )

    def remove_position(self, pair: str):
        """Supprime une position."""
        self.positions = [p for p in self.positions if p.pair != pair]

    def get_portfolio_summary(self) -> dict:
        """Résumé du portfolio multi-paires."""
        return {
            "total_positions": len(self.positions),
            "max_positions": self.max_positions,
            "positions": [
                {
                    "pair": p.pair,
                    "entry_price": p.entry_price,
                    "amount": p.amount,
                    "allocation_pct": p.allocation_pct,
                    "stop_loss": p.stop_loss,
                    "take_profit": p.take_profit,
                    "score_at_entry": p.score_at_entry,
                }
                for p in self.positions
            ],
        }

    async def close(self):
        await self.scanner.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Test standalone
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def test():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s │ %(message)s")

    print("""
    ╔══════════════════════════════════════════════════════════════╗
    ║  🔍 NEXUS TRADER — Market Scanner Test                      ║
    ╚══════════════════════════════════════════════════════════════╝
    """)

    scanner = MarketScanner()
    await scanner.connect()

    scores = await scanner.scan()

    print(f"\n{'═'*100}")
    print(f"  {'Rank':<5} {'Pair':<12} {'Price':>12} {'Score':>7} {'Signal':<8} {'24h':>8} {'Mom':>6} {'Vol':>6} {'Trend':>6} {'RSI':>5}")
    print(f"  {'─'*5} {'─'*12} {'─'*12} {'─'*7} {'─'*8} {'─'*8} {'─'*6} {'─'*6} {'─'*6} {'─'*5}")

    for i, s in enumerate(scores[:20]):
        emoji = "🟢" if s.signal == "BUY" else "🔴" if s.signal == "SELL" else "⚪"
        print(
            f"  {i+1:<5} {s.pair:<12} ${s.price:>11.4f} {s.score:>6.1f} "
            f"{emoji} {s.signal:<6} {s.change_24h:>+7.2f}% {s.momentum_score:>5.0f} "
            f"{s.volume_score:>5.0f} {s.trend_score:>5.0f} {s.rsi:>5.1f}"
        )

    print(f"{'═'*100}")

    # Test allocation
    top = [s for s in scores if s.signal == "BUY"][:5]
    allocator = PortfolioAllocator()
    allocs = allocator.allocate(10000, top)

    print(f"\n  💰 ALLOCATION RECOMMANDÉE (Portfolio: $10,000)")
    print(f"  {'─'*60}")
    for a in allocs:
        print(f"  {a['pair']:<12} ${a['allocation_usd']:>8.2f} ({a['allocation_pct']:>5.1f}%)  {a['reason']}")

    await scanner.close()


if __name__ == "__main__":
    asyncio.run(test())
