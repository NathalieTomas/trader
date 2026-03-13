"""
╔══════════════════════════════════════════════════════════════════════╗
║  NEXUS TRADER — Backend Python pour trading crypto automatisé      ║
║  Exchange: Binance (adaptable à tout exchange via ccxt)            ║
║  API: FastAPI avec WebSocket pour le frontend React                ║
╚══════════════════════════════════════════════════════════════════════╝

INSTALLATION:
    pip install ccxt fastapi uvicorn websockets python-dotenv pydantic aiosqlite

LANCEMENT:
    # Mode paper trading (simulation avec données réelles)
    python bot.py --mode paper

    # Mode live (VRAI trading — attention !)
    python bot.py --mode live

CONFIGURATION:
    Crée un fichier .env à la racine :
        BINANCE_API_KEY=ta_cle_api
        BINANCE_API_SECRET=ton_secret
        TRADING_MODE=paper          # paper | live
        TRADING_PAIR=BTC/USDT
        BASE_CURRENCY=USDT
        INITIAL_BALANCE=10000       # pour paper trading
        LOG_LEVEL=INFO
"""

import asyncio
import json
import logging
import os
import sys
import time
import sqlite3
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from dataclasses import dataclass, field, asdict
from pathlib import Path

try:
    import ccxt.async_support as ccxt
except ImportError:
    print("❌ ccxt non installé. Lance: pip install ccxt")
    sys.exit(1)

try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.middleware.cors import CORSMiddleware
    import uvicorn
except ImportError:
    print("❌ FastAPI non installé. Lance: pip install fastapi uvicorn websockets")
    sys.exit(1)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # .env optionnel

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Configuration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TradingMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"

@dataclass
class BotConfig:
    # Exchange
    api_key: str = os.getenv("BINANCE_API_KEY", "")
    api_secret: str = os.getenv("BINANCE_API_SECRET", "")
    trading_mode: TradingMode = TradingMode(os.getenv("TRADING_MODE", "paper"))
    pair: str = os.getenv("TRADING_PAIR", "BTC/USDT")
    base_currency: str = os.getenv("BASE_CURRENCY", "USDT")
    
    # Strategy
    active_strategy: str = "combined"
    timeframe: str = "5m"         # 1m, 5m, 15m, 1h, 4h, 1d
    candle_limit: int = 200       # nombre de bougies chargées
    tick_interval: int = 10       # secondes entre chaque évaluation
    
    # Risk Management
    stop_loss_pct: float = 2.0
    take_profit_pct: float = 4.0
    position_size_pct: float = 10.0   # % du portfolio par trade
    max_open_positions: int = 3
    min_confidence: float = 0.4
    max_daily_loss_pct: float = 5.0   # perte max journalière → arrêt
    
    # Indicator params
    rsi_period: int = 14
    rsi_buy: int = 30
    rsi_sell: int = 70
    ema_fast: int = 9
    ema_slow: int = 21
    bb_period: int = 20
    
    # Paper trading
    initial_balance: float = float(os.getenv("INITIAL_BALANCE", "10000"))
    
    # System
    db_path: str = "nexus_trades.db"
    log_level: str = os.getenv("LOG_LEVEL", "INFO")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Logging
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def setup_logging(level: str = "INFO"):
    fmt = "%(asctime)s │ %(levelname)-7s │ %(message)s"
    logging.basicConfig(level=getattr(logging, level), format=fmt, datefmt="%H:%M:%S")
    return logging.getLogger("nexus")

