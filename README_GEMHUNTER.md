# 💎 NEXUS GEMHUNTER

Bot Telegram multi-chain qui détecte les nouveaux tokens en temps réel, analyse leur sécurité, et permet d'acheter en 1 clic.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Telegram Bot (@NexusGemHunterBot)                      │
│  Alertes scorées + Buy en 1 clic + Settings par user    │
│         ↕                                                │
├─────────────────────────────────────────────────────────┤
│  gemhunter_main.py — Orchestrateur principal             │
│  ├── pool_listener.py — Détection temps réel (WebSocket) │
│  │   ├── EVM: Uniswap V2/V3 (Ethereum, Base, Arbitrum)  │
│  │   └── Solana: Raydium AMM (logsSubscribe)             │
│  ├── score_enricher.py — Scoring avancé                  │
│  │   ├── DexScreener/CoinGecko Trending                  │
│  │   ├── Smart Money / Volume Anomaly                    │
│  │   └── Market Context (RSS news feeds)                 │
│  ├── swap_executor.py — Exécution on-chain + fees        │
│  │   ├── EVM: Uniswap V2 Router                         │
│  │   └── Solana: Jupiter Aggregator                      │
│  └── user_db.py — SQLite (users, wallets, trades)        │
├─────────────────────────────────────────────────────────┤
│  Blockchain RPC (Alchemy, Helius, public nodes)          │
└─────────────────────────────────────────────────────────┘
```

## Quick Start

### 1. Cloner et configurer

```bash
git clone https://github.com/NathalieTomas/trader.git gemhunter
cd gemhunter
cp env.example .env
# Éditer .env avec tes clés (voir Configuration ci-dessous)
```

### 2. Lancer en local

```bash
pip install -r requirements.txt
python gemhunter_main.py
```

### 3. Déployer sur VPS (Docker)

```bash
# Créer Dockerfile.gemhunter
cat > Dockerfile.gemhunter << 'EOF'
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY pool_listener.py gemhunter_main.py swap_executor.py user_db.py score_enricher.py .
CMD ["python", "gemhunter_main.py"]
EOF

# Créer docker-compose.gemhunter.yml
cat > docker-compose.gemhunter.yml << 'EOF'
services:
  gemhunter:
    build:
      context: .
      dockerfile: Dockerfile.gemhunter
    container_name: nexus-gemhunter
    restart: unless-stopped
    env_file: [.env]
    ports: ["8081:8081"]
    volumes: [gemhunter-data:/app/data]
    mem_limit: 512m
    cpus: 0.5
volumes:
  gemhunter-data:
EOF

