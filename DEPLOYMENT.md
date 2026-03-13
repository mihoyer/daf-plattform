# Deployment-Anleitung – DaF Sprachdiagnostik Plattform

## Voraussetzungen

- DigitalOcean Droplet: Ubuntu 22.04, min. 2 GB RAM (empfohlen: 4 GB)
- Domain mit Subdomain (z. B. `einstufung.ihre-domain.de`)
- OpenAI API-Key mit Zugriff auf GPT-4.1, GPT-4o, Whisper, TTS
- Stripe-Account (Testmodus für den Anfang)

---

## Schritt 1: Repository auf GitHub hochladen

```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/DEIN-USERNAME/daf-plattform.git
git push -u origin main
```

---

## Schritt 2: Droplet vorbereiten

Verbinde dich mit der DigitalOcean Web Console oder per SSH:

```bash
ssh root@DEINE-IP
```

Repository klonen:
```bash
git clone https://github.com/DEIN-USERNAME/daf-plattform.git /var/www/daf-plattform
```

---

## Schritt 3: Deployment-Skript ausführen

```bash
cd /var/www/daf-plattform && bash scripts/deploy.sh
```

Das Skript installiert automatisch:
- Python 3, pip, venv
- PostgreSQL
- Nginx
- ffmpeg (für Audio-Konvertierung)
- Certbot (für HTTPS)
- Alle Python-Abhängigkeiten

---

## Schritt 4: .env konfigurieren

```bash
nano /var/www/daf-plattform/.env
```

Mindestens diese Werte eintragen:

```env
OPENAI_API_KEY=sk-...
STRIPE_SECRET_KEY=sk_test_...
STRIPE_PUBLISHABLE_KEY=pk_test_...
DATABASE_URL=postgresql+asyncpg://dafuser:dafpassword_AENDERN@localhost/dafplattform
ADMIN_PASSWORD=sicheres-passwort
SECRET_KEY=langer-zufaelliger-string-min-32-zeichen
BASE_URL=https://einstufung.ihre-domain.de
BETREIBER_NAME=Ihr Name / Ihre Organisation
BETREIBER_ADRESSE=Straße, PLZ Ort, Land
BETREIBER_EMAIL=datenschutz@ihre-domain.de
```

Service neu starten:
```bash
systemctl restart daf-plattform
```

---

## Schritt 5: PostgreSQL-Passwort ändern

```bash
sudo -u postgres psql
ALTER USER dafuser WITH PASSWORD 'NEUES-SICHERES-PASSWORT';
\q
```

Dann in der `.env` das neue Passwort eintragen und Service neu starten.

---

## Schritt 6: DNS einrichten

Bei Ihrem DNS-Anbieter (Artfiles):
- Typ: `A`
- Name: `einstufung` (oder gewünschte Subdomain)
- Wert: `IHRE-DROPLET-IP`
- TTL: 300

---

## Schritt 7: HTTPS einrichten

```bash
sed -i 's/server_name _;/server_name einstufung.ihre-domain.de;/' /etc/nginx/sites-available/daf-plattform
nginx -t && systemctl reload nginx
certbot --nginx -d einstufung.ihre-domain.de
```

---

## Schritt 8: Stripe Webhook einrichten

Im Stripe Dashboard → Webhooks → Endpoint hinzufügen:
- URL: `https://einstufung.ihre-domain.de/api/stripe/webhook`
- Events: `checkout.session.completed`, `payment_intent.payment_failed`

Den Webhook-Secret in die `.env` eintragen:
```env
STRIPE_WEBHOOK_SECRET=whsec_...
```

---

## Updates einspielen

```bash
cd /var/www/daf-plattform && git pull origin main && systemctl restart daf-plattform
```

---

## Backup erstellen

```bash
# Anwendung
tar -czf /root/backup-daf-$(date +%Y%m%d).tar.gz /var/www/daf-plattform

# Datenbank
sudo -u postgres pg_dump dafplattform > /root/backup-db-$(date +%Y%m%d).sql
```

---

## Troubleshooting

```bash
# Service-Status
systemctl status daf-plattform

# Logs anzeigen
journalctl -u daf-plattform --no-pager | tail -30

# Nginx-Fehler
journalctl -u nginx --no-pager | tail -20

# Port prüfen
ss -tlnp | grep 8001
```