log = setup_logging()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Database
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TradeDB:
    """SQLite pour persister tous les trades et l'état du portfolio."""
    
    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()
    
    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                pair TEXT NOT NULL,
                side TEXT NOT NULL,          -- BUY, SELL, STOP_LOSS, TAKE_PROFIT
                price REAL NOT NULL,
                amount REAL NOT NULL,
                cost REAL NOT NULL,
                strategy TEXT,
                reason TEXT,
                confidence REAL,
                pnl REAL DEFAULT 0,
                mode TEXT NOT NULL           -- paper, live
            );
            
            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                total_value_usdt REAL NOT NULL,
                balances TEXT NOT NULL        -- JSON
            );
            
            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        self.conn.commit()
    
    def log_trade(self, trade: dict):
        self.conn.execute("""
            INSERT INTO trades (timestamp, pair, side, price, amount, cost, strategy, reason, confidence, pnl, mode)
            VALUES (:timestamp, :pair, :side, :price, :amount, :cost, :strategy, :reason, :confidence, :pnl, :mode)
        """, trade)
        self.conn.commit()
    
    def log_snapshot(self, total_value: float, balances: dict):
        self.conn.execute(
            "INSERT INTO portfolio_snapshots (timestamp, total_value_usdt, balances) VALUES (?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), total_value, json.dumps(balances))
        )
        self.conn.commit()
    
    def get_trades(self, limit: int = 50) -> list:
        rows = self.conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    
    def get_daily_pnl(self) -> float:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = self.conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) as daily_pnl FROM trades WHERE timestamp LIKE ?",
            (f"{today}%",)
        ).fetchone()
        return row["daily_pnl"] if row else 0.0
    
    def close(self):
        self.conn.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Indicateurs Techniques
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class Indicators:
    """Calculs d'indicateurs techniques sur une liste de bougies OHLCV."""
    
    @staticmethod
    def sma(closes: list[float], period: int) -> Optional[float]:
        if len(closes) < period:
            return None
        return sum(closes[-period:]) / period
    
    @staticmethod
    def ema(closes: list[float], period: int) -> Optional[float]:
        if len(closes) < period:
            return None
        k = 2 / (period + 1)
        ema = sum(closes[:period]) / period
        for price in closes[period:]:
            ema = price * k + ema * (1 - k)
        return ema
    
    @staticmethod
    def rsi(closes: list[float], period: int = 14) -> float:
        if len(closes) < period + 1:
            return 50.0
        gains = losses = 0.0
        for i in range(len(closes) - period, len(closes)):
            diff = closes[i] - closes[i - 1]
            if diff > 0:
                gains += diff
            else:
                losses -= diff
        if losses == 0:
            return 100.0
        rs = gains / losses
        return round(100 - 100 / (1 + rs), 2)
    
    @staticmethod
    def macd(closes: list[float]) -> dict:
        ema12 = Indicators.ema(closes, 12)
        ema26 = Indicators.ema(closes, 26)
        if not ema12 or not ema26:
            return {"macd": 0, "signal": 0, "histogram": 0}
        macd_line = ema12 - ema26
        # Signal simplifié (en prod, calcule une vraie EMA9 du MACD)
        signal = macd_line * 0.15
        return {
            "macd": round(macd_line, 2),
            "signal": round(signal, 2),
            "histogram": round(macd_line - signal, 2),
        }
    
    @staticmethod
    def bollinger(closes: list[float], period: int = 20) -> Optional[dict]:
        if len(closes) < period:
            return None
        window = closes[-period:]
        sma = sum(window) / period
        variance = sum((x - sma) ** 2 for x in window) / period
        std = variance ** 0.5
        return {
            "upper": round(sma + 2 * std, 2),
            "middle": round(sma, 2),
            "lower": round(sma - 2 * std, 2),
        }
    
    @staticmethod
    def atr(candles: list[dict], period: int = 14) -> Optional[float]:
        """Average True Range — utile pour le sizing dynamique du stop-loss."""
        if len(candles) < period + 1:
            return None
        trs = []
        for i in range(1, len(candles)):
            c = candles[i]
            prev_close = candles[i - 1]["close"]
            tr = max(c["high"] - c["low"], abs(c["high"] - prev_close), abs(c["low"] - prev_close))
            trs.append(tr)
        return round(sum(trs[-period:]) / period, 2)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Stratégies
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class Signal:
    action: str     # BUY, SELL, HOLD
    confidence: float
    reason: str

class Strategy:
    """Classe de base pour les stratégies."""
    name: str = "base"
    description: str = ""
    
    def evaluate(self, candles: list[dict], config: BotConfig) -> Signal:
        raise NotImplementedError