# Lancer
docker compose -f docker-compose.gemhunter.yml build
docker compose -f docker-compose.gemhunter.yml up -d
docker compose -f docker-compose.gemhunter.yml logs -f
```

## Configuration (.env)

### Obligatoire

| Variable | Description | Où l'obtenir |
|----------|-------------|--------------|
| `ETH_WS_RPC` | WebSocket Ethereum | [alchemy.com](https://alchemy.com) (gratuit) |
| `ETH_HTTP_RPC` | HTTP Ethereum | Même clé Alchemy |
| `BASE_WS_RPC` | WebSocket Base | Même clé Alchemy |
| `BASE_HTTP_RPC` | HTTP Base | Même clé Alchemy |
| `SOLANA_WS_RPC` | WebSocket Solana | `wss://api.mainnet-beta.solana.com` (public) |
| `SOLANA_HTTP_RPC` | HTTP Solana | `https://api.mainnet-beta.solana.com` (public) |
| `TELEGRAM_BOT_TOKEN` | Token du bot Telegram | [@BotFather](https://t.me/BotFather) sur Telegram |
| `ENCRYPTION_KEY` | Chiffrement des wallets users | Généré automatiquement au 1er lancement |

### Filtres

| Variable | Défaut | Description |
|----------|--------|-------------|
| `MIN_SCORE` | 60 | Score minimum pour envoyer une alerte (0-100) |
| `MIN_LIQUIDITY_USD` | 10000 | Liquidité minimum en USD |

### Optionnel

| Variable | Description |
|----------|-------------|
| `FEE_PCT` | Fee par trade en % (défaut: 0.8) |
| `FEE_WALLET_EVM` | Wallet qui reçoit les fees EVM |
| `FEE_WALLET_SOLANA` | Wallet qui reçoit les fees Solana |
| `ANTHROPIC_API_KEY` | Pour l'analyse IA des news |

## Commandes Telegram

| Commande | Description |
|----------|-------------|
| `/start` | Inscription au bot |
| `/settings` | Configurer ses filtres (chains, score min, taxes max) |
| `/status` | Voir ses stats et wallets |
| `/wallet` | Voir ses wallets |
| `/createwallet base` | Créer un wallet (ethereum, base, solana) |
| `/trades` | Historique des trades |
| `/recent` | 5 dernières détections |
| `/referral` | Code de parrainage |
| `/setscore 60` | Changer le score minimum |
| `/setbet 25` | Changer la mise par trade ($) |
| `/help` | Aide |

## Scoring des tokens

Chaque token détecté reçoit un score de 0 à 100 :

### Score de base (TokenAnalyzer)
- Honeypot détecté → **-50 pts** (bloqué)
- Sell tax > 10% → **-20 pts**
- Token mintable → **-15 pts**
- Contract non vérifié → **-5 pts**
- Ownership safe → **+10 pts**
- Forte liquidité (>$50k) → **+15 pts**
- Fort volume → **+10 pts**
- Buy pressure dominante → **+5 pts**

### Score enrichi (ScoreEnricher)
- Token trending (DexScreener/CoinGecko) → **+15 pts**
- Smart money signal fort → **+20 pts**
- Volume en accélération → **+10 pts**
- Marché bullish (news) → **+5 pts**
- Marché en panique → **-20 pts**

### Interprétation
- **80-100** 🟢 GOLD — Signal fort, multiple indicateurs positifs
- **60-79** 🟡 OK — Potentiel, à évaluer
- **40-59** 🟠 RISKY — Risqué, peu d'info
- **0-39** 🔴 DANGER — Ne pas toucher

## Fichiers du projet

| Fichier | Rôle |
|---------|------|
| `gemhunter_main.py` | Point d'entrée — lance tout |
| `pool_listener.py` | Listeners WebSocket multi-chain + TokenAnalyzer |
| `score_enricher.py` | Scoring avancé (trending, smart money, news) |
| `swap_executor.py` | Buy on-chain (Uniswap/Jupiter) + fees |
| `user_db.py` | Base SQLite (users, wallets, trades, referrals) |
| `bot.py` | Trading bot perso Binance (projet séparé) |
| `intelligence.py` | Module intelligence contextuelle |
| `sentiment.py` | Analyse de sentiment marché |
| `newstrading.py` | Trading sur événements macro |

## API Admin

Accessible sur `http://localhost:8081` quand le bot tourne.

| Endpoint | Description |
|----------|-------------|
| `GET /` | Status |
| `GET /api/stats` | Stats complètes (pools, users, fees) |
| `GET /api/users` | Liste des utilisateurs |
| `GET /api/trades` | Stats globales des trades |

## Commandes Docker utiles

```bash
# Voir les logs
docker compose -f docker-compose.gemhunter.yml logs -f

# Arrêter
docker compose -f docker-compose.gemhunter.yml down

# Redémarrer
docker compose -f docker-compose.gemhunter.yml restart

# État des containers
docker ps

# Ressources utilisées
docker stats nexus-gemhunter
```

## Modèle économique

- **Fee de 0.8%** sur chaque trade exécuté via le bot
- **Tier premium** (futur) — alertes plus rapides, filtres avancés
- **Referral** — 10% de réduction pour le filleul, 10% de rebate pour le parrain

## ⚠️ Avertissements

- **Ce bot ne garantit aucun profit.** Le trading crypto est extrêmement risqué.
- **La majorité des nouveaux tokens perdent de la valeur.** Le scoring réduit le risque mais ne l'élimine pas.
- **Ne tradez jamais avec de l'argent que vous ne pouvez pas vous permettre de perdre.**
- **Les performances passées ne préjugent pas des résultats futurs.**
- En France, les plus-values crypto sont soumises à la flat tax de 30%.
