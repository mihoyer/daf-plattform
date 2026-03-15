#!/usr/bin/env python3
"""
setup_testgruppe.py – Einmalig auf dem Server ausführen.

Was dieses Skript tut:
  1. Trägt 20 Kandidaten-Codes (max. 2 Nutzungen) in die Datenbank ein
  2. Trägt 1 Admin-Code (unbegrenzt) ein
  3. Generiert eine PDF-Datei mit allen QR-Codes zum Ausdrucken

Ausführung auf dem Server:
  cd /var/www/daf-plattform
  python3 setup_testgruppe.py --url https://deine-domain.ch

Optionen:
  --url     Basis-URL der Plattform (Standard: http://localhost:8000)
  --out     Ausgabe-PDF (Standard: qr_codes_testgruppe.pdf)
  --prefix  Code-Präfix (Standard: TG)
  --admin   Admin-Code (Standard: ADMIN-MASTER)
"""
import argparse
import asyncio
import io
import os
import random
import string
import sys
from datetime import datetime, timezone

# ── Argumente ─────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="Testgruppen-Setup")
parser.add_argument("--url", default="http://localhost:8000", help="Basis-URL der Plattform")
parser.add_argument("--out", default="qr_codes_testgruppe.pdf", help="Ausgabe-PDF")
parser.add_argument("--prefix", default="TG", help="Code-Präfix")
parser.add_argument("--admin", default="ADMIN-MASTER", help="Admin-Code (unbegrenzt)")
parser.add_argument("--anzahl", type=int, default=20, help="Anzahl Kandidaten-Codes")
parser.add_argument("--max-nutzungen", type=int, default=2, help="Max. Nutzungen pro Kandidat")
parser.add_argument("--nur-pdf", action="store_true", help="Nur PDF generieren, keine DB-Einträge")
args = parser.parse_args()

BASE_URL = args.url.rstrip("/")

# ── Imports ───────────────────────────────────────────────────────────────────
try:
    import qrcode
    from PIL import Image
except ImportError:
    print("FEHLER: pip install qrcode[pil]")
    sys.exit(1)

try:
    from fpdf import FPDF
except ImportError:
    print("FEHLER: pip install fpdf2")
    sys.exit(1)


# ── Code-Generierung ──────────────────────────────────────────────────────────

def generiere_code(prefix: str, nummer: int) -> str:
    """Generiert einen lesbaren Code: TG-01-XXXX"""
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
    return f"{prefix}-{nummer:02d}-{suffix}"


# ── Datenbank-Einträge ────────────────────────────────────────────────────────

async def trage_codes_ein(codes_kandidaten: list[str], code_admin: str):
    """Trägt alle Codes in die PostgreSQL-Datenbank ein."""
    try:
        # Projektpfad zum Python-Pfad hinzufügen
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from app.models.database import AsyncSessionLocal, GutscheinCode, PaketTyp, init_db

        await init_db()

        async with AsyncSessionLocal() as db:
            # Kandidaten-Codes
            eingetragen = 0
            for code in codes_kandidaten:
                # Prüfen ob Code schon existiert
                from sqlalchemy import select
                result = await db.execute(select(GutscheinCode).where(GutscheinCode.code == code))
                if result.scalar_one_or_none():
                    print(f"  ⚠️  Code {code} bereits vorhanden – übersprungen")
                    continue

                gc = GutscheinCode(
                    code=code,
                    paket=PaketTyp.premium,
                    max_nutzungen=args.max_nutzungen,
                    genutzt=0,
                    aktiv=True,
                    notiz=f"Testgruppe {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
                )
                db.add(gc)
                eingetragen += 1

            # Admin-Code
            result = await db.execute(select(GutscheinCode).where(GutscheinCode.code == code_admin))
            if not result.scalar_one_or_none():
                gc_admin = GutscheinCode(
                    code=code_admin,
                    paket=PaketTyp.premium,
                    max_nutzungen=9999,  # Unbegrenzt
                    genutzt=0,
                    aktiv=True,
                    notiz="Admin-Dauerzugang",
                )
                db.add(gc_admin)
                print(f"  ✅ Admin-Code eingetragen: {code_admin}")
            else:
                print(f"  ⚠️  Admin-Code {code_admin} bereits vorhanden")

            await db.commit()
            print(f"  ✅ {eingetragen} Kandidaten-Codes eingetragen")

    except Exception as e:
        print(f"  ❌ Datenbankfehler: {e}")
        print("     Tipp: Stelle sicher dass die .env-Datei korrekt ist und die DB läuft.")
        raise


