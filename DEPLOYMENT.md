# Deployment-Anleitung: DaF/DaZ Sprachdiagnostik-Plattform

## Voraussetzungen

- Ubuntu 22.04 LTS (frischer Server)
- Root-Zugang
- Domain mit DNS-Eintrag auf die Server-IP
- PostgreSQL-Datenbank (lokal oder extern)

## Schritt 1: System vorbereiten

```bash
apt update && apt upgrade -y
apt install -y python3.12 python3.12-venv python3-pip git nginx certbot python3-certbot-nginx postgresql postgresql-contrib
```

## Schritt 2: Datenbank einrichten

```bash
sudo -u postgres psql -c "CREATE USER dafuser WITH PASSWORD 'DEIN_DB_PASSWORT';"
sudo -u postgres psql -c "CREATE DATABASE dafplattform OWNER dafuser;"
```

## Schritt 3: Code klonen

```bash
mkdir -p /var/www
cd /var/www
git clone https://github.com/mihoyer/daf-plattform.git
cd daf-plattform
```

## Schritt 4: Python-Umgebung einrichten

```bash
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Schritt 5: Umgebungsvariablen konfigurieren

```bash
cp .env.example .env
nano .env
```

Folgende Werte in der `.env` anpassen:

```env
DATABASE_URL=postgresql+asyncpg://dafuser:DEIN_DB_PASSWORT@localhost/dafplattform
OPENAI_API_KEY=sk-...
ADMIN_PASSWORD=DEIN_ADMIN_PASSWORT
TESTGRUPPE_PASSWORT=DEIN_TESTGRUPPEN_PASSWORT
SECRET_KEY=ZUFAELLIGER_LANGER_STRING
BASE_URL=https://DEINE_DOMAIN.de
STRIPE_SECRET_KEY=sk_live_... (optional, fuer Zahlungen)
STRIPE_PUBLISHABLE_KEY=pk_live_... (optional)
```

## Schritt 6: Datenbank-Tabellen erstellen

```bash
source venv/bin/activate
python3 -c "import asyncio; from app.models.database import init_db; asyncio.run(init_db())"
```

## Schritt 7: Systemd-Service einrichten

```bash
cp scripts/daf-plattform.service /etc/systemd/system/
# WorkingDirectory und ExecStart in der Service-Datei anpassen falls noetig
systemctl daemon-reload
systemctl enable daf-plattform
systemctl start daf-plattform
systemctl status daf-plattform
```

## Schritt 8: Nginx konfigurieren

```bash
cp scripts/nginx.conf /etc/nginx/sites-available/daf-plattform
# Domain in der Konfiguration anpassen:
nano /etc/nginx/sites-available/daf-plattform
ln -s /etc/nginx/sites-available/daf-plattform /etc/nginx/sites-enabled/
nginx -t
systemctl reload nginx
```

## Schritt 9: SSL-Zertifikat einrichten

```bash
certbot --nginx -d DEINE_DOMAIN.de
```

## Schritt 10: Matplotlib-Cache-Verzeichnis setzen

In `/etc/systemd/system/daf-plattform.service` unter `[Service]` hinzufügen:
```
Environment=MPLCONFIGDIR=/tmp/matplotlib
```
Dann: `systemctl daemon-reload && systemctl restart daf-plattform`

## Zugang nach Deployment

| URL | Beschreibung |
|---|---|
| `https://DEINE_DOMAIN.de/` | Startseite / Test-Einstieg |
| `https://DEINE_DOMAIN.de/admin/login` | Admin-Dashboard |
| `https://DEINE_DOMAIN.de/testgruppe` | Testgruppen-Uebersicht |
| `https://DEINE_DOMAIN.de/k/CODE` | Direktzugang via QR-Code |

## Updates einspielen

```bash
cd /var/www/daf-plattform
git pull
source venv/bin/activate
pip install -r requirements.txt
systemctl restart daf-plattform
```
