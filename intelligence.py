"""
╔══════════════════════════════════════════════════════════════════════╗
║  NEXUS TRADER — Module Intelligence Contextuelle                   ║
║  Donne au bot une "conscience du monde"                            ║
║                                                                      ║
║  Composants:                                                         ║
║  1. MarketRegimeDetector — Détecte le type de marché               ║
║  2. WhaleWatcher — Surveille les gros mouvements on-chain          ║
║  3. EventRadar — Détecte les événements critiques en temps réel    ║
║  4. DeepAnalyst — Claude analyse les news en profondeur            ║
║  5. AdaptiveStrategyManager — Change de stratégie automatiquement  ║
║  6. EmergencyShield — Mode défensif automatique                    ║
╚══════════════════════════════════════════════════════════════════════╝

INSTALLATION:
    pip install aiohttp anthropic feedparser

CONFIGURATION .env:
    ANTHROPIC_API_KEY=sk-ant-...
    WHALE_ALERT_API_KEY=...          (optionnel, whale-alert.io)
    EMERGENCY_SHIELD_ENABLED=true
    AUTO_REGIME_SWITCH=true
"""

import asyncio
import json
import logging
import math
import os
import time
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

try:
    import aiohttp
except ImportError:
    pass

try:
    from anthropic import AsyncAnthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

try:
    import feedparser
    HAS_FEEDPARSER = True
except ImportError:
    HAS_FEEDPARSER = False

log = logging.getLogger("nexus.intelligence")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1. MARKET REGIME DETECTOR
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Détecte automatiquement dans quel "régime" le marché se trouve
# et adapte la stratégie en conséquence.

class MarketRegime(str, Enum):
    STRONG_UPTREND = "strong_uptrend"      # Tendance haussière forte
    MILD_UPTREND = "mild_uptrend"          # Tendance haussière modérée
    RANGING = "ranging"                     # Marché latéral / range
    MILD_DOWNTREND = "mild_downtrend"      # Tendance baissière modérée
    STRONG_DOWNTREND = "strong_downtrend"  # Tendance baissière forte
    HIGH_VOLATILITY = "high_volatility"    # Volatilité extrême / chaos
    CRASH = "crash"                         # Crash / panique


@dataclass
class RegimeReport:
    regime: MarketRegime
    confidence: float
    volatility_percentile: float    # 0-100, où se situe la volatilité actuelle
    trend_strength: float           # -1.0 (forte baisse) à +1.0 (forte hausse)
    recommended_strategy: str
    recommended_exposure: float     # 0.0 (cash) à 1.0 (full exposure)
    details: str


