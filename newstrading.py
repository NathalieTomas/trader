"""
+======================================================================+
|  NEXUS TRADER -- Module News Trading (MEV-style)                     |
|  Trade les annonces macro AVANT que le marche ne reagisse            |
|                                                                      |
|  Principe: L'info est publique, on est juste plus RAPIDE a la       |
|  traiter. On parse le communique avec l'IA, on extrait le chiffre   |
|  cle, on compare au consensus, et on execute en secondes.           |
|                                                                      |
|  Sources:                                                            |
|  - Calendrier eco (investing.com / forexfactory / tradingeconomics) |
|  - Communiques Fed/BCE (feeds RSS)                                   |
|  - Earnings reports (SEC EDGAR / Yahoo Finance)                      |
+======================================================================+

INSTALLATION:
    pip install aiohttp anthropic feedparser

CONFIGURATION .env:
    ANTHROPIC_API_KEY=sk-ant-...
    NEWS_TRADING_ENABLED=true
    NEWS_TRADING_AGGRESSIVENESS=medium    # low, medium, high
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

try:
    import aiohttp
except ImportError:
    aiohttp = None

try:
    from anthropic import AsyncAnthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

log = logging.getLogger("nexus.newstrading")


# =====================================================================
# Data Models
# =====================================================================

class EventImpact(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"    # Fed rate decision, CPI, NFP


class EventStatus(str, Enum):
    UPCOMING = "upcoming"
    LIVE = "live"            # En cours de publication
    PROCESSED = "processed"  # Deja traite
    MISSED = "missed"        # Rate (trop tard)


@dataclass
class EconomicEvent:
    """Un evenement economique programme."""
    id: str
    name: str                    # "US CPI YoY", "Fed Rate Decision"
    datetime_utc: str            # ISO format
    impact: EventImpact
    currency: str                # "USD", "EUR", etc.
    previous: Optional[str] = None     # Valeur precedente
    forecast: Optional[str] = None     # Consensus des analystes
    actual: Optional[str] = None       # Valeur reelle (rempli apres publication)
    status: EventStatus = EventStatus.UPCOMING
    crypto_correlation: float = 0.0    # -1 a +1 (comment ca impacte BTC)


@dataclass
class NewsSignal:
    """Signal genere par une annonce."""
    action: str              # BUY, SELL, HOLD
    confidence: float        # 0 a 1
    reason: str
    event_name: str
    surprise_pct: float      # Ecart vs consensus en %
    urgency: str             # "immediate", "short_term", "medium_term"
    recommended_size_mult: float = 1.0  # Multiplicateur de taille de position
    ttl_seconds: int = 300   # Duree de validite du signal (5 min par defaut)
    timestamp: float = 0.0


# =====================================================================
# Calendrier Economique
# =====================================================================

# Evenements macro qui impactent le plus la crypto
# crypto_correlation: positif = bonne nouvelle pour crypto, negatif = mauvaise
MACRO_EVENTS_TEMPLATE = [
    {
        "name": "US CPI YoY",
        "impact": EventImpact.CRITICAL,
        "currency": "USD",
        "crypto_correlation": -0.7,  # Inflation haute = Fed hawkish = crypto baisse
        "description": "Inflation US - chiffre au-dessus du forecast = baissier crypto",
    },
    {
        "name": "US CPI MoM",
        "impact": EventImpact.HIGH,
        "currency": "USD",
        "crypto_correlation": -0.6,
    },
    {
        "name": "Fed Interest Rate Decision",
        "impact": EventImpact.CRITICAL,
        "currency": "USD",
        "crypto_correlation": -0.8,  # Hausse taux = baissier crypto
        "description": "Decision taux Fed - hausse = bearish, baisse = bullish, maintien = depends du ton",
    },
    {
        "name": "US Non-Farm Payrolls",
        "impact": EventImpact.CRITICAL,
        "currency": "USD",
        "crypto_correlation": -0.5,  # Emploi fort = Fed hawkish = crypto baisse
        "description": "Emploi US - chiffre fort = hawkish = baissier crypto",
    },
    {
        "name": "US GDP QoQ",
        "impact": EventImpact.HIGH,
        "currency": "USD",
        "crypto_correlation": -0.3,
    },
    {
        "name": "US Unemployment Rate",
        "impact": EventImpact.HIGH,
        "currency": "USD",
        "crypto_correlation": 0.4,   # Chomage haut = Fed dovish = crypto monte
    },
    {
        "name": "US PPI MoM",
        "impact": EventImpact.MEDIUM,
        "currency": "USD",
        "crypto_correlation": -0.5,
    },
    {
        "name": "ECB Interest Rate Decision",
        "impact": EventImpact.HIGH,
        "currency": "EUR",
        "crypto_correlation": -0.4,
    },
    {
        "name": "US Initial Jobless Claims",
        "impact": EventImpact.MEDIUM,
        "currency": "USD",
        "crypto_correlation": 0.3,
    },
    {
        "name": "US Retail Sales MoM",
        "impact": EventImpact.MEDIUM,
        "currency": "USD",
        "crypto_correlation": -0.3,
    },
    {
        "name": "US ISM Manufacturing PMI",
        "impact": EventImpact.MEDIUM,
        "currency": "USD",
        "crypto_correlation": -0.3,
    },
    {
        "name": "FOMC Minutes",
        "impact": EventImpact.HIGH,
        "currency": "USD",
        "crypto_correlation": -0.5,
        "description": "Minutes de la Fed - ton hawkish = baissier, ton dovish = haussier",
    },
    {
        "name": "US Core PCE Price Index MoM",
        "impact": EventImpact.CRITICAL,
        "currency": "USD",
        "crypto_correlation": -0.7,
        "description": "Indicateur d'inflation prefere de la Fed",
    },
    {
        "name": "EIA Crude Oil Inventories",
        "impact": EventImpact.MEDIUM,
        "currency": "USD",
        "crypto_correlation": 0.2,
    },
]


class EconomicCalendar:
    """Recupere le calendrier economique depuis des sources publiques."""

    # API gratuite: Trading Economics calendar (limitee) ou scraping
    FOREX_FACTORY_RSS = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    TRADING_ECONOMICS_CAL = "https://api.tradingeconomics.com/calendar"

    def __init__(self):
        self.events: list[EconomicEvent] = []
        self._last_fetch = 0
        self._fetch_interval = 3600  # Refresh toutes les heures

    async def fetch_events(self) -> list[EconomicEvent]:
        """Recupere les evenements de la semaine."""
        now = time.time()
        if now - self._last_fetch < self._fetch_interval and self.events:
            return self.events

        events = []

        # Source 1: ForexFactory JSON (gratuit, fiable)
        try:
            events = await self._fetch_forexfactory()
        except Exception as e:
            log.warning(f"ForexFactory fetch failed: {e}")

        # Fallback: evenements statiques connus
        if not events:
            log.info("Using static event templates as fallback")
            events = self._generate_static_events()

        self.events = events
        self._last_fetch = now
        log.info(f"Calendrier eco: {len(events)} evenements charges ({self._count_high_impact(events)} high/critical)")
        return events

    async def _fetch_forexfactory(self) -> list[EconomicEvent]:
        """Fetch depuis ForexFactory JSON feed."""
        if not aiohttp:
            return []

        events = []
        async with aiohttp.ClientSession() as session:
            async with session.get(
                self.FOREX_FACTORY_RSS,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()

        for item in data:
            title = item.get("title", "")
            impact_str = item.get("impact", "").lower()
            country = item.get("country", "")
            date_str = item.get("date", "")
            forecast = item.get("forecast", "")
            previous = item.get("previous", "")

            # Filtre: seulement USD et EUR, impact medium+
            if country not in ("USD", "EUR", "GBP"):
                continue

            impact = EventImpact.LOW
            if impact_str == "high" or impact_str == "holiday":
                impact = EventImpact.HIGH
            elif impact_str == "medium":
                impact = EventImpact.MEDIUM

            # Check si c'est un evenement critique
            for template in MACRO_EVENTS_TEMPLATE:
                if template["name"].lower() in title.lower():
                    impact = template["impact"]
                    break

            if impact in (EventImpact.LOW,):
                continue  # Skip low impact

            # Determine la correlation crypto
            crypto_corr = 0.0
            for template in MACRO_EVENTS_TEMPLATE:
                if template["name"].lower() in title.lower():
                    crypto_corr = template["crypto_correlation"]
                    break

            event = EconomicEvent(
                id=f"ff_{hash(title + date_str) % 10**8}",
                name=title,
                datetime_utc=date_str,
                impact=impact,
                currency=country,
                previous=previous if previous else None,
                forecast=forecast if forecast else None,
                crypto_correlation=crypto_corr,
            )
            events.append(event)

        return events

    def _generate_static_events(self) -> list[EconomicEvent]:
        """Genere des events statiques pour fallback."""
        events = []
        now = datetime.now(timezone.utc)
        for i, template in enumerate(MACRO_EVENTS_TEMPLATE):
            event = EconomicEvent(
                id=f"static_{i}",
                name=template["name"],
                datetime_utc=(now + timedelta(hours=i * 24)).isoformat(),
                impact=template["impact"],
                currency=template["currency"],
                crypto_correlation=template.get("crypto_correlation", 0.0),
            )
            events.append(event)
        return events

    def _count_high_impact(self, events: list[EconomicEvent]) -> int:
        return sum(1 for e in events if e.impact in (EventImpact.HIGH, EventImpact.CRITICAL))

    def get_upcoming_events(self, hours_ahead: int = 24) -> list[EconomicEvent]:
        """Retourne les evenements dans les X prochaines heures."""
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=hours_ahead)
        upcoming = []
        for event in self.events:
            try:
                event_dt = datetime.fromisoformat(event.datetime_utc.replace("Z", "+00:00"))
                if now <= event_dt <= cutoff and event.status == EventStatus.UPCOMING:
                    upcoming.append(event)
            except (ValueError, TypeError):
                continue
        return sorted(upcoming, key=lambda e: e.datetime_utc)

    def get_imminent_events(self, minutes_window: int = 5) -> list[EconomicEvent]:
        """Retourne les evenements dans les X prochaines minutes (prets a etre trades)."""
        now = datetime.now(timezone.utc)
        imminent = []
        for event in self.events:
            try:
                event_dt = datetime.fromisoformat(event.datetime_utc.replace("Z", "+00:00"))
                diff = (event_dt - now).total_seconds()
                # Fenetre: de -2 min (vient de tomber) a +X min
                if -120 <= diff <= minutes_window * 60 and event.status != EventStatus.PROCESSED:
                    imminent.append(event)
            except (ValueError, TypeError):
                continue
        return imminent


# =====================================================================
# Analyseur IA Rapide (le coeur du MEV)
# =====================================================================

class RapidNewsAnalyzer:
    """
    Analyse ultra-rapide des annonces avec Claude.
    L'objectif: parser un communique et generer un signal en <3 secondes.
    """

    SYSTEM_PROMPT = """Tu es un analyste macro-financier ultra-rapide specialise dans l'impact des annonces economiques sur les crypto-monnaies (BTC, ETH).

