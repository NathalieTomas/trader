"""
╔══════════════════════════════════════════════════════════════════════╗
║  NEXUS GEMHUNTER — Main Entry Point                                 ║
║  Intègre Pool Listener + Swap Executor + User DB                    ║
║                                                                      ║
║  C'est LE fichier à lancer pour le produit GemHunter.               ║
║                                                                      ║
║  Usage:                                                              ║
║    python gemhunter_main.py                                          ║
║                                                                      ║
║  Ce qu'il fait:                                                      ║
║  1. Lance les pool listeners (EVM + Solana) en websocket            ║
║  2. Analyse chaque nouveau pool (honeypot, taxes, liquidité)        ║
║  3. Envoie des alertes Telegram aux users selon leurs filtres       ║
║  4. Gère le Buy en 1 clic quand un user clique le bouton           ║
║  5. Persiste tout en SQLite (users, wallets, trades, alertes)       ║
║  6. Expose une API REST pour le dashboard admin                     ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from dataclasses import asdict

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from pool_listener import (
    PoolListenerOrchestrator,
    TelegramMultiUserBot,
    EVMPoolListener,
    SolanaPoolListener,
    TokenAnalyzer,
    NewPool,
    Chain,
)
from swap_executor import SwapRouter, SwapRequest, SwapChain, SwapResult
from user_db import UserDB
from score_enricher import ScoreEnricher

try:
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    import uvicorn
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False

log = logging.getLogger("nexus.gemhunter")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Chain mapping entre les modules
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

CHAIN_TO_SWAP = {
    Chain.ETHEREUM: SwapChain.ETHEREUM,
    Chain.BASE: SwapChain.BASE,
    Chain.ARBITRUM: SwapChain.ARBITRUM,
    Chain.SOLANA: SwapChain.SOLANA,
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# INTEGRATED TELEGRAM BOT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class IntegratedTelegramBot(TelegramMultiUserBot):
    """
    Extends le bot Telegram de base avec:
    - Persistence SQLite via UserDB
    - Buy en 1 clic via SwapRouter
    - Commandes wallet (/wallet, /connectwallet)
    - Commandes trade (/buy, /trades)
    - Referral system (/referral, /invite)
    """

    def __init__(self, bot_token: str, db: UserDB, swap_router: SwapRouter):
        super().__init__(bot_token)
        self.db = db
        self.swap_router = swap_router
        # Cache des pools récents pour le buy callback
        self._recent_pools: dict[str, NewPool] = {}  # pool_addr[:40] -> NewPool

    def cache_pool(self, pool: NewPool):
        """Cache un pool pour que le bouton Buy puisse le retrouver."""
        key = pool.pool_address[:40]
        self._recent_pools[key] = pool
        # Garde max 200 pools en cache
        if len(self._recent_pools) > 200:
            oldest_keys = list(self._recent_pools.keys())[:100]
            for k in oldest_keys:
                del self._recent_pools[k]

    # ── Override: persistence des users dans SQLite ──

    async def _handle_message(self, msg: dict):
        """Override avec persistence DB."""
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = msg.get("text", "").strip()
        username = msg.get("from", {}).get("username", "")

        if not chat_id or not text:
            return

        # Touch user (update last_active)
        if self.db.get_user(chat_id):
            self.db.touch_user(chat_id)

        # ── /start ──
        if text.startswith("/start"):
            await self._cmd_start(chat_id, username, text)
            return

        # ── /settings ──
        if text == "/settings":
            await self._cmd_settings(chat_id)
            return

        # ── /status ──
        if text == "/status":
            await self._cmd_status(chat_id)
            return

        # ── /wallet ──
        if text == "/wallet":
            await self._cmd_wallet(chat_id)
            return

        # ── /createwallet ──
        if text.startswith("/createwallet"):
            await self._cmd_create_wallet(chat_id, text)
            return

        # ── /trades ──
        if text == "/trades":
            await self._cmd_trades(chat_id)
            return

        # ── /recent ──
        if text == "/recent":
            await self._cmd_recent(chat_id)
            return

        # ── /referral ──
        if text == "/referral":
            await self._cmd_referral(chat_id)
            return

        # ── /setscore <value> ──
        if text.startswith("/setscore"):
            parts = text.split()
            if len(parts) == 2:
                try:
                    val = int(parts[1])
                    if 0 <= val <= 100:
                        self.db.update_user_setting(chat_id, "min_score", val)
                        # Update local cache too
                        if chat_id in self.users:
                            self.users[chat_id].min_score = val
                        await self._send(chat_id, f"✅ Score minimum: <b>{val}</b>")
                        return
                except ValueError:
                    pass
            await self._send(chat_id, "Usage: /setscore 50")
            return

        # ── /setbet <value> ──
        if text.startswith("/setbet"):
            parts = text.split()
            if len(parts) == 2:
                try:
                    val = float(parts[1])
                    if 1 <= val <= 10000:
                        self.db.update_user_setting(chat_id, "bet_size_usd", val)
                        await self._send(chat_id, f"✅ Mise par trade: <b>${val}</b>")
                        return
                except ValueError:
                    pass
            await self._send(chat_id, "Usage: /setbet 10")
            return

        # ── /help ──
        if text == "/help":
            await self._send(chat_id, (
                "📖 <b>Commandes disponibles</b>\n\n"
                "/start — Inscription\n"
                "/settings — Configurer tes filtres\n"
                "/status — Tes stats\n"
                "/wallet — Voir tes wallets\n"
                "/createwallet base — Créer un wallet\n"
                "/trades — Historique de tes trades\n"
                "/recent — 5 dernières détections\n"
                "/referral — Ton lien de parrainage\n"
                "/setscore 60 — Changer le score min\n"
                "/setbet 25 — Changer la mise par trade\n"
                "/help — Cette aide"
            ))
            return

    async def _cmd_start(self, chat_id: str, username: str, text: str):
        """Inscription avec support referral."""
        is_new = self.db.register_user(chat_id, username)

        if is_new:
            # Check referral code
            parts = text.split()
            if len(parts) > 1:
                ref_code = parts[1]
                referrer = self.db.get_user_by_referral_code(ref_code)
                if referrer and referrer["chat_id"] != chat_id:
                    self.db.create_referral(referrer["chat_id"], chat_id, ref_code)
                    await self._send(chat_id, f"🎁 Parrainé par @{referrer.get('username', '?')} — fees réduites!")
                    await self._send(referrer["chat_id"], f"🎉 Nouveau filleul: @{username}!")

            # Load user into local cache
            user_data = self.db.get_user(chat_id)
            if user_data:
                from pool_listener import UserConfig
                self.users[chat_id] = UserConfig(
                    chat_id=chat_id,
                    username=username,
                    min_score=user_data["min_score"],
                    chains=user_data["chains"],
                    min_liquidity=user_data["min_liquidity"],
                    max_buy_tax=user_data["max_buy_tax"],
                    max_sell_tax=user_data["max_sell_tax"],
                    registered_at=user_data["registered_at"],
                )

            await self._send(chat_id, (
                "💎 <b>Bienvenue sur Nexus GemHunter!</b>\n\n"
                "Je détecte les nouveaux tokens en temps réel sur:\n"
                "• Ethereum + Base (Uniswap)\n"
                "• Solana (Raydium / Jupiter)\n\n"
                "Tu recevras des alertes avec score de sécurité, "
                "analyse honeypot, et bouton <b>Buy en 1 clic</b>.\n\n"
                "📌 <b>Pour commencer:</b>\n"
                "1. /createwallet base — Crée ton wallet\n"
                "2. Envoie de l'ETH/SOL dessus\n"
                "3. Reçois les alertes et clique Buy!\n\n"
                "/help — Toutes les commandes"
            ))
        else:
            await self._send(chat_id, "Tu es déjà inscrit! /help pour les commandes.")

    async def _cmd_settings(self, chat_id: str):
        user = self.db.get_user(chat_id)
        if not user:
            await self._send(chat_id, "Envoie /start d'abord.")
            return

        chains = user.get("chains", [])
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": f"Score min: {user['min_score']}", "callback_data": "set_score"},
                    {"text": f"Mise: ${user['bet_size_usd']}", "callback_data": "set_bet"},
                ],
                [
                    {"text": "⟠ ETH " + ("✅" if "ethereum" in chains else "❌"), "callback_data": "toggle_eth"},
                    {"text": "🔵 Base " + ("✅" if "base" in chains else "❌"), "callback_data": "toggle_base"},
                    {"text": "🟣 Sol " + ("✅" if "solana" in chains else "❌"), "callback_data": "toggle_sol"},
                ],
                [
                    {"text": f"Liq min: ${user['min_liquidity']:,.0f}", "callback_data": "set_liq"},
                    {"text": f"Tax max: {user['max_sell_tax']}%", "callback_data": "set_tax"},
                ],
            ]
        }
        await self._send(chat_id, "⚙️ <b>Tes paramètres:</b>", reply_markup=keyboard)

    async def _cmd_status(self, chat_id: str):
        user = self.db.get_user(chat_id)
        if not user:
            await self._send(chat_id, "Envoie /start d'abord.")
            return

        wallets = self.db.get_user_wallets(chat_id)
        wallet_text = ""
        for w in wallets:
            wallet_text += f"\n  • {w['chain']}: <code>{w['address'][:10]}...{w['address'][-6:]}</code>"

        if not wallet_text:
            wallet_text = "\n  Aucun — /createwallet pour en créer"

        ref_stats = self.db.get_referral_stats(chat_id)

        await self._send(chat_id, (
            f"📊 <b>Ton profil</b>\n\n"
            f"👤 @{user.get('username', '?')}\n"
            f"💳 Compte: {'⭐ Premium' if user.get('is_premium') else 'Free'}\n"
            f"📅 Inscrit le: {user['registered_at'][:10]}\n\n"
            f"📈 <b>Stats</b>\n"
            f"  Alertes reçues: {user.get('total_alerts', 0)}\n"
            f"  Trades exécutés: {user.get('total_trades', 0)}\n"
            f"  P&L total: ${user.get('total_pnl_usd', 0):+.2f}\n\n"
            f"💰 <b>Wallets</b>{wallet_text}\n\n"
            f"🎁 <b>Referrals</b>\n"
            f"  Filleuls: {ref_stats.get('referrals', 0)}\n"
            f"  Code: <code>{user.get('referral_code', '?')}</code>"
        ))

    async def _cmd_wallet(self, chat_id: str):
        wallets = self.db.get_user_wallets(chat_id)
        if not wallets:
            await self._send(chat_id, (
                "💰 <b>Aucun wallet</b>\n\n"
                "Crée-en un:\n"
                "/createwallet ethereum\n"
                "/createwallet base\n"
                "/createwallet solana"
            ))
            return

        text = "💰 <b>Tes wallets</b>\n"
        for w in wallets:
            chain_emoji = {"ethereum": "⟠", "base": "🔵", "solana": "🟣", "arbitrum": "🔷"}.get(w["chain"], "⚪")
            text += (
                f"\n{chain_emoji} <b>{w['chain'].upper()}</b>\n"
                f"<code>{w['address']}</code>\n"
                f"Envoie de l'ETH/SOL à cette adresse pour trader.\n"
            )

        await self._send(chat_id, text)

    async def _cmd_create_wallet(self, chat_id: str, text: str):
        parts = text.split()
        if len(parts) < 2:
            await self._send(chat_id, "Usage: /createwallet base\nChains: ethereum, base, solana, arbitrum")
            return

        chain_str = parts[1].lower()
        chain_map = {
            "ethereum": SwapChain.ETHEREUM, "eth": SwapChain.ETHEREUM,
            "base": SwapChain.BASE,
            "solana": SwapChain.SOLANA, "sol": SwapChain.SOLANA,
            "arbitrum": SwapChain.ARBITRUM, "arb": SwapChain.ARBITRUM,
        }

        swap_chain = chain_map.get(chain_str)
        if not swap_chain:
            await self._send(chat_id, f"Chain inconnue: {chain_str}\nChains: ethereum, base, solana, arbitrum")
            return

        # Check if already exists
        existing = self.db.get_wallet(chat_id, swap_chain.value)
        if existing:
            await self._send(chat_id, (
                f"Tu as déjà un wallet {chain_str}:\n"
                f"<code>{existing['address']}</code>"
            ))
            return

        # Create wallet
        address = self.swap_router.create_wallet(chat_id, swap_chain)
        if not address:
            await self._send(chat_id, f"Impossible de créer le wallet (chain {chain_str} non configurée)")
            return

        # Persist
        wallet = self.swap_router.get_wallet(chat_id, swap_chain)
        if wallet:
            self.db.save_wallet(chat_id, swap_chain.value, wallet.address, wallet.encrypted_private_key)

        chain_emoji = {"ethereum": "⟠", "base": "🔵", "solana": "🟣", "arbitrum": "🔷"}.get(chain_str, "⚪")
        await self._send(chat_id, (
            f"{chain_emoji} <b>Wallet {chain_str.upper()} créé!</b>\n\n"
            f"Adresse:\n<code>{address}</code>\n\n"
            f"⚠️ Envoie de l'ETH/SOL à cette adresse pour pouvoir trader.\n"
            f"Le montant minimum par trade est $1."
        ))

    async def _cmd_trades(self, chat_id: str):
        trades = self.db.get_user_trades(chat_id, 10)
        if not trades:
            await self._send(chat_id, "📜 Aucun trade pour le moment.")
            return

        text = "📜 <b>Tes derniers trades</b>\n"
        for t in trades:
            emoji = "🟢" if t.get("success") else "🔴"
            text += (
                f"\n{emoji} {t['side']} {t.get('token_symbol', '?')} "
                f"({t['chain']})\n"
                f"  ${t.get('amount_in', 0):.4f} → TX: "
                f"<code>{t.get('tx_hash', '?')[:12]}...</code>\n"
            )

        await self._send(chat_id, text)

    async def _cmd_recent(self, chat_id: str):
        alerts = self.db.get_recent_alerts(chat_id, 5)
        if not alerts:
            await self._send(chat_id, "📡 Aucune alerte récente.")
            return

        text = "📡 <b>5 dernières alertes</b>\n"
        for a in alerts:
            score_emoji = "🟢" if a["score"] >= 60 else "🟠" if a["score"] >= 40 else "🔴"
            text += (
                f"\n{score_emoji} {a.get('token_symbol', '?')} "
                f"({a['chain']}) — Score: {a['score']:.0f}\n"
                f"  Liq: ${a.get('liquidity_usd', 0):,.0f} — "
                f"HP: {'🚫' if a.get('is_honeypot') else '✅'}\n"
            )

        await self._send(chat_id, text)

    async def _cmd_referral(self, chat_id: str):
        user = self.db.get_user(chat_id)
        if not user:
            await self._send(chat_id, "Envoie /start d'abord.")
            return

        code = user.get("referral_code", "")
        ref_stats = self.db.get_referral_stats(chat_id)
        bot_username = ""  # TODO: fetch from Telegram API

        await self._send(chat_id, (
            f"🎁 <b>Parrainage</b>\n\n"
            f"Ton code: <code>{code}</code>\n"
            f"Lien: <code>https://t.me/NexusGemHunterBot?start={code}</code>\n\n"
            f"📊 Filleuls: {ref_stats.get('referrals', 0)}\n"
            f"💰 Rebates gagnés: ${ref_stats.get('total_rebate', 0) or 0:.2f}\n\n"
            f"Chaque filleul te rapporte 10% de ses fees!"
        ))

    # ── Override: handle callbacks avec swap intégré ──

    async def _handle_callback(self, callback: dict):
        """Override avec buy intégré."""
        chat_id = str(callback.get("message", {}).get("chat", {}).get("id", ""))
        data = callback.get("data", "")
        callback_id = callback.get("id", "")

        user = self.db.get_user(chat_id)
        if not user:
            await self._answer_callback(callback_id, "Envoie /start d'abord")
            return

        # ── Toggle chains ──
        chain_toggles = {"toggle_eth": "ethereum", "toggle_base": "base", "toggle_sol": "solana"}
        if data in chain_toggles:
            chain = chain_toggles[data]
            chains = user.get("chains", [])
            if chain in chains:
                chains.remove(chain)
            else:
                chains.append(chain)
            self.db.update_user_setting(chat_id, "chains", chains)
            # Update local cache
            if chat_id in self.users:
                self.users[chat_id].chains = chains
            await self._answer_callback(callback_id, f"{chain} {'activé' if chain in chains else 'désactivé'}")
            return

        # ── Settings hints ──
        if data == "set_score":
            await self._send(chat_id, "Envoie /setscore <valeur>\nEx: /setscore 60")
            await self._answer_callback(callback_id, "")
            return

        if data == "set_bet":
            await self._send(chat_id, "Envoie /setbet <montant>\nEx: /setbet 25")
            await self._answer_callback(callback_id, "")
            return

        # ── BUY BUTTON ──
        if data.startswith("buy_"):
            pool_key = data[4:]
            await self._execute_buy(chat_id, pool_key, callback_id)
            return

        # ── Details ──
        if data.startswith("details_"):
            pool_key = data[8:]
            pool = self._recent_pools.get(pool_key)
            if pool:
                flags = "\n".join(f"  {'✅' if f in pool.green_flags else '⚠️'} {f}" for f in pool.green_flags + pool.red_flags)
                await self._send(chat_id, (
                    f"🔍 <b>Détails — {pool.target_symbol}</b>\n\n"
                    f"Contract: <code>{pool.target_token}</code>\n"
                    f"Pool: <code>{pool.pool_address}</code>\n"
                    f"Chain: {pool.chain.value}\n"
                    f"DEX: {pool.dex}\n"
                    f"Block: {pool.block_number}\n\n"
                    f"Score: {pool.score:.0f}/100\n"
                    f"Honeypot: {pool.is_honeypot}\n"
                    f"Buy tax: {pool.buy_tax_pct:.1f}%\n"
                    f"Sell tax: {pool.sell_tax_pct:.1f}%\n"
                    f"Liquidity: ${pool.initial_liquidity_usd:,.0f}\n\n"
                    f"<b>Flags:</b>\n{flags}"
                ))
            else:
                await self._send(chat_id, "Pool expiré du cache.")
            await self._answer_callback(callback_id, "")
            return

        # ── Ignore ──
        if data.startswith("ignore_"):
            await self._answer_callback(callback_id, "Ignoré")
            return

    async def _execute_buy(self, chat_id: str, pool_key: str, callback_id: str):
        """Exécute un buy quand l'user clique le bouton."""
        pool = self._recent_pools.get(pool_key)
        if not pool:
            await self._answer_callback(callback_id, "Pool expiré")
            await self._send(chat_id, "⏰ Ce pool a expiré du cache. Attends la prochaine alerte.")
            return

        user = self.db.get_user(chat_id)
        if not user:
            await self._answer_callback(callback_id, "Erreur")
            return

        swap_chain = CHAIN_TO_SWAP.get(pool.chain)
        if not swap_chain:
            await self._answer_callback(callback_id, "Chain non supportée")
            return

        # Check wallet
        wallet = self.db.get_wallet(chat_id, swap_chain.value)
        if not wallet:
            await self._answer_callback(callback_id, "Pas de wallet")
            await self._send(chat_id, (
                f"❌ Tu n'as pas de wallet {pool.chain.value}.\n"
                f"Crée-en un: /createwallet {pool.chain.value}"
            ))
            return

        # Load wallet into swap router
        from swap_executor import UserWallet
        if chat_id not in self.swap_router.wallets:
            self.swap_router.wallets[chat_id] = {}
        self.swap_router.wallets[chat_id][swap_chain] = UserWallet(
            user_id=chat_id,
            chain=swap_chain,
            address=wallet["address"],
            encrypted_private_key=wallet["encrypted_private_key"],
        )

        bet_size = user.get("bet_size_usd", 10.0)

        await self._answer_callback(callback_id, "Exécution en cours...")
        await self._send(chat_id, (
            f"⏳ <b>Exécution du swap...</b>\n\n"
            f"Token: {pool.target_symbol}\n"
            f"Chain: {pool.chain.value}\n"
            f"Montant: ${bet_size}\n"
            f"Slippage max: 5%"
        ))

        # Execute swap
        request = SwapRequest(
            user_id=chat_id,
            chain=swap_chain,
            token_address=pool.target_token,
            amount_in_usd=bet_size,
            pool_address=pool.pool_address,
            slippage_pct=5.0,
        )

        result = await self.swap_router.swap(request)

        # Log trade in DB
        self.db.log_trade({
            "chat_id": chat_id,
            "chain": swap_chain.value,
            "token_address": pool.target_token,
            "token_symbol": pool.target_symbol,
            "side": "BUY",
            "amount_in": result.amount_in,
            "amount_out": result.amount_out,
            "price_per_token": result.price_per_token,
            "fee_usd": result.fee_usd,
            "gas_cost_usd": result.gas_cost_usd,
            "tx_hash": result.tx_hash,
            "success": 1 if result.success else 0,
            "error": result.error,
            "pool_score": pool.score,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        if result.success:
            self.db.increment_user_stat(chat_id, "total_trades", 1)
            await self._send(chat_id, (
                f"🟢 <b>Swap réussi!</b>\n\n"
                f"Token: {pool.target_symbol}\n"
                f"Dépensé: {result.amount_in:.6f} ETH/SOL\n"
                f"Reçu: {result.amount_out:.4f} tokens\n"
                f"Fee: ${result.fee_usd:.2f}\n"
                f"Gas: ${result.gas_cost_usd:.2f}\n\n"
                f"🔗 <a href='{result.explorer_url}'>Voir la transaction</a>"
            ))
        else:
            await self._send(chat_id, (
                f"🔴 <b>Swap échoué</b>\n\n"
                f"Erreur: {result.error}\n\n"
                f"Vérifie que ton wallet a assez d'ETH/SOL."
            ))

    # ── Override: broadcast avec persistence ──

    async def broadcast_alert(self, pool: NewPool):
        """Override avec persistence DB et cache pool."""
        if not self.enabled:
            return

        self.cache_pool(pool)

        # Broadcast à tous les users de la DB
        all_users = self.db.get_all_users()

        for user in all_users:
            chat_id = user["chat_id"]
            chains = user.get("chains", [])

            # Filtres
            if pool.chain.value not in chains:
                continue
            if pool.score < user.get("min_score", 50):
                continue
            if pool.initial_liquidity_usd < user.get("min_liquidity", 5000):
                continue
            if pool.buy_tax_pct > user.get("max_buy_tax", 10):
                continue
            if pool.sell_tax_pct > user.get("max_sell_tax", 10):
                continue

            # Envoie l'alerte
            await self._send_pool_alert(chat_id, pool)

            # Log
            self.db.increment_user_stat(chat_id, "total_alerts", 1)
            self.db.log_alert({
                "chat_id": chat_id,
                "chain": pool.chain.value,
                "token_address": pool.target_token,
                "token_symbol": pool.target_symbol,
                "pool_address": pool.pool_address,
                "score": pool.score,
                "liquidity_usd": pool.initial_liquidity_usd,
                "is_honeypot": 1 if pool.is_honeypot else 0,
                "action_taken": "alerted",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

    # ── Load users from DB on startup ──

    async def load_users_from_db(self):
        """Charge les users de la DB dans le cache mémoire."""
        from pool_listener import UserConfig
        all_users = self.db.get_all_users()
        for u in all_users:
            self.users[u["chat_id"]] = UserConfig(
                chat_id=u["chat_id"],
                username=u.get("username", ""),
                min_score=u.get("min_score", 50),
                chains=u.get("chains", ["ethereum", "base", "solana"]),
                min_liquidity=u.get("min_liquidity", 5000),
                max_buy_tax=u.get("max_buy_tax", 10),
                max_sell_tax=u.get("max_sell_tax", 10),
                registered_at=u.get("registered_at", ""),
            )
        log.info(f"📱 {len(self.users)} users chargés depuis la DB")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# INTEGRATED ORCHESTRATOR
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class GemHunterApp:
    """
    Application principale — relie tout.
    
    Components:
    - UserDB          → persistence
    - SwapRouter      → exécution des trades
    - IntegratedBot   → Telegram multi-users
    - TokenAnalyzer   → analyse des tokens
    - Pool Listeners  → détection temps réel
    - FastAPI         → dashboard admin (optionnel)
    """

    def __init__(self):
        # Core
        self.db = UserDB()
        self.swap_router = SwapRouter()
        self.analyzer = TokenAnalyzer()
        self.enricher = ScoreEnricher()

        # Telegram bot intégré
        self.telegram = IntegratedTelegramBot(
            os.getenv("TELEGRAM_BOT_TOKEN", ""),
            self.db,
            self.swap_router,
        )

        # Pool listeners
        self.listeners: list = []

        # Stats
        self.stats = {
            "pools_detected": 0,
            "pools_analyzed": 0,
            "alerts_sent": 0,
            "honeypots_caught": 0,
            "start_time": None,
        }

        self.min_alert_score = int(os.getenv("MIN_SCORE", "50"))
        self.recent_pools: list[NewPool] = []

    async def on_new_pool(self, pool: NewPool):
        """Callback quand un nouveau pool est détecté."""
        self.stats["pools_detected"] += 1
        detect_time = time.time()

        log.info(
            f"🔍 Analyse — {pool.chain.value}/{pool.dex} "
            f"— Token: {pool.target_token[:16]}..."
        )

        # Analyse de base (honeypot, taxes, liquidité)
        pool = await self.analyzer.analyze(pool)
        self.stats["pools_analyzed"] += 1

        # Enrichissement (trending, smart money, market context)
        pool = await self.enricher.enrich(pool)

        analysis_time = time.time() - detect_time

        if pool.is_honeypot:
            self.stats["honeypots_caught"] += 1

        score_emoji = "🟢" if pool.score >= 60 else "🟠" if pool.score >= 40 else "🔴"
        log.info(
            f"{score_emoji} [{pool.chain.value}] {pool.target_symbol} — "
            f"Score: {pool.score:.0f} — Liq: ${pool.initial_liquidity_usd:,.0f} — "
            f"HP: {pool.is_honeypot} — {analysis_time:.1f}s"
        )

        self.recent_pools.append(pool)
        if len(self.recent_pools) > 100:
            self.recent_pools = self.recent_pools[-100:]

        # Broadcast
        if pool.score >= self.min_alert_score and not pool.is_honeypot:
            await self.telegram.broadcast_alert(pool)
            self.stats["alerts_sent"] += 1

    async def start(self):
        """Lance tout."""
        self.stats["start_time"] = datetime.now(timezone.utc).isoformat()

        # Load users from DB
        await self.telegram.load_users_from_db()

        tasks = []

        # ── EVM Listeners ──
        evm_configs = {
            Chain.ETHEREUM: os.getenv("ETH_WS_RPC", ""),
            Chain.BASE: os.getenv("BASE_WS_RPC", ""),
            Chain.ARBITRUM: os.getenv("ARB_WS_RPC", ""),
        }

        for chain, ws_rpc in evm_configs.items():
            if ws_rpc:
                listener = EVMPoolListener(chain, ws_rpc, self.on_new_pool)
                self.listeners.append(listener)
                tasks.append(listener.start())
                log.info(f"✅ [{chain.value}] Listener ready")

        # ── Solana Listener ──
        sol_ws = os.getenv("SOLANA_WS_RPC", "")
        sol_http = os.getenv("SOLANA_HTTP_RPC", "")
        if sol_ws and sol_http:
            sol_listener = SolanaPoolListener(sol_ws, sol_http, self.on_new_pool)
            self.listeners.append(sol_listener)
            tasks.append(sol_listener.start())
            log.info("✅ [solana] Listener ready")

        # ── Telegram ──
        tasks.append(self.telegram.start_polling())

        # ── Admin API ──
        if HAS_FASTAPI:
            tasks.append(self._start_admin_api())

        # ── Stats loop ──
        tasks.append(self._stats_loop())

        if not self.listeners:
            log.warning("⚠️  Aucun listener configuré — configure tes RPC dans .env")

        log.info(
            f"\n"
            f"╔══════════════════════════════════════════════════════════════╗\n"
            f"║  💎 NEXUS GEMHUNTER — LIVE                                  ║\n"
            f"║                                                              ║\n"
            f"║  Listeners: {len(self.listeners)} chains                               ║\n"
            f"║  Telegram:  {'ON' if self.telegram.enabled else 'OFF':3s}                                         ║\n"
            f"║  Swap:      {len(self.swap_router.executors)} chains                               ║\n"
            f"║  Users:     {len(self.telegram.users)}                                            ║\n"
            f"║  Admin API: {'http://localhost:8081' if HAS_FASTAPI else 'OFF':25s}        ║\n"
            f"╚══════════════════════════════════════════════════════════════╝"
        )

        await asyncio.gather(*tasks, return_exceptions=True)

    async def _start_admin_api(self):
        """API REST admin pour le dashboard."""
        app = FastAPI(title="GemHunter Admin", version="1.0.0")
        app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

        @app.get("/")
        async def root():
            return {"status": "ok", "name": "Nexus GemHunter"}

        @app.get("/api/stats")
        async def get_stats():
            return {
                **self.stats,
                "users": self.db.get_dashboard_stats(),
                "swap": self.swap_router.get_stats(),
                "recent_pools": [
                    {
                        "chain": p.chain.value,
                        "symbol": p.target_symbol,
                        "score": p.score,
                        "liquidity": p.initial_liquidity_usd,
                        "honeypot": p.is_honeypot,
                        "timestamp": p.timestamp,
                    }
                    for p in self.recent_pools[-20:]
                ],
            }

        @app.get("/api/users")
        async def get_users():
            return self.db.get_all_users()

        @app.get("/api/trades")
        async def get_trades():
            return self.db.get_global_trade_stats()

        config = uvicorn.Config(app, host="0.0.0.0", port=8081, log_level="warning")
        server = uvicorn.Server(config)
        await server.serve()

    async def _stats_loop(self):
        while True:
            await asyncio.sleep(300)
            db_stats = self.db.get_dashboard_stats()
            log.info(
                f"📊 Pools: {self.stats['pools_detected']} det / "
                f"{self.stats['alerts_sent']} alerts / "
                f"{self.stats['honeypots_caught']} HP caught — "
                f"Users: {db_stats['total_users']} ({db_stats['active_24h']} active 24h) — "
                f"Fees: ${db_stats['total_fees_usd']:.2f}"
            )

    async def stop(self):
        for listener in self.listeners:
            await listener.stop()
        await self.analyzer.close()
        await self.enricher.close()
        await self.telegram.stop()
        await self.swap_router.close()
        self.db.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ENTRY POINT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s │ %(levelname)-7s │ %(message)s",
        datefmt="%H:%M:%S",
    )

    print("""
    ╔══════════════════════════════════════════════════════════════╗
    ║  💎 NEXUS GEMHUNTER                                          ║
    ║                                                              ║
    ║  Real-time pool detection + 1-click buy + Telegram bot       ║
    ║                                                              ║
    ║  Chains: Ethereum, Base, Solana                              ║
    ║  Income: 0.8% fee on every trade                             ║
    ║                                                              ║
    ║  python gemhunter_main.py                                    ║
    ╚══════════════════════════════════════════════════════════════╝
    """)

    app = GemHunterApp()

    try:
        await app.start()
    except KeyboardInterrupt:
        log.info("Arrêt...")
    finally:
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