class MarketRegimeDetector:
    """
    Analyse les bougies pour déterminer le régime de marché actuel.
    
    Utilise :
    - ADX (Average Directional Index) pour la force de tendance
    - Volatilité historique vs moyenne
    - Pente des moyennes mobiles
    - Volume relatif
    - Taux de changement (ROC) multi-périodes
    """

    # Quelle stratégie marche le mieux dans chaque régime
    REGIME_STRATEGY_MAP = {
        MarketRegime.STRONG_UPTREND:    {"strategy": "ma_crossover",      "exposure": 1.0},
        MarketRegime.MILD_UPTREND:      {"strategy": "combined",          "exposure": 0.8},
        MarketRegime.RANGING:           {"strategy": "rsi_reversal",      "exposure": 0.6},
        MarketRegime.MILD_DOWNTREND:    {"strategy": "bollinger_bounce",  "exposure": 0.4},
        MarketRegime.STRONG_DOWNTREND:  {"strategy": "rsi_reversal",      "exposure": 0.2},
        MarketRegime.HIGH_VOLATILITY:   {"strategy": "bollinger_bounce",  "exposure": 0.3},
        MarketRegime.CRASH:             {"strategy": None,                "exposure": 0.0},  # Ne rien faire
    }

    def detect(self, candles: list[dict]) -> RegimeReport:
        """Analyse les bougies et retourne le régime actuel."""
        if len(candles) < 50:
            return RegimeReport(
                regime=MarketRegime.RANGING,
                confidence=0.3,
                volatility_percentile=50,
                trend_strength=0,
                recommended_strategy="combined",
                recommended_exposure=0.5,
                details="Données insuffisantes pour une détection fiable",
            )

        closes = [c["close"] for c in candles]

        # ── Calcul de la tendance ──
        trend = self._calculate_trend(closes)

        # ── Calcul de la volatilité ──
        volatility = self._calculate_volatility(closes)
        vol_percentile = self._volatility_percentile(candles)

        # ── Détection de crash ──
        is_crash = self._detect_crash(closes)

        # ── Détermination du régime ──
        if is_crash:
            regime = MarketRegime.CRASH
            confidence = 0.9
        elif vol_percentile > 90:
            regime = MarketRegime.HIGH_VOLATILITY
            confidence = 0.8
        elif trend > 0.6:
            regime = MarketRegime.STRONG_UPTREND
            confidence = min(trend, 1.0)
        elif trend > 0.2:
            regime = MarketRegime.MILD_UPTREND
            confidence = 0.6 + trend * 0.3
        elif trend < -0.6:
            regime = MarketRegime.STRONG_DOWNTREND
            confidence = min(abs(trend), 1.0)
        elif trend < -0.2:
            regime = MarketRegime.MILD_DOWNTREND
            confidence = 0.6 + abs(trend) * 0.3
        else:
            regime = MarketRegime.RANGING
            confidence = 0.7 - abs(trend)

        mapping = self.REGIME_STRATEGY_MAP[regime]

        return RegimeReport(
            regime=regime,
            confidence=round(confidence, 3),
            volatility_percentile=round(vol_percentile, 1),
            trend_strength=round(trend, 3),
            recommended_strategy=mapping["strategy"],
            recommended_exposure=mapping["exposure"],
            details=self._build_details(regime, trend, vol_percentile, closes),
        )

    def _calculate_trend(self, closes: list[float]) -> float:
        """
        Calcule la force et direction de la tendance (-1.0 à +1.0).
        Combine pente SMA + ROC multi-périodes.
        """
        scores = []

        # Pente de la SMA 20 vs SMA 50
        sma20 = sum(closes[-20:]) / 20
        sma50 = sum(closes[-50:]) / 50
        sma_diff = (sma20 - sma50) / sma50
        scores.append(max(-1, min(1, sma_diff * 20)))  # Normalisé

        # Rate of Change (ROC) sur différentes périodes
        for period in [5, 10, 20]:
            if len(closes) > period:
                roc = (closes[-1] - closes[-period]) / closes[-period]
                scores.append(max(-1, min(1, roc * 10)))

        # Position du prix par rapport aux SMAs
        price = closes[-1]
        if price > sma20 > sma50:
            scores.append(0.5)
        elif price < sma20 < sma50:
            scores.append(-0.5)
        else:
            scores.append(0)

        return sum(scores) / len(scores) if scores else 0

    def _calculate_volatility(self, closes: list[float], period: int = 20) -> float:
        """Volatilité historique (écart-type des rendements)."""
        if len(closes) < period + 1:
            return 0
        returns = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(-period, 0)]
        mean_ret = sum(returns) / len(returns)
        variance = sum((r - mean_ret) ** 2 for r in returns) / len(returns)
        return math.sqrt(variance) * math.sqrt(365)  # Annualisée

    def _volatility_percentile(self, candles: list[dict]) -> float:
        """Où se situe la volatilité actuelle par rapport à l'historique."""
        if len(candles) < 50:
            return 50

        closes = [c["close"] for c in candles]
        
        # Calcule la vol sur des fenêtres glissantes
        window = 14
        vols = []
        for i in range(window, len(closes)):
            window_closes = closes[i-window:i]
            returns = [(window_closes[j] - window_closes[j-1]) / window_closes[j-1] 
                       for j in range(1, len(window_closes))]
            if returns:
                mean_r = sum(returns) / len(returns)
                var = sum((r - mean_r) ** 2 for r in returns) / len(returns)
                vols.append(math.sqrt(var))

        if not vols:
            return 50

        current_vol = vols[-1]
        rank = sum(1 for v in vols if v <= current_vol) / len(vols) * 100
        return rank

    def _detect_crash(self, closes: list[float]) -> bool:
        """Détecte un crash (chute rapide et violente)."""
        if len(closes) < 10:
            return False
        
        # Chute de plus de 8% en 5 bougies
        change_5 = (closes[-1] - closes[-5]) / closes[-5]
        if change_5 < -0.08:
            return True
        
        # Chute de plus de 15% en 20 bougies
        if len(closes) >= 20:
            change_20 = (closes[-1] - closes[-20]) / closes[-20]
            if change_20 < -0.15:
                return True

        return False

    def _build_details(self, regime, trend, vol_pct, closes) -> str:
        price = closes[-1]
        sma20 = sum(closes[-20:]) / 20
        sma50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else sma20

        parts = [
            f"Régime: {regime.value}",
            f"Prix: ${price:.0f} | SMA20: ${sma20:.0f} | SMA50: ${sma50:.0f}",
            f"Tendance: {trend:+.3f} | Volatilité: P{vol_pct:.0f}",
        ]

        if regime == MarketRegime.CRASH:
            parts.append("⚠️ CRASH DÉTECTÉ — Toutes positions fermées recommandé")
        elif regime == MarketRegime.HIGH_VOLATILITY:
            parts.append("⚡ Volatilité extrême — Réduction d'exposition recommandée")
        elif regime == MarketRegime.STRONG_UPTREND:
            parts.append("🚀 Forte tendance haussière — Follow the trend")

        return " | ".join(parts)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2. WHALE WATCHER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Surveille les gros mouvements on-chain.
# Quand une baleine déplace 5000 BTC vers un exchange, c'est souvent
# un signe de vente imminente. L'inverse = accumulation.

@dataclass
class WhaleAlert:
    timestamp: str
    amount_usd: float
    crypto: str
    from_type: str      # "exchange", "wallet", "unknown"
    to_type: str
    exchange: str
    direction: str      # "to_exchange" (bearish), "from_exchange" (bullish), "between_wallets"
    significance: float # 0-1