class RSIReversal(Strategy):
    name = "rsi_reversal"
    description = "Achète quand RSI < seuil bas, vend quand RSI > seuil haut"
    
    def evaluate(self, candles, config):
        closes = [c["close"] for c in candles]
        rsi = Indicators.rsi(closes, config.rsi_period)
        
        if rsi < config.rsi_buy:
            return Signal("BUY", min((config.rsi_buy - rsi) / config.rsi_buy, 1.0), f"RSI oversold: {rsi}")
        if rsi > config.rsi_sell:
            return Signal("SELL", min((rsi - config.rsi_sell) / (100 - config.rsi_sell), 1.0), f"RSI overbought: {rsi}")
        return Signal("HOLD", 0, f"RSI neutral: {rsi}")


class MACrossover(Strategy):
    name = "ma_crossover"
    description = "Signal basé sur le croisement EMA rapide/lente"
    
    def evaluate(self, candles, config):
        closes = [c["close"] for c in candles]
        fast = Indicators.ema(closes, config.ema_fast)
        slow = Indicators.ema(closes, config.ema_slow)
        
        if not fast or not slow:
            return Signal("HOLD", 0, "Données insuffisantes")
        
        diff_pct = ((fast - slow) / slow) * 100
        
        if diff_pct > 0.1:
            return Signal("BUY", min(diff_pct / 2, 1.0), f"EMA bullish: {diff_pct:.3f}%")
        if diff_pct < -0.1:
            return Signal("SELL", min(abs(diff_pct) / 2, 1.0), f"EMA bearish: {diff_pct:.3f}%")
        return Signal("HOLD", 0, f"EMAs converging: {diff_pct:.3f}%")


class BollingerBounce(Strategy):
    name = "bollinger_bounce"
    description = "Achète sur bande basse, vend sur bande haute"
    
    def evaluate(self, candles, config):
        closes = [c["close"] for c in candles]
        bb = Indicators.bollinger(closes, config.bb_period)
        
        if not bb:
            return Signal("HOLD", 0, "Données insuffisantes")
        
        price = closes[-1]
        band_range = bb["upper"] - bb["lower"]
        
        if price <= bb["lower"]:
            return Signal("BUY", min((bb["lower"] - price) / band_range + 0.5, 1.0), f"Prix sous BB basse: {price:.0f}")
        if price >= bb["upper"]:
            return Signal("SELL", min((price - bb["upper"]) / band_range + 0.5, 1.0), f"Prix sur BB haute: {price:.0f}")
        return Signal("HOLD", 0, f"Prix dans les bandes: {price:.0f}")


class CombinedStrategy(Strategy):
    name = "combined"
    description = "Combine RSI + MACD + Bollinger pour confirmation multi-signal"
    
    def evaluate(self, candles, config):
        closes = [c["close"] for c in candles]
        price = closes[-1]
        
        rsi = Indicators.rsi(closes, config.rsi_period)
        macd = Indicators.macd(closes)
        bb = Indicators.bollinger(closes, config.bb_period)
        
        score = 0
        reasons = []
        
        # RSI
        if rsi < 35:
            score += 1
            reasons.append(f"RSI bas ({rsi})")
        elif rsi > 65:
            score -= 1
            reasons.append(f"RSI haut ({rsi})")
        
        # MACD
        if macd["histogram"] > 0:
            score += 1
            reasons.append("MACD positif")
        elif macd["histogram"] < 0:
            score -= 1
            reasons.append("MACD négatif")
        
        # Bollinger
        if bb:
            if price < bb["lower"]:
                score += 1
                reasons.append("Sous Bollinger")
            elif price > bb["upper"]:
                score -= 1
                reasons.append("Sur Bollinger")
        
        reason_str = ", ".join(reasons) or "Signaux mixtes"
        
        if score >= 2:
            return Signal("BUY", score / 3, reason_str)
        if score <= -2:
            return Signal("SELL", abs(score) / 3, reason_str)
        return Signal("HOLD", 0, reason_str)


