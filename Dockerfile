# ══════════════════════════════════════════════════════════════
# NEXUS TRADER — Dockerfile
# Image légère Python 3.12 Alpine
# ══════════════════════════════════════════════════════════════

FROM python:3.12-slim

# Métadonnées
LABEL maintainer="nexus-trader"
LABEL description="Crypto Trading Bot"

# Variables d'environnement
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Crée un user non-root (sécurité)
RUN groupadd -r nexus && useradd -r -g nexus -d /app -s /sbin/nologin nexus

# Dépendances système
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Dossier de travail
WORKDIR /app

# Copie et installe les dépendances d'abord (cache Docker)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copie le code
COPY bot.py .
COPY sentiment.py .
COPY intelligence.py .
COPY backtesting.py .
COPY multipair.py .

# Crée les dossiers nécessaires
RUN mkdir -p /app/data /app/logs && \
    chown -R nexus:nexus /app

# Passe en user non-root
USER nexus

# Port API
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/')" || exit 1

# Lancement
ENTRYPOINT ["python", "bot.py"]
CMD ["--mode", "paper"]
