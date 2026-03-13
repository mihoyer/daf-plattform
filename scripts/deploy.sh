#!/bin/bash
# =============================================================================
# DaF Sprachdiagnostik Plattform – Deployment-Skript für DigitalOcean (Ubuntu 22.04)
# =============================================================================
set -e

APP_DIR="/var/www/daf-plattform"
SERVICE_NAME="daf-plattform"
PYTHON_VERSION="python3"

echo "=============================================="
echo " DaF Sprachdiagnostik – Deployment"
echo "=============================================="

# 1. System-Updates
echo "[1/9] System-Pakete aktualisieren..."
apt-get update -qq
apt-get install -y -qq \
    python3 python3-pip python3-venv \
    postgresql postgresql-contrib \
    nginx certbot python3-certbot-nginx \
    ffmpeg \
    git curl

# 2. PostgreSQL einrichten
echo "[2/9] PostgreSQL einrichten..."
systemctl start postgresql
systemctl enable postgresql

# Datenbank und Benutzer anlegen (idempotent)
sudo -u postgres psql -tc "SELECT 1 FROM pg_user WHERE usename = 'dafuser'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE USER dafuser WITH PASSWORD 'dafpassword_AENDERN';"
sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname = 'dafplattform'" | grep -q 1 || \
    sudo -u postgres createdb -O dafuser dafplattform

# 3. Anwendungsverzeichnis
echo "[3/9] Anwendungsverzeichnis einrichten..."
mkdir -p $APP_DIR/data/audio
mkdir -p $APP_DIR/data/temp
chown -R www-data:www-data $APP_DIR/data 2>/dev/null || true

# 4. Python-Umgebung
echo "[4/9] Python-Umgebung einrichten..."
cd $APP_DIR
if [ ! -d "venv" ]; then
    $PYTHON_VERSION -m venv venv
fi
source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q

# 5. .env konfigurieren
echo "[5/9] Konfiguration prüfen..."
if [ ! -f "$APP_DIR/.env" ]; then
    if [ -f "$APP_DIR/.env.example" ]; then
        cp $APP_DIR/.env.example $APP_DIR/.env
        echo "  ⚠️  .env wurde aus .env.example erstellt – bitte anpassen!"
    else
        echo "  ⚠️  .env fehlt – bitte manuell erstellen!"
    fi
fi

# 6. Systemd Service
echo "[6/9] Systemd Service einrichten..."
cat > /etc/systemd/system/${SERVICE_NAME}.service << EOF
[Unit]
Description=DaF Sprachdiagnostik Plattform
After=network.target postgresql.service

[Service]
Type=simple
User=www-data
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=${APP_DIR}/venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000 --workers 2
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable ${SERVICE_NAME}

# 7. Nginx konfigurieren
echo "[7/9] Nginx konfigurieren..."
cat > /etc/nginx/sites-available/${SERVICE_NAME} << 'EOF'
server {
    listen 80;
    server_name _;

    client_max_body_size 20M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
        proxy_connect_timeout 10s;
    }

    location /static/ {
        alias /var/www/daf-plattform/app/static/;
        expires 7d;
        add_header Cache-Control "public, immutable";
    }
}
EOF

rm -f /etc/nginx/sites-enabled/default
ln -sf /etc/nginx/sites-available/${SERVICE_NAME} /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx

# 8. Berechtigungen
echo "[8/9] Berechtigungen setzen..."
chown -R www-data:www-data $APP_DIR
chmod -R 755 $APP_DIR
chmod 600 $APP_DIR/.env 2>/dev/null || true

# 9. Service starten
echo "[9/9] Service starten..."
systemctl restart ${SERVICE_NAME}
sleep 3
systemctl status ${SERVICE_NAME} --no-pager

echo ""
echo "=============================================="
echo " Deployment abgeschlossen!"
echo "=============================================="
echo ""
echo " Nächste Schritte:"
echo " 1. .env anpassen: nano $APP_DIR/.env"
echo " 2. DB-Passwort ändern: sudo -u postgres psql"
echo " 3. HTTPS einrichten:"
echo "    certbot --nginx -d ihre-domain.de"
echo ""
