#!/bin/bash

# setup.sh - Automatisches Deployment-Skript für die DaF-Plattform
# Dieses Skript installiert alle Abhängigkeiten, richtet die Datenbank ein,
# klont das Repository und konfiguriert Nginx und Systemd.

set -e

# --- Konfiguration ---
DOMAIN="daf-plattform.de" # Bitte anpassen!
DB_USER="dafuser"
DB_PASS="DEIN_DB_PASSWORT" # Bitte anpassen!
DB_NAME="dafplattform"
REPO_URL="https://github.com/mihoyer/daf-plattform.git"
APP_DIR="/var/www/daf-plattform"
# ---------------------

echo "Starte DaF-Plattform Setup..."

# 1. System aktualisieren und Pakete installieren
echo "Installiere Systempakete..."
apt update && apt upgrade -y
apt install -y python3.12 python3.12-venv python3-pip git nginx certbot python3-certbot-nginx postgresql postgresql-contrib

# 2. Datenbank einrichten
echo "Richte PostgreSQL ein..."
sudo -u postgres psql -c "CREATE USER $DB_USER WITH PASSWORD '$DB_PASS';" || true
sudo -u postgres psql -c "CREATE DATABASE $DB_NAME OWNER $DB_USER;" || true

# 3. Repository klonen
echo "Klone Repository..."
mkdir -p /var/www
if [ ! -d "$APP_DIR" ]; then
    git clone $REPO_URL $APP_DIR
else
    echo "Verzeichnis $APP_DIR existiert bereits. Überspringe Klonen."
fi
cd $APP_DIR

# 4. Python-Umgebung einrichten
echo "Richte Python venv ein..."
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 5. .env Datei vorbereiten
echo "Erstelle .env Vorlage..."
if [ ! -f ".env" ]; then
    cp .env.example .env
    sed -i "s/DATABASE_URL=.*/DATABASE_URL=postgresql+asyncpg:\/\/$DB_USER:$DB_PASS@localhost\/$DB_NAME/" .env
    sed -i "s/BASE_URL=.*/BASE_URL=https:\/\/$DOMAIN/" .env
    echo "Bitte bearbeite die .env Datei und trage deine API-Keys ein: nano $APP_DIR/.env"
fi

# 6. Systemd Service einrichten
echo "Richte Systemd Service ein..."
cat <<EOF > /etc/systemd/system/daf-plattform.service
[Unit]
Description=DaF Sprachdiagnostik Plattform
After=network.target

[Service]
User=root
Group=www-data
WorkingDirectory=$APP_DIR
Environment="PATH=$APP_DIR/venv/bin"
Environment="MPLCONFIGDIR=/tmp/matplotlib"
ExecStart=$APP_DIR/venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000 --workers 2

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable daf-plattform
systemctl start daf-plattform

# 7. Nginx konfigurieren
echo "Richte Nginx ein..."
cat <<EOF > /etc/nginx/sites-available/daf-plattform
server {
    listen 80;
    server_name $DOMAIN;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF

ln -sf /etc/nginx/sites-available/daf-plattform /etc/nginx/sites-enabled/
nginx -t
systemctl reload nginx

# 8. SSL Zertifikat (Optional, erfordert gültigen DNS-Eintrag)
echo "Möchtest du jetzt ein SSL-Zertifikat mit Certbot einrichten? (y/n)"
read -r setup_ssl
if [ "$setup_ssl" = "y" ]; then
    certbot --nginx -d $DOMAIN
fi

echo "Setup abgeschlossen! Die Plattform sollte nun unter http://$DOMAIN erreichbar sein."
echo "Vergiss nicht, die .env Datei anzupassen und die Datenbank-Tabellen zu erstellen (Schritt 6 in DEPLOYMENT.md)."
