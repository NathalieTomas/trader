# ⚡ NEXUS TRADER — Bot de Trading Crypto Automatisé

Bot de trading algorithmique pour cryptomonnaies avec connexion Binance, 4 stratégies intégrées, gestion du risque, et dashboard temps réel.

## Architecture

```
┌─────────────────────────────────────────────────┐
│  Frontend React (trading-bot.jsx)                │
│  Dashboard / Stratégies / Config / Logs          │
│         ↕ WebSocket (ws://localhost:8080/ws)     │
├─────────────────────────────────────────────────┤
│  Backend Python (bot.py)                         │
│  ├── FastAPI — API REST + WebSocket server       │
│  ├── Strategy Engine — RSI, MA, BB, Combined     │
│  ├── Risk Manager — SL/TP, sizing, circuit break │
│  ├── Exchange Manager — ccxt → Binance API       │
│  └── TradeDB — SQLite persistence                │
├─────────────────────────────────────────────────┤
│  Binance API                                     │
│  Market data (OHLCV) + Order execution           │
└─────────────────────────────────────────────────┘
```

## Quick Start

### 1. Installation

```bash
# Clone et installe
pip install -r requirements.txt

# Copie la config
cp .env.example .env
```

### 2. Configuration API Binance

1. Va sur https://www.binance.com/en/my/settings/api-management
2. Crée une nouvelle clé API
3. Active **uniquement** :
   - ✅ Enable Reading
   - ✅ Enable Spot & Margin Trading
   - ❌ **NE PAS** activer Enable Withdrawals
4. Restreins les IP si possible (recommandé)
5. Copie la clé et le secret dans ton `.env`

### 3. Lancement

```bash
# Mode paper trading (recommandé pour commencer)
python bot.py --mode paper

# Mode live (VRAI argent — sois sûr de toi)
python bot.py --mode live
```

Le bot sera accessible sur :
- **API REST** : http://localhost:8080
- **Docs Swagger** : http://localhost:8080/docs
- **WebSocket** : ws://localhost:8080/ws

## Stratégies

| Stratégie | Description | Meilleur pour |
|-----------|-------------|---------------|
| `rsi_reversal` | Achète RSI < 30, vend RSI > 70 | Marchés range-bound |
| `ma_crossover` | Croisement EMA 9/21 | Marchés en tendance |
| `bollinger_bounce` | Rebond sur bandes de Bollinger | Volatilité moyenne |
| `combined` | Multi-signal (RSI + MACD + BB) | Usage général |

## Risk Management

- **Stop-Loss** : Configurable (défaut 2%)
- **Take-Profit** : Configurable (défaut 4%)
- **Position Sizing** : % du portfolio par trade (défaut 10%)
- **Max Positions** : Limite le nombre de positions ouvertes (défaut 3)
- **Circuit Breaker** : Arrêt automatique si perte journalière > 5%
- **Confiance Minimum** : Filtre les signaux faibles (défaut 40%)

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/status` | État du bot, balance, positions |
| GET | `/api/trades?limit=50` | Historique des trades |
| POST | `/api/config` | Met à jour la config à chaud |
| POST | `/api/strategy/{name}` | Change de stratégie |
| WS | `/ws` | Stream temps réel |

## Déploiement VPS (24/7)

```bash
# Sur un VPS Ubuntu (DigitalOcean, OVH, Hetzner...)
sudo apt update && sudo apt install python3-pip python3-venv

# Crée un environnement virtuel
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Lance avec systemd pour auto-restart
sudo nano /etc/systemd/system/nexus-trader.service
```

Contenu du service systemd :

```ini
[Unit]
Description=Nexus Trader Bot
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/nexus-trader
ExecStart=/home/ubuntu/nexus-trader/venv/bin/python bot.py --mode paper
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable nexus-trader
sudo systemctl start nexus-trader
sudo journalctl -u nexus-trader -f  # voir les logs
```

## Ajouter ta propre stratégie

```python
class MaStrategie(Strategy):
    name = "ma_strategie"
    description = "Description ici"
    
    def evaluate(self, candles, config):
        closes = [c["close"] for c in candles]
        # Ta logique ici...
        return Signal("BUY", 0.8, "Raison du signal")

# Enregistre-la
STRATEGIES["ma_strategie"] = MaStrategie()
```

## ⚠️ Avertissements

- **Ce bot ne garantit aucun profit.** Le trading crypto est extrêmement risqué.
- **Commence TOUJOURS en mode paper** pour valider ta stratégie.
- **Ne trade jamais avec de l'argent que tu ne peux pas te permettre de perdre.**
- **Les performances passées ne préjugent pas des résultats futurs.**
- En France, les plus-values crypto sont soumises à la flat tax de 30%.
- L'auteur décline toute responsabilité pour les pertes financières.
