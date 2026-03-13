"""
╔══════════════════════════════════════════════════════════════════════╗
║  NEXUS TRADER — Module Sentiment Analysis                          ║
║  Analyse l'actualité crypto et le sentiment du marché              ║
║  Sources: CryptoCompare, Alternative.me, Claude AI                 ║
╚══════════════════════════════════════════════════════════════════════╝

INSTALLATION SUPPLÉMENTAIRE:
    pip install aiohttp anthropic

USAGE:
    Ce module s'intègre au bot principal (bot.py).
    Importe-le et ajoute-le comme signal supplémentaire.

CONFIGURATION .env:
    ANTHROPIC_API_KEY=sk-ant-...       (optionnel, pour l'analyse IA)
    SENTIMENT_ENABLED=true
    SENTIMENT_WEIGHT=0.3               (poids du sentiment dans le score final)
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

try:
    import aiohttp
except ImportError:
    print("❌ aiohttp non installé. Lance: pip install aiohttp")

try:
    from anthropic import AsyncAnthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

log = logging.getLogger("nexus.sentiment")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Data Models
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class NewsItem:
    title: str
    source: str
    url: str
    published_at: str
    body: str = ""
    sentiment_score: float = 0.0      # -1.0 (très négatif) à +1.0 (très positif)
    relevance: float = 0.0            # 0 à 1

@dataclass
class SentimentReport:
    """Rapport de sentiment agrégé."""
    timestamp: str
    overall_score: float              # -1.0 à +1.0
    fear_greed_index: int             # 0-100
    fear_greed_label: str             # "Extreme Fear", "Fear", "Neutral", "Greed", "Extreme Greed"
    news_sentiment: float             # -1.0 à +1.0
    ai_analysis: str                  # Résumé IA
    news_count: int
    top_headlines: list[str] = field(default_factory=list)
    signal: str = "NEUTRAL"           # BULLISH, BEARISH, NEUTRAL
    confidence: float = 0.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# News Fetcher — Sources multiples
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class NewsFetcher:
    """Récupère les news crypto depuis plusieurs sources gratuites."""

    # APIs gratuites (pas de clé nécessaire)
    SOURCES = {
        "cryptocompare": "https://min-api.cryptocompare.com/data/v2/news/?lang=EN&sortOrder=latest",
        "coingecko_trending": "https://api.coingecko.com/api/v3/search/trending",
    }

    FEAR_GREED_URL = "https://api.alternative.me/fng/?limit=1"

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: dict = {}
        self._cache_ttl = 300  # 5 minutes

    async def _get_session(self) -> aiohttp.ClientSession:
        if not self._session or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=15)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def fetch_news(self) -> list[NewsItem]:
        """Récupère les dernières news crypto."""
        cache_key = "news"
        if cache_key in self._cache:
            cached_time, cached_data = self._cache[cache_key]
            if time.time() - cached_time < self._cache_ttl:
                return cached_data

        news = []
        session = await self._get_session()

        # ── CryptoCompare News ──
        try:
            async with session.get(self.SOURCES["cryptocompare"]) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for item in data.get("Data", [])[:20]:
                        news.append(NewsItem(
                            title=item.get("title", ""),
                            source=item.get("source", "CryptoCompare"),
                            url=item.get("url", ""),
                            published_at=datetime.fromtimestamp(
                                item.get("published_on", 0), tz=timezone.utc
                            ).isoformat(),
                            body=item.get("body", "")[:500],
                        ))
                    log.info(f"📰 {len(news)} news récupérées depuis CryptoCompare")
        except Exception as e:
            log.warning(f"⚠️ Erreur CryptoCompare news: {e}")

        self._cache[cache_key] = (time.time(), news)
        return news

    async def fetch_fear_greed(self) -> tuple[int, str]:
        """Récupère le Fear & Greed Index (0=peur extrême, 100=avidité extrême)."""
        cache_key = "fear_greed"
        if cache_key in self._cache:
            cached_time, cached_data = self._cache[cache_key]
            if time.time() - cached_time < self._cache_ttl:
                return cached_data

        session = await self._get_session()
        try:
            async with session.get(self.FEAR_GREED_URL) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    fng = data.get("data", [{}])[0]
                    value = int(fng.get("value", 50))
                    label = fng.get("value_classification", "Neutral")
                    log.info(f"😱 Fear & Greed Index: {value} ({label})")
                    result = (value, label)
                    self._cache[cache_key] = (time.time(), result)
                    return result
        except Exception as e:
            log.warning(f"⚠️ Erreur Fear & Greed: {e}")

        return (50, "Neutral")

    async def fetch_trending(self) -> list[str]:
        """Récupère les cryptos tendance sur CoinGecko."""
        session = await self._get_session()
        try:
            async with session.get(self.SOURCES["coingecko_trending"]) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    coins = data.get("coins", [])
                    return [c["item"]["name"] for c in coins[:5]]
        except Exception as e:
            log.warning(f"⚠️ Erreur trending: {e}")
        return []

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Keyword-Based Sentiment Scorer (sans IA)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class KeywordSentimentScorer:
    """
    Analyse de sentiment basique par mots-clés.
    Utilisé comme fallback quand l'API Claude n'est pas disponible.
    """

    BULLISH_KEYWORDS = {
        # Fort bullish (+0.8 à +1.0)
        "soars": 0.9, "surge": 0.9, "skyrocket": 1.0, "moon": 0.8,
        "breakout": 0.8, "all-time high": 1.0, "ath": 0.9, "pump": 0.7,
        "rally": 0.8, "bull run": 0.9, "bullish": 0.8, "adoption": 0.7,
        "institutional": 0.6, "etf approved": 0.9, "partnership": 0.6,
        "upgrade": 0.5, "halving": 0.6,
        # Modéré bullish (+0.3 à +0.6)
        "growth": 0.5, "gain": 0.5, "positive": 0.4, "up": 0.3,
        "recovery": 0.5, "bounce": 0.4, "accumulate": 0.5, "buy": 0.3,
        "support": 0.3, "strong": 0.4, "confidence": 0.4,
    }

    BEARISH_KEYWORDS = {
        # Fort bearish (-0.8 à -1.0)
        "crash": -0.9, "plunge": -0.9, "collapse": -1.0, "dump": -0.8,
        "bear market": -0.9, "bearish": -0.8, "hack": -0.9, "scam": -0.8,
        "fraud": -0.9, "ban": -0.8, "regulate": -0.5, "sec lawsuit": -0.7,
        "bankruptcy": -1.0, "insolvent": -0.9, "liquidation": -0.7,
        # Modéré bearish (-0.3 à -0.6)
        "decline": -0.5, "drop": -0.5, "fall": -0.4, "down": -0.3,
        "fear": -0.5, "sell": -0.4, "risk": -0.3, "concern": -0.4,
        "warning": -0.5, "volatile": -0.3, "uncertainty": -0.4,
    }

    def score_text(self, text: str) -> float:
        """Score un texte de -1.0 à +1.0."""
        text_lower = text.lower()
        scores = []

        for keyword, score in self.BULLISH_KEYWORDS.items():
            if keyword in text_lower:
                scores.append(score)

        for keyword, score in self.BEARISH_KEYWORDS.items():
            if keyword in text_lower:
                scores.append(score)

        if not scores:
            return 0.0

        # Moyenne pondérée (les scores extrêmes pèsent plus)
        weighted = sum(s * abs(s) for s in scores) / sum(abs(s) for s in scores)
        return max(-1.0, min(1.0, weighted))

    def score_news_batch(self, news_items: list[NewsItem]) -> float:
        """Score un ensemble de news."""
        if not news_items:
            return 0.0

        scores = []
        for item in news_items:
            text = f"{item.title} {item.body}"
            score = self.score_text(text)
            item.sentiment_score = score
            scores.append(score)

        # Les news récentes comptent plus
        weighted_scores = []
        for i, score in enumerate(scores):
            weight = 1.0 / (1 + i * 0.1)  # décroissance progressive
            weighted_scores.append(score * weight)

        return sum(weighted_scores) / len(weighted_scores)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# AI-Powered Sentiment Analyzer (Claude API)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class AISentimentAnalyzer:
    """
    Utilise Claude pour analyser les news crypto en profondeur.
    Comprend le contexte, les nuances, et les implications.
    """

    SYSTEM_PROMPT = """Tu es un analyste crypto senior spécialisé dans le sentiment de marché.
