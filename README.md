# DaF Sprachdiagnostik Plattform

Vollständige, modulare Plattform zur KI-gestützten Einstufung von Deutschlernenden nach CEFR.

## Module

| Modul | Beschreibung | Paket |
|---|---|---|
| M1 – Grammatik & Wortschatz | Adaptives Multiple-Choice, GPT-generiert | Alle |
| M2 – Lesen & Leseverstehen | Adaptiver Lesetext + Verständnisfragen | Standard, Premium |
| M3 – Hörverstehen | TTS-Audio + Verständnisfragen | Standard, Premium |
| M4 – Vorlesen | Vorgegebener Text vorlesen, Ausspracheanalyse | Premium |
| M5 – Freies Sprechen | Freie Sprachprobe, GPT-4o Analyse | Alle |
| M6 – Schreiben | Texteingabe oder Handschrift-Foto, GPT-Analyse | Standard, Premium |

## Pakete

| Paket | Module | Preis |
|---|---|---|
| Basis | M1 + M5 | 6 CHF / 6 EUR |
| Standard | M1 + M2 + M3 + M5 + M6 | 10 CHF / 10 EUR |
| Premium | Alle 6 Module | 14 CHF / 14 EUR |

## Technologie

- **Backend:** Python 3.11 + FastAPI + SQLAlchemy (async) + PostgreSQL
- **KI:** OpenAI Whisper, GPT-4.1, GPT-4o Audio, GPT-4o Vision, TTS
- **Zahlung:** Stripe (CHF + EUR, Testmodus)
- **Frontend:** Vanilla HTML/CSS/JS (kein Framework)
- **Deployment:** DigitalOcean Droplet + Nginx + Systemd

## Schnellstart

```bash
# 1. Repository klonen
git clone https://github.com/DEIN-USERNAME/daf-plattform.git /var/www/daf-plattform

# 2. Deployment-Skript ausführen
cd /var/www/daf-plattform && bash scripts/deploy.sh

# 3. .env konfigurieren
nano /var/www/daf-plattform/.env

# 4. Service neu starten
systemctl restart daf-plattform
```

## Umgebungsvariablen (.env)

```env
OPENAI_API_KEY=sk-...
STRIPE_SECRET_KEY=sk_test_...
STRIPE_PUBLISHABLE_KEY=pk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...
DATABASE_URL=postgresql+asyncpg://dafuser:PASSWORT@localhost/dafplattform
ADMIN_PASSWORD=sicher-aendern
SECRET_KEY=langer-zufaelliger-string
BASE_URL=https://ihre-domain.de
BETREIBER_NAME=Ihr Name
BETREIBER_ADRESSE=Ihre Adresse
BETREIBER_EMAIL=ihre@email.de
```

## DSGVO

- Keine Speicherung personenbezogener Daten
- Audio- und Bilddateien werden nach Analyse sofort gelöscht
- Sessions werden nach 24 Stunden automatisch gelöscht
- Alle Übertragungen über HTTPS/TLS
- Server in EU (DigitalOcean Frankfurt)

## Lizenz

Proprietär – alle Rechte vorbehalten.
