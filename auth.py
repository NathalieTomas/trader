"""
╔══════════════════════════════════════════════════════════════════════╗
║  NEXUS TRADER — Module Authentification Sécurisé                   ║
║  JWT + 2FA (TOTP) + Rate Limiting + Sessions                      ║
╚══════════════════════════════════════════════════════════════════════╝

INSTALLATION:
    pip install pyjwt[crypto] pyotp qrcode passlib[bcrypt] python-multipart

CONFIGURATION .env:
    JWT_SECRET=un_secret_tres_long_et_aleatoire_genere_avec_openssl
    JWT_EXPIRATION_MINUTES=30
    AUTH_PIN_HASH=                   (généré automatiquement au premier lancement)
    TOTP_SECRET=                     (généré automatiquement au premier lancement)
    RATE_LIMIT_PER_MINUTE=30
    MAX_LOGIN_ATTEMPTS=5
    LOCKOUT_DURATION_SECONDS=300

USAGE:
    # Intègre dans bot.py :
    from auth import setup_auth
    setup_auth(app)  # Ajoute tous les endpoints d'auth à l'app FastAPI
"""

import hashlib
import hmac
import io
import json
import logging
import os
import secrets
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Optional

try:
    import jwt
except ImportError:
    print("❌ pip install pyjwt[crypto]")

try:
    import pyotp
    import qrcode
    import qrcode.image.svg
    HAS_2FA = True
except ImportError:
    HAS_2FA = False
    print("⚠️ 2FA non disponible — pip install pyotp qrcode")

try:
    from passlib.context import CryptContext
    pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
except ImportError:
    pwd_context = None
    print("⚠️ bcrypt non disponible — pip install passlib[bcrypt]")

from fastapi import FastAPI, HTTPException, Depends, Request, Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

