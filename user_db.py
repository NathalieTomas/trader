"""
╔══════════════════════════════════════════════════════════════════════╗
║  NEXUS GEMHUNTER — User Database                                    ║
║  Persistence SQLite des users, wallets, trades, referrals           ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("nexus.user_db")


class UserDB:
    """
    Base de données SQLite pour le bot multi-utilisateurs.
    
    Tables:
    - users          — profils et settings des utilisateurs Telegram
    - wallets        — wallets chiffrés par user/chain
    - trades         — historique des trades (buy/sell)
    - referrals      — système de parrainage
    - alerts_log     — historique des alertes envoyées
    """

    def __init__(self, db_path: str = "gemhunter_users.db"):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._create_tables()
        log.info(f"📦 UserDB initialisée: {db_path}")

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id TEXT PRIMARY KEY,
                username TEXT DEFAULT '',
                -- Settings
                min_score INTEGER DEFAULT 50,
                chains TEXT DEFAULT '["ethereum","base","solana"]',
                min_liquidity REAL DEFAULT 5000,
                max_buy_tax REAL DEFAULT 10.0,
                max_sell_tax REAL DEFAULT 10.0,
                bet_size_usd REAL DEFAULT 10.0,
                auto_buy INTEGER DEFAULT 0,
                -- Account
                is_premium INTEGER DEFAULT 0,
                premium_until TEXT DEFAULT '',
                referral_code TEXT DEFAULT '',
                referred_by TEXT DEFAULT '',
                -- Stats
                total_alerts INTEGER DEFAULT 0,
                total_trades INTEGER DEFAULT 0,
                total_pnl_usd REAL DEFAULT 0.0,
                -- Meta
                registered_at TEXT NOT NULL,
                last_active TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS wallets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                chain TEXT NOT NULL,
                address TEXT NOT NULL,
                encrypted_private_key TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (chat_id) REFERENCES users(chat_id),
                UNIQUE(chat_id, chain)
            );

            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                chain TEXT NOT NULL,
                token_address TEXT NOT NULL,
                token_symbol TEXT DEFAULT '?',
                side TEXT NOT NULL,
                amount_in REAL NOT NULL,
                amount_out REAL DEFAULT 0,
                price_per_token REAL DEFAULT 0,
                fee_usd REAL DEFAULT 0,
                gas_cost_usd REAL DEFAULT 0,
                tx_hash TEXT DEFAULT '',
                success INTEGER DEFAULT 1,
                error TEXT DEFAULT '',
                pool_score REAL DEFAULT 0,
                timestamp TEXT NOT NULL,
                FOREIGN KEY (chat_id) REFERENCES users(chat_id)
            );

            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_chat_id TEXT NOT NULL,
                referred_chat_id TEXT NOT NULL,
                referral_code TEXT NOT NULL,
                fee_rebate_pct REAL DEFAULT 10.0,
                total_rebate_usd REAL DEFAULT 0.0,
                created_at TEXT NOT NULL,
                FOREIGN KEY (referrer_chat_id) REFERENCES users(chat_id),
                FOREIGN KEY (referred_chat_id) REFERENCES users(chat_id)
            );

            CREATE TABLE IF NOT EXISTS alerts_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                chain TEXT NOT NULL,
                token_address TEXT NOT NULL,
                token_symbol TEXT DEFAULT '?',
                pool_address TEXT DEFAULT '',
                score REAL DEFAULT 0,
                liquidity_usd REAL DEFAULT 0,
                is_honeypot INTEGER DEFAULT 0,
                action_taken TEXT DEFAULT 'none',
                timestamp TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_trades_user ON trades(chat_id);
            CREATE INDEX IF NOT EXISTS idx_trades_time ON trades(timestamp);
            CREATE INDEX IF NOT EXISTS idx_alerts_user ON alerts_log(chat_id);
            CREATE INDEX IF NOT EXISTS idx_alerts_time ON alerts_log(timestamp);
        """)
        self.conn.commit()

    # ── Users ──

    def register_user(self, chat_id: str, username: str = "") -> bool:
        """Inscrit un nouvel utilisateur. Retourne True si nouveau."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            self.conn.execute(
                "INSERT INTO users (chat_id, username, registered_at, last_active) "
                "VALUES (?, ?, ?, ?)",
                (chat_id, username, now, now)
            )
            self.conn.commit()

            import secrets
            code = secrets.token_hex(4).upper()
            self.conn.execute(
                "UPDATE users SET referral_code = ? WHERE chat_id = ?",
                (code, chat_id)
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def get_user(self, chat_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM users WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if row:
            d = dict(row)
            d["chains"] = json.loads(d.get("chains", "[]"))
            return d
        return None

    def update_user_setting(self, chat_id: str, key: str, value) -> bool:
        allowed = {
            "min_score", "chains", "min_liquidity", "max_buy_tax",
            "max_sell_tax", "bet_size_usd", "auto_buy", "is_premium",
            "premium_until", "username",
        }
        if key not in allowed:
            return False
        if key == "chains":
            value = json.dumps(value)
        self.conn.execute(
            f"UPDATE users SET {key} = ?, last_active = ? WHERE chat_id = ?",
            (value, datetime.now(timezone.utc).isoformat(), chat_id)
        )
        self.conn.commit()
        return True

    def touch_user(self, chat_id: str):
        self.conn.execute(
            "UPDATE users SET last_active = ? WHERE chat_id = ?",
            (datetime.now(timezone.utc).isoformat(), chat_id)
        )
        self.conn.commit()

    def increment_user_stat(self, chat_id: str, field: str, amount: float = 1):
        allowed = {"total_alerts", "total_trades", "total_pnl_usd"}
        if field not in allowed:
            return
        self.conn.execute(
            f"UPDATE users SET {field} = {field} + ? WHERE chat_id = ?",
            (amount, chat_id)
        )
        self.conn.commit()

    def get_user_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) as c FROM users").fetchone()
        return row["c"] if row else 0

    def get_all_users(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM users").fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["chains"] = json.loads(d.get("chains", "[]"))
            result.append(d)
        return result

    # ── Wallets ──

    def save_wallet(self, chat_id: str, chain: str, address: str, encrypted_pk: str):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT OR REPLACE INTO wallets (chat_id, chain, address, encrypted_private_key, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (chat_id, chain, address, encrypted_pk, now)
        )
        self.conn.commit()

    def get_wallet(self, chat_id: str, chain: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM wallets WHERE chat_id = ? AND chain = ?",
            (chat_id, chain)
        ).fetchone()
        return dict(row) if row else None

    def get_user_wallets(self, chat_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM wallets WHERE chat_id = ?", (chat_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Trades ──

    def log_trade(self, trade: dict):
        self.conn.execute(
            "INSERT INTO trades "
            "(chat_id, chain, token_address, token_symbol, side, amount_in, "
            "amount_out, price_per_token, fee_usd, gas_cost_usd, tx_hash, "
            "success, error, pool_score, timestamp) "
            "VALUES (:chat_id, :chain, :token_address, :token_symbol, :side, "
            ":amount_in, :amount_out, :price_per_token, :fee_usd, :gas_cost_usd, "
            ":tx_hash, :success, :error, :pool_score, :timestamp)",
            trade
        )
        self.conn.commit()

    def get_user_trades(self, chat_id: str, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM trades WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_global_trade_stats(self) -> dict:
        row = self.conn.execute("""
            SELECT 
                COUNT(*) as total_trades,
                SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as successful,
                SUM(fee_usd) as total_fees,
                SUM(amount_in) as total_volume
            FROM trades
        """).fetchone()
        return dict(row) if row else {}

    # ── Referrals ──

    def create_referral(self, referrer_id: str, referred_id: str, code: str):
        now = datetime.now(timezone.utc).isoformat()
        try:
            self.conn.execute(
                "INSERT INTO referrals (referrer_chat_id, referred_chat_id, referral_code, created_at) "
                "VALUES (?, ?, ?, ?)",
                (referrer_id, referred_id, code, now)
            )
            self.conn.execute(
                "UPDATE users SET referred_by = ? WHERE chat_id = ?",
                (referrer_id, referred_id)
            )
            self.conn.commit()
        except sqlite3.IntegrityError:
            pass

    def get_user_by_referral_code(self, code: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM users WHERE referral_code = ?", (code,)
        ).fetchone()
        return dict(row) if row else None

    def get_referral_stats(self, chat_id: str) -> dict:
        row = self.conn.execute(
            "SELECT COUNT(*) as referrals, SUM(total_rebate_usd) as total_rebate "
            "FROM referrals WHERE referrer_chat_id = ?",
            (chat_id,)
        ).fetchone()
        return dict(row) if row else {"referrals": 0, "total_rebate": 0}

    # ── Alerts Log ──

    def log_alert(self, alert: dict):
        self.conn.execute(
            "INSERT INTO alerts_log "
            "(chat_id, chain, token_address, token_symbol, pool_address, "
            "score, liquidity_usd, is_honeypot, action_taken, timestamp) "
            "VALUES (:chat_id, :chain, :token_address, :token_symbol, "
            ":pool_address, :score, :liquidity_usd, :is_honeypot, "
            ":action_taken, :timestamp)",
            alert
        )
        self.conn.commit()

    def get_recent_alerts(self, chat_id: str, limit: int = 5) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM alerts_log WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Admin ──

    def get_dashboard_stats(self) -> dict:
        users = self.get_user_count()
        trades = self.get_global_trade_stats()
        
        active_24h = self.conn.execute(
            "SELECT COUNT(*) as c FROM users WHERE last_active > datetime('now', '-1 day')"
        ).fetchone()
        
        premium = self.conn.execute(
            "SELECT COUNT(*) as c FROM users WHERE is_premium = 1"
        ).fetchone()
        
        return {
            "total_users": users,
            "active_24h": active_24h["c"] if active_24h else 0,
            "premium_users": premium["c"] if premium else 0,
            "total_trades": trades.get("total_trades", 0),
            "successful_trades": trades.get("successful", 0),
            "total_fees_usd": trades.get("total_fees", 0) or 0,
            "total_volume_usd": trades.get("total_volume", 0) or 0,
        }

    def close(self):
        self.conn.close()