# ── QR-Code-Generierung ───────────────────────────────────────────────────────

def erstelle_qr_image(url: str, size: int = 200) -> Image.Image:
    """Erstellt ein QR-Code-Bild für die gegebene URL."""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    img = img.resize((size, size), Image.LANCZOS)
    return img


def erstelle_pdf(alle_codes: list[dict], output_path: str):
    """
    Erstellt ein A4-PDF mit je 4 QR-Codes pro Seite.
    Jeder QR-Code zeigt: Code, URL, Max-Nutzungen.
    """
    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=False)

    # Seitenlayout: 2 Spalten × 2 Zeilen = 4 Codes pro Seite
    COLS = 2
    ROWS = 2
    MARGIN_X = 15
    MARGIN_Y = 20
    PAGE_W = 210
    PAGE_H = 297
    CELL_W = (PAGE_W - 2 * MARGIN_X) / COLS
    CELL_H = (PAGE_H - 2 * MARGIN_Y) / ROWS
    QR_SIZE = 55  # mm

    for page_start in range(0, len(alle_codes), COLS * ROWS):
        pdf.add_page()

        # Seitentitel
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(37, 99, 235)
        pdf.set_xy(MARGIN_X, 8)
        pdf.cell(PAGE_W - 2 * MARGIN_X, 8, "DaF Sprachdiagnostik - Testzugaenge", align="C")

        page_codes = alle_codes[page_start:page_start + COLS * ROWS]

        for i, eintrag in enumerate(page_codes):
            col = i % COLS
            row = i // COLS

            x = MARGIN_X + col * CELL_W
            y = MARGIN_Y + row * CELL_H

            # Rahmen
            pdf.set_draw_color(200, 200, 200)
            pdf.set_line_width(0.3)
            pdf.rect(x + 3, y + 3, CELL_W - 6, CELL_H - 6)

            # QR-Code als Bild
            qr_img = erstelle_qr_image(eintrag["url"], size=300)
            import tempfile, os as _os
            tmp_qr = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            qr_img.save(tmp_qr.name, format="PNG")
            tmp_qr.close()

            qr_x = x + (CELL_W - QR_SIZE) / 2
            qr_y = y + 10
            pdf.image(tmp_qr.name, x=qr_x, y=qr_y, w=QR_SIZE, h=QR_SIZE)
            _os.unlink(tmp_qr.name)

            # Code-Text
            pdf.set_font("Courier", "B", 14)
            pdf.set_text_color(30, 30, 30)
            pdf.set_xy(x, qr_y + QR_SIZE + 4)
            pdf.cell(CELL_W, 7, eintrag["code"], align="C")

            # URL (klein)
            pdf.set_font("Helvetica", "", 7)
            pdf.set_text_color(100, 100, 100)
            pdf.set_xy(x, qr_y + QR_SIZE + 12)
            url_kurz = eintrag["url"].replace("https://", "").replace("http://", "")
            pdf.cell(CELL_W, 5, url_kurz, align="C")

            # Nutzungsinfo
            if eintrag.get("ist_admin"):
                info = "Unbegrenzte Nutzung (Admin)"
                pdf.set_text_color(37, 99, 235)
            else:
                info = f"Max. {eintrag['max_nutzungen']}x verwendbar"
                pdf.set_text_color(80, 80, 80)
            pdf.set_font("Helvetica", "I", 8)
            pdf.set_xy(x, qr_y + QR_SIZE + 18)
            pdf.cell(CELL_W, 5, info, align="C")

            # Kandidaten-Nummer (wenn nicht Admin)
            if not eintrag.get("ist_admin") and eintrag.get("nummer"):
                pdf.set_font("Helvetica", "", 8)
                pdf.set_text_color(150, 150, 150)
                pdf.set_xy(x, qr_y + QR_SIZE + 24)
                pdf.cell(CELL_W, 5, f"Kandidat #{eintrag['nummer']:02d}", align="C")

        # Seitennummer
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(180, 180, 180)
        seite = page_start // (COLS * ROWS) + 1
        gesamt = (len(alle_codes) + COLS * ROWS - 1) // (COLS * ROWS)
        pdf.set_xy(0, PAGE_H - 10)
        pdf.cell(PAGE_W, 5, f"Seite {seite} von {gesamt} - Vertraulich - Nur fuer interne Verwendung", align="C")

    pdf.output(output_path)
    print(f"  PDF gespeichert: {output_path}")


