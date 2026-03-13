#!/bin/bash
# ══════════════════════════════════════════════════════════════
# NEXUS TRADER — Script d'installation VPS
# 
# Ce script installe tout automatiquement sur ton VPS.
# Compatible: Ubuntu 22.04+, Debian 12+
#
# Usage:
#   chmod +x install.sh
#   ./install.sh
# ══════════════════════════════════════════════════════════════

set -e  # Arrête si une commande échoue

# Couleurs
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo ""
echo -e "${BLUE}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║            ⚡ NEXUS TRADER — Installation VPS               ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""

# ── 1. Vérifications ──
echo -e "${YELLOW}[1/6] Vérification du système...${NC}"

if [ "$EUID" -eq 0 ]; then
    echo -e "${RED}⚠️  Ne lance pas ce script en root !${NC}"
    echo "    Utilise un user normal avec sudo."
    echo "    Si besoin: adduser nexus && usermod -aG sudo nexus"
    exit 1
fi

OS=$(lsb_release -is 2>/dev/null || echo "Unknown")
echo "  OS: $OS"
echo "  RAM: $(free -h | awk '/^Mem:/ {print $2}')"
echo "  Disque: $(df -h / | awk 'NR==2 {print $4}') disponible"
echo "  CPU: $(nproc) cœurs"

# Vérifie qu'on a assez de RAM
TOTAL_RAM_MB=$(free -m | awk '/^Mem:/ {print $2}')
if [ "$TOTAL_RAM_MB" -lt 1024 ]; then
    echo -e "${YELLOW}⚠️  RAM faible (${TOTAL_RAM_MB}Mo). Le bot fonctionnera mais sera limité.${NC}"
    echo "    Recommandé: 2 Go minimum"
fi

echo -e "${GREEN}  ✅ Vérifications OK${NC}"

# ── 2. Installe Docker ──
echo ""
echo -e "${YELLOW}[2/6] Installation de Docker...${NC}"

if command -v docker &> /dev/null; then
    echo -e "${GREEN}  ✅ Docker déjà installé ($(docker --version))${NC}"
else
    echo "  Installation de Docker..."
    sudo apt-get update -qq
    sudo apt-get install -y -qq ca-certificates curl gnupg
    
    # Clé GPG Docker
    sudo install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    sudo chmod a+r /etc/apt/keyrings/docker.gpg
    
    # Repository
    echo \
      "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
      $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
      sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
    
    sudo apt-get update -qq
    sudo apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
    
    # Ajoute l'user au groupe docker (pas besoin de sudo pour docker)
    sudo usermod -aG docker $USER
    
    echo -e "${GREEN}  ✅ Docker installé${NC}"
    echo -e "${YELLOW}  ⚠️  Tu devras te reconnecter pour que le groupe docker prenne effet${NC}"
    echo "     Ou lance: newgrp docker"
fi

# ── 3. Crée le dossier du projet ──
echo ""
echo -e "${YELLOW}[3/6] Création du projet...${NC}"

PROJECT_DIR="$HOME/nexus-trader"
mkdir -p "$PROJECT_DIR"
cd "$PROJECT_DIR"

echo "  Dossier: $PROJECT_DIR"

# Copie les fichiers (s'ils sont dans le même dossier que le script)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for file in bot.py sentiment.py intelligence.py backtesting.py multipair.py \
            Dockerfile docker-compose.yml requirements.txt .env.example; do
    if [ -f "$SCRIPT_DIR/$file" ]; then
        cp "$SCRIPT_DIR/$file" "$PROJECT_DIR/"
        echo "  📄 $file copié"
    fi
done

echo -e "${GREEN}  ✅ Fichiers en place${NC}"

# ── 4. Configuration ──
echo ""
echo -e "${YELLOW}[4/6] Configuration...${NC}"

if [ ! -f .env ]; then
    cp .env.example .env
    echo ""
    echo -e "${YELLOW}  ⚠️  IMPORTANT: Configure ton fichier .env !${NC}"
    echo "  $PROJECT_DIR/.env"
    echo ""
    echo "  À remplir:"
    echo "    BINANCE_API_KEY=ta_cle"
    echo "    BINANCE_API_SECRET=ton_secret"
    echo "    TRADING_MODE=paper          (commence par paper !)"
    echo "    ANTHROPIC_API_KEY=sk-ant-.. (optionnel, pour l'IA)"
    echo ""
    read -p "  Tu veux éditer .env maintenant ? (o/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Oo]$ ]]; then
        ${EDITOR:-nano} .env
    fi
else
    echo -e "${GREEN}  ✅ .env existe déjà${NC}"
fi

# ── 5. Build Docker ──
echo ""
echo -e "${YELLOW}[5/6] Build de l'image Docker...${NC}"

# Utilise sudo si l'user n'est pas dans le groupe docker
if groups $USER | grep -q docker; then
    DOCKER_CMD="docker"
else
    DOCKER_CMD="sudo docker"
    echo "  (utilisation de sudo — reconnecte-toi pour éviter ça)"
fi

$DOCKER_CMD compose build

echo -e "${GREEN}  ✅ Image construite${NC}"

# ── 6. Instructions de lancement ──
echo ""
echo -e "${BLUE}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║  ✅ INSTALLATION TERMINÉE !                                 ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${GREEN}  Commandes utiles:${NC}"
echo ""
echo "  📦 Démarrer le bot:"
echo "     cd $PROJECT_DIR"
echo "     docker compose up -d"
echo ""
echo "  📊 Voir les logs en direct:"
echo "     docker compose logs -f"
echo ""
echo "  ⏹  Arrêter le bot:"
echo "     docker compose down"
echo ""
echo "  🔄 Redémarrer:"
echo "     docker compose restart"
echo ""
echo "  📈 Voir l'état:"
echo "     docker compose ps"
echo "     curl http://localhost:8080/api/status"
echo ""
echo "  🔬 Lancer un backtest:"
echo "     docker compose exec nexus-trader python backtesting.py --compare"
echo ""
echo "  🔍 Scanner le marché:"
echo "     docker compose exec nexus-trader python multipair.py"
echo ""
echo "  📊 Voir la consommation de ressources:"
echo "     docker stats nexus-trader"
echo ""
echo "  🗑  Tout supprimer (données incluses):"
echo "     docker compose down -v"
echo ""
echo -e "${YELLOW}  ⚠️  RAPPEL: Commence TOUJOURS en mode paper !${NC}"
echo -e "${YELLOW}     Édite .env et mets TRADING_MODE=paper${NC}"
echo -e "${YELLOW}     Lance un backtest avant de passer en live${NC}"
echo ""