Ton role: analyser INSTANTANEMENT une annonce economique et determiner son impact sur le prix du Bitcoin.

REGLES:
1. Reponds UNIQUEMENT en JSON valide, pas de texte avant/apres
2. Sois DECISIF - pas de "neutre" sauf si vraiment aucun impact
3. La SURPRISE (ecart vs consensus) est le facteur #1
4. Plus la surprise est grande, plus la confiance doit etre haute
5. Pense en termes de politique monetaire: hawkish = baissier crypto, dovish = haussier crypto

FORMAT DE REPONSE (JSON strict):
{
    "action": "BUY" | "SELL" | "HOLD",
    "confidence": 0.0-1.0,
    "reasoning": "explication courte en 1 phrase",
    "surprise_direction": "above" | "below" | "inline",
    "surprise_magnitude": "small" | "medium" | "large" | "shock",
    "crypto_impact": "bullish" | "bearish" | "neutral",
    "urgency": "immediate" | "short_term" | "medium_term",
    "position_size_multiplier": 0.5-2.0
}"""

    def __init__(self):
        self.api_key = os.getenv("ANTHROPIC_API_KEY", "")
        self.client = None
        if HAS_ANTHROPIC and self.api_key:
            self.client = AsyncAnthropic(api_key=self.api_key)

    async def analyze_event(
        self,
        event: EconomicEvent,
        current_btc_price: float,
        recent_price_action: str = "",
    ) -> NewsSignal:
        """
        Analyse un evenement et retourne un signal de trading.
        Essaie d'abord Claude, fallback sur regles deterministes.
        """
        start = time.time()

        # Essaie Claude en premier
        if self.client:
            try:
                signal = await self._analyze_with_ai(event, current_btc_price, recent_price_action)
                elapsed = time.time() - start
                log.info(f"AI analysis in {elapsed:.1f}s: {event.name} -> {signal.action} (conf: {signal.confidence:.0%})")
                return signal
            except Exception as e:
                log.warning(f"AI analysis failed ({e}), falling back to rules")

        # Fallback: regles deterministes
        return self._analyze_with_rules(event)

    async def _analyze_with_ai(
        self,
        event: EconomicEvent,
        btc_price: float,
        price_action: str,
    ) -> NewsSignal:
        """Analyse avec Claude - rapide et decisif."""
        prompt = f"""ANNONCE ECONOMIQUE A ANALYSER:
