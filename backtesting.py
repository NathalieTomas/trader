"""
╔══════════════════════════════════════════════════════════════════════╗
║  NEXUS TRADER — Module Backtesting                                 ║
║  Teste les stratégies sur données historiques avant de risquer     ║
║  du vrai argent.                                                    ║
╚══════════════════════════════════════════════════════════════════════╝

USAGE:
    python backtesting.py                          # Test par défaut BTC 6 mois
    python backtesting.py --pair ETH/USDT --days 365
    python backtesting.py --optimize                # Optimise les paramètres
    python backtesting.py --compare                 # Compare toutes les stratégies

INSTALLATION:
    pip install ccxt pandas tabulate matplotlib
"""

import asyncio
import json
import math
import os
import sys
import time
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

try:
    import ccxt.async_support as ccxt_async
    import ccxt as ccxt_sync
except ImportError:
    print("❌ pip install ccxt")
    sys.exit(1)

try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False
    print("⚠️ pandas non installé — résultats en mode texte (pip install pandas)")

# On importe les stratégies et indicateurs du bot principal
# En standalone, on les redéfinit ici
import importlib
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s │ %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("nexus.backtest")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Indicateurs (copie allégée de bot.py pour standalone)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class Indicators:
    @staticmethod
    def ema(closes, period):
        if len(closes) < period: return None
        k = 2 / (period + 1)
        e = sum(closes[:period]) / period
        for p in closes[period:]:
            e = p * k + e * (1 - k)
        return e

    @staticmethod
    def sma(closes, period):
        if len(closes) < period: return None
        return sum(closes[-period:]) / period

    @staticmethod
    def rsi(closes, period=14):
        if len(closes) < period + 1: return 50
        gains = losses = 0
        for i in range(len(closes) - period, len(closes)):
            d = closes[i] - closes[i - 1]
            if d > 0: gains += d
            else: losses -= d
        if losses == 0: return 100
        return round(100 - 100 / (1 + gains / losses), 2)

    @staticmethod
    def macd(closes):
        e12 = Indicators.ema(closes, 12)
        e26 = Indicators.ema(closes, 26)
        if not e12 or not e26: return {"macd": 0, "signal": 0, "histogram": 0}
        m = e12 - e26
        s = m * 0.15
        return {"macd": round(m, 2), "signal": round(s, 2), "histogram": round(m - s, 2)}

    @staticmethod
    def bollinger(closes, period=20):
        if len(closes) < period: return None
        w = closes[-period:]
        sma = sum(w) / period
        std = (sum((x - sma) ** 2 for x in w) / period) ** 0.5
        return {"upper": round(sma + 2 * std, 2), "middle": round(sma, 2), "lower": round(sma - 2 * std, 2)}

    @staticmethod
    def atr(candles, period=14):
        if len(candles) < period + 1: return None
        trs = []
        for i in range(1, len(candles)):
            c = candles[i]
            pc = candles[i - 1]["close"]
            trs.append(max(c["high"] - c["low"], abs(c["high"] - pc), abs(c["low"] - pc)))
        return round(sum(trs[-period:]) / period, 2)


# Stratégies
class Signal:
    def __init__(self, action, confidence, reason):
        self.action = action
        self.confidence = confidence
        self.reason = reason

def eval_rsi(candles, config):
    closes = [c["close"] for c in candles]
    rsi = Indicators.rsi(closes, config.get("rsi_period", 14))
    if rsi < config.get("rsi_buy", 30):
        return Signal("BUY", min((config.get("rsi_buy", 30) - rsi) / 30, 1), f"RSI={rsi}")
    if rsi > config.get("rsi_sell", 70):
        return Signal("SELL", min((rsi - config.get("rsi_sell", 70)) / 30, 1), f"RSI={rsi}")
    return Signal("HOLD", 0, f"RSI={rsi}")

def eval_ma(candles, config):
    closes = [c["close"] for c in candles]
    f = Indicators.ema(closes, config.get("ema_fast", 9))
    s = Indicators.ema(closes, config.get("ema_slow", 21))
    if not f or not s: return Signal("HOLD", 0, "N/A")
    d = ((f - s) / s) * 100
    if d > 0.1: return Signal("BUY", min(d / 2, 1), f"EMA diff={d:.3f}%")
    if d < -0.1: return Signal("SELL", min(abs(d) / 2, 1), f"EMA diff={d:.3f}%")
    return Signal("HOLD", 0, f"EMA diff={d:.3f}%")