Tu reçois des titres d'actualité crypto récents et tu dois fournir une analyse structurée.

Réponds UNIQUEMENT en JSON valide avec ce format exact:
{
    "overall_sentiment": <float entre -1.0 et 1.0>,
    "signal": "<BULLISH|BEARISH|NEUTRAL>",
    "confidence": <float entre 0.0 et 1.0>,
    "key_factors": ["facteur1", "facteur2", "facteur3"],
    "risk_level": "<LOW|MEDIUM|HIGH|EXTREME>",
    "summary": "<résumé en 2-3 phrases de l'état du marché>",
    "recommendation": "<conseil bref pour un trader algorithmique>"
}

Règles:
- Sois objectif et analytique, pas émotionnel
- Un seul tweet ou news ne fait pas une tendance
- Considère le contexte macro (taux, régulation, adoption)
- La confiance doit refléter la clarté des signaux (signaux mixtes = confiance basse)
- Ne sois pas excessivement bullish ou bearish sans raison forte"""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
        self.client: Optional[AsyncAnthropic] = None
        self._cache: dict = {}
        self._cache_ttl = 600  # 10 minutes (on n'a pas besoin d'analyser chaque tick)

        if self.api_key and HAS_ANTHROPIC:
            self.client = AsyncAnthropic(api_key=self.api_key)
            log.info("🧠 Analyse IA activée (Claude API)")
        else:
            log.info("📊 Analyse IA désactivée — utilisation du scorer par mots-clés")

    async def analyze(self, news_items: list[NewsItem], fear_greed: tuple[int, str]) -> Optional[dict]:
        """Analyse les news avec Claude."""
        if not self.client:
            return None

        # Check cache
        cache_key = hash(tuple(n.title for n in news_items[:10]))
        if cache_key in self._cache:
            cached_time, cached_data = self._cache[cache_key]
            if time.time() - cached_time < self._cache_ttl:
                return cached_data

        # Prépare le prompt
        headlines = "\n".join(
            f"- [{n.source}] {n.title}" for n in news_items[:15]
        )

        prompt = f"""Voici les dernières news crypto (les plus récentes en premier):