- Evenement: {event.name}
- Devise: {event.currency}
- Impact attendu: {event.impact.value}
- Valeur precedente: {event.previous or 'N/A'}
- Consensus/Forecast: {event.forecast or 'N/A'}
- Valeur reelle: {event.actual or 'PAS ENCORE PUBLIEE'}

CONTEXTE MARCHE:
- Prix BTC actuel: ${btc_price:,.0f}
- Action de prix recente: {price_action or 'N/A'}

Analyse l'impact sur le Bitcoin et genere un signal de trading."""

        response = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",  # Haiku pour la vitesse
            max_tokens=300,
            system=self.SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text.strip()

        # Parse le JSON
        # Gere le cas ou Claude entoure de ```json ... ```
        if "```" in text:
            text = text.split("```json")[-1].split("```")[0].strip()

        data = json.loads(text)

        # Calcule la surprise en %
        surprise_pct = self._calc_surprise_pct(event)

        return NewsSignal(
            action=data.get("action", "HOLD"),
            confidence=float(data.get("confidence", 0.3)),
            reason=data.get("reasoning", f"{event.name} analyzed by AI"),
            event_name=event.name,
            surprise_pct=surprise_pct,
            urgency=data.get("urgency", "short_term"),
            recommended_size_mult=float(data.get("position_size_multiplier", 1.0)),
            ttl_seconds=300 if data.get("urgency") == "immediate" else 900,
            timestamp=time.time(),
        )

    def _analyze_with_rules(self, event: EconomicEvent) -> NewsSignal:
        """
        Analyse deterministe basee sur des regles.
        Utilise quand Claude n'est pas disponible.
        """
        surprise_pct = self._calc_surprise_pct(event)
        correlation = event.crypto_correlation

        # Pas de donnee actual = on ne peut pas analyser
        if event.actual is None:
            return NewsSignal(
                action="HOLD",
                confidence=0.0,
                reason=f"{event.name}: en attente de publication",
                event_name=event.name,
                surprise_pct=0.0,
                urgency="medium_term",
                timestamp=time.time(),
            )

        # Determine la direction
        # correlation negative: chiffre au-dessus du forecast = baissier crypto
        # correlation positive: chiffre au-dessus du forecast = haussier crypto
        if abs(surprise_pct) < 0.5:
            # Pas de surprise significative
            return NewsSignal(
                action="HOLD",
                confidence=0.2,
                reason=f"{event.name}: inline with expectations ({surprise_pct:+.1f}%)",
                event_name=event.name,
                surprise_pct=surprise_pct,
                urgency="medium_term",
                timestamp=time.time(),
            )

        # Direction basee sur la correlation
        if correlation < 0:
            # Correlation negative: surprise positive = bearish crypto
            action = "SELL" if surprise_pct > 0 else "BUY"
        else:
            # Correlation positive: surprise positive = bullish crypto
            action = "BUY" if surprise_pct > 0 else "SELL"

        # Confiance basee sur la magnitude de la surprise
        abs_surprise = abs(surprise_pct)
        if abs_surprise > 5:
            confidence = 0.9
            urgency = "immediate"
            size_mult = 1.5
        elif abs_surprise > 2:
            confidence = 0.7
            urgency = "immediate"
            size_mult = 1.2
        elif abs_surprise > 1:
            confidence = 0.5
            urgency = "short_term"
            size_mult = 1.0
        else:
            confidence = 0.3
            urgency = "short_term"
            size_mult = 0.8

        # Ajuste par l'impact de l'evenement
        impact_mult = {
            EventImpact.CRITICAL: 1.0,
            EventImpact.HIGH: 0.8,
            EventImpact.MEDIUM: 0.6,
            EventImpact.LOW: 0.3,
        }
        confidence *= impact_mult.get(event.impact, 0.5)
        confidence = min(confidence, 1.0)

        return NewsSignal(
            action=action,
            confidence=round(confidence, 3),
            reason=f"{event.name}: actual vs forecast = {surprise_pct:+.1f}% surprise ({action.lower()} crypto)",
            event_name=event.name,
            surprise_pct=surprise_pct,
            urgency=urgency,
            recommended_size_mult=size_mult,
            ttl_seconds=300 if urgency == "immediate" else 900,
            timestamp=time.time(),
        )

    def _calc_surprise_pct(self, event: EconomicEvent) -> float:
        """Calcule l'ecart en % entre actual et forecast."""
        if not event.actual or not event.forecast:
            return 0.0
        try:
            actual = float(event.actual.replace("%", "").replace(",", ".").strip())
            forecast = float(event.forecast.replace("%", "").replace(",", ".").strip())
            if forecast == 0:
                return 0.0
            return ((actual - forecast) / abs(forecast)) * 100
        except (ValueError, AttributeError):
            return 0.0