class WhaleWatcher:
    """
    Surveille les mouvements de whales via l'API Whale Alert
    et les données publiques blockchain.
    """

    WHALE_ALERT_URL = "https://api.whale-alert.io/v1/transactions"

    # Données publiques sans API key
    BLOCKCHAIR_URL = "https://api.blockchair.com/bitcoin/transactions"
    
    # Seuils
    MIN_AMOUNT_USD = 1_000_000      # Minimum $1M pour être considéré
    HIGH_AMOUNT_USD = 10_000_000    # $10M+ = signal fort
    MEGA_AMOUNT_USD = 50_000_000   # $50M+ = alerte critique

    def __init__(self):
        self.api_key = os.getenv("WHALE_ALERT_API_KEY", "")
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: list[WhaleAlert] = []
        self._cache_time = 0
        self._cache_ttl = 120  # 2 minutes

    async def _get_session(self) -> aiohttp.ClientSession:
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        return self._session

    async def get_recent_whale_moves(self) -> list[WhaleAlert]:
        """Récupère les mouvements de whales récents."""
        now = time.time()
        if self._cache and (now - self._cache_time < self._cache_ttl):
            return self._cache

        alerts = []

        # ── Whale Alert API (si clé disponible) ──
        if self.api_key:
            try:
                session = await self._get_session()
                params = {
                    "api_key": self.api_key,
                    "min_value": self.MIN_AMOUNT_USD,
                    "start": int(now - 3600),  # Dernière heure
                    "currency": "btc",
                }
                async with session.get(self.WHALE_ALERT_URL, params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for tx in data.get("transactions", []):
                            direction = self._classify_direction(tx)
                            alerts.append(WhaleAlert(
                                timestamp=datetime.fromtimestamp(tx.get("timestamp", 0), tz=timezone.utc).isoformat(),
                                amount_usd=tx.get("amount_usd", 0),
                                crypto=tx.get("symbol", "BTC"),
                                from_type=tx.get("from", {}).get("owner_type", "unknown"),
                                to_type=tx.get("to", {}).get("owner_type", "unknown"),
                                exchange=tx.get("to", {}).get("owner", "") or tx.get("from", {}).get("owner", ""),
                                direction=direction,
                                significance=self._calc_significance(tx.get("amount_usd", 0)),
                            ))
                        log.info(f"🐋 {len(alerts)} mouvements de whales détectés")
            except Exception as e:
                log.warning(f"⚠️ Erreur Whale Alert: {e}")

        # ── Fallback: données publiques via Blockchair ──
        if not alerts:
            try:
                session = await self._get_session()
                params = {"limit": 5, "s": "output_total_usd(desc)"}
                async with session.get(self.BLOCKCHAIR_URL, params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for tx in data.get("data", []):
                            usd_value = tx.get("output_total_usd", 0)
                            if usd_value >= self.MIN_AMOUNT_USD:
                                alerts.append(WhaleAlert(
                                    timestamp=datetime.now(timezone.utc).isoformat(),
                                    amount_usd=usd_value,
                                    crypto="BTC",
                                    from_type="unknown",
                                    to_type="unknown",
                                    exchange="",
                                    direction="unknown",
                                    significance=self._calc_significance(usd_value),
                                ))
            except Exception as e:
                log.debug(f"Blockchair fallback failed: {e}")

        self._cache = alerts
        self._cache_time = now
        return alerts

    def analyze_whale_sentiment(self, alerts: list[WhaleAlert]) -> dict:
        """
        Analyse le sentiment basé sur les mouvements des whales.
        
        Vers exchange = probable vente = bearish
        Hors exchange = accumulation = bullish
        """
        if not alerts:
            return {"signal": "NEUTRAL", "score": 0, "reason": "Pas de données whale", "alert_level": 0}

        bullish_volume = 0
        bearish_volume = 0
        alert_level = 0

        for alert in alerts:
            if alert.direction == "to_exchange":
                bearish_volume += alert.amount_usd
                if alert.amount_usd >= self.MEGA_AMOUNT_USD:
                    alert_level = max(alert_level, 3)  # Critique
                elif alert.amount_usd >= self.HIGH_AMOUNT_USD:
                    alert_level = max(alert_level, 2)  # Important
            elif alert.direction == "from_exchange":
                bullish_volume += alert.amount_usd
                if alert.amount_usd >= self.HIGH_AMOUNT_USD:
                    alert_level = max(alert_level, 1)  # Notable

        total = bullish_volume + bearish_volume
        if total == 0:
            return {"signal": "NEUTRAL", "score": 0, "reason": "Volume whale neutre", "alert_level": 0}

        score = (bullish_volume - bearish_volume) / total  # -1 à +1

        if score > 0.3:
            signal = "BULLISH"
            reason = f"Whales en accumulation (${bullish_volume/1e6:.0f}M sortis des exchanges)"
        elif score < -0.3:
            signal = "BEARISH"
            reason = f"Whales en distribution (${bearish_volume/1e6:.0f}M envoyés vers exchanges)"
        else:
            signal = "NEUTRAL"
            reason = f"Activité whale mixte (B:${bullish_volume/1e6:.0f}M / S:${bearish_volume/1e6:.0f}M)"

        return {
            "signal": signal,
            "score": round(score, 3),
            "reason": reason,
            "alert_level": alert_level,
            "total_volume_usd": total,
        }

    def _classify_direction(self, tx: dict) -> str:
        from_type = tx.get("from", {}).get("owner_type", "")
        to_type = tx.get("to", {}).get("owner_type", "")
        if to_type == "exchange" and from_type != "exchange":
            return "to_exchange"
        if from_type == "exchange" and to_type != "exchange":
            return "from_exchange"
        return "between_wallets"

    def _calc_significance(self, amount_usd: float) -> float:
        if amount_usd >= self.MEGA_AMOUNT_USD:
            return 1.0
        if amount_usd >= self.HIGH_AMOUNT_USD:
            return 0.7
        if amount_usd >= self.MIN_AMOUNT_USD:
            return 0.4
        return 0.1

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3. EVENT RADAR
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Détecte les événements critiques qui nécessitent une réaction immédiate.

class EventSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class MarketEvent:
    title: str
    severity: EventSeverity
    category: str           # "regulation", "hack", "macro", "adoption", "technical"
    impact_direction: str   # "bullish", "bearish", "uncertain"
    recommended_action: str
    source: str
    timestamp: str


class EventRadar:
    """
    Détecte les événements critiques via RSS feeds et APIs.
    Catégorise automatiquement et évalue l'impact.
    """

    # RSS feeds crypto majeurs
    RSS_FEEDS = [
        "https://cointelegraph.com/rss",
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "https://decrypt.co/feed",
    ]

    # Mots-clés critiques qui déclenchent une alerte immédiate
    CRITICAL_KEYWORDS = {
        # Bearish critique
        "hack": ("hack", EventSeverity.CRITICAL, "bearish"),
        "exploit": ("hack", EventSeverity.CRITICAL, "bearish"),
        "stolen": ("hack", EventSeverity.HIGH, "bearish"),
        "breach": ("hack", EventSeverity.HIGH, "bearish"),
        "ban crypto": ("regulation", EventSeverity.CRITICAL, "bearish"),
        "ban bitcoin": ("regulation", EventSeverity.CRITICAL, "bearish"),
        "sec charges": ("regulation", EventSeverity.HIGH, "bearish"),
        "sec sues": ("regulation", EventSeverity.HIGH, "bearish"),
        "ponzi": ("hack", EventSeverity.HIGH, "bearish"),
        "rug pull": ("hack", EventSeverity.CRITICAL, "bearish"),
        "bankruptcy": ("macro", EventSeverity.CRITICAL, "bearish"),
        "insolvent": ("macro", EventSeverity.HIGH, "bearish"),
        "crash": ("technical", EventSeverity.HIGH, "bearish"),
        "war": ("macro", EventSeverity.HIGH, "uncertain"),

        # Bullish critique
        "etf approved": ("regulation", EventSeverity.CRITICAL, "bullish"),
        "etf approval": ("regulation", EventSeverity.CRITICAL, "bullish"),
        "legal tender": ("adoption", EventSeverity.CRITICAL, "bullish"),
        "rate cut": ("macro", EventSeverity.HIGH, "bullish"),
        "institutional adoption": ("adoption", EventSeverity.HIGH, "bullish"),
        "halving": ("technical", EventSeverity.MEDIUM, "bullish"),

        # Incertain mais important
        "regulation": ("regulation", EventSeverity.MEDIUM, "uncertain"),
        "federal reserve": ("macro", EventSeverity.MEDIUM, "uncertain"),
        "interest rate": ("macro", EventSeverity.MEDIUM, "uncertain"),
    }

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._events: list[MarketEvent] = []
        self._last_scan = 0
        self._scan_interval = 300  # 5 minutes

    async def _get_session(self):
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        return self._session

    async def scan(self) -> list[MarketEvent]:
        """Scanne les sources pour détecter des événements critiques."""
        now = time.time()
        if now - self._last_scan < self._scan_interval:
            return self._events

        events = []

        if HAS_FEEDPARSER:
            for feed_url in self.RSS_FEEDS:
                try:
                    session = await self._get_session()
                    async with session.get(feed_url) as resp:
                        if resp.status == 200:
                            content = await resp.text()
                            feed = feedparser.parse(content)
                            for entry in feed.entries[:10]:
                                event = self._analyze_entry(entry, feed_url)
                                if event:
                                    events.append(event)
                except Exception as e:
                    log.debug(f"RSS feed error ({feed_url}): {e}")

        # Trie par sévérité
        severity_order = {EventSeverity.CRITICAL: 0, EventSeverity.HIGH: 1, 
                         EventSeverity.MEDIUM: 2, EventSeverity.LOW: 3}
        events.sort(key=lambda e: severity_order.get(e.severity, 99))

        self._events = events
        self._last_scan = now

        critical_count = sum(1 for e in events if e.severity == EventSeverity.CRITICAL)
        if critical_count > 0:
            log.warning(f"🚨 {critical_count} ÉVÉNEMENTS CRITIQUES DÉTECTÉS!")
            for e in events:
                if e.severity == EventSeverity.CRITICAL:
                    log.warning(f"   ⚠️ [{e.category}] {e.title}")
        elif events:
            log.info(f"📡 {len(events)} événements détectés")

        return events

    def _analyze_entry(self, entry, source_url) -> Optional[MarketEvent]:
        """Analyse un article RSS et détecte les événements critiques."""
        title = entry.get("title", "").lower()
        summary = entry.get("summary", "").lower()
        text = f"{title} {summary}"

        for keyword, (category, severity, direction) in self.CRITICAL_KEYWORDS.items():
            if keyword in text:
                # Détermine l'action recommandée
                if severity == EventSeverity.CRITICAL:
                    if direction == "bearish":
                        action = "EMERGENCY_EXIT — Fermer toutes les positions immédiatement"
                    elif direction == "bullish":
                        action = "STRONG_BUY — Opportunité majeure détectée"
                    else:
                        action = "PAUSE — Mettre le bot en pause en attendant clarification"
                elif severity == EventSeverity.HIGH:
                    if direction == "bearish":
                        action = "REDUCE_EXPOSURE — Réduire les positions de 50%"
                    elif direction == "bullish":
                        action = "INCREASE_EXPOSURE — Augmenter l'exposition"
                    else:
                        action = "TIGHTEN_STOPS — Resserrer les stop-loss"
                else:
                    action = "MONITOR — Surveiller l'évolution"

                return MarketEvent(
                    title=entry.get("title", ""),
                    severity=severity,
                    category=category,
                    impact_direction=direction,
                    recommended_action=action,
                    source=source_url,
                    timestamp=entry.get("published", datetime.now(timezone.utc).isoformat()),
                )

        return None

    def get_risk_level(self) -> dict:
        """Évalue le niveau de risque global basé sur les événements détectés."""
        if not self._events:
            return {"level": "NORMAL", "score": 0, "action": "CONTINUE", "events": []}

        critical = sum(1 for e in self._events if e.severity == EventSeverity.CRITICAL)
        high = sum(1 for e in self._events if e.severity == EventSeverity.HIGH)
        bearish_critical = sum(1 for e in self._events 
                              if e.severity == EventSeverity.CRITICAL and e.impact_direction == "bearish")

        if bearish_critical > 0:
            return {
                "level": "EMERGENCY",
                "score": 1.0,
                "action": "EMERGENCY_EXIT",
                "reason": f"{bearish_critical} événement(s) critique(s) bearish détecté(s)",
                "events": [e.title for e in self._events if e.severity == EventSeverity.CRITICAL],
            }
        elif critical > 0:
            return {
                "level": "HIGH_ALERT",
                "score": 0.8,
                "action": "PAUSE_TRADING",
                "reason": f"{critical} événement(s) critique(s) — situation incertaine",
                "events": [e.title for e in self._events if e.severity in (EventSeverity.CRITICAL, EventSeverity.HIGH)],
            }
        elif high >= 2:
            return {
                "level": "ELEVATED",
                "score": 0.5,
                "action": "REDUCE_EXPOSURE",
                "reason": f"{high} événements importants détectés",
                "events": [e.title for e in self._events if e.severity == EventSeverity.HIGH],
            }
        else:
            return {"level": "NORMAL", "score": 0.1, "action": "CONTINUE", "events": []}

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4. DEEP ANALYST (Claude AI)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Utilise Claude pour une analyse profonde — pas juste du scoring,
# mais une vraie compréhension du contexte et des implications.

class DeepAnalyst:
    """
    Claude analyse les événements comme un trader senior le ferait :
    - Lit entre les lignes
    - Comprend les implications de second ordre
    - Évalue si le marché a déjà "pricé" l'information
    - Donne des recommandations actionables
    """

    SYSTEM_PROMPT = """Tu es un trader crypto senior avec 15 ans d'expérience.
Tu analyses les événements de marché pour un bot de trading algorithmique.

Ta force : comprendre les implications NON ÉVIDENTES des événements.
- Un hack d'exchange peut être bullish si ça pousse vers le self-custody
- Une régulation "négative" peut être bullish si ça donne de la clarté juridique
- Un pump violent peut être bearish si c'est un short squeeze sans fondamentaux

Tu dois analyser CHAQUE événement en profondeur et répondre en JSON:
{
    "events_analysis": [
        {
            "event": "titre",
            "surface_reading": "Ce que le marché voit en premier",
            "deep_reading": "Ce que ça implique vraiment",
            "already_priced": true/false,
            "time_horizon": "immediate|short_term|medium_term",
            "impact_score": <-1.0 à +1.0>
        }
    ],
    "market_context": "Résumé du contexte macro en 2 phrases",
    "overall_recommendation": {
        "action": "AGGRESSIVE_BUY|BUY|HOLD|REDUCE|SELL|EMERGENCY_EXIT",
        "confidence": <0 à 1>,
        "reasoning": "Explication en 2-3 phrases",
        "key_risk": "Le plus gros risque à surveiller",
        "contrarian_view": "Et si on avait tort — quel est le scénario inverse?"
    }
}

IMPORTANT:
- Sois honnête quand tu n'es pas sûr (confiance basse)
- Le "already_priced" est crucial — si tout le monde en parle depuis 24h, c'est probablement déjà dans le prix
- Pense toujours au scénario contrarian"""

    def __init__(self):
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        self.client = AsyncAnthropic(api_key=api_key) if api_key and HAS_ANTHROPIC else None
        self._cache: dict = {}
        self._cache_ttl = 900  # 15 minutes

    async def analyze(
        self,
        events: list[MarketEvent],
        regime: RegimeReport,
        whale_data: dict,
        current_price: float,
    ) -> Optional[dict]:
        """Analyse profonde de la situation complète."""
        if not self.client:
            return None

        # Cache basé sur les titres d'événements
        cache_key = hash(tuple(e.title for e in events[:5]) + (regime.regime,))
        if cache_key in self._cache:
            ct, cd = self._cache[cache_key]
            if time.time() - ct < self._cache_ttl:
                return cd

        # Construit le prompt
        events_text = "\n".join(
            f"- [{e.severity.value.upper()}] [{e.category}] {e.title}"
            for e in events[:10]
        ) or "Aucun événement notable détecté"

        prompt = f"""SITUATION ACTUELLE DU MARCHÉ CRYPTO:

Prix BTC: ${current_price:,.0f}
Régime de marché: {regime.regime.value} (tendance: {regime.trend_strength:+.2f}, vol: P{regime.volatility_percentile:.0f})

ÉVÉNEMENTS RÉCENTS:
{events_text}

ACTIVITÉ WHALES:
Signal: {whale_data.get('signal', 'N/A')}
Détails: {whale_data.get('reason', 'Pas de données')}
Niveau d'alerte: {whale_data.get('alert_level', 0)}/3

Analyse cette situation en profondeur. Que devrait faire le bot?"""

        try:
            response = await self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=800,
                system=self.SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )

            text = response.content[0].text.strip()
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
                text = text.strip()

            result = json.loads(text)
            self._cache[cache_key] = (time.time(), result)

            rec = result.get("overall_recommendation", {})
            log.info(
                f"🧠 Deep Analysis: {rec.get('action', '?')} "
                f"(confiance: {rec.get('confidence', 0):.0%}) — "
                f"{rec.get('reasoning', '')[:100]}"
            )
            return result

        except Exception as e:
            log.warning(f"⚠️ Erreur Deep Analyst: {e}")
            return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 5. ADAPTIVE STRATEGY MANAGER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Change automatiquement de stratégie selon les conditions du marché.

class AdaptiveStrategyManager:
    """
    Gère le switch automatique de stratégie basé sur :
    - Le régime de marché
    - Le sentiment
    - Les événements
    - La performance récente de chaque stratégie
    """

    def __init__(self):
        self.current_strategy = "combined"
        self.strategy_performance: dict[str, list[float]] = {}  # Historique P&L par stratégie
        self._last_switch_time = 0
        self._min_switch_interval = 1800  # Pas de switch plus fréquent que 30 min

    def recommend_strategy(
        self,
        regime: RegimeReport,
        event_risk: dict,
        deep_analysis: Optional[dict] = None,
    ) -> dict:
        """Recommande la meilleure stratégie pour les conditions actuelles."""
        
        now = time.time()
        can_switch = (now - self._last_switch_time) >= self._min_switch_interval

        # Cas d'urgence — override tout
        if event_risk.get("level") == "EMERGENCY":
            return {
                "strategy": None,
                "action": "STOP",
                "reason": "🚨 Urgence détectée — trading suspendu",
                "exposure": 0.0,
            }

        if event_risk.get("level") == "HIGH_ALERT":
            return {
                "strategy": self.current_strategy,
                "action": "PAUSE",
                "reason": "⚠️ Alerte haute — trades en pause, positions maintenues",
                "exposure": 0.3,
            }

        # Recommandation basée sur le régime
        recommended = regime.recommended_strategy
        exposure = regime.recommended_exposure

        # Si l'analyse IA est dispo, elle peut override
        if deep_analysis:
            rec = deep_analysis.get("overall_recommendation", {})
            ai_action = rec.get("action", "HOLD")
            ai_confidence = rec.get("confidence", 0)

            if ai_action == "EMERGENCY_EXIT" and ai_confidence > 0.7:
                return {
                    "strategy": None,
                    "action": "STOP",
                    "reason": f"🧠 IA recommande EXIT — {rec.get('reasoning', '')}",
                    "exposure": 0.0,
                }
            elif ai_action == "AGGRESSIVE_BUY" and ai_confidence > 0.7:
                exposure = min(1.0, exposure + 0.3)
            elif ai_action in ("REDUCE", "SELL"):
                exposure = max(0.1, exposure - 0.3)

        # Vérifie la performance récente de la stratégie recommandée
        perf = self.strategy_performance.get(recommended, [])
        if len(perf) >= 5:
            recent_win_rate = sum(1 for p in perf[-5:] if p > 0) / 5
            if recent_win_rate < 0.3:
                # Cette stratégie performe mal récemment, fallback sur combined
                log.info(f"📉 {recommended} sous-performe ({recent_win_rate:.0%} win rate) → fallback combined")
                recommended = "combined"

        should_switch = recommended != self.current_strategy and can_switch

        if should_switch:
            self._last_switch_time = now
            log.info(f"🔄 Switch stratégie: {self.current_strategy} → {recommended} (régime: {regime.regime.value})")
            self.current_strategy = recommended

        return {
            "strategy": self.current_strategy,
            "action": "SWITCH" if should_switch else "MAINTAIN",
            "reason": f"Régime {regime.regime.value} → {self.current_strategy}",
            "exposure": exposure,
        }

    def record_trade_result(self, strategy: str, pnl: float):
        """Enregistre le résultat d'un trade pour évaluer la performance."""
        if strategy not in self.strategy_performance:
            self.strategy_performance[strategy] = []
        self.strategy_performance[strategy].append(pnl)
        # Garde les 50 derniers trades par stratégie
        self.strategy_performance[strategy] = self.strategy_performance[strategy][-50:]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 6. EMERGENCY SHIELD
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Système de protection automatique multi-niveaux.

class EmergencyShield:
    """
    Protection automatique du portfolio en cas de danger.
    
    Niveaux :
    1. YELLOW  — Resserre les stop-loss, réduit la taille des positions
    2. ORANGE  — Stop les nouveaux achats, conserve les positions
    3. RED     — Ferme 50% des positions
    4. BLACK   — Ferme TOUTES les positions, bot en pause
    """

    def __init__(self):
        self.current_level = 0  # 0 = normal, 1-4 = alert levels
        self.activated_at: Optional[float] = None
        self.cooldown = 3600  # 1 heure avant de revenir à la normale

    def evaluate(
        self,
        regime: RegimeReport,
        event_risk: dict,
        whale_data: dict,
        daily_pnl_pct: float,  # Perte journalière en %
    ) -> dict:
        """Évalue le niveau de danger et retourne les actions à prendre."""

        danger_score = 0
        reasons = []

        # ── Crash détecté ──
        if regime.regime == MarketRegime.CRASH:
            danger_score += 40
            reasons.append("Crash de marché détecté")

        # ── Volatilité extrême ──
        if regime.volatility_percentile > 95:
            danger_score += 20
            reasons.append(f"Volatilité au P{regime.volatility_percentile:.0f}")

        # ── Événements critiques ──
        event_level = event_risk.get("level", "NORMAL")
        if event_level == "EMERGENCY":
            danger_score += 50
            reasons.append("Événement critique bearish")
        elif event_level == "HIGH_ALERT":
            danger_score += 30
            reasons.append("Événement à haut risque")

        # ── Whales en mode vente ──
        if whale_data.get("alert_level", 0) >= 3:
            danger_score += 15
            reasons.append("Mouvement whale critique")

        # ── Pertes journalières ──
        if daily_pnl_pct < -3:
            danger_score += 15
            reasons.append(f"Perte journalière: {daily_pnl_pct:.1f}%")
        if daily_pnl_pct < -5:
            danger_score += 20
            reasons.append(f"Perte journalière sévère: {daily_pnl_pct:.1f}%")

        # ── Tendance fortement baissière ──
        if regime.trend_strength < -0.7:
            danger_score += 10
            reasons.append("Tendance fortement baissière")

        # Détermine le niveau
        if danger_score >= 60:
            level = 4  # BLACK
        elif danger_score >= 40:
            level = 3  # RED
        elif danger_score >= 25:
            level = 2  # ORANGE
        elif danger_score >= 15:
            level = 1  # YELLOW
        else:
            level = 0  # NORMAL

        # Cooldown avant de baisser le niveau
        if level < self.current_level and self.activated_at:
            if time.time() - self.activated_at < self.cooldown:
                level = self.current_level  # Maintient le niveau

        if level > self.current_level:
            self.activated_at = time.time()
            level_names = {0: "NORMAL", 1: "🟡 YELLOW", 2: "🟠 ORANGE", 3: "🔴 RED", 4: "⬛ BLACK"}
            log.warning(f"🛡️ EMERGENCY SHIELD → {level_names[level]} (score: {danger_score})")

        self.current_level = level

        # Actions par niveau
        actions = {
            0: {
                "level_name": "NORMAL",
                "stop_loss_multiplier": 1.0,
                "position_size_multiplier": 1.0,
                "allow_new_buys": True,
                "close_positions_pct": 0,
                "pause_bot": False,
            },
            1: {
                "level_name": "YELLOW",
                "stop_loss_multiplier": 0.7,      # Stop-loss 30% plus serré
                "position_size_multiplier": 0.5,   # Positions 50% plus petites
                "allow_new_buys": True,
                "close_positions_pct": 0,
                "pause_bot": False,
            },
            2: {
                "level_name": "ORANGE",
                "stop_loss_multiplier": 0.5,
                "position_size_multiplier": 0.3,
                "allow_new_buys": False,           # Plus d'achats
                "close_positions_pct": 0,
                "pause_bot": False,
            },
            3: {
                "level_name": "RED",
                "stop_loss_multiplier": 0.3,
                "position_size_multiplier": 0,
                "allow_new_buys": False,
                "close_positions_pct": 50,         # Ferme 50% des positions
                "pause_bot": False,
            },
            4: {
                "level_name": "BLACK",
                "stop_loss_multiplier": 0,
                "position_size_multiplier": 0,
                "allow_new_buys": False,
                "close_positions_pct": 100,        # Ferme TOUT
                "pause_bot": True,                 # Bot en pause
            },
        }

        result = actions[level]
        result["danger_score"] = danger_score
        result["reasons"] = reasons
        return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ORCHESTRATEUR — Combine tout
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ContextualIntelligence:
    """
    Orchestrateur principal. Combine tous les modules pour donner
    au bot une vision complète du marché.
    
    Usage dans bot.py:
    
        from intelligence import ContextualIntelligence
        
        # Init
        self.intelligence = ContextualIntelligence()
        
        # Dans _tick():
        ctx = await self.intelligence.analyze(self.candles, price)
        
        # ctx contient:
        # - regime (quel type de marché)
        # - events (événements critiques)
        # - whales (mouvements de gros porteurs)
        # - deep_analysis (analyse IA)
        # - strategy_recommendation (quelle stratégie utiliser)
        # - shield (niveau de protection actif)
        # - should_trade (bool — est-ce qu'on trade ou pas)
    """

    def __init__(self):
        self.regime_detector = MarketRegimeDetector()
        self.whale_watcher = WhaleWatcher()
        self.event_radar = EventRadar()
        self.deep_analyst = DeepAnalyst()
        self.strategy_manager = AdaptiveStrategyManager()
        self.shield = EmergencyShield()
        self._last_full_analysis = 0
        self._analysis_interval = 120  # Analyse complète toutes les 2 min

    async def analyze(self, candles: list[dict], current_price: float, daily_pnl_pct: float = 0) -> dict:
        """Analyse contextuelle complète."""

        # ── 1. Régime de marché (toujours, c'est rapide) ──
        regime = self.regime_detector.detect(candles)

        # ── 2. Les autres sources (throttled) ──
        now = time.time()
        if now - self._last_full_analysis >= self._analysis_interval:
            self._last_full_analysis = now

            # Parallélise les appels réseau
            whale_alerts, events = await asyncio.gather(
                self.whale_watcher.get_recent_whale_moves(),
                self.event_radar.scan(),
            )

            whale_data = self.whale_watcher.analyze_whale_sentiment(whale_alerts)
            event_risk = self.event_radar.get_risk_level()

            # Deep analysis IA (si disponible)
            deep_analysis = await self.deep_analyst.analyze(
                events, regime, whale_data, current_price
            )

            # Sauvegarde pour réutiliser entre les analyses complètes
            self._cached = {
                "whale_data": whale_data,
                "event_risk": event_risk,
                "events": events,
                "deep_analysis": deep_analysis,
            }
        else:
            whale_data = self._cached.get("whale_data", {})
            event_risk = self._cached.get("event_risk", {"level": "NORMAL"})
            events = self._cached.get("events", [])
            deep_analysis = self._cached.get("deep_analysis")

        # ── 3. Emergency Shield ──
        shield_status = self.shield.evaluate(regime, event_risk, whale_data, daily_pnl_pct)

        # ── 4. Recommandation de stratégie ──
        strategy_rec = self.strategy_manager.recommend_strategy(regime, event_risk, deep_analysis)

        # ── 5. Décision finale: est-ce qu'on trade ? ──
        should_trade = (
            not shield_status["pause_bot"]
            and strategy_rec["action"] != "STOP"
            and shield_status["allow_new_buys"]
        )

        context = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "price": current_price,

            # Régime
            "regime": regime.regime.value,
            "regime_confidence": regime.confidence,
            "trend_strength": regime.trend_strength,
            "volatility_percentile": regime.volatility_percentile,

            # Events
            "event_risk_level": event_risk.get("level", "NORMAL"),
            "critical_events": [e.title for e in events if e.severity == EventSeverity.CRITICAL],

            # Whales
            "whale_signal": whale_data.get("signal", "NEUTRAL"),
            "whale_alert_level": whale_data.get("alert_level", 0),

            # IA
            "ai_recommendation": deep_analysis.get("overall_recommendation", {}).get("action") if deep_analysis else None,
            "ai_confidence": deep_analysis.get("overall_recommendation", {}).get("confidence") if deep_analysis else None,
            "ai_reasoning": deep_analysis.get("overall_recommendation", {}).get("reasoning") if deep_analysis else None,
            "ai_contrarian": deep_analysis.get("overall_recommendation", {}).get("contrarian_view") if deep_analysis else None,

            # Stratégie
            "recommended_strategy": strategy_rec["strategy"],
            "strategy_action": strategy_rec["action"],
            "recommended_exposure": strategy_rec["exposure"],

            # Shield
            "shield_level": shield_status["level_name"],
            "shield_reasons": shield_status["reasons"],
            "position_size_mult": shield_status["position_size_multiplier"],
            "stop_loss_mult": shield_status["stop_loss_multiplier"],
            "allow_new_buys": shield_status["allow_new_buys"],
            "close_positions_pct": shield_status["close_positions_pct"],

            # Final
            "should_trade": should_trade,
            "regime_details": regime.details,
        }

        return context

    def record_trade(self, strategy: str, pnl: float):
        """Enregistre un résultat de trade pour l'adaptation."""
        self.strategy_manager.record_trade_result(strategy, pnl)

    async def close(self):
        await self.whale_watcher.close()
        await self.event_radar.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Test standalone
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def test():
    """Test de tous les modules d'intelligence."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s │ %(levelname)-7s │ %(message)s")

    print("""
    ╔══════════════════════════════════════════════════════════════╗
    ║  ⚡ NEXUS TRADER — Test Intelligence Contextuelle           ║
    ╚══════════════════════════════════════════════════════════════╝
    """)

    # Simule des bougies
    import random
    candles = []
    price = 65000
    for i in range(100):
        change = (random.random() - 0.48) * 0.02
        o = price
        c = price * (1 + change)
        h = max(o, c) * 1.003
        l = min(o, c) * 0.997
        candles.append({"open": o, "high": h, "low": l, "close": c, "volume": random.random() * 200})
        price = c

    intel = ContextualIntelligence()

    print("\n🔍 Analyse en cours...\n")
    ctx = await intel.analyze(candles, price, daily_pnl_pct=-1.5)

    print(f"{'='*60}")
    print(f"📊 RAPPORT D'INTELLIGENCE CONTEXTUELLE")
    print(f"{'='*60}")
    print(f"  Prix:               ${ctx['price']:,.0f}")
    print(f"  Régime:             {ctx['regime']} (confiance: {ctx['regime_confidence']:.0%})")
    print(f"  Tendance:           {ctx['trend_strength']:+.3f}")
    print(f"  Volatilité:         P{ctx['volatility_percentile']:.0f}")
    print(f"{'─'*60}")
    print(f"  Événements:         Risque {ctx['event_risk_level']}")
    if ctx['critical_events']:
        for e in ctx['critical_events']:
            print(f"    🚨 {e}")
    print(f"  Whales:             {ctx['whale_signal']} (alerte: {ctx['whale_alert_level']}/3)")
    print(f"{'─'*60}")
    if ctx['ai_recommendation']:
        print(f"  🧠 IA:              {ctx['ai_recommendation']} ({ctx['ai_confidence']:.0%})")
        print(f"  Raisonnement:       {ctx['ai_reasoning']}")
        print(f"  Vue contrarian:     {ctx['ai_contrarian']}")
    else:
        print(f"  🧠 IA:              Non disponible (ajoute ANTHROPIC_API_KEY)")
    print(f"{'─'*60}")
    print(f"  Stratégie:          {ctx['recommended_strategy']} ({ctx['strategy_action']})")
    print(f"  Exposition:         {ctx['recommended_exposure']:.0%}")
    print(f"  Shield:             {ctx['shield_level']}")
    if ctx['shield_reasons']:
        for r in ctx['shield_reasons']:
            print(f"    ⚡ {r}")
    print(f"{'─'*60}")
    print(f"  ✅ TRADER: {'OUI' if ctx['should_trade'] else '❌ NON'}")
    print(f"{'='*60}")

    await intel.close()
    print("\n✅ Test terminé\n")


if __name__ == "__main__":
    asyncio.run(test())