{headlines}

Fear & Greed Index actuel: {fear_greed[0]}/100 ({fear_greed[1]})

Analyse le sentiment global du marché crypto basé sur ces données."""

        try:
            response = await self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=500,
                system=self.SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )

            text = response.content[0].text.strip()
            # Nettoie le JSON si nécessaire
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
                text = text.strip()

            result = json.loads(text)
            self._cache[cache_key] = (time.time(), result)
            log.info(f"🧠 Analyse IA: {result.get('signal', '?')} (confiance: {result.get('confidence', 0):.0%})")
            return result

        except json.JSONDecodeError as e:
            log.warning(f"⚠️ Erreur parsing réponse IA: {e}")
            return None
        except Exception as e:
            log.warning(f"⚠️ Erreur API Claude: {e}")
            return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Sentiment Engine — Orchestrateur principal
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SentimentEngine:
    """
    Moteur principal de sentiment. Agrège toutes les sources
    et produit un SentimentReport utilisable par la stratégie.
    """

    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}
        self.fetcher = NewsFetcher()
        self.keyword_scorer = KeywordSentimentScorer()
        self.ai_analyzer = AISentimentAnalyzer()
        self.sentiment_weight = float(os.getenv("SENTIMENT_WEIGHT", "0.3"))
        self._last_report: Optional[SentimentReport] = None
        self._report_interval = 300  # Met à jour le sentiment toutes les 5 min
        self._last_report_time = 0

    async def get_report(self, force: bool = False) -> SentimentReport:
        """
        Génère un rapport de sentiment complet.
        Utilise un cache pour éviter de spammer les APIs.
        """
        now = time.time()
        if not force and self._last_report and (now - self._last_report_time < self._report_interval):
            return self._last_report

        # Récupère les données en parallèle
        news, fear_greed, trending = await asyncio.gather(
            self.fetcher.fetch_news(),
            self.fetcher.fetch_fear_greed(),
            self.fetcher.fetch_trending(),
        )

        # Score par mots-clés (toujours disponible)
        keyword_sentiment = self.keyword_scorer.score_news_batch(news)

        # Analyse IA (si disponible)
        ai_result = await self.ai_analyzer.analyze(news, fear_greed)

        # Agrège les scores
        if ai_result:
            # Combine IA (60%) + mots-clés (20%) + Fear&Greed normalisé (20%)
            ai_score = ai_result.get("overall_sentiment", 0)
            fg_normalized = (fear_greed[0] - 50) / 50  # -1 à +1
            overall = ai_score * 0.6 + keyword_sentiment * 0.2 + fg_normalized * 0.2
            ai_analysis = ai_result.get("summary", "")
            signal = ai_result.get("signal", "NEUTRAL")
            confidence = ai_result.get("confidence", 0.5)
        else:
            # Sans IA: mots-clés (60%) + Fear&Greed (40%)
            fg_normalized = (fear_greed[0] - 50) / 50
            overall = keyword_sentiment * 0.6 + fg_normalized * 0.4
            ai_analysis = "Analyse IA non disponible — utilisation du scorer par mots-clés"

            # Détermine le signal
            if overall > 0.2:
                signal = "BULLISH"
                confidence = min(abs(overall), 1.0)
            elif overall < -0.2:
                signal = "BEARISH"
                confidence = min(abs(overall), 1.0)
            else:
                signal = "NEUTRAL"
                confidence = 0.3

        report = SentimentReport(
            timestamp=datetime.now(timezone.utc).isoformat(),
            overall_score=round(max(-1.0, min(1.0, overall)), 3),
            fear_greed_index=fear_greed[0],
            fear_greed_label=fear_greed[1],
            news_sentiment=round(keyword_sentiment, 3),
            ai_analysis=ai_analysis,
            news_count=len(news),
            top_headlines=[n.title for n in news[:5]],
            signal=signal,
            confidence=round(confidence, 3),
        )

        self._last_report = report
        self._last_report_time = now

        log.info(
            f"📊 Sentiment Report: {report.signal} (score: {report.overall_score:+.3f}, "
            f"confiance: {report.confidence:.0%}, F&G: {report.fear_greed_index})"
        )

        return report

    def get_strategy_modifier(self, report: SentimentReport) -> dict:
        """
        Convertit le rapport de sentiment en modificateur pour la stratégie.
        Retourne un dict avec les ajustements à appliquer.
        """
        modifier = {
            "score_adjustment": 0,      # Ajustement du score de la stratégie technique
            "confidence_boost": 0,       # Boost de confiance si sentiment confirme
            "should_block_buy": False,   # Bloque les achats si sentiment très négatif
            "should_block_sell": False,  # Bloque les ventes si sentiment très positif
            "position_size_mult": 1.0,   # Multiplicateur de taille de position
            "reason": "",
        }

        score = report.overall_score
        weight = self.sentiment_weight

        if report.signal == "BULLISH" and report.confidence > 0.5:
            modifier["score_adjustment"] = weight * report.confidence
            modifier["confidence_boost"] = 0.1
            modifier["position_size_mult"] = 1.0 + (report.confidence * 0.3)  # max +30%
            modifier["reason"] = f"Sentiment bullish ({score:+.2f})"

            # Si sentiment très bullish, empêche les ventes
            if score > 0.6 and report.confidence > 0.7:
                modifier["should_block_sell"] = True
                modifier["reason"] += " — ventes bloquées"

        elif report.signal == "BEARISH" and report.confidence > 0.5:
            modifier["score_adjustment"] = -weight * report.confidence
            modifier["confidence_boost"] = 0.1
            modifier["position_size_mult"] = max(0.5, 1.0 - (report.confidence * 0.3))  # min -30%
            modifier["reason"] = f"Sentiment bearish ({score:+.2f})"

            # Si sentiment très bearish, empêche les achats
            if score < -0.6 and report.confidence > 0.7:
                modifier["should_block_buy"] = True
                modifier["reason"] += " — achats bloqués"

        elif report.fear_greed_index < 20:
            # Peur extrême — historiquement un bon moment pour acheter
            modifier["score_adjustment"] = weight * 0.3
            modifier["reason"] = f"Extreme Fear ({report.fear_greed_index}) — potentiel contrarian buy"

        elif report.fear_greed_index > 80:
            # Avidité extrême — prudence
            modifier["score_adjustment"] = -weight * 0.3
            modifier["position_size_mult"] = 0.7
            modifier["reason"] = f"Extreme Greed ({report.fear_greed_index}) — réduction exposition"

        else:
            modifier["reason"] = f"Sentiment neutre ({score:+.2f})"

        return modifier

    async def close(self):
        await self.fetcher.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Intégration avec le bot principal
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SentimentEnhancedStrategy:
    """
    Wrapper qui ajoute le sentiment à n'importe quelle stratégie technique.
    
    Usage dans bot.py:
    
        from sentiment import SentimentEngine, SentimentEnhancedStrategy
        
        # Dans TradingBot.__init__:
        self.sentiment = SentimentEngine()
        
        # Dans TradingBot._tick, remplace l'appel à la stratégie:
        base_strategy = STRATEGIES[self.config.active_strategy]
        enhanced = SentimentEnhancedStrategy(base_strategy, self.sentiment)
        signal = await enhanced.evaluate_with_sentiment(self.candles, self.config)
    """

    def __init__(self, base_strategy, sentiment_engine: SentimentEngine):
        self.base_strategy = base_strategy
        self.sentiment = sentiment_engine
        self.name = f"{base_strategy.name}+sentiment"

    async def evaluate_with_sentiment(self, candles: list[dict], config) -> dict:
        """Évalue la stratégie technique + le sentiment."""

        # 1. Signal technique de base
        base_signal = self.base_strategy.evaluate(candles, config)

        # 2. Rapport de sentiment
        report = await self.sentiment.get_report()
        modifier = self.sentiment.get_strategy_modifier(report)

        # 3. Combine les deux
        action = base_signal.action
        confidence = base_signal.confidence
        reasons = [base_signal.reason]

        # Applique les blocages
        if action == "BUY" and modifier["should_block_buy"]:
            action = "HOLD"
            reasons.append(f"⛔ Achat bloqué: {modifier['reason']}")

        elif action == "SELL" and modifier["should_block_sell"]:
            action = "HOLD"
            reasons.append(f"⛔ Vente bloquée: {modifier['reason']}")

        else:
            # Boost ou réduit la confiance
            confidence = min(1.0, confidence + modifier["confidence_boost"])
            reasons.append(modifier["reason"])

        # Ajuste la taille de position recommandée
        position_size_mult = modifier["position_size_mult"]

        return {
            "action": action,
            "confidence": round(confidence, 3),
            "reason": " | ".join(reasons),
            "sentiment_report": {
                "signal": report.signal,
                "score": report.overall_score,
                "fear_greed": report.fear_greed_index,
                "headlines": report.top_headlines[:3],
            },
            "position_size_multiplier": position_size_mult,
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Trailing Stop-Loss Manager
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TrailingStopManager:
    """
    Gère les trailing stop-loss pour maximiser les gains.
    
    Au lieu d'un stop-loss fixe à -2%, le trailing stop suit le prix :
    - BTC acheté à 65,000$ avec trailing stop de 2%
    - Prix monte à 67,000$ → stop remonte à 65,660$ (67000 * 0.98)
    - Prix monte à 70,000$ → stop remonte à 68,600$ (70000 * 0.98)
    - Prix redescend à 68,600$ → VENTE déclenchée
    - Résultat: gain de 3,600$ au lieu de 1,300$ avec un TP fixe à +2%
    """

    def __init__(self, trail_pct: float = 2.0):
        self.trail_pct = trail_pct
        self.positions: dict[str, dict] = {}  # id -> {highest_price, stop_price}

    def register_position(self, position_id: str, entry_price: float):
        """Enregistre une nouvelle position."""
        stop = entry_price * (1 - self.trail_pct / 100)
        self.positions[position_id] = {
            "entry_price": entry_price,
            "highest_price": entry_price,
            "stop_price": round(stop, 2),
        }
        log.info(f"📍 Trailing stop enregistré: {position_id} @ ${entry_price:.2f} (stop: ${stop:.2f})")

    def update(self, position_id: str, current_price: float) -> dict:
        """
        Met à jour le trailing stop avec le prix actuel.
        Retourne {'triggered': bool, 'stop_price': float, 'highest': float}
        """
        if position_id not in self.positions:
            return {"triggered": False, "stop_price": 0, "highest": 0}

        pos = self.positions[position_id]

        # Met à jour le plus haut
        if current_price > pos["highest_price"]:
            pos["highest_price"] = current_price
            pos["stop_price"] = round(current_price * (1 - self.trail_pct / 100), 2)

        # Vérifie si le stop est déclenché
        triggered = current_price <= pos["stop_price"]

        if triggered:
            log.info(
                f"🔔 Trailing stop déclenché pour {position_id}: "
                f"prix ${current_price:.2f} ≤ stop ${pos['stop_price']:.2f} "
                f"(plus haut: ${pos['highest_price']:.2f})"
            )
            del self.positions[position_id]

        return {
            "triggered": triggered,
            "stop_price": pos["stop_price"] if not triggered else 0,
            "highest": pos["highest_price"] if not triggered else 0,
        }

    def remove(self, position_id: str):
        self.positions.pop(position_id, None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Dynamic Position Sizer
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class DynamicPositionSizer:
    """
    Ajuste la taille des positions en fonction de la volatilité (ATR).
    
    Volatilité haute → positions plus petites (moins de risque)
    Volatilité basse → positions plus grandes (plus d'opportunité)
    """

    def __init__(self, base_risk_pct: float = 1.0):
        self.base_risk_pct = base_risk_pct  # % du portfolio risqué par trade

    def calculate_size(
        self,
        portfolio_value: float,
        entry_price: float,
        stop_loss_price: float,
        atr: Optional[float] = None,
        sentiment_multiplier: float = 1.0,
    ) -> float:
        """
        Calcule la taille optimale de position.

        Méthode: Risk-based position sizing
        Position = (Portfolio * Risk%) / (Entry - StopLoss)
        """
        risk_per_trade = portfolio_value * self.base_risk_pct / 100
        price_risk = abs(entry_price - stop_loss_price)

        if price_risk == 0:
            return 0

        # Taille de base
        position_size = risk_per_trade / price_risk

        # Ajustement ATR (si disponible)
        if atr and atr > 0:
            # Normalise l'ATR par rapport au prix
            atr_pct = (atr / entry_price) * 100
            if atr_pct > 3:  # Haute volatilité
                position_size *= 0.6
            elif atr_pct > 2:
                position_size *= 0.8
            elif atr_pct < 1:  # Basse volatilité
                position_size *= 1.2

        # Ajustement sentiment
        position_size *= sentiment_multiplier

        # Limite max: jamais plus de 20% du portfolio
        max_size = (portfolio_value * 0.20) / entry_price
        position_size = min(position_size, max_size)

        return round(position_size, 6)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Test standalone
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def test():
    """Test le module de sentiment en standalone."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s │ %(levelname)-7s │ %(message)s")

    engine = SentimentEngine()

    print("\n⚡ Test du module Sentiment Analysis\n")
    print("=" * 60)

    report = await engine.get_report(force=True)

    print(f"\n📊 RAPPORT DE SENTIMENT")
    print(f"   Signal:        {report.signal}")
    print(f"   Score global:  {report.overall_score:+.3f}")
    print(f"   Confiance:     {report.confidence:.0%}")
    print(f"   Fear & Greed:  {report.fear_greed_index}/100 ({report.fear_greed_label})")
    print(f"   News analysées: {report.news_count}")
    print(f"\n   📰 Top headlines:")
    for h in report.top_headlines[:5]:
        print(f"      • {h}")
    print(f"\n   🧠 Analyse: {report.ai_analysis}")

    modifier = engine.get_strategy_modifier(report)
    print(f"\n   📈 Modificateur stratégie:")
    print(f"      Score adj:    {modifier['score_adjustment']:+.3f}")
    print(f"      Position mult: {modifier['position_size_mult']:.2f}x")
    print(f"      Block buy:    {modifier['should_block_buy']}")
    print(f"      Block sell:   {modifier['should_block_sell']}")
    print(f"      Raison:       {modifier['reason']}")

    await engine.close()
    print("\n✅ Test terminé")


if __name__ == "__main__":
    asyncio.run(test())