# =====================================================================
# Cross-Asset Price Fetcher (le "scanner" actif)
# =====================================================================

class CrossAssetFetcher:
    """
    Fetch en temps reel les prix des assets leaders (DXY, SPX, Gold, US10Y).
    Utilise des APIs gratuites pour avoir les donnees sans abonnement.

    Sources (gratuites, sans cle API):
    - Yahoo Finance (via query API): SPX, Gold, DXY
    - FRED (Federal Reserve): US10Y (avec leger delai)
    - Binance: PAXG/USDT comme proxy gold, stablecoins comme proxy DXY
    """

    # Symboles Yahoo Finance pour chaque asset
    YAHOO_SYMBOLS = {
        "DXY": "DX-Y.NYB",        # Dollar Index
        "SPX": "^GSPC",            # S&P 500
        "GOLD": "GC=F",            # Gold Futures
        "US10Y": "^TNX",           # 10-Year Treasury Yield
    }

    # Proxies crypto sur Binance (disponibles 24/7, pas de horaires de marche)
    BINANCE_PROXIES = {
        "GOLD": "PAXG/USDT",      # PAX Gold = proxy gold 24/7
        "SPX": None,               # Pas de bon proxy crypto pour SPX
        "DXY": None,               # On peut utiliser USDT/DAI spread comme indicateur
    }

    def __init__(self):
        self._last_fetch: dict[str, float] = {}
        self._fetch_interval = 15  # Fetch toutes les 15 secondes

    async def fetch_all(self, exchange=None) -> dict[str, float]:
        """
        Fetch tous les prix disponibles.
        Retourne {asset_name: price}.
        Essaie Yahoo d'abord, fallback sur Binance proxies.
        """
        prices = {}
        now = time.time()

        # Methode 1: Yahoo Finance (marches ouverts seulement)
        yahoo_prices = await self._fetch_yahoo()
        prices.update(yahoo_prices)

        # Methode 2: Binance proxies (24/7)
        if exchange:
            binance_prices = await self._fetch_binance_proxies(exchange)
            # N'override que si Yahoo n'a pas repondu
            for asset, price in binance_prices.items():
                if asset not in prices:
                    prices[asset] = price

        return prices

    async def _fetch_yahoo(self) -> dict[str, float]:
        """Fetch depuis Yahoo Finance API v8 (gratuit, pas de cle)."""
        if not aiohttp:
            return {}

        prices = {}
        symbols = list(self.YAHOO_SYMBOLS.values())
        symbols_str = ",".join(symbols)

        try:
            url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={symbols_str}&fields=regularMarketPrice,regularMarketChangePercent"
            headers = {"User-Agent": "Mozilla/5.0"}

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status != 200:
                        return {}
                    data = await resp.json()

            results = data.get("quoteResponse", {}).get("result", [])
            symbol_to_asset = {v: k for k, v in self.YAHOO_SYMBOLS.items()}

            for quote in results:
                symbol = quote.get("symbol", "")
                price = quote.get("regularMarketPrice", 0)
                if symbol in symbol_to_asset and price:
                    asset = symbol_to_asset[symbol]
                    prices[asset] = float(price)

        except Exception as e:
            log.debug(f"Yahoo fetch failed: {e}")

        return prices

    async def _fetch_binance_proxies(self, exchange) -> dict[str, float]:
        """Fetch les proxies cross-asset depuis Binance (24/7)."""
        prices = {}

        for asset, pair in self.BINANCE_PROXIES.items():
            if not pair:
                continue
            try:
                ticker = await exchange.fetch_ticker(pair)
                if ticker and ticker.get("last"):
                    prices[asset] = float(ticker["last"])
            except Exception:
                pass

        return prices