def eval_bb(candles, config):
    closes = [c["close"] for c in candles]
    bb = Indicators.bollinger(closes, config.get("bb_period", 20))
    if not bb: return Signal("HOLD", 0, "N/A")
    p = closes[-1]
    r = bb["upper"] - bb["lower"]
    if p <= bb["lower"]: return Signal("BUY", min((bb["lower"] - p) / r + 0.5, 1), f"BB low")
    if p >= bb["upper"]: return Signal("SELL", min((p - bb["upper"]) / r + 0.5, 1), f"BB high")
    return Signal("HOLD", 0, "BB mid")

def eval_combined(candles, config):
    closes = [c["close"] for c in candles]
    price = closes[-1]
    rsi = Indicators.rsi(closes)
    macd = Indicators.macd(closes)
    bb = Indicators.bollinger(closes)
    score = 0
    reasons = []
    if rsi < 35: score += 1; reasons.append(f"RSI({rsi})")
    elif rsi > 65: score -= 1; reasons.append(f"RSI({rsi})")
    if macd["histogram"] > 0: score += 1; reasons.append("MACD+")
    elif macd["histogram"] < 0: score -= 1; reasons.append("MACD-")
    if bb:
        if price < bb["lower"]: score += 1; reasons.append("BB↓")
        elif price > bb["upper"]: score -= 1; reasons.append("BB↑")
    r = ", ".join(reasons) or "mixed"
    if score >= 2: return Signal("BUY", score / 3, r)
    if score <= -2: return Signal("SELL", abs(score) / 3, r)
    return Signal("HOLD", 0, r)

