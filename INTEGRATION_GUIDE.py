"""
╔══════════════════════════════════════════════════════════════════════╗
║  NEXUS TRADER — Guide d'intégration Sentiment + Améliorations      ║
║  Ce fichier montre comment brancher sentiment.py dans bot.py       ║
╚══════════════════════════════════════════════════════════════════════╝

Copie les sections ci-dessous dans bot.py aux endroits indiqués.
"""

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1. IMPORTS — Ajoute en haut de bot.py
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

"""
from sentiment import (
    SentimentEngine,
    SentimentEnhancedStrategy,
    TrailingStopManager,
    DynamicPositionSizer,
)
"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2. Dans TradingBot.__init__ — Ajoute ces lignes
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

"""
class TradingBot:
    def __init__(self, config: BotConfig):
        # ... (code existant) ...
        
        # ✨ NOUVEAU: Modules avancés
        self.sentiment = SentimentEngine()
        self.trailing_stops = TrailingStopManager(trail_pct=config.stop_loss_pct)
        self.position_sizer = DynamicPositionSizer(base_risk_pct=1.0)
"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3. Remplace la méthode _tick() dans TradingBot
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

"""
    async def _tick(self):
        # Rafraîchit les données
        new_candles = await self.exchange.fetch_candles(self.config.candle_limit)
        if new_candles:
            self.candles = new_candles

        price = self.candles[-1]["close"] if self.candles else 0
        if price == 0:
            return

        # Circuit breaker
        daily_pnl = self.db.get_daily_pnl()
        self.stats["daily_pnl"] = daily_pnl
        balance = await self.exchange.get_balance()
        portfolio_value = balance.get(self.config.base_currency, 0)

        if daily_pnl < 0 and abs(daily_pnl) > portfolio_value * self.config.max_daily_loss_pct / 100:
            log.warning(f"🛑 CIRCUIT BREAKER activé")
            return

        # ✨ NOUVEAU: Vérifie les trailing stops (remplace le check fixe)
        for pos in self.positions[:]:
            pos_id = f"{pos.entry_time}_{pos.entry_price}"
            result = self.trailing_stops.update(pos_id, price)
            
            if result["triggered"]:
                pnl = (price - pos.entry_price) * pos.amount
                order = await self.exchange.place_order("sell", pos.amount, price)
                if order:
                    self.db.log_trade({
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "pair": self.config.pair,
                        "side": "TRAILING_STOP",
                        "price": price,
                        "amount": round(pos.amount, 6),
                        "cost": round(pos.amount * price, 2),
                        "strategy": pos.strategy,
                        "reason": f"Trailing stop @ ${price:.2f} (plus haut: ${result['highest']:.2f})",
                        "confidence": 1.0,
                        "pnl": round(pnl, 2),
                        "mode": self.config.trading_mode.value,
                    })
                    self.stats["total_trades"] += 1
                    self.stats["total_pnl"] += pnl
                    if pnl > 0:
                        self.stats["win_trades"] += 1
                    self.positions.remove(pos)
                    log.info(f"📍 TRAILING STOP — P&L: ${pnl:.2f}")

        # ✨ NOUVEAU: Évalue avec sentiment
        base_strategy = STRATEGIES.get(self.config.active_strategy)
        if not base_strategy:
            return
        
        enhanced = SentimentEnhancedStrategy(base_strategy, self.sentiment)
        signal = await enhanced.evaluate_with_sentiment(self.candles, self.config)

        # Exécute si confiant
        if signal["action"] == "BUY" and signal["confidence"] >= self.config.min_confidence:
            await self._execute_buy_enhanced(price, signal)
        elif signal["action"] == "SELL" and signal["confidence"] >= self.config.min_confidence:
            await self._execute_sell(price, Signal(signal["action"], signal["confidence"], signal["reason"]), base_strategy.name)
"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4. Nouvelle méthode _execute_buy_enhanced (ajoute dans TradingBot)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

"""
    async def _execute_buy_enhanced(self, price: float, signal: dict):
        if len(self.positions) >= self.config.max_open_positions:
            return

        balance = await self.exchange.get_balance()
        available = balance.get(self.config.base_currency, 0)
        if available < 10:
            return

        # ✨ NOUVEAU: Calcul ATR pour volatilité
        atr = Indicators.atr(self.candles)
        
        # ✨ NOUVEAU: Stop-loss dynamique basé sur ATR
        if atr:
            stop_loss = price - (atr * 1.5)  # 1.5x ATR sous le prix
        else:
            stop_loss = price * (1 - self.config.stop_loss_pct / 100)
        
        take_profit = price * (1 + self.config.take_profit_pct / 100)

        # ✨ NOUVEAU: Position sizing dynamique
        sentiment_mult = signal.get("position_size_multiplier", 1.0)
        amount = self.position_sizer.calculate_size(
            portfolio_value=available,
            entry_price=price,
            stop_loss_price=stop_loss,
            atr=atr,
            sentiment_multiplier=sentiment_mult,
        )
        
        if amount * price < 10:  # minimum $10
            return

        order = await self.exchange.place_order("buy", amount, price)
        if not order:
            return

        # Enregistre la position
        position = Position(
            entry_price=price,
            amount=amount,
            side="long",
            stop_loss=round(stop_loss, 2),
            take_profit=round(take_profit, 2),
            entry_time=datetime.now(timezone.utc).isoformat(),
            strategy=self.config.active_strategy,
        )
        self.positions.append(position)

        # ✨ NOUVEAU: Enregistre le trailing stop
        pos_id = f"{position.entry_time}_{position.entry_price}"
        self.trailing_stops.register_position(pos_id, price)

        # Log
        self.db.log_trade({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pair": self.config.pair,
            "side": "BUY",
            "price": price,
            "amount": round(amount, 6),
            "cost": round(amount * price, 2),
            "strategy": self.config.active_strategy,
            "reason": signal["reason"],
            "confidence": signal["confidence"],
            "pnl": 0,
            "mode": self.config.trading_mode.value,
        })
        
        self.stats["total_trades"] += 1
        
        sentiment_info = signal.get("sentiment_report", {})
        log.info(
            f"🟢 ACHAT {amount:.6f} BTC @ ${price:.2f} "
            f"(SL: ${stop_loss:.2f} / TP: ${take_profit:.2f}) "
            f"— Sentiment: {sentiment_info.get('signal', '?')} "
            f"— {signal['reason']}"
        )
"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 5. Ajoute l'endpoint sentiment à l'API FastAPI
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

"""
@app.get("/api/sentiment")
async def get_sentiment():
    report = await bot.sentiment.get_report()
    return {
        "signal": report.signal,
        "score": report.overall_score,
        "confidence": report.confidence,
        "fear_greed": report.fear_greed_index,
        "fear_greed_label": report.fear_greed_label,
        "news_sentiment": report.news_sentiment,
        "ai_analysis": report.ai_analysis,
        "headlines": report.top_headlines,
        "timestamp": report.timestamp,
    }
"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 6. Variables .env supplémentaires
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

"""
# Ajoute dans ton .env :

# Analyse IA (optionnel mais recommandé)
ANTHROPIC_API_KEY=sk-ant-...

# Sentiment
SENTIMENT_ENABLED=true
SENTIMENT_WEIGHT=0.3
"""