# Registry
STRATEGIES = {
    "rsi_reversal": RSIReversal(),
    "ma_crossover": MACrossover(),
    "bollinger_bounce": BollingerBounce(),
    "combined": CombinedStrategy(),
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Exchange Manager
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ExchangeManager:
    """Gère la connexion à l'exchange et les ordres."""
    
    def __init__(self, config: BotConfig):
        self.config = config
        self.exchange: Optional[ccxt.binance] = None
        
        # Paper trading state
        self._paper_balance = {
            config.base_currency: config.initial_balance,
        }
    
    async def connect(self):
        """Initialise la connexion à Binance."""
        params = {
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        }
        
        if self.config.api_key and self.config.api_secret:
            params["apiKey"] = self.config.api_key
            params["secret"] = self.config.api_secret
        
        self.exchange = ccxt.binance(params)
        
        # Teste la connexion
        try:
            await self.exchange.load_markets()
            log.info(f"✅ Connecté à Binance — {len(self.exchange.markets)} paires disponibles")
            
            if self.config.trading_mode == TradingMode.LIVE:
                balance = await self.exchange.fetch_balance()
                log.info(f"💰 Balance réelle: {balance['total']}")
            else:
                log.info(f"📝 Mode Paper Trading — Balance simulée: ${self.config.initial_balance}")
        except Exception as e:
            log.error(f"❌ Erreur connexion: {e}")
            log.info("💡 Passage en mode paper trading avec données publiques")
            self.config.trading_mode = TradingMode.PAPER
    
    async def fetch_candles(self, limit: int = 200) -> list[dict]:
        """Récupère les bougies OHLCV depuis l'exchange."""
        try:
            ohlcv = await self.exchange.fetch_ohlcv(
                self.config.pair,
                timeframe=self.config.timeframe,
                limit=limit,
            )
            return [
                {
                    "time": candle[0],
                    "open": candle[1],
                    "high": candle[2],
                    "low": candle[3],
                    "close": candle[4],
                    "volume": candle[5],
                }
                for candle in ohlcv
            ]
        except Exception as e:
            log.error(f"Erreur fetch candles: {e}")
            return []
    
    async def get_ticker_price(self) -> float:
        """Prix actuel."""
        try:
            ticker = await self.exchange.fetch_ticker(self.config.pair)
            return ticker["last"]
        except Exception as e:
            log.error(f"Erreur ticker: {e}")
            return 0.0
    
    async def get_balance(self) -> dict:
        """Retourne les balances (réelles ou paper)."""
        if self.config.trading_mode == TradingMode.PAPER:
            return self._paper_balance.copy()
        
        try:
            balance = await self.exchange.fetch_balance()
            return balance["total"]
        except Exception as e:
            log.error(f"Erreur balance: {e}")
            return {}
    
    async def place_order(self, side: str, amount: float, price: float) -> Optional[dict]:
        """
        Place un ordre.
        - En mode PAPER: simule l'exécution
        - En mode LIVE: place un vrai ordre market sur Binance
        """
        pair = self.config.pair
        base, quote = pair.split("/")
        
        if self.config.trading_mode == TradingMode.PAPER:
            # Simulation
            cost = amount * price
            
            if side == "buy":
                if self._paper_balance.get(quote, 0) < cost:
                    log.warning(f"⚠️ Balance insuffisante: {self._paper_balance.get(quote, 0):.2f} {quote} < {cost:.2f}")
                    return None
                self._paper_balance[quote] = self._paper_balance.get(quote, 0) - cost
                self._paper_balance[base] = self._paper_balance.get(base, 0) + amount
            
            elif side == "sell":
                if self._paper_balance.get(base, 0) < amount:
                    log.warning(f"⚠️ Balance insuffisante: {self._paper_balance.get(base, 0):.6f} {base}")
                    return None
                self._paper_balance[base] = self._paper_balance.get(base, 0) - amount
                self._paper_balance[quote] = self._paper_balance.get(quote, 0) + cost
            
            log.info(f"📝 [PAPER] {side.upper()} {amount:.6f} {base} @ ${price:.2f} (coût: ${cost:.2f})")
            return {
                "id": f"paper_{int(time.time()*1000)}",
                "side": side,
                "amount": amount,
                "price": price,
                "cost": cost,
                "status": "filled",
            }
        
        else:
            # LIVE — ordre market
            try:
                order = await self.exchange.create_market_order(pair, side, amount)
                log.info(f"🔥 [LIVE] {side.upper()} {amount:.6f} {base} — Order ID: {order['id']}")
                return order
            except Exception as e:
                log.error(f"❌ Erreur ordre: {e}")
                return None
    
    async def close(self):
        if self.exchange:
            await self.exchange.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Trading Bot Engine
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class Position:
    entry_price: float
    amount: float
    side: str
    stop_loss: float
    take_profit: float
    entry_time: str
    strategy: str


class TradingBot:
    """Moteur principal du bot."""
    
    def __init__(self, config: BotConfig):
        self.config = config
        self.exchange = ExchangeManager(config)
        self.db = TradeDB(config.db_path)
        self.positions: list[Position] = []
        self.is_running = False
        self.candles: list[dict] = []
        self.stats = {
            "total_trades": 0,
            "win_trades": 0,
            "total_pnl": 0.0,
            "daily_pnl": 0.0,
        }
        self._ws_clients: list[WebSocket] = []
    
    async def start(self):
        """Démarre le bot."""
        await self.exchange.connect()
        self.is_running = True
        
        # Charge l'historique
        self.candles = await self.exchange.fetch_candles(self.config.candle_limit)
        log.info(f"📊 {len(self.candles)} bougies chargées ({self.config.pair} / {self.config.timeframe})")
        
        # Boucle principale
        log.info("🚀 Bot démarré — En attente de signaux...")
        while self.is_running:
            try:
                await self._tick()
                await self._broadcast_state()
                await asyncio.sleep(self.config.tick_interval)
            except Exception as e:
                log.error(f"Erreur tick: {e}")
                await asyncio.sleep(5)
    
    async def stop(self):
        """Arrête le bot proprement."""
        self.is_running = False
        await self.exchange.close()
        self.db.close()
        log.info("⏹ Bot arrêté")
    
    async def _tick(self):
        """Un cycle d'évaluation."""
        # Rafraîchit les données
        new_candles = await self.exchange.fetch_candles(self.config.candle_limit)
        if new_candles:
            self.candles = new_candles
        
        price = self.candles[-1]["close"] if self.candles else 0
        if price == 0:
            return
        
        # Vérifie le circuit breaker (perte journalière max)
        daily_pnl = self.db.get_daily_pnl()
        self.stats["daily_pnl"] = daily_pnl
        balance = await self.exchange.get_balance()
        portfolio_value = balance.get(self.config.base_currency, 0)
        
        if daily_pnl < 0 and abs(daily_pnl) > portfolio_value * self.config.max_daily_loss_pct / 100:
            log.warning(f"🛑 CIRCUIT BREAKER: Perte journalière ${daily_pnl:.2f} > {self.config.max_daily_loss_pct}% du portfolio")
            return
        
        # Vérifie stop-loss / take-profit sur positions ouvertes
        await self._check_exit_conditions(price)
        
        # Évalue la stratégie
        strategy = STRATEGIES.get(self.config.active_strategy)
        if not strategy:
            return
        
        signal = strategy.evaluate(self.candles, self.config)
        
        # Exécute si signal suffisamment confiant
        if signal.action == "BUY" and signal.confidence >= self.config.min_confidence:
            await self._execute_buy(price, signal, strategy.name)
        elif signal.action == "SELL" and signal.confidence >= self.config.min_confidence:
            await self._execute_sell(price, signal, strategy.name)
    
    async def _execute_buy(self, price: float, signal: Signal, strategy_name: str):
        """Exécute un achat."""
        if len(self.positions) >= self.config.max_open_positions:
            log.debug("Max positions atteint, skip BUY")
            return
        
        balance = await self.exchange.get_balance()
        available = balance.get(self.config.base_currency, 0)
        
        if available < 10:  # minimum $10
            return
        
        # Calcule la taille de la position
        allocation = available * self.config.position_size_pct / 100
        amount = allocation / price
        
        # Place l'ordre
        order = await self.exchange.place_order("buy", amount, price)
        if not order:
            return
        
        # Enregistre la position
        stop_loss = price * (1 - self.config.stop_loss_pct / 100)
        take_profit = price * (1 + self.config.take_profit_pct / 100)
        
        position = Position(
            entry_price=price,
            amount=amount,
            side="long",
            stop_loss=round(stop_loss, 2),
            take_profit=round(take_profit, 2),
            entry_time=datetime.now(timezone.utc).isoformat(),
            strategy=strategy_name,
        )
        self.positions.append(position)
        
        # Log en DB
        self.db.log_trade({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pair": self.config.pair,
            "side": "BUY",
            "price": price,
            "amount": round(amount, 6),
            "cost": round(amount * price, 2),
            "strategy": strategy_name,
            "reason": signal.reason,
            "confidence": round(signal.confidence, 3),
            "pnl": 0,
            "mode": self.config.trading_mode.value,
        })
        
        self.stats["total_trades"] += 1
        log.info(f"🟢 ACHAT {amount:.6f} BTC @ ${price:.2f} — SL: ${stop_loss:.2f} / TP: ${take_profit:.2f} — {signal.reason}")
    
    async def _execute_sell(self, price: float, signal: Signal, strategy_name: str):
        """Vend toutes les positions ouvertes."""
        if not self.positions:
            return
        
        for pos in self.positions[:]:
            pnl = (price - pos.entry_price) * pos.amount
            
            order = await self.exchange.place_order("sell", pos.amount, price)
            if not order:
                continue
            
            self.db.log_trade({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "pair": self.config.pair,
                "side": "SELL",
                "price": price,
                "amount": round(pos.amount, 6),
                "cost": round(pos.amount * price, 2),
                "strategy": strategy_name,
                "reason": signal.reason,
                "confidence": round(signal.confidence, 3),
                "pnl": round(pnl, 2),
                "mode": self.config.trading_mode.value,
            })
            
            self.stats["total_trades"] += 1
            self.stats["total_pnl"] += pnl
            if pnl > 0:
                self.stats["win_trades"] += 1
            
            self.positions.remove(pos)
            log.info(f"🔴 VENTE {pos.amount:.6f} BTC @ ${price:.2f} — P&L: ${pnl:.2f} — {signal.reason}")
    
    async def _check_exit_conditions(self, price: float):
        """Vérifie stop-loss et take-profit."""
        for pos in self.positions[:]:
            pnl = (price - pos.entry_price) * pos.amount
            
            triggered = None
            if price <= pos.stop_loss:
                triggered = "STOP_LOSS"
            elif price >= pos.take_profit:
                triggered = "TAKE_PROFIT"
            
            if triggered:
                order = await self.exchange.place_order("sell", pos.amount, price)
                if not order:
                    continue
                
                self.db.log_trade({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "pair": self.config.pair,
                    "side": triggered,
                    "price": price,
                    "amount": round(pos.amount, 6),
                    "cost": round(pos.amount * price, 2),
                    "strategy": pos.strategy,
                    "reason": f"{triggered} @ ${price:.2f}",
                    "confidence": 1.0,
                    "pnl": round(pnl, 2),
                    "mode": self.config.trading_mode.value,
                })
                
                self.stats["total_trades"] += 1
                self.stats["total_pnl"] += pnl
                if pnl > 0:
                    self.stats["win_trades"] += 1
                
                self.positions.remove(pos)
                emoji = "🎯" if triggered == "TAKE_PROFIT" else "⛔"
                log.info(f"{emoji} {triggered} — {pos.amount:.6f} BTC @ ${price:.2f} — P&L: ${pnl:.2f}")
    
    async def _broadcast_state(self):
        """Envoie l'état actuel à tous les clients WebSocket connectés."""
        if not self._ws_clients:
            return
        
        balance = await self.exchange.get_balance()
        price = self.candles[-1]["close"] if self.candles else 0
        
        state = {
            "type": "state_update",
            "price": price,
            "pair": self.config.pair,
            "balance": balance,
            "positions": [asdict(p) for p in self.positions],
            "stats": self.stats,
            "strategy": self.config.active_strategy,
            "is_running": self.is_running,
            "mode": self.config.trading_mode.value,
            "candles": self.candles[-60:],  # dernières 60 bougies
            "indicators": {
                "rsi": Indicators.rsi([c["close"] for c in self.candles], self.config.rsi_period),
                "macd": Indicators.macd([c["close"] for c in self.candles]),
                "bollinger": Indicators.bollinger([c["close"] for c in self.candles], self.config.bb_period),
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        
        msg = json.dumps(state)
        dead = []
        for ws in self._ws_clients:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._ws_clients.remove(ws)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# API FastAPI + WebSocket
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

app = FastAPI(title="Nexus Trader API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

config = BotConfig()
bot = TradingBot(config)


@app.get("/")
async def root():
    return {"status": "ok", "name": "Nexus Trader", "mode": config.trading_mode.value}


@app.get("/api/status")
async def get_status():
    balance = await bot.exchange.get_balance()
    price = bot.candles[-1]["close"] if bot.candles else 0
    return {
        "is_running": bot.is_running,
        "mode": config.trading_mode.value,
        "pair": config.pair,
        "price": price,
        "balance": balance,
        "positions": [asdict(p) for p in bot.positions],
        "stats": bot.stats,
    }


@app.get("/api/trades")
async def get_trades(limit: int = 50):
    return bot.db.get_trades(limit)


@app.post("/api/config")
async def update_config(updates: dict):
    """Met à jour la config à chaud."""
    for key, value in updates.items():
        if hasattr(config, key):
            setattr(config, key, value)
            log.info(f"⚙️ Config mise à jour: {key} = {value}")
    return {"status": "ok", "config": asdict(config)}


@app.post("/api/strategy/{name}")
async def set_strategy(name: str):
    if name in STRATEGIES:
        config.active_strategy = name
        log.info(f"🧠 Stratégie changée: {name}")
        return {"status": "ok", "strategy": name}
    return {"status": "error", "message": f"Stratégie inconnue: {name}"}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """WebSocket pour le streaming temps réel vers le frontend."""
    await ws.accept()
    bot._ws_clients.append(ws)
    log.info(f"🔌 Client WebSocket connecté ({len(bot._ws_clients)} total)")
    
    try:
        while True:
            # Reçoit les commandes du frontend
            data = await ws.receive_text()
            msg = json.loads(data)
            
            if msg.get("type") == "set_strategy":
                config.active_strategy = msg["strategy"]
                log.info(f"🧠 Stratégie changée via WS: {msg['strategy']}")
            
            elif msg.get("type") == "set_config":
                for k, v in msg.get("config", {}).items():
                    if hasattr(config, k):
                        setattr(config, k, v)
            
            elif msg.get("type") == "toggle_bot":
                if bot.is_running:
                    bot.is_running = False
                    log.info("⏸ Bot mis en pause via WS")
                else:
                    bot.is_running = True
                    log.info("▶️ Bot repris via WS")
    
    except WebSocketDisconnect:
        bot._ws_clients.remove(ws)
        log.info(f"🔌 Client WebSocket déconnecté ({len(bot._ws_clients)} restants)")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Point d'entrée
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def main():
    """Lance le bot + l'API en parallèle."""
    # Parse les arguments
    mode = "paper"
    if "--mode" in sys.argv:
        idx = sys.argv.index("--mode")
        if idx + 1 < len(sys.argv):
            mode = sys.argv[idx + 1]
    
    config.trading_mode = TradingMode(mode)
    
    if config.trading_mode == TradingMode.LIVE:
        log.warning("⚠️  MODE LIVE ACTIVÉ — De vrais ordres seront passés !")
        log.warning("⚠️  Assure-toi d'avoir configuré tes clés API dans .env")
        await asyncio.sleep(3)  # Pause de sécurité
    
    # Lance le serveur API dans un thread
    server_config = uvicorn.Config(app, host="0.0.0.0", port=8080, log_level="warning")
    server = uvicorn.Server(server_config)
    
    # Lance bot + API en parallèle
    await asyncio.gather(
        server.serve(),
        bot.start(),
    )


if __name__ == "__main__":
    print("""
    ╔══════════════════════════════════════════════════════════════╗
    ║            ⚡ NEXUS TRADER — Crypto Trading Bot             ║
    ║                                                              ║
    ║  Usage:                                                      ║
    ║    python bot.py --mode paper    (simulation)                ║
    ║    python bot.py --mode live     (trading réel)              ║
    ║                                                              ║
    ║  API:       http://localhost:8080                             ║
    ║  WebSocket: ws://localhost:8080/ws                            ║
    ║  Docs:      http://localhost:8080/docs                        ║
    ╚══════════════════════════════════════════════════════════════╝
    """)
    asyncio.run(main())