STRATEGIES = {
    "rsi_reversal": eval_rsi,
    "ma_crossover": eval_ma,
    "bollinger_bounce": eval_bb,
    "combined": eval_combined,
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Data Fetcher — Récupère les données historiques
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class HistoricalDataFetcher:
    """Télécharge les bougies historiques depuis Binance."""

    CACHE_DIR = ".backtest_cache"

    def __init__(self):
        os.makedirs(self.CACHE_DIR, exist_ok=True)

    async def fetch(self, pair: str, timeframe: str, days: int) -> list[dict]:
        """Récupère les données historiques (avec cache local)."""
        cache_file = os.path.join(
            self.CACHE_DIR,
            f"{pair.replace('/', '_')}_{timeframe}_{days}d.json"
        )

        # Check cache
        if os.path.exists(cache_file):
            mtime = os.path.getmtime(cache_file)
            if time.time() - mtime < 86400:  # Cache valide 24h
                with open(cache_file) as f:
                    data = json.load(f)
                    log.info(f"📂 Cache chargé: {len(data)} bougies ({pair} {timeframe} {days}j)")
                    return data

        log.info(f"⬇️ Téléchargement: {pair} {timeframe} sur {days} jours...")

        exchange = ccxt_async.binance({"enableRateLimit": True})
        all_candles = []

        try:
            since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
            while True:
                ohlcv = await exchange.fetch_ohlcv(pair, timeframe, since=since, limit=1000)
                if not ohlcv:
                    break
                for c in ohlcv:
                    all_candles.append({
                        "time": c[0], "open": c[1], "high": c[2],
                        "low": c[3], "close": c[4], "volume": c[5],
                    })
                since = ohlcv[-1][0] + 1
                if len(ohlcv) < 1000:
                    break
                await asyncio.sleep(0.1)  # Rate limit
        finally:
            await exchange.close()

        # Sauvegarde cache
        with open(cache_file, "w") as f:
            json.dump(all_candles, f)

        log.info(f"✅ {len(all_candles)} bougies téléchargées ({pair} {timeframe})")
        return all_candles


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Trade Simulator
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class BacktestTrade:
    entry_time: int
    exit_time: int
    side: str
    entry_price: float
    exit_price: float
    amount: float
    pnl: float
    pnl_pct: float
    exit_reason: str
    strategy: str


@dataclass
class BacktestConfig:
    strategy: str = "combined"
    initial_balance: float = 10000
    position_size_pct: float = 10
    stop_loss_pct: float = 2.0
    take_profit_pct: float = 4.0
    min_confidence: float = 0.4
    trading_fee_pct: float = 0.1       # Frais Binance (0.1% maker/taker)
    slippage_pct: float = 0.05         # Slippage estimé
    use_trailing_stop: bool = False
    trailing_stop_pct: float = 2.0
    # Indicator params
    rsi_period: int = 14
    rsi_buy: int = 30
    rsi_sell: int = 70
    ema_fast: int = 9
    ema_slow: int = 21
    bb_period: int = 20

    def to_dict(self):
        return self.__dict__


@dataclass
class BacktestResult:
    config: dict
    pair: str
    timeframe: str
    period_days: int
    start_date: str
    end_date: str
    # Performance
    initial_balance: float
    final_balance: float
    total_return_pct: float
    buy_and_hold_return_pct: float     # Comparaison: juste acheter et garder
    alpha: float                        # Surperformance vs buy & hold
    # Trades
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    avg_win_pct: float
    avg_loss_pct: float
    profit_factor: float               # Gains totaux / Pertes totales
    # Risk
    max_drawdown_pct: float            # Pire perte depuis un sommet
    max_drawdown_duration_hours: float
    sharpe_ratio: float                # Rendement ajusté au risque
    sortino_ratio: float               # Sharpe mais ne pénalise que les pertes
    calmar_ratio: float                # Rendement / Max Drawdown
    # Détails
    trades: list
    equity_curve: list                 # Evolution du portfolio
    monthly_returns: dict


class BacktestEngine:
    """Moteur de backtesting — simule le trading sur données historiques."""

    def __init__(self, config: BacktestConfig):
        self.config = config
        self.strategy_fn = STRATEGIES.get(config.strategy)

    def run(self, candles: list[dict], pair: str = "BTC/USDT", timeframe: str = "1h") -> BacktestResult:
        """Lance le backtest."""
        if not self.strategy_fn:
            raise ValueError(f"Stratégie inconnue: {self.config.strategy}")

        log.info(f"🔬 Backtest: {self.config.strategy} sur {pair} ({len(candles)} bougies)")

        balance = self.config.initial_balance
        position = None  # {entry_price, amount, highest_price}
        trades: list[BacktestTrade] = []
        equity_curve = []
        peak_balance = balance
        max_drawdown = 0
        drawdown_start = 0
        max_dd_duration = 0
        config_dict = self.config.to_dict()
        fee_pct = self.config.trading_fee_pct / 100
        slippage_pct = self.config.slippage_pct / 100

        lookback = 50  # Minimum de bougies pour les indicateurs

        for i in range(lookback, len(candles)):
            window = candles[max(0, i - 200):i + 1]
            current = candles[i]
            price = current["close"]

            # Calcule la valeur du portfolio
            portfolio_value = balance
            if position:
                portfolio_value += position["amount"] * price

            equity_curve.append({
                "time": current["time"],
                "value": round(portfolio_value, 2),
                "price": price,
            })

            # Track drawdown
            if portfolio_value > peak_balance:
                peak_balance = portfolio_value
                drawdown_start = current["time"]
            dd = (peak_balance - portfolio_value) / peak_balance * 100
            if dd > max_drawdown:
                max_drawdown = dd
                max_dd_duration = (current["time"] - drawdown_start) / 3600000  # en heures

            # ── Check stop-loss / take-profit ──
            if position:
                pnl_pct = (price - position["entry_price"]) / position["entry_price"] * 100

                exit_reason = None

                # Trailing stop
                if self.config.use_trailing_stop:
                    if price > position["highest_price"]:
                        position["highest_price"] = price
                    trail_stop = position["highest_price"] * (1 - self.config.trailing_stop_pct / 100)
                    if price <= trail_stop:
                        exit_reason = "TRAILING_STOP"
                else:
                    if pnl_pct <= -self.config.stop_loss_pct:
                        exit_reason = "STOP_LOSS"

                if pnl_pct >= self.config.take_profit_pct:
                    exit_reason = "TAKE_PROFIT"

                if exit_reason:
                    # Vend avec frais + slippage
                    sell_price = price * (1 - slippage_pct)
                    revenue = position["amount"] * sell_price * (1 - fee_pct)
                    pnl = revenue - (position["amount"] * position["entry_price"])
                    pnl_pct_actual = pnl / (position["amount"] * position["entry_price"]) * 100

                    trades.append(BacktestTrade(
                        entry_time=position["entry_time"],
                        exit_time=current["time"],
                        side="SELL",
                        entry_price=position["entry_price"],
                        exit_price=sell_price,
                        amount=position["amount"],
                        pnl=round(pnl, 2),
                        pnl_pct=round(pnl_pct_actual, 2),
                        exit_reason=exit_reason,
                        strategy=self.config.strategy,
                    ))
                    balance += revenue
                    position = None
                    continue

            # ── Évalue la stratégie ──
            signal = self.strategy_fn(window, config_dict)

            if signal.action == "BUY" and signal.confidence >= self.config.min_confidence and not position:
                # Achète avec frais + slippage
                allocation = balance * self.config.position_size_pct / 100
                buy_price = price * (1 + slippage_pct)
                amount = (allocation * (1 - fee_pct)) / buy_price

                if amount * buy_price > 10:  # min $10
                    balance -= allocation
                    position = {
                        "entry_price": buy_price,
                        "amount": amount,
                        "entry_time": current["time"],
                        "highest_price": buy_price,
                    }

            elif signal.action == "SELL" and signal.confidence >= self.config.min_confidence and position:
                sell_price = price * (1 - slippage_pct)
                revenue = position["amount"] * sell_price * (1 - fee_pct)
                pnl = revenue - (position["amount"] * position["entry_price"])
                pnl_pct_actual = pnl / (position["amount"] * position["entry_price"]) * 100

                trades.append(BacktestTrade(
                    entry_time=position["entry_time"],
                    exit_time=current["time"],
                    side="SELL",
                    entry_price=position["entry_price"],
                    exit_price=sell_price,
                    amount=position["amount"],
                    pnl=round(pnl, 2),
                    pnl_pct=round(pnl_pct_actual, 2),
                    exit_reason="SIGNAL",
                    strategy=self.config.strategy,
                ))
                balance += revenue
                position = None

        # Ferme la position ouverte à la fin
        if position:
            final_price = candles[-1]["close"]
            revenue = position["amount"] * final_price * (1 - fee_pct)
            pnl = revenue - (position["amount"] * position["entry_price"])
            trades.append(BacktestTrade(
                entry_time=position["entry_time"],
                exit_time=candles[-1]["time"],
                side="SELL",
                entry_price=position["entry_price"],
                exit_price=final_price,
                amount=position["amount"],
                pnl=round(pnl, 2),
                pnl_pct=round(pnl / (position["amount"] * position["entry_price"]) * 100, 2),
                exit_reason="END_OF_TEST",
                strategy=self.config.strategy,
            ))
            balance += revenue

        # ── Calcul des métriques ──
        final_balance = balance
        total_return = (final_balance - self.config.initial_balance) / self.config.initial_balance * 100

        # Buy & Hold
        bh_return = (candles[-1]["close"] - candles[lookback]["close"]) / candles[lookback]["close"] * 100

        # Trade stats
        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl <= 0]
        win_rate = len(wins) / len(trades) * 100 if trades else 0
        avg_win = sum(t.pnl_pct for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t.pnl_pct for t in losses) / len(losses) if losses else 0
        total_gains = sum(t.pnl for t in wins)
        total_losses = abs(sum(t.pnl for t in losses))
        profit_factor = total_gains / total_losses if total_losses > 0 else float("inf")

        # Sharpe & Sortino
        if len(equity_curve) > 1:
            returns = []
            for j in range(1, len(equity_curve)):
                r = (equity_curve[j]["value"] - equity_curve[j-1]["value"]) / equity_curve[j-1]["value"]
                returns.append(r)
            
            avg_ret = sum(returns) / len(returns) if returns else 0
            std_ret = (sum((r - avg_ret)**2 for r in returns) / len(returns))**0.5 if returns else 1
            
            sharpe = (avg_ret / std_ret) * (252**0.5) if std_ret > 0 else 0
            
            downside = [r for r in returns if r < 0]
            down_std = (sum(r**2 for r in downside) / len(downside))**0.5 if downside else 1
            sortino = (avg_ret / down_std) * (252**0.5) if down_std > 0 else 0
        else:
            sharpe = sortino = 0

        calmar = total_return / max_drawdown if max_drawdown > 0 else float("inf")

        # Monthly returns
        monthly = {}
        for ec in equity_curve:
            month = datetime.fromtimestamp(ec["time"] / 1000, tz=timezone.utc).strftime("%Y-%m")
            if month not in monthly:
                monthly[month] = {"start": ec["value"], "end": ec["value"]}
            monthly[month]["end"] = ec["value"]
        monthly_returns = {
            m: round((v["end"] - v["start"]) / v["start"] * 100, 2)
            for m, v in monthly.items()
        }

        return BacktestResult(
            config=self.config.to_dict(),
            pair=pair,
            timeframe=timeframe,
            period_days=len(candles) * {"1m": 1/1440, "5m": 5/1440, "15m": 15/1440, "1h": 1/24, "4h": 4/24, "1d": 1}.get(timeframe, 1/24),
            start_date=datetime.fromtimestamp(candles[lookback]["time"]/1000, tz=timezone.utc).strftime("%Y-%m-%d"),
            end_date=datetime.fromtimestamp(candles[-1]["time"]/1000, tz=timezone.utc).strftime("%Y-%m-%d"),
            initial_balance=self.config.initial_balance,
            final_balance=round(final_balance, 2),
            total_return_pct=round(total_return, 2),
            buy_and_hold_return_pct=round(bh_return, 2),
            alpha=round(total_return - bh_return, 2),
            total_trades=len(trades),
            winning_trades=len(wins),
            losing_trades=len(losses),
            win_rate=round(win_rate, 1),
            avg_win_pct=round(avg_win, 2),
            avg_loss_pct=round(avg_loss, 2),
            profit_factor=round(profit_factor, 2),
            max_drawdown_pct=round(max_drawdown, 2),
            max_drawdown_duration_hours=round(max_dd_duration, 1),
            sharpe_ratio=round(sharpe, 2),
            sortino_ratio=round(sortino, 2),
            calmar_ratio=round(calmar, 2),
            trades=[t.__dict__ for t in trades],
            equity_curve=equity_curve[::max(1, len(equity_curve)//500)],  # Downsample pour la taille
            monthly_returns=monthly_returns,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Parameter Optimizer
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ParameterOptimizer:
    """
    Optimise les paramètres de la stratégie par grid search.
    Teste toutes les combinaisons et trouve les meilleurs paramètres.
    """

    # Espaces de recherche par paramètre
    SEARCH_SPACE = {
        "stop_loss_pct": [1.0, 1.5, 2.0, 3.0, 4.0, 5.0],
        "take_profit_pct": [2.0, 3.0, 4.0, 6.0, 8.0, 10.0],
        "position_size_pct": [5, 10, 15, 20],
        "min_confidence": [0.2, 0.3, 0.4, 0.5, 0.6],
        "rsi_buy": [20, 25, 30, 35],
        "rsi_sell": [65, 70, 75, 80],
        "ema_fast": [5, 7, 9, 12],
        "ema_slow": [18, 21, 26, 30],
    }

    def optimize(
        self,
        candles: list[dict],
        strategy: str,
        target_metric: str = "sharpe_ratio",
        max_combinations: int = 500,
        pair: str = "BTC/USDT",
    ) -> list[dict]:
        """
        Optimise les paramètres.
        Retourne les top 10 combinaisons triées par la métrique cible.
        """
        import itertools
        import random

        # Génère les combinaisons
        params = list(self.SEARCH_SPACE.keys())
        values = list(self.SEARCH_SPACE.values())
        all_combos = list(itertools.product(*values))

        # Si trop de combinaisons, échantillonne
        if len(all_combos) > max_combinations:
            random.shuffle(all_combos)
            all_combos = all_combos[:max_combinations]

        log.info(f"🔧 Optimisation: {len(all_combos)} combinaisons à tester...")

        results = []
        for idx, combo in enumerate(all_combos):
            config = BacktestConfig(strategy=strategy)
            for param, value in zip(params, combo):
                setattr(config, param, value)

            engine = BacktestEngine(config)
            try:
                result = engine.run(candles, pair)
                score = getattr(result, target_metric, 0)
                if not math.isinf(score) and not math.isnan(score):
                    results.append({
                        "params": {p: v for p, v in zip(params, combo)},
                        "score": score,
                        "return": result.total_return_pct,
                        "sharpe": result.sharpe_ratio,
                        "win_rate": result.win_rate,
                        "max_dd": result.max_drawdown_pct,
                        "trades": result.total_trades,
                        "profit_factor": result.profit_factor,
                    })
            except Exception as e:
                pass

            if (idx + 1) % 50 == 0:
                log.info(f"   {idx + 1}/{len(all_combos)} testées...")

        results.sort(key=lambda x: x["score"], reverse=True)
        log.info(f"✅ Optimisation terminée — Top score: {results[0]['score'] if results else 'N/A'}")
        return results[:10]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Display Results
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def display_result(r: BacktestResult):
    """Affiche les résultats du backtest de manière lisible."""
    
    alpha_emoji = "🟢" if r.alpha > 0 else "🔴"
    
    print(f"""
╔══════════════════════════════════════════════════════════════════╗
║  📊 RÉSULTATS DU BACKTEST                                       ║
╠══════════════════════════════════════════════════════════════════╣
║  Paire:      {r.pair:<20}  Stratégie: {r.config['strategy']:<15}║
║  Période:    {r.start_date} → {r.end_date}  ({r.period_days:.0f} jours)       ║
║  Timeframe:  {r.timeframe:<20}  Trades: {r.total_trades:<18}║
╠══════════════════════════════════════════════════════════════════╣
║                                                                  ║
║  💰 PERFORMANCE                                                  ║
║  ─────────────────────────────────────────────────────────────── ║
║  Balance initiale:    ${r.initial_balance:>12,.2f}                         ║
║  Balance finale:      ${r.final_balance:>12,.2f}                         ║
║  Rendement total:     {r.total_return_pct:>+10.2f}%                            ║
║  Buy & Hold:          {r.buy_and_hold_return_pct:>+10.2f}%                            ║
║  Alpha (vs B&H):      {alpha_emoji} {r.alpha:>+9.2f}%                            ║
║                                                                  ║
║  📈 TRADES                                                       ║
║  ─────────────────────────────────────────────────────────────── ║
║  Trades gagnants:     {r.winning_trades:>5} / {r.total_trades:<5}  ({r.win_rate:.1f}%)                ║
║  Gain moyen:          {r.avg_win_pct:>+10.2f}%                            ║
║  Perte moyenne:       {r.avg_loss_pct:>+10.2f}%                            ║
║  Profit Factor:       {r.profit_factor:>10.2f}                             ║
║                                                                  ║
║  ⚠️  RISQUE                                                      ║
║  ─────────────────────────────────────────────────────────────── ║
║  Max Drawdown:        {r.max_drawdown_pct:>10.2f}%                            ║
║  DD Durée max:        {r.max_drawdown_duration_hours:>10.1f}h                            ║
║  Sharpe Ratio:        {r.sharpe_ratio:>10.2f}                             ║
║  Sortino Ratio:       {r.sortino_ratio:>10.2f}                             ║
║  Calmar Ratio:        {r.calmar_ratio:>10.2f}                             ║
║                                                                  ║
╠══════════════════════════════════════════════════════════════════╣
║  📅 RENDEMENTS MENSUELS                                          ║
║  ─────────────────────────────────────────────────────────────── ║""")
    
    for month, ret in r.monthly_returns.items():
        bar_len = min(30, abs(int(ret)))
        bar = ("█" * bar_len) if ret >= 0 else ("▓" * bar_len)
        color_emoji = "🟢" if ret >= 0 else "🔴"
        print(f"║  {month}:  {color_emoji} {ret:>+8.2f}%  {bar:<30}    ║")

    print(f"""╠══════════════════════════════════════════════════════════════════╣
║  🎯 VERDICT                                                     ║
║  ─────────────────────────────────────────────────────────────── ║""")

    # Verdict automatique
    if r.sharpe_ratio > 1.5 and r.alpha > 0 and r.profit_factor > 1.5:
        print(f"║  ✅ EXCELLENT — Stratégie viable pour le live trading           ║")
    elif r.sharpe_ratio > 0.8 and r.alpha > -5 and r.profit_factor > 1.2:
        print(f"║  🟡 CORRECT — Potentiel mais nécessite optimisation             ║")
    elif r.sharpe_ratio > 0 and r.total_return_pct > 0:
        print(f"║  🟠 MÉDIOCRE — Rendement positif mais risque/rendement faible   ║")
    else:
        print(f"║  🔴 MAUVAIS — Cette stratégie perd de l'argent, ne pas utiliser ║")

    print(f"╚══════════════════════════════════════════════════════════════════╝")


def display_comparison(results: list[tuple[str, BacktestResult]]):
    """Compare plusieurs stratégies côte à côte."""
    print(f"\n{'═'*90}")
    print(f"  📊 COMPARAISON DES STRATÉGIES")
    print(f"{'═'*90}")
    print(f"  {'Stratégie':<20} {'Return':>8} {'B&H':>8} {'Alpha':>8} {'WinRate':>8} {'Sharpe':>8} {'MaxDD':>8} {'PF':>6}")
    print(f"  {'─'*20} {'─'*8} {'─'*8} {'─'*8} {'─'*8} {'─'*8} {'─'*8} {'─'*6}")

    for name, r in results:
        e = "🟢" if r.alpha > 0 else "🔴"
        print(f"  {name:<20} {r.total_return_pct:>+7.1f}% {r.buy_and_hold_return_pct:>+7.1f}% {e}{r.alpha:>+6.1f}% {r.win_rate:>7.1f}% {r.sharpe_ratio:>7.2f} {r.max_drawdown_pct:>7.2f}% {r.profit_factor:>5.2f}")

    print(f"{'═'*90}")
    best = max(results, key=lambda x: x[1].sharpe_ratio)
    print(f"  🏆 Meilleure stratégie: {best[0]} (Sharpe: {best[1].sharpe_ratio:.2f})")
    print()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Main
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def main():
    print("""
    ╔══════════════════════════════════════════════════════════════╗
    ║  🔬 NEXUS TRADER — Backtesting Engine                       ║
    ╚══════════════════════════════════════════════════════════════╝
    """)

    pair = "BTC/USDT"
    timeframe = "1h"
    days = 180
    mode = "compare"  # default

    # Parse args
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--pair" and i + 1 < len(args): pair = args[i + 1]
        elif arg == "--timeframe" and i + 1 < len(args): timeframe = args[i + 1]
        elif arg == "--days" and i + 1 < len(args): days = int(args[i + 1])
        elif arg == "--optimize": mode = "optimize"
        elif arg == "--compare": mode = "compare"
        elif arg == "--strategy" and i + 1 < len(args): mode = "single"; strategy = args[i + 1]

    # Fetch data
    fetcher = HistoricalDataFetcher()
    candles = await fetcher.fetch(pair, timeframe, days)

    if mode == "compare":
        # Compare toutes les stratégies
        results = []
        for name in STRATEGIES:
            config = BacktestConfig(strategy=name)
            engine = BacktestEngine(config)
            result = engine.run(candles, pair, timeframe)
            results.append((name, result))
            display_result(result)

        display_comparison(results)

    elif mode == "optimize":
        optimizer = ParameterOptimizer()
        for strategy_name in STRATEGIES:
            print(f"\n🔧 Optimisation de {strategy_name}...")
            top = optimizer.optimize(candles, strategy_name, pair=pair)
            if top:
                print(f"\n  Top 5 pour {strategy_name}:")
                print(f"  {'Score':>8} {'Return':>8} {'Sharpe':>8} {'WinRate':>8} {'MaxDD':>8} {'Params'}")
                for t in top[:5]:
                    params_str = ", ".join(f"{k}={v}" for k, v in t["params"].items())
                    print(f"  {t['score']:>8.2f} {t['return']:>+7.1f}% {t['sharpe']:>7.2f} {t['win_rate']:>7.1f}% {t['max_dd']:>7.2f}% {params_str}")

    else:
        config = BacktestConfig(strategy=strategy)
        engine = BacktestEngine(config)
        result = engine.run(candles, pair, timeframe)
        display_result(result)


if __name__ == "__main__":
    asyncio.run(main())