# ── Hauptprogramm ─────────────────────────────────────────────────────────────

async def main():
    print(f"\n{'='*60}")
    print(f"  DaF Sprachdiagnostik – Testgruppen-Setup")
    print(f"{'='*60}")
    print(f"  Basis-URL:      {BASE_URL}")
    print(f"  Kandidaten:     {args.anzahl} Codes (max. {args.max_nutzungen}× je)")
    print(f"  Admin-Code:     {args.admin} (unbegrenzt)")
    print(f"  Ausgabe-PDF:    {args.out}")
    print(f"{'='*60}\n")

    # Codes generieren
    kandidaten_codes = [generiere_code(args.prefix, i + 1) for i in range(args.anzahl)]
    admin_code = args.admin.upper()

    print("Generierte Kandidaten-Codes:")
    for i, code in enumerate(kandidaten_codes):
        url = f"{BASE_URL}/k/{code}"
        print(f"  {i+1:2d}. {code:20s}  →  {url}")
    print(f"\n  Admin: {admin_code:20s}  →  {BASE_URL}/k/{admin_code}\n")

    # Datenbank-Einträge
    if not args.nur_pdf:
        print("Trage Codes in Datenbank ein…")
        try:
            await trage_codes_ein(kandidaten_codes, admin_code)
        except Exception:
            print("\n  ⚠️  DB-Einträge fehlgeschlagen. PDF wird trotzdem generiert.")
    else:
        print("  (--nur-pdf: DB-Einträge übersprungen)")

    # PDF generieren
    print("\nGeneriere QR-Code-PDF…")
    alle_codes = []

    # Admin zuerst (erste Seite)
    alle_codes.append({
        "code": admin_code,
        "url": f"{BASE_URL}/k/{admin_code}",
        "max_nutzungen": 9999,
        "ist_admin": True,
        "nummer": 0,
    })

    # Kandidaten
    for i, code in enumerate(kandidaten_codes):
        alle_codes.append({
            "code": code,
            "url": f"{BASE_URL}/k/{code}",
            "max_nutzungen": args.max_nutzungen,
            "ist_admin": False,
            "nummer": i + 1,
        })

    erstelle_pdf(alle_codes, args.out)

    # Zusammenfassung
    print(f"\n{'='*60}")
    print(f"  Setup abgeschlossen!")
    print(f"{'='*60}")
    print(f"  PDF zum Ausdrucken: {args.out}")
    print(f"  Admin-URL: {BASE_URL}/k/{admin_code}")
    print(f"\n  Nächste Schritte:")
    print(f"  1. .env ergänzen (KANDIDAT_SMTP_HOST, KANDIDAT_EMAIL_TO usw.)")
    print(f"  2. Server neu starten: systemctl restart daf-plattform")
    print(f"  3. PDF ausdrucken und QR-Codes ausschneiden")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    asyncio.run(main())