# =====================================================================
# Cross-Asset Correlation Monitor
# =====================================================================

class CrossAssetMonitor:
    """
    Monitore les correlations cross-asset pour detecter les lags.
    Quand le DXY (dollar) bouge, BTC reagit avec un lag de 30s-2min.
    Quand les taux US bougent, les altcoins reagissent encore plus lentement.
    """

    # Correlations connues et leur lag typique
    CORRELATIONS = {
        "DXY": {
            "crypto_correlation": -0.7,   # Dollar monte -> crypto baisse
            "typical_lag_seconds": 60,
            "threshold_move_pct": 0.2,    # Mouvement minimum pour trigger
        },
        "US10Y": {
            "crypto_correlation": -0.5,   # Taux montent -> crypto baisse
            "typical_lag_seconds": 120,
            "threshold_move_pct": 1.0,
        },
        "SPX": {
            "crypto_correlation": 0.6,    # S&P monte -> crypto monte (risk-on)
            "typical_lag_seconds": 30,
            "threshold_move_pct": 0.5,
        },
        "GOLD": {
            "crypto_correlation": 0.3,    # Or monte -> crypto suit (flight to safety)
            "typical_lag_seconds": 90,
            "threshold_move_pct": 0.5,
        },
    }

    def __init__(self):
        self._price_history: dict[str, list[dict]] = {}  # asset -> [{time, price}]
        self._last_signals: dict[str, float] = {}  # asset -> timestamp du dernier signal
        self.fetcher = CrossAssetFetcher()
        self._last_prices: dict[str, float] = {}    # Dernier prix connu par asset

    async def fetch_and_record(self, exchange=None):
        """Fetch les prix et les enregistre automatiquement."""
        prices = await self.fetcher.fetch_all(exchange)
        for asset, price in prices.items():
            old_price = self._last_prices.get(asset)
            self._last_prices[asset] = price
            self.record_price(asset, price)
            if old_price and abs((price - old_price) / old_price) > 0.001:
                log.debug(f"Cross-asset {asset}: ${price:.2f} ({((price - old_price) / old_price) * 100:+.3f}%)")
        return prices

    def get_dashboard_data(self) -> dict:
        """Donnees pour le dashboard."""
        data = {}
        for asset, config in self.CORRELATIONS.items():
            history = self._price_history.get(asset, [])
            current = self._last_prices.get(asset)
            change_pct = 0.0
            if len(history) >= 2:
                oldest = history[0]["price"]
                newest = history[-1]["price"]
                change_pct = ((newest - oldest) / oldest) * 100

            data[asset] = {
                "price": current,
                "change_pct": round(change_pct, 3),
                "correlation": config["crypto_correlation"],
                "lag_seconds": config["typical_lag_seconds"],
                "data_points": len(history),
            }
        return data

    def record_price(self, asset: str, price: float):
        """Enregistre un prix pour tracker les mouvements."""
        if asset not in self._price_history:
            self._price_history[asset] = []
        self._price_history[asset].append({
            "time": time.time(),
            "price": price,
        })
        # Garde seulement les 30 dernieres minutes
        cutoff = time.time() - 1800
        self._price_history[asset] = [
            p for p in self._price_history[asset] if p["time"] > cutoff
        ]

    def check_for_signals(self) -> list[NewsSignal]:
        """
        Verifie si un asset leader a bouge significativement.
        Si oui, genere un signal pour trader le suiveur (crypto).
        """
        signals = []
        now = time.time()

        for asset, config in self.CORRELATIONS.items():
            if asset not in self._price_history:
                continue

            history = self._price_history[asset]
            if len(history) < 2:
                continue

            # Cooldown: pas plus d'un signal par asset toutes les 5 min
            last_signal_time = self._last_signals.get(asset, 0)
            if now - last_signal_time < 300:
                continue

            # Calcule le mouvement sur les dernieres N secondes
            lag = config["typical_lag_seconds"]
            recent = [p for p in history if now - p["time"] <= lag * 2]
            if len(recent) < 2:
                continue

            oldest_price = recent[0]["price"]
            latest_price = recent[-1]["price"]
            move_pct = ((latest_price - oldest_price) / oldest_price) * 100

            if abs(move_pct) < config["threshold_move_pct"]:
                continue

            # Mouvement significatif detecte!
            correlation = config["crypto_correlation"]
            if correlation < 0:
                # Correlation negative: asset monte -> crypto baisse
                action = "SELL" if move_pct > 0 else "BUY"
            else:
                # Correlation positive: asset monte -> crypto monte
                action = "BUY" if move_pct > 0 else "SELL"

            confidence = min(abs(move_pct) / (config["threshold_move_pct"] * 3), 0.8)

            signal = NewsSignal(
                action=action,
                confidence=round(confidence, 3),
                reason=f"{asset} moved {move_pct:+.2f}% -> expected crypto {action.lower()} (lag: {lag}s)",
                event_name=f"cross_asset_{asset}",
                surprise_pct=move_pct,
                urgency="immediate",
                recommended_size_mult=0.8,  # Taille reduite car correlation imparfaite
                ttl_seconds=lag * 2,        # Signal expire apres 2x le lag
                timestamp=now,
            )
            signals.append(signal)
            self._last_signals[asset] = now
            log.info(f"Cross-asset signal: {asset} {move_pct:+.2f}% -> {action} crypto (conf: {confidence:.0%})")

        return signals