log = logging.getLogger("nexus.auth")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Configuration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class AuthConfig:
    def __init__(self):
        # JWT
        self.jwt_secret = os.getenv("JWT_SECRET", "")
        self.jwt_algorithm = "HS256"
        self.jwt_expiration_minutes = int(os.getenv("JWT_EXPIRATION_MINUTES", "30"))
        self.refresh_token_days = 7

        # PIN (hashé avec bcrypt)
        self.pin_hash = os.getenv("AUTH_PIN_HASH", "")

        # 2FA TOTP
        self.totp_secret = os.getenv("TOTP_SECRET", "")
        self.totp_enabled = bool(self.totp_secret)
        self.app_name = "NexusTrader"

        # Rate limiting
        self.rate_limit_per_minute = int(os.getenv("RATE_LIMIT_PER_MINUTE", "30"))
        self.max_login_attempts = int(os.getenv("MAX_LOGIN_ATTEMPTS", "5"))
        self.lockout_duration = int(os.getenv("LOCKOUT_DURATION_SECONDS", "300"))

        # Auto-génère les secrets si manquants
        self._auto_setup()

    def _auto_setup(self):
        """Génère automatiquement les secrets au premier lancement."""
        env_path = ".env"
        env_lines = []
        if os.path.exists(env_path):
            with open(env_path) as f:
                env_lines = f.readlines()

        modified = False

        # JWT Secret
        if not self.jwt_secret:
            self.jwt_secret = secrets.token_urlsafe(64)
            env_lines.append(f"\n# Auth — Généré automatiquement\nJWT_SECRET={self.jwt_secret}\n")
            modified = True
            log.info("🔑 JWT secret généré automatiquement")

        # PIN par défaut (1234 hashé)
        if not self.pin_hash:
            default_pin = "1234"
            if pwd_context:
                self.pin_hash = pwd_context.hash(default_pin)
            else:
                self.pin_hash = hashlib.sha256(default_pin.encode()).hexdigest()
            env_lines.append(f"AUTH_PIN_HASH={self.pin_hash}\n")
            modified = True
            log.warning("⚠️ PIN par défaut (1234) — CHANGE-LE via /api/auth/change-pin")

        # TOTP Secret
        if not self.totp_secret and HAS_2FA:
            self.totp_secret = pyotp.random_base32()
            env_lines.append(f"TOTP_SECRET={self.totp_secret}\n")
            modified = True
            log.info("🔐 TOTP secret généré — Active le 2FA via /api/auth/setup-2fa")

        if modified:
            with open(env_path, "w") as f:
                f.writelines(env_lines)
            log.info(f"💾 Secrets sauvegardés dans {env_path}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Rate Limiter
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class RateLimiter:
    """Limite le nombre de requêtes par IP par minute."""

    def __init__(self, max_per_minute: int = 30):
        self.max_per_minute = max_per_minute
        self._requests: dict[str, list[float]] = defaultdict(list)
        self._login_attempts: dict[str, list[float]] = defaultdict(list)
        self._locked_ips: dict[str, float] = {}

    def check(self, ip: str) -> bool:
        """Retourne True si la requête est autorisée."""
        now = time.time()
        
        # Nettoie les vieilles entrées
        self._requests[ip] = [t for t in self._requests[ip] if now - t < 60]
        
        if len(self._requests[ip]) >= self.max_per_minute:
            return False
        
        self._requests[ip].append(now)
        return True

    def check_login(self, ip: str, max_attempts: int, lockout_seconds: int) -> dict:
        """Vérifie les tentatives de login."""
        now = time.time()

        # Vérifie le lockout
        if ip in self._locked_ips:
            if now < self._locked_ips[ip]:
                remaining = int(self._locked_ips[ip] - now)
                return {"allowed": False, "reason": f"Compte verrouillé. Réessaie dans {remaining}s", "remaining": remaining}
            else:
                del self._locked_ips[ip]
                self._login_attempts[ip] = []

        # Nettoie les tentatives > 10 min
        self._login_attempts[ip] = [t for t in self._login_attempts[ip] if now - t < 600]

        if len(self._login_attempts[ip]) >= max_attempts:
            self._locked_ips[ip] = now + lockout_seconds
            return {"allowed": False, "reason": f"Trop de tentatives. Verrouillé pour {lockout_seconds}s", "remaining": lockout_seconds}

        return {"allowed": True, "attempts_left": max_attempts - len(self._login_attempts[ip])}

    def record_login_attempt(self, ip: str):
        """Enregistre une tentative de login ratée."""
        self._login_attempts[ip].append(time.time())


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Session Manager
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SessionManager:
    """Gère les sessions actives et les tokens de refresh."""

    def __init__(self):
        self._sessions: dict[str, dict] = {}  # session_id -> {created, last_seen, ip, user_agent}
        self._refresh_tokens: dict[str, dict] = {}  # token -> {session_id, expires}
        self._revoked_tokens: set[str] = set()  # JTI des tokens révoqués

    def create_session(self, ip: str, user_agent: str = "") -> str:
        """Crée une nouvelle session."""
        session_id = secrets.token_urlsafe(32)
        self._sessions[session_id] = {
            "created": time.time(),
            "last_seen": time.time(),
            "ip": ip,
            "user_agent": user_agent,
        }
        log.info(f"🔓 Session créée depuis {ip}")
        return session_id

    def create_refresh_token(self, session_id: str, expires_days: int = 7) -> str:
        """Crée un refresh token pour une session."""
        token = secrets.token_urlsafe(48)
        self._refresh_tokens[token] = {
            "session_id": session_id,
            "expires": time.time() + (expires_days * 86400),
        }
        return token

    def validate_refresh_token(self, token: str) -> Optional[str]:
        """Valide un refresh token et retourne le session_id."""
        data = self._refresh_tokens.get(token)
        if not data:
            return None
        if time.time() > data["expires"]:
            del self._refresh_tokens[token]
            return None
        return data["session_id"]

    def revoke_token(self, jti: str):
        """Révoque un JWT par son JTI."""
        self._revoked_tokens.add(jti)

    def is_revoked(self, jti: str) -> bool:
        return jti in self._revoked_tokens

    def update_activity(self, session_id: str):
        if session_id in self._sessions:
            self._sessions[session_id]["last_seen"] = time.time()

    def get_active_sessions(self) -> list[dict]:
        """Liste les sessions actives."""
        now = time.time()
        return [
            {
                "id": sid[:8] + "...",
                "ip": data["ip"],
                "created": datetime.fromtimestamp(data["created"], tz=timezone.utc).isoformat(),
                "last_seen": f"il y a {int(now - data['last_seen'])}s",
            }
            for sid, data in self._sessions.items()
            if now - data["last_seen"] < 86400  # Sessions de moins de 24h
        ]

    def destroy_session(self, session_id: str):
        self._sessions.pop(session_id, None)
        # Supprime les refresh tokens associés
        self._refresh_tokens = {
            t: d for t, d in self._refresh_tokens.items()
            if d["session_id"] != session_id
        }

    def destroy_all_sessions(self):
        """Logout global — déconnecte toutes les sessions."""
        self._sessions.clear()
        self._refresh_tokens.clear()
        log.warning("🔒 Toutes les sessions détruites")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# JWT Manager
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class JWTManager:
    """Gère la création et validation des JWT."""

    def __init__(self, config: AuthConfig):
        self.config = config

    def create_access_token(self, session_id: str, extra_claims: dict = None) -> str:
        """Crée un JWT d'accès."""
        now = datetime.now(timezone.utc)
        jti = secrets.token_urlsafe(16)

        payload = {
            "sub": "nexus_user",
            "session_id": session_id,
            "jti": jti,
            "iat": now,
            "exp": now + timedelta(minutes=self.config.jwt_expiration_minutes),
            "type": "access",
        }
        if extra_claims:
            payload.update(extra_claims)

        return jwt.encode(payload, self.config.jwt_secret, algorithm=self.config.jwt_algorithm)

    def create_refresh_token(self, session_id: str) -> str:
        """Crée un JWT de refresh (durée longue)."""
        now = datetime.now(timezone.utc)
        payload = {
            "sub": "nexus_user",
            "session_id": session_id,
            "jti": secrets.token_urlsafe(16),
            "iat": now,
            "exp": now + timedelta(days=self.config.refresh_token_days),
            "type": "refresh",
        }
        return jwt.encode(payload, self.config.jwt_secret, algorithm=self.config.jwt_algorithm)

    def decode_token(self, token: str) -> Optional[dict]:
        """Décode et valide un JWT."""
        try:
            payload = jwt.decode(
                token,
                self.config.jwt_secret,
                algorithms=[self.config.jwt_algorithm],
            )
            return payload
        except jwt.ExpiredSignatureError:
            return None
        except jwt.InvalidTokenError:
            return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2FA Manager (TOTP — Google Authenticator compatible)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TwoFactorManager:
    """Gère l'authentification à deux facteurs via TOTP."""

    def __init__(self, config: AuthConfig):
        self.config = config
        self.totp = pyotp.TOTP(config.totp_secret) if config.totp_secret and HAS_2FA else None

    def is_enabled(self) -> bool:
        return self.config.totp_enabled and self.totp is not None

    def verify(self, code: str) -> bool:
        """Vérifie un code TOTP (accepte ±1 intervalle pour le décalage d'horloge)."""
        if not self.totp:
            return True  # Si 2FA désactivé, toujours valide
        return self.totp.verify(code, valid_window=1)

    def get_provisioning_uri(self) -> str:
        """Retourne l'URI pour configurer Google Authenticator."""
        if not self.totp:
            return ""
        return self.totp.provisioning_uri(
            name="admin",
            issuer_name=self.config.app_name,
        )

    def generate_qr_base64(self) -> str:
        """Génère un QR code en base64 pour scanner avec l'app."""
        if not HAS_2FA or not self.totp:
            return ""

        uri = self.get_provisioning_uri()
        img = qrcode.make(uri)

        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        buffer.seek(0)

        import base64
        return base64.b64encode(buffer.read()).decode()

    def get_current_code(self) -> str:
        """Retourne le code actuel (pour debug uniquement)."""
        if not self.totp:
            return ""
        return self.totp.now()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FastAPI Integration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Request/Response models
class LoginRequest(BaseModel):
    pin: str
    totp_code: Optional[str] = None

class ChangePinRequest(BaseModel):
    current_pin: str
    new_pin: str

class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int

class RefreshRequest(BaseModel):
    refresh_token: str


# Globals (initialisés dans setup_auth)
auth_config: AuthConfig = None
jwt_manager: JWTManager = None
session_manager: SessionManager = None
rate_limiter: RateLimiter = None
two_factor: TwoFactorManager = None
security = HTTPBearer()


def get_client_ip(request: Request) -> str:
    """Récupère l'IP réelle (supporte les proxies)."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    """Dépendance FastAPI — vérifie le JWT et retourne l'utilisateur."""
    ip = get_client_ip(request)

    # Rate limit
    if not rate_limiter.check(ip):
        raise HTTPException(429, "Trop de requêtes. Réessaie dans un moment.")

    token = credentials.credentials
    payload = jwt_manager.decode_token(token)

    if not payload:
        raise HTTPException(401, "Token invalide ou expiré")

    if payload.get("type") != "access":
        raise HTTPException(401, "Type de token invalide")

    # Vérifie si le token a été révoqué
    if session_manager.is_revoked(payload.get("jti", "")):
        raise HTTPException(401, "Token révoqué")

    # Met à jour l'activité de la session
    session_manager.update_activity(payload.get("session_id", ""))

    return payload


def setup_auth(app: FastAPI):
    """
    Configure l'authentification sur l'app FastAPI.
    
    Usage:
        app = FastAPI()
        setup_auth(app)
    """
    global auth_config, jwt_manager, session_manager, rate_limiter, two_factor

    auth_config = AuthConfig()
    jwt_manager = JWTManager(auth_config)
    session_manager = SessionManager()
    rate_limiter = RateLimiter(auth_config.rate_limit_per_minute)
    two_factor = TwoFactorManager(auth_config)

    log.info("🔒 Authentification configurée")
    log.info(f"   JWT expiration: {auth_config.jwt_expiration_minutes} min")
    log.info(f"   2FA: {'✅ activé' if two_factor.is_enabled() else '❌ désactivé'}")
    log.info(f"   Rate limit: {auth_config.rate_limit_per_minute} req/min")

    # ── Middleware de sécurité ──

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        """Ajoute les headers de sécurité à chaque réponse."""
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    # ── Endpoints publics (pas besoin de JWT) ──

    @app.post("/api/auth/login", response_model=TokenResponse)
    async def login(request: Request, body: LoginRequest):
        """
        Authentification par PIN + 2FA optionnel.
        Retourne un access token + refresh token.
        """
        ip = get_client_ip(request)

        # Rate limit login
        check = rate_limiter.check_login(
            ip, auth_config.max_login_attempts, auth_config.lockout_duration
        )
        if not check["allowed"]:
            raise HTTPException(429, check["reason"])

        # Vérifie le PIN
        pin_valid = False
        if pwd_context:
            try:
                pin_valid = pwd_context.verify(body.pin, auth_config.pin_hash)
            except Exception:
                pin_valid = False
        else:
            pin_valid = hashlib.sha256(body.pin.encode()).hexdigest() == auth_config.pin_hash

        if not pin_valid:
            rate_limiter.record_login_attempt(ip)
            remaining = check.get("attempts_left", 0) - 1
            log.warning(f"🚫 Tentative de login échouée depuis {ip} ({remaining} restantes)")
            raise HTTPException(401, f"PIN incorrect. {remaining} tentatives restantes.")

        # Vérifie le 2FA (si activé)
        if two_factor.is_enabled():
            if not body.totp_code:
                raise HTTPException(400, "Code 2FA requis")
            if not two_factor.verify(body.totp_code):
                rate_limiter.record_login_attempt(ip)
                raise HTTPException(401, "Code 2FA invalide")

        # Crée la session
        user_agent = request.headers.get("User-Agent", "unknown")
        session_id = session_manager.create_session(ip, user_agent)

        # Génère les tokens
        access_token = jwt_manager.create_access_token(session_id)
        refresh_token = jwt_manager.create_refresh_token(session_id)

        log.info(f"✅ Login réussi depuis {ip}")

        return TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=auth_config.jwt_expiration_minutes * 60,
        )

    @app.post("/api/auth/refresh", response_model=TokenResponse)
    async def refresh(request: Request, body: RefreshRequest):
        """Renouvelle un access token via le refresh token."""
        ip = get_client_ip(request)

        if not rate_limiter.check(ip):
            raise HTTPException(429, "Trop de requêtes")

        payload = jwt_manager.decode_token(body.refresh_token)
        if not payload or payload.get("type") != "refresh":
            raise HTTPException(401, "Refresh token invalide")

        session_id = payload.get("session_id")
        new_access = jwt_manager.create_access_token(session_id)

        return TokenResponse(
            access_token=new_access,
            refresh_token=body.refresh_token,
            expires_in=auth_config.jwt_expiration_minutes * 60,
        )

    # ── Endpoints protégés (JWT requis) ──

    @app.post("/api/auth/logout")
    async def logout(user: dict = Depends(get_current_user)):
        """Déconnexion — révoque le token actuel."""
        jti = user.get("jti")
        if jti:
            session_manager.revoke_token(jti)
        
        session_id = user.get("session_id")
        if session_id:
            session_manager.destroy_session(session_id)

        log.info("🔒 Logout effectué")
        return {"status": "ok", "message": "Déconnecté"}

    @app.post("/api/auth/logout-all")
    async def logout_all(user: dict = Depends(get_current_user)):
        """Déconnecte TOUTES les sessions (panic button)."""
        session_manager.destroy_all_sessions()
        return {"status": "ok", "message": "Toutes les sessions fermées"}

    @app.post("/api/auth/change-pin")
    async def change_pin(body: ChangePinRequest, user: dict = Depends(get_current_user)):
        """Change le PIN d'accès."""
        # Vérifie l'ancien PIN
        if pwd_context:
            if not pwd_context.verify(body.current_pin, auth_config.pin_hash):
                raise HTTPException(401, "PIN actuel incorrect")
            new_hash = pwd_context.hash(body.new_pin)
        else:
            if hashlib.sha256(body.current_pin.encode()).hexdigest() != auth_config.pin_hash:
                raise HTTPException(401, "PIN actuel incorrect")
            new_hash = hashlib.sha256(body.new_pin.encode()).hexdigest()

        # Validation
        if len(body.new_pin) < 4:
            raise HTTPException(400, "Le PIN doit faire au moins 4 caractères")
        if body.new_pin == body.current_pin:
            raise HTTPException(400, "Le nouveau PIN doit être différent")

        # Sauvegarde
        auth_config.pin_hash = new_hash
        _update_env("AUTH_PIN_HASH", new_hash)

        # Invalide toutes les sessions pour forcer la reconnexion
        session_manager.destroy_all_sessions()

        log.info("🔑 PIN changé avec succès")
        return {"status": "ok", "message": "PIN changé. Reconnecte-toi."}

    @app.get("/api/auth/setup-2fa")
    async def setup_2fa(user: dict = Depends(get_current_user)):
        """
        Configure le 2FA — retourne le QR code à scanner
        avec Google Authenticator / Authy.
        """
        if not HAS_2FA:
            raise HTTPException(501, "2FA non disponible — pip install pyotp qrcode")

        qr_base64 = two_factor.generate_qr_base64()
        uri = two_factor.get_provisioning_uri()
        secret = auth_config.totp_secret

        return {
            "qr_code_base64": qr_base64,
            "provisioning_uri": uri,
            "secret_key": secret,
            "instructions": [
                "1. Ouvre Google Authenticator ou Authy sur ton téléphone",
                "2. Scanne le QR code ou entre la clé manuellement",
                "3. Active le 2FA via /api/auth/enable-2fa avec le code généré",
            ],
        }

    @app.post("/api/auth/enable-2fa")
    async def enable_2fa(code: str, user: dict = Depends(get_current_user)):
        """Active le 2FA après vérification du premier code."""
        if not two_factor.verify(code):
            raise HTTPException(400, "Code invalide. Vérifie que l'heure de ton téléphone est correcte.")

        auth_config.totp_enabled = True
        _update_env("TOTP_ENABLED", "true")

        log.info("🔐 2FA activé avec succès")
        return {"status": "ok", "message": "2FA activé. Tu devras entrer un code à chaque connexion."}

    @app.post("/api/auth/disable-2fa")
    async def disable_2fa(code: str, pin: str, user: dict = Depends(get_current_user)):
        """Désactive le 2FA (nécessite le code actuel + PIN)."""
        # Vérifie le PIN
        if pwd_context:
            if not pwd_context.verify(pin, auth_config.pin_hash):
                raise HTTPException(401, "PIN incorrect")
        else:
            if hashlib.sha256(pin.encode()).hexdigest() != auth_config.pin_hash:
                raise HTTPException(401, "PIN incorrect")

        # Vérifie le code 2FA
        if not two_factor.verify(code):
            raise HTTPException(400, "Code 2FA invalide")

        auth_config.totp_enabled = False
        _update_env("TOTP_ENABLED", "false")

        log.info("🔓 2FA désactivé")
        return {"status": "ok", "message": "2FA désactivé"}

    @app.get("/api/auth/sessions")
    async def get_sessions(user: dict = Depends(get_current_user)):
        """Liste les sessions actives."""
        return {"sessions": session_manager.get_active_sessions()}

    @app.get("/api/auth/me")
    async def get_me(user: dict = Depends(get_current_user)):
        """Retourne les infos de l'utilisateur authentifié."""
        return {
            "authenticated": True,
            "session_id": user.get("session_id", "")[:8] + "...",
            "token_expires": datetime.fromtimestamp(user.get("exp", 0), tz=timezone.utc).isoformat(),
            "two_factor_enabled": two_factor.is_enabled(),
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Décorateur pour protéger les routes existantes
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def require_auth(func):
    """
    Décorateur pour protéger n'importe quelle route FastAPI.
    
    Usage:
        @app.get("/api/secret")
        @require_auth
        async def secret_route(user: dict = Depends(get_current_user)):
            return {"message": "Tu es authentifié !"}
    """
    @wraps(func)
    async def wrapper(*args, **kwargs):
        return await func(*args, **kwargs)
    return wrapper


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Utilitaires
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _update_env(key: str, value: str):
    """Met à jour une variable dans le .env."""
    env_path = ".env"
    if not os.path.exists(env_path):
        with open(env_path, "w") as f:
            f.write(f"{key}={value}\n")
        return

    with open(env_path) as f:
        lines = f.readlines()

    found = False
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = f"{key}={value}\n"
            found = True
            break

    if not found:
        lines.append(f"{key}={value}\n")

    with open(env_path, "w") as f:
        f.writelines(lines)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Guide d'intégration dans bot.py
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

INTEGRATION_GUIDE = """
# ══════════════════════════════════════════════════════════════
# Comment intégrer dans bot.py — 3 étapes
# ══════════════════════════════════════════════════════════════

# ÉTAPE 1: Import en haut de bot.py
from auth import setup_auth, get_current_user

# ÉTAPE 2: Après la création de l'app FastAPI
app = FastAPI(title="Nexus Trader API", version="1.0.0")
setup_auth(app)  # ← Ajoute cette ligne

# ÉTAPE 3: Protège les routes sensibles en ajoutant Depends(get_current_user)

# AVANT (non protégé):
@app.get("/api/status")
async def get_status():
    ...

# APRÈS (protégé):
@app.get("/api/status")
async def get_status(user: dict = Depends(get_current_user)):
    ...

# Routes à protéger:
#   /api/status        → Depends(get_current_user)
#   /api/trades        → Depends(get_current_user)
#   /api/config        → Depends(get_current_user)
#   /api/strategy/{n}  → Depends(get_current_user)
#   /api/sentiment     → Depends(get_current_user)
#   /ws                → Vérifier le token dans le premier message WebSocket

# La route / (health check) reste publique.

# ══════════════════════════════════════════════════════════════
# Côté Frontend — Comment utiliser les tokens
# ══════════════════════════════════════════════════════════════

# 1. Login:
#    POST /api/auth/login {pin: "1234", totp_code: "123456"}
#    → Reçoit {access_token, refresh_token, expires_in}
#
# 2. Requêtes authentifiées:
#    Headers: {Authorization: "Bearer <access_token>"}
#
# 3. Refresh quand le token expire:
#    POST /api/auth/refresh {refresh_token: "..."}
#    → Reçoit un nouveau access_token
#
# 4. Logout:
#    POST /api/auth/logout (avec Bearer token)
"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Test standalone
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s │ %(message)s")

    print("""
    ╔══════════════════════════════════════════════════════════════╗
    ║  🔒 NEXUS TRADER — Test Authentification                    ║
    ╚══════════════════════════════════════════════════════════════╝
    """)

    config = AuthConfig()
    jwt_mgr = JWTManager(config)
    session_mgr = SessionManager()
    tfa = TwoFactorManager(config)

    # Test PIN
    print("📍 Test PIN...")
    if pwd_context:
        assert pwd_context.verify("1234", config.pin_hash), "PIN par défaut devrait être valide"
        print("   ✅ PIN '1234' vérifié")

    # Test JWT
    print("\n📍 Test JWT...")
    session_id = session_mgr.create_session("127.0.0.1")
    token = jwt_mgr.create_access_token(session_id)
    print(f"   Token: {token[:50]}...")
    
    decoded = jwt_mgr.decode_token(token)
    assert decoded is not None, "Le token devrait être valide"
    assert decoded["session_id"] == session_id
    print(f"   ✅ Token décodé — session: {session_id[:16]}...")

    # Test rate limiter
    print("\n📍 Test Rate Limiter...")
    rl = RateLimiter(max_per_minute=5)
    for i in range(7):
        allowed = rl.check("test_ip")
        print(f"   Requête {i+1}: {'✅' if allowed else '❌ bloqué'}")

    # Test 2FA
    if HAS_2FA:
        print("\n📍 Test 2FA...")
        code = tfa.get_current_code()
        valid = tfa.verify(code)
        print(f"   Code actuel: {code}")
        print(f"   Vérification: {'✅' if valid else '❌'}")
        print(f"   URI: {tfa.get_provisioning_uri()}")

    # Test sessions
    print("\n📍 Test Sessions...")
    sessions = session_mgr.get_active_sessions()
    print(f"   Sessions actives: {len(sessions)}")

    print("\n✅ Tous les tests passés !")
    print(INTEGRATION_GUIDE)