# =====================================================================
# News Trading Engine (orchestre tout)
# =====================================================================

class NewsTradingEngine:
    """
    Moteur principal du news trading.
    S'integre dans la boucle _tick() du bot principal.
    """

    def __init__(self):
        self.calendar = EconomicCalendar()
        self.analyzer = RapidNewsAnalyzer()
        self.cross_asset = CrossAssetMonitor()
        self._active_signals: list[NewsSignal] = []
        self._processed_events: set[str] = set()

        # Config
        aggressiveness = os.getenv("NEWS_TRADING_AGGRESSIVENESS", "medium")
        self.min_confidence = {
            "low": 0.6,
            "medium": 0.4,
            "high": 0.25,
        }.get(aggressiveness, 0.4)

        self.enabled = os.getenv("NEWS_TRADING_ENABLED", "true").lower() == "true"

    async def initialize(self):
        """Charge le calendrier au demarrage."""
        if not self.enabled:
            log.info("News trading disabled")
            return
        await self.calendar.fetch_events()
        upcoming = self.calendar.get_upcoming_events(hours_ahead=24)
        if upcoming:
            log.info(f"Prochains evenements (24h):")
            for event in upcoming[:5]:
                log.info(f"  {event.impact.value.upper():>8} | {event.datetime_utc[:16]} | {event.name}")

    async def check_and_signal(self, btc_price: float, recent_candles: list[dict] = None, exchange=None) -> Optional[NewsSignal]:
        """
        Verifie les evenements imminents et retourne un signal si necessaire.
        Appele a chaque tick du bot.
        """
        if not self.enabled:
            return None

        # Nettoie les signaux expires
        now = time.time()
        self._active_signals = [
            s for s in self._active_signals
            if now - s.timestamp < s.ttl_seconds
        ]

        # Si on a deja un signal actif, le retourner
        if self._active_signals:
            best = max(self._active_signals, key=lambda s: s.confidence)
            if best.confidence >= self.min_confidence:
                return best

        # Refresh calendrier periodiquement
        await self.calendar.fetch_events()

        # Check evenements imminents
        imminent = self.calendar.get_imminent_events(minutes_window=5)
        for event in imminent:
            if event.id in self._processed_events:
                continue

            # Construit le contexte de prix recent
            price_action = ""
            if recent_candles and len(recent_candles) >= 5:
                last5 = recent_candles[-5:]
                change = ((last5[-1]["close"] - last5[0]["open"]) / last5[0]["open"]) * 100
                price_action = f"BTC {change:+.2f}% sur les 5 dernieres bougies"

            signal = await self.analyzer.analyze_event(event, btc_price, price_action)

            if signal.confidence >= self.min_confidence and signal.action != "HOLD":
                self._active_signals.append(signal)
                self._processed_events.add(event.id)
                event.status = EventStatus.PROCESSED
                log.info(f"NEWS SIGNAL: {event.name} -> {signal.action} (conf: {signal.confidence:.0%}, surprise: {signal.surprise_pct:+.1f}%)")
                return signal

            self._processed_events.add(event.id)
            event.status = EventStatus.PROCESSED

        # Fetch cross-asset prices (actif - pas besoin d'appel externe)
        await self.cross_asset.fetch_and_record(exchange)

        # Check cross-asset signals
        cross_signals = self.cross_asset.check_for_signals()
        for signal in cross_signals:
            if signal.confidence >= self.min_confidence and signal.action != "HOLD":
                self._active_signals.append(signal)
                return signal

        return None

    def record_cross_asset_price(self, asset: str, price: float):
        """Enregistre un prix cross-asset (a appeler manuellement si besoin)."""
        self.cross_asset.record_price(asset, price)

    def get_upcoming_events_summary(self) -> list[dict]:
        """Resume des evenements a venir (pour le dashboard)."""
        upcoming = self.calendar.get_upcoming_events(hours_ahead=48)
        return [
            {
                "name": e.name,
                "datetime": e.datetime_utc,
                "impact": e.impact.value,
                "currency": e.currency,
                "forecast": e.forecast,
                "previous": e.previous,
                "crypto_correlation": e.crypto_correlation,
                "status": e.status.value,
            }
            for e in upcoming
        ]

    def get_active_signals(self) -> list[dict]:
        """Signaux actifs (pour le dashboard)."""
        return [
            {
                "action": s.action,
                "confidence": s.confidence,
                "reason": s.reason,
                "event": s.event_name,
                "surprise_pct": s.surprise_pct,
                "urgency": s.urgency,
                "age_seconds": round(time.time() - s.timestamp),
                "ttl_seconds": s.ttl_seconds,
            }
            for s in self._active_signals
        ]

    def get_cross_asset_data(self) -> dict:
        """Donnees cross-asset pour le dashboard."""
        return self.cross_asset.get_dashboard_data()
