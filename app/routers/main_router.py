"""
Haupt-Router: Alle API-Endpunkte der DaF-Plattform.
"""
import json
import os
import secrets
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.database import (
    CEFRNiveau, GutscheinCode, Hilfssprache, ModulErgebnis, ModulStatus,
    ModulTyp, PAKET_MODULE, PaketTyp, SessionStatus, TestSession, ZahlungsStatus, get_db,
)
from app.services import m1_service, m2_service, m3_service, openai_service, session_service, stripe_service

router = APIRouter()

UPLOAD_DIR = "/tmp/daf_uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)


# ── Hilfsfunktionen ──────────────────────────────────────────────────────────

def _get_modul(sess, modul_typ: ModulTyp) -> Optional[ModulErgebnis]:
    for m in sess.module:
        if m.modul == modul_typ:
            return m
    return None


def _score_to_cefr(score: float) -> str:
    """Leitet CEFR-Niveau aus absolutem Score (0-100) ab."""
    if score >= 80: return "C1"
    if score >= 60: return "B2"
    if score >= 35: return "B1"
    if score >= 15: return "A2"
    return "A1"


# Absolute Score-Skala (0-100) – Niveaugrenzen
# A1: 0–14 | A2: 15–34 | B1: 35–59 | B2: 60–79 | C1/C2: 80–100
_CEFR_ABSOLUT_BEREICHE = {
    "A1": (0,  14),
    "A2": (15, 34),
    "B1": (35, 59),
    "B2": (60, 79),
    "C1": (80, 92),
    "C2": (93, 100),
}


def cefr_relativer_zu_absolut(relativer_score: float, cefr: str) -> float:
    """
    Wandelt einen relativen Modul-Score (0-100) in einen absoluten Score (0-100) um.

    Das CEFR-Niveau bestimmt den Bereich auf der absoluten Skala.
    Der relative Score bestimmt die Position innerhalb dieses Bereichs.

    Beispiel: cefr='B1', relativer_score=72 → absolut = 35 + (72/100 * 24) = 52.3
    """
    cefr = cefr.upper().strip() if cefr else "B1"
    # C2 auf C1 mappen falls nötig
    if cefr not in _CEFR_ABSOLUT_BEREICHE:
        cefr = "B1"
    untergrenze, obergrenze = _CEFR_ABSOLUT_BEREICHE[cefr]
    breite = obergrenze - untergrenze
    # relativer_score (0-100) auf den Bereich projizieren
    absolut = untergrenze + (max(0.0, min(100.0, relativer_score)) / 100.0) * breite
    return round(absolut, 1)


# ── Session ──────────────────────────────────────────────────────────────────

@router.post("/api/session/erstelle")
async def erstelle_session(
    paket: str = Form("demo"),
    hilfssprache: str = Form("de"),
    waehrung: str = Form("CHF"),
    gutschein: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    paket_enum = PaketTyp(paket) if paket in PaketTyp._value2member_map_ else PaketTyp.demo
    hilfs_enum = Hilfssprache(hilfssprache) if hilfssprache in Hilfssprache._value2member_map_ else Hilfssprache.de

    zahlungs_status = ZahlungsStatus.demo
    stripe_pi_id = None

    # Gutschein prüfen
    if gutschein:
        gc = await session_service.validiere_gutschein(db, gutschein)
        if gc:
            paket_enum = gc.paket
            zahlungs_status = ZahlungsStatus.bezahlt
            gc.genutzt += 1
            await db.commit()
        else:
            raise HTTPException(status_code=400, detail="Ungültiger oder abgelaufener Gutscheincode.")

    sess = await session_service.erstelle_session(db, paket_enum, hilfs_enum, waehrung)

    if zahlungs_status == ZahlungsStatus.bezahlt:
        sess.zahlungs_status = ZahlungsStatus.bezahlt
        sess.status = SessionStatus.laufend
        sess.laeuft_ab_am = datetime.now(timezone.utc) + timedelta(hours=2)
        await db.commit()

    return {"token": sess.token, "paket": paket_enum.value, "zahlungs_status": zahlungs_status.value}


@router.get("/api/session/{token}/status")
async def session_status(token: str, db: AsyncSession = Depends(get_db)):
    sess = await session_service.lade_session(db, token)
    if not sess:
        raise HTTPException(status_code=404, detail="Session nicht gefunden.")
    return {
        "token": sess.token,
        "paket": sess.paket.value,
        "status": sess.status.value,
        "zahlungs_status": sess.zahlungs_status.value,
        "grob_niveau": sess.grob_niveau.value if sess.grob_niveau else None,
        "module": [{"modul": m.modul.value, "status": m.status.value, "reihenfolge": m.reihenfolge} for m in sorted(sess.module, key=lambda x: x.reihenfolge)],
    }


# ── Demo-Bypass ─────────────────────────────────────────────────────────────
# Testgruppen-Modus: Demo-Bypass nur noch über den Kandidaten-Router (/k/{code}).
# Direktaufrufe werden mit 403 abgewiesen.

@router.post("/api/zahlung/demo-bypass")
async def demo_bypass(
    token: str = Form(""),
    paket: str = Form("basis"),
    hilfssprache: str = Form("de"),
    waehrung: str = Form("CHF"),
    kandidat_ref: str = Form(""),  # Wird vom Kandidaten-Router gesetzt
    db: AsyncSession = Depends(get_db),
):
    """Demo-Bypass: Gesperrt. Zugang nur über personalisierten QR-Code (/k/{code})."""
    # Testgruppen-Modus: Direktzugang komplett gesperrt
    raise HTTPException(
        status_code=403,
        detail="Zugang nur über personalisierten QR-Code möglich."
    )

    if token:  # noqa: unreachable
        sess = await session_service.lade_session(db, token)
    else:
        paket_enum = PaketTyp(paket) if paket in PaketTyp._value2member_map_ else PaketTyp.basis
        hilfs_enum = Hilfssprache(hilfssprache) if hilfssprache in Hilfssprache._value2member_map_ else Hilfssprache.de
        sess = await session_service.erstelle_session(db, paket_enum, hilfs_enum, waehrung)

    if not sess:
        raise HTTPException(status_code=404, detail="Session nicht gefunden.")

    sess.zahlungs_status = ZahlungsStatus.bezahlt
    sess.status = SessionStatus.laufend
    await db.commit()
    await db.refresh(sess)

    return {"token": sess.token, "paket": sess.paket.value, "redirect": f"/test/{sess.token}"}


# ── Stripe ───────────────────────────────────────────────────────────────────

@router.post("/api/zahlung/erstelle-intent")
async def erstelle_payment_intent(
    paket: str = Form("premium"),
    waehrung: str = Form("CHF"),
    token: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    paket_enum = PaketTyp(paket) if paket in PaketTyp._value2member_map_ else PaketTyp.premium
    result = await stripe_service.erstelle_payment_intent(paket_enum, waehrung, token)

    if result["payment_intent_id"] and token:
        sess = await session_service.lade_session(db, token)
        if sess:
            sess.stripe_payment_intent_id = result["payment_intent_id"]
            await db.commit()

    return result


@router.post("/api/zahlung/webhook")
async def stripe_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    event = await stripe_service.verarbeite_webhook(payload, sig)
    if not event:
        raise HTTPException(status_code=400, detail="Ungültige Webhook-Signatur.")

    if event["type"] == "payment_intent.succeeded":
        pi_id = event["data"]["object"]["id"]
        sess = await session_service.aktiviere_session_nach_zahlung(db, pi_id)

    return {"status": "ok"}


@router.get("/api/zahlung/publishable-key")
async def get_publishable_key():
    return {"key": stripe_service.get_publishable_key()}


# ── M1: Grammatik & Wortschatz (Adaptives CAT) ──────────────────────────────

@router.get("/api/m1/{token}/items")
async def m1_items(token: str, db: AsyncSession = Depends(get_db)):
    """
    Startet den adaptiven M1-Test.
    Gibt 3 Einstiegsfragen (A2/B1/B2 gemischt) zurück.
    Kein Cache – jeder Aufruf generiert frische Fragen.
    """
    sess = await session_service.lade_session(db, token)
    if not sess or sess.zahlungs_status not in (ZahlungsStatus.bezahlt, ZahlungsStatus.demo):
        raise HTTPException(status_code=403, detail="Session nicht bezahlt oder nicht gefunden.")

    modul = _get_modul(sess, ModulTyp.m1_grammatik)
    if not modul:
        raise HTTPException(status_code=404, detail="M1 nicht in diesem Paket.")

    # Adaptiven Test starten – immer frisch generieren (kein Cache)
    zustand = await m1_service.starte_adaptiven_test(sess.hilfssprache.value)

    # Zustand im Modul speichern
    modul.set_roh_antworten({
        "alle_fragen": zustand["fragen"],
        "antworten_verlauf": [],
        "naechste_id": zustand["naechste_id"],
        "geschaetztes_niveau": zustand["geschaetztes_niveau"],
    })
    modul.schwierigkeitsgrad = "B1"
    modul.status = ModulStatus.laufend
    await db.commit()

    return {
        "items": zustand["fragen"],
        "geschaetztes_niveau": zustand["geschaetztes_niveau"],
        "gesamt_fragen": 10,
        "adaptiv": True,
    }


@router.post("/api/m1/{token}/naechste")
async def m1_naechste(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    """
    Verarbeitet eine Antwort und gibt die nächste adaptive Frage zurück.
    Body: {"item_id": 1, "gewaehlt": 2}
    """
    sess = await session_service.lade_session(db, token)
    if not sess:
        raise HTTPException(status_code=404, detail="Session nicht gefunden.")

    modul = _get_modul(sess, ModulTyp.m1_grammatik)
    if not modul:
        raise HTTPException(status_code=404, detail="M1 nicht gefunden.")

    body = await request.json()
    item_id = body.get("item_id")
    gewaehlt = body.get("gewaehlt", -1)

    cached = modul.get_roh_antworten() or {}
    alle_fragen = cached.get("alle_fragen", [])
    antworten_verlauf = cached.get("antworten_verlauf", [])
    naechste_id = cached.get("naechste_id", 4)

    zustand = {
        "alle_fragen": alle_fragen,
        "antworten_verlauf": antworten_verlauf,
        "naechste_id": naechste_id,
    }

    ergebnis = await m1_service.naechste_frage(
        zustand, item_id, gewaehlt, sess.hilfssprache.value
    )

    # Zustand aktualisieren
    alle_fragen.append(ergebnis["frage"])
    modul.set_roh_antworten({
        "alle_fragen": alle_fragen,
        "antworten_verlauf": ergebnis["antworten_verlauf"],
        "naechste_id": ergebnis["naechste_id"],
        "geschaetztes_niveau": ergebnis["geschaetztes_niveau"],
    })
    modul.schwierigkeitsgrad = ergebnis["geschaetztes_niveau"]
    await db.commit()

    return {
        "frage": ergebnis["frage"],
        "geschaetztes_niveau": ergebnis["geschaetztes_niveau"],
        "korrekt": ergebnis["korrekt"],
        "naechste_id": ergebnis["naechste_id"],
    }


@router.post("/api/m1/{token}/submit")
async def m1_submit(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    """
    Endauswertung nach allen 10 Fragen.
    Body: {"antworten": {"1": 2, "2": 0, ...}}
    """
    sess = await session_service.lade_session(db, token)
    if not sess:
        raise HTTPException(status_code=404, detail="Session nicht gefunden.")

    modul = _get_modul(sess, ModulTyp.m1_grammatik)
    if not modul:
        raise HTTPException(status_code=404, detail="M1 nicht gefunden.")

    body = await request.json()
    antworten = body.get("antworten", {})

    cached = modul.get_roh_antworten() or {}
    alle_fragen = cached.get("alle_fragen", [])

    auswertung = await m1_service.werte_aus(alle_fragen, antworten)

    modul.set_ki_analyse(auswertung)
    modul.gesamt_score = cefr_relativer_zu_absolut(auswertung["score"], auswertung["cefr"])
    modul.cefr_niveau = CEFRNiveau(auswertung["cefr"])
    modul.status = ModulStatus.abgeschlossen
    modul.abgeschlossen_am = datetime.now(timezone.utc)

    sess.grob_niveau = CEFRNiveau(auswertung["cefr"])
    sess.status = SessionStatus.laufend
    sess.laeuft_ab_am = datetime.now(timezone.utc) + timedelta(hours=2)
    await db.commit()

    if sess.alle_abgeschlossen():
        await session_service.berechne_gesamt_ergebnis(db, sess)

    return auswertung


# ── M2: Lesen & Leseverstehen ────────────────────────────────────────────────

@router.get("/api/m2/{token}/aufgabe")
async def m2_aufgabe(token: str, db: AsyncSession = Depends(get_db)):
    sess = await session_service.lade_session(db, token)
    if not sess:
        raise HTTPException(status_code=404, detail="Session nicht gefunden.")

    modul = _get_modul(sess, ModulTyp.m2_lesen)
    if not modul:
        raise HTTPException(status_code=404, detail="M2 nicht in diesem Paket.")

    # Adaptives Niveau aus M1-Ergebnis übernehmen, immer frisch generieren
    niveau = sess.grob_niveau.value if sess.grob_niveau else "B1"
    aufgabe = await m2_service.generiere_leseaufgabe(niveau, sess.hilfssprache.value)
    modul.set_roh_antworten({"aufgabe": aufgabe})
    modul.schwierigkeitsgrad = niveau
    modul.status = ModulStatus.laufend
    await db.commit()

    return aufgabe


@router.post("/api/m2/{token}/submit")
async def m2_submit(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    sess = await session_service.lade_session(db, token)
    if not sess:
        raise HTTPException(status_code=404, detail="Session nicht gefunden.")

    modul = _get_modul(sess, ModulTyp.m2_lesen)
    if not modul:
        raise HTTPException(status_code=404, detail="M2 nicht gefunden.")

    body = await request.json()
    antworten = body.get("antworten", {})

    cached = modul.get_roh_antworten()
    aufgabe = cached.get("aufgabe", {})
    fragen = aufgabe.get("fragen", [])

    auswertung = await openai_service.analysiere_lesen(
        aufgabe.get("text", ""), fragen, antworten, modul.schwierigkeitsgrad or "B1"
    )

    modul.set_ki_analyse(auswertung)
    modul.gesamt_score = cefr_relativer_zu_absolut(auswertung["score"], auswertung["cefr"])
    modul.cefr_niveau = CEFRNiveau(auswertung["cefr"])
    modul.status = ModulStatus.abgeschlossen
    modul.abgeschlossen_am = datetime.now(timezone.utc)
    sess.laeuft_ab_am = datetime.now(timezone.utc) + timedelta(hours=2)
    await db.commit()

    if sess.alle_abgeschlossen():
        await session_service.berechne_gesamt_ergebnis(db, sess)

    return auswertung


# ── M3: Hörverstehen ─────────────────────────────────────────────────────────

@router.get("/api/m3/{token}/aufgabe")
async def m3_aufgabe(token: str, db: AsyncSession = Depends(get_db)):
    sess = await session_service.lade_session(db, token)
    if not sess:
        raise HTTPException(status_code=404, detail="Session nicht gefunden.")

    modul = _get_modul(sess, ModulTyp.m3_hoerverstehen)
    if not modul:
        raise HTTPException(status_code=404, detail="M3 nicht in diesem Paket.")

    # Adaptives Niveau aus M1-Ergebnis übernehmen, immer frisch generieren
    niveau = sess.grob_niveau.value if sess.grob_niveau else "B1"
    aufgabe = await m3_service.generiere_hoeraufgabe(niveau, sess.hilfssprache.value)
    modul.set_roh_antworten({"aufgabe": aufgabe})
    modul.schwierigkeitsgrad = niveau
    modul.status = ModulStatus.laufend
    await db.commit()

    return aufgabe


@router.post("/api/m3/{token}/submit")
async def m3_submit(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    sess = await session_service.lade_session(db, token)
    if not sess:
        raise HTTPException(status_code=404, detail="Session nicht gefunden.")

    modul = _get_modul(sess, ModulTyp.m3_hoerverstehen)
    if not modul:
        raise HTTPException(status_code=404, detail="M3 nicht gefunden.")

    body = await request.json()
    antworten = body.get("antworten", {})

    cached = modul.get_roh_antworten()
    aufgabe = cached.get("aufgabe", {})
    fragen = aufgabe.get("fragen", [])

    auswertung = await openai_service.analysiere_hoerverstehen(fragen, antworten, modul.schwierigkeitsgrad or "B1")

    modul.set_ki_analyse(auswertung)
    modul.gesamt_score = cefr_relativer_zu_absolut(auswertung["score"], auswertung["cefr"])
    modul.cefr_niveau = CEFRNiveau(auswertung["cefr"])
    modul.status = ModulStatus.abgeschlossen
    modul.abgeschlossen_am = datetime.now(timezone.utc)
    sess.laeuft_ab_am = datetime.now(timezone.utc) + timedelta(hours=2)
    await db.commit()

    if sess.alle_abgeschlossen():
        await session_service.berechne_gesamt_ergebnis(db, sess)

    return auswertung


# ── M4: Vorlesen ─────────────────────────────────────────────────────────────

@router.get("/api/m4/{token}/text")
async def m4_text(token: str, db: AsyncSession = Depends(get_db)):
    sess = await session_service.lade_session(db, token)
    if not sess:
        raise HTTPException(status_code=404, detail="Session nicht gefunden.")

    modul = _get_modul(sess, ModulTyp.m4_vorlesen)
    if not modul:
        raise HTTPException(status_code=404, detail="M4 nicht in diesem Paket.")

    # Vorlese-Sätze aus M2 übernehmen falls vorhanden
    m2 = _get_modul(sess, ModulTyp.m2_lesen)
    if m2 and m2.roh_antworten_json:
        cached = m2.get_roh_antworten()
        saetze = cached.get("aufgabe", {}).get("vorlese_saetze", [])
        if saetze:
            return {"saetze": saetze, "niveau": modul.schwierigkeitsgrad or "B1"}

    # GPT generiert frische Vorlese-Sätze
    niveau = sess.grob_niveau.value if sess.grob_niveau else "B1"
    import random
    themen_pool = {
        "A1": ["Einkaufen im Supermarkt", "Familie und Zuhause", "Beim Arzt", "Mit dem Bus fahren", "Essen und Kochen"],
        "A2": ["Arztbesuch und Krankmeldung", "Wohnung suchen", "Mit dem Zug fahren", "Auf dem Amt", "Arbeit und Kollegen"],
        "B1": ["Wohnungssuche und Mietvertrag", "Gespräch beim Jobcenter", "Kinder und Schule", "Gesundheit und Vorsorge", "Nachbarschaft und Alltag"],
        "B2": ["Pflege von Angehörigen", "Berufseinstieg und Bewerbung", "Mietpreise und Wohnungsnot", "Familie und Beruf vereinbaren", "Gesundheitsversorgung"],
        "C1": ["Soziale Ungleichheit im Alltag", "Chancen und Hürden auf dem Arbeitsmarkt", "Wohnen als gesellschaftliche Frage", "Pflege und Würde im Alter"],
    }
    thema = random.choice(themen_pool.get(niveau, themen_pool["B1"]))
    try:
        from openai import AsyncOpenAI
        from app.config import settings as cfg
        oai = AsyncOpenAI(api_key=cfg.openai_api_key)
        resp = await oai.chat.completions.create(
            model="gpt-4.1",
            messages=[{"role": "user", "content": f"""Erstelle 3 deutsche Sätze zum Thema '{thema}' für CEFR-Niveau {niveau} zum Vorlesen.
Anforderungen: Natürliche Sprache, für das Niveau angemessene Komplexität, keine Listen.
Antworte NUR mit JSON: {{"saetze": ["Satz 1", "Satz 2", "Satz 3"]}}"""}],
            response_format={"type": "json_object"},
            temperature=0.9,
        )
        import json as _json
        saetze = _json.loads(resp.choices[0].message.content).get("saetze", [])
        if not saetze:
            raise ValueError("Leere Antwort")
    except Exception as e:
        print(f"[M4] Vorlese-Generierung fehlgeschlagen: {e}")
        fallback = {
            "A1": ["Ich kaufe jeden Tag Brot beim Bäcker.", "Meine Mutter kocht gerne Suppe.", "Das Busticket kostet zwei Euro."],
            "A2": ["Jeden Morgen fahre ich mit dem Bus zur Arbeit.", "Ich habe nächste Woche einen Termin beim Arzt.", "Am Wochenende räume ich meine Wohnung auf."],
            "B1": ["Viele Familien suchen eine bezahlbare Wohnung in der Nähe der Schule.", "Wer krank ist, sollte früh zum Arzt gehen und nicht zu lange warten.", "Ein gutes Gespräch mit dem Chef kann helfen, Probleme im Job zu lösen."],
            "B2": ["Wer Angehörige pflegt, braucht Unterstützung vom Staat und von der Familie.", "Eine Bewerbung auf Deutsch zu schreiben ist für viele Zugewanderte eine große Hürde.", "Steigende Mieten machen es für viele Familien schwer, in der Stadt zu bleiben."],
            "C1": ["Armut trotz Arbeit ist ein wachsendes Problem in wohlhabenden Gesellschaften.", "Wer in einer anderen Sprache aufgewachsen ist, erlebt Deutsch oft als Schlüssel und Barriere zugleich.", "Die Pflege älterer Menschen ist eine gesellschaftliche Aufgabe, die mehr Anerkennung verdient."],
        }
        saetze = fallback.get(niveau, fallback["B1"])

    modul.schwierigkeitsgrad = niveau
    modul.status = ModulStatus.laufend
    await db.commit()

    return {"saetze": saetze, "niveau": niveau}


@router.post("/api/m4/{token}/upload")
async def m4_upload(
    token: str,
    audio: UploadFile = File(...),
    mime_type: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    sess = await session_service.lade_session(db, token)
    if not sess:
        raise HTTPException(status_code=404, detail="Session nicht gefunden.")

    modul = _get_modul(sess, ModulTyp.m4_vorlesen)
    if not modul:
        raise HTTPException(status_code=404, detail="M4 nicht gefunden.")

    # Dateiendung aus Dateiname oder Content-Type ableiten (iOS liefert .mp4)
    original_name = audio.filename or ""
    content_type = mime_type or audio.content_type or ""
    if original_name.endswith(".mp4") or "mp4" in content_type:
        ext = ".mp4"
    elif original_name.endswith(".ogg") or "ogg" in content_type:
        ext = ".ogg"
    else:
        ext = ".webm"

    pfad = os.path.join(UPLOAD_DIR, f"m4_{token}_{secrets.token_hex(8)}{ext}")
    content = await audio.read()
    max_bytes = settings.max_audio_mb * 1024 * 1024
    if len(content) > max_bytes:
        raise HTTPException(status_code=413, detail=f"Datei zu groß (max. {settings.max_audio_mb} MB).")

    with open(pfad, "wb") as f:
        f.write(content)

    modul.audio_pfad = pfad
    modul.status = ModulStatus.laufend
    await db.commit()

    return {"status": "hochgeladen", "pfad": pfad}


@router.post("/api/m4/{token}/analysiere")
async def m4_analysiere(token: str, db: AsyncSession = Depends(get_db)):
    sess = await session_service.lade_session(db, token)
    if not sess:
        raise HTTPException(status_code=404, detail="Session nicht gefunden.")

    modul = _get_modul(sess, ModulTyp.m4_vorlesen)
    if not modul or not modul.audio_pfad:
        raise HTTPException(status_code=400, detail="Kein Audio hochgeladen.")

    # Vorlese-Text ermitteln
    m2 = _get_modul(sess, ModulTyp.m2_lesen)
    vorlesetext = ""
    if m2 and m2.roh_antworten_json:
        saetze = m2.get_roh_antworten().get("aufgabe", {}).get("vorlese_saetze", [])
        vorlesetext = " ".join(saetze)

    analyse = await openai_service.analysiere_vorlesen(
        modul.audio_pfad, vorlesetext, modul.schwierigkeitsgrad or "B1"
    )

    # DSGVO: Audio löschen
    if settings.delete_audio_after_analysis:
        await session_service.loesche_mediendateien(modul)

    modul.set_ki_analyse(analyse)
    audio_score = analyse.get("audio_analyse", {})
    if audio_score:
        raw_score = audio_score.get("gesamt_score", 0)
        # GPT gibt 0-10 zurück → auf 0-100 normalisieren
        relativer_score = round(raw_score * 10, 1) if raw_score <= 10 else raw_score
        cefr_str = audio_score.get("cefr_niveau", "B1")
    else:
        relativer_score = analyse.get("lesegenauigkeit", 50)
        cefr_str = _score_to_cefr(relativer_score)

    cefr_str = cefr_str if cefr_str in CEFRNiveau._value2member_map_ else "B1"
    modul.gesamt_score = cefr_relativer_zu_absolut(relativer_score, cefr_str)
    modul.cefr_niveau = CEFRNiveau(cefr_str)
    modul.status = ModulStatus.abgeschlossen
    modul.abgeschlossen_am = datetime.now(timezone.utc)
    sess.laeuft_ab_am = datetime.now(timezone.utc) + timedelta(hours=2)
    await db.commit()

    if sess.alle_abgeschlossen():
        await session_service.berechne_gesamt_ergebnis(db, sess)

    return analyse


# ── M5: Sprechen ─────────────────────────────────────────────────────────────

@router.get("/api/m5/{token}/thema")
async def m5_thema(token: str, db: AsyncSession = Depends(get_db)):
    sess = await session_service.lade_session(db, token)
    if not sess:
        raise HTTPException(status_code=404, detail="Session nicht gefunden.")

    niveau = sess.grob_niveau.value if sess.grob_niveau else "B1"

    # Niveau-spezifische Vorgaben für Sprechaufgabe
    vorgaben = {
        "A1": ("1 Minute", "einfache Selbstvorstellung oder Alltagsbeschreibung", "Einfache Sätze, Präsens"),
        "A2": ("1–2 Minuten", "Erzählung aus dem Alltag oder Freizeitbeschreibung", "Einfache Verbindungen, Vergangenheit"),
        "B1": ("2 Minuten", "Meinungs\u00e4u\u00dferung oder Erfahrungsbericht", "Begr\u00fcndungen, Nebens\u00e4tze"),
        "B2": ("2–3 Minuten", "Argumentativer Vortrag oder Diskussion", "Komplexe Strukturen, Modalit\u00e4t"),
        "C1": ("3 Minuten", "Analyse oder kritische Auseinandersetzung", "Nuancierte Sprache, Fachvokabular"),
    }
    dauer, aufgabentyp, sprachl_anforderungen = vorgaben.get(niveau, vorgaben["B1"])

    try:
        from openai import AsyncOpenAI
        from app.config import settings as cfg
        import random
        oai = AsyncOpenAI(api_key=cfg.openai_api_key)
        resp = await oai.chat.completions.create(
            model="gpt-4.1",
            messages=[{"role": "user", "content": f"""Erstelle eine m\u00fcndliche Sprechaufgabe f\u00fcr DaF-Lernende auf CEFR-Niveau {niveau}.

Anforderungen:
- Aufgabentyp: {aufgabentyp}
- Sprechdauer: {dauer}
- Sprachliche Anforderungen: {sprachl_anforderungen}
- Konkretes, lebensnahes Thema passend zum Niveau – Themen, die wirklich jeden betreffen: z.B. Arztbesuch, Wohnungssuche, Busfahren, Einkaufen, Nachbarn, Kinder und Schule, Jobsuche, Behördengänge, Krankmeldung, Feierabend, Kochen, Familie, Freunde, Urlaub, Geld, Gesundheit – für höhere Niveaus auch: Pflege, Mietpreise, Berufseinstieg, Familie und Arbeit vereinbaren, soziale Absicherung
- Kein Klimawandel, keine KI, keine Wissenschaft, keine akademischen Themen
- 2-3 konkrete Leitfragen oder Sprechimpulse

Antworte NUR mit JSON: {{\"thema\": \"Kurzer Titel\", \"aufgabe\": \"Die vollst\u00e4ndige Aufgabenstellung mit Leitfragen\"}}"""}],
            response_format={"type": "json_object"},
            temperature=0.9,
        )
        import json as _json
        data = _json.loads(resp.choices[0].message.content)
        thema_titel = data.get("thema", "Freies Sprechen")
        aufgabe_text = data.get("aufgabe") or thema_titel
        thema = aufgabe_text  # vollständige Aufgabenstellung für Speicherung
    except Exception as e:
        print(f"[M5] Thema-Generierung fehlgeschlagen: {e}")
        fallback = {
            "A1": ("Selbstvorstellung", "Stell dich vor: Wie heißt du? Wo wohnst du? Was machst du gerne?"),
            "A2": ("Mein Alltag", "Erzähl von deinem Alltag: Was machst du morgens? Wie kommst du zur Arbeit oder zur Schule? Was isst du gerne?"),
            "B1": ("Meine Wohnsituation", "Beschreibe, wo du wohnst: Wie ist deine Wohnung? Was gefällt dir? Was ist manchmal schwierig – zum Beispiel mit Nachbarn, Vermieter oder Nebenkosten?"),
            "B2": ("Familie und Beruf", "Erzähl: Wie organisierst du deinen Alltag mit Familie und Arbeit? Was ist einfach, was ist schwierig? Was wünschst du dir?"),
            "C1": ("Wohnen in Deutschland", "Diskutiere: Warum ist es für viele Menschen so schwer, eine bezahlbare Wohnung zu finden? Was sind die Ursachen? Was könnte helfen?"),
        }
        thema_titel, aufgabe_text = fallback.get(niveau, fallback["B1"])
        thema = aufgabe_text

    modul = _get_modul(sess, ModulTyp.m5_sprechen)
    if modul:
        modul.schwierigkeitsgrad = niveau
        modul.set_roh_antworten({"thema": thema})
        modul.status = ModulStatus.laufend
        await db.commit()

    return {"thema": aufgabe_text, "thema_titel": thema_titel, "niveau": niveau, "dauer_sekunden": 90}


@router.post("/api/m5/{token}/upload")
async def m5_upload(
    token: str,
    audio: UploadFile = File(...),
    modus: str = Form("tief"),
    mime_type: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    sess = await session_service.lade_session(db, token)
    if not sess:
        raise HTTPException(status_code=404, detail="Session nicht gefunden.")

    modul = _get_modul(sess, ModulTyp.m5_sprechen)
    if not modul:
        raise HTTPException(status_code=404, detail="M5 nicht gefunden.")

    # Dateiendung aus Dateiname oder Content-Type ableiten (iOS liefert .mp4)
    original_name = audio.filename or ""
    content_type = mime_type or audio.content_type or ""
    if original_name.endswith(".mp4") or "mp4" in content_type:
        ext = ".mp4"
    elif original_name.endswith(".ogg") or "ogg" in content_type:
        ext = ".ogg"
    else:
        ext = ".webm"

    pfad = os.path.join(UPLOAD_DIR, f"m5_{token}_{secrets.token_hex(8)}{ext}")
    content = await audio.read()
    max_bytes = settings.max_audio_mb * 1024 * 1024
    if len(content) > max_bytes:
        raise HTTPException(status_code=413, detail=f"Datei zu groß.")

    with open(pfad, "wb") as f:
        f.write(content)

    cached = modul.get_roh_antworten()
    cached["modus"] = modus
    modul.set_roh_antworten(cached)
    modul.audio_pfad = pfad
    await db.commit()

    return {"status": "hochgeladen"}


@router.post("/api/m5/{token}/analysiere")
async def m5_analysiere(token: str, db: AsyncSession = Depends(get_db)):
    sess = await session_service.lade_session(db, token)
    if not sess:
        raise HTTPException(status_code=404, detail="Session nicht gefunden.")

    modul = _get_modul(sess, ModulTyp.m5_sprechen)
    if not modul or not modul.audio_pfad:
        raise HTTPException(status_code=400, detail="Kein Audio hochgeladen.")

    cached = modul.get_roh_antworten()
    thema = cached.get("thema", "Freies Sprechen")
    modus = cached.get("modus", "tief")

    # Transkription
    transkript_data = await openai_service.transkribiere_audio(modul.audio_pfad)
    transkript = transkript_data["text"]

    # Vollanalyse
    analyse = await openai_service.analysiere_sprechen(
        modul.audio_pfad, transkript, thema, modul.schwierigkeitsgrad or "B1", modus
    )

    # DSGVO: Audio löschen
    if settings.delete_audio_after_analysis:
        await session_service.loesche_mediendateien(modul)

    modul.set_ki_analyse(analyse)
    text_analyse = analyse.get("text_analyse", {})
    raw_score = text_analyse.get("gesamt_score", 5.0)
    # GPT gibt 0-10 zurück → auf 0-100 normalisieren
    relativer_score = round(raw_score * 10, 1) if raw_score <= 10 else raw_score
    cefr_str = text_analyse.get("cefr_niveau", _score_to_cefr(relativer_score))
    cefr_str = cefr_str if cefr_str in CEFRNiveau._value2member_map_ else "B1"
    modul.gesamt_score = cefr_relativer_zu_absolut(relativer_score, cefr_str)
    modul.cefr_niveau = CEFRNiveau(cefr_str)
    modul.status = ModulStatus.abgeschlossen
    modul.abgeschlossen_am = datetime.now(timezone.utc)
    sess.laeuft_ab_am = datetime.now(timezone.utc) + timedelta(hours=2)
    await db.commit()

    if sess.alle_abgeschlossen():
        await session_service.berechne_gesamt_ergebnis(db, sess)

    return analyse


# ── M6: Schreiben ─────────────────────────────────────────────────────────────

@router.get("/api/m6/{token}/aufgabe")
async def m6_aufgabe(token: str, db: AsyncSession = Depends(get_db)):
    sess = await session_service.lade_session(db, token)
    if not sess:
        raise HTTPException(status_code=404, detail="Session nicht gefunden.")

    niveau = sess.grob_niveau.value if sess.grob_niveau else "B1"
    import random

    # Niveau-spezifische Textsorte und Länge
    vorgaben = {
        "A1": ("3–4 Sätze", "persönliche Vorstellung oder Alltagsbeschreibung"),
        "A2": ("5–7 Sätze", "persönliche Nachricht oder Beschreibung"),
        "B1": ("8–10 Sätze", "Meinungstext oder Erfahrungsbericht"),
        "B2": ("12–15 Sätze", "Argumentativer Text oder Kommentar"),
        "C1": ("150–200 Wörter", "Essay oder Analyse"),
        "C2": ("200–250 Wörter", "Wissenschaftlicher oder literarischer Text"),
    }
    laenge, textsorte = vorgaben.get(niveau, vorgaben["B1"])

    try:
        from openai import AsyncOpenAI
        from app.config import settings as cfg
        oai = AsyncOpenAI(api_key=cfg.openai_api_key)
        resp = await oai.chat.completions.create(
            model="gpt-4.1",
            messages=[{"role": "user", "content": f"""Erstelle eine Schreibaufgabe für DaF-Lernende auf CEFR-Niveau {niveau}.

Anforderungen:
- Textsorte: {textsorte}
- Länge: {laenge}
- Konkretes, lebensnahes Thema passend zum Niveau – Themen, die wirklich jeden betreffen: z.B. Arztbesuch, Wohnungssuche, Einkaufen, Nachbarn, Kinder und Schule, Jobsuche, Behördengänge, Kochen, Familie, Freunde, Urlaub, Geld, Gesundheit, Feierabend – für höhere Niveaus auch: Pflege, Mietpreise, Berufseinstieg, Familie und Arbeit vereinbaren, soziale Absicherung
- Kein Klimawandel, keine KI, keine Wissenschaft, keine rein akademischen Themen
- Klare Aufgabenstellung mit 2–3 Leitfragen oder Punkten

Antworte NUR mit JSON: {"aufgabe": "Die vollständige Aufgabenstellung auf Deutsch"}"""}],
            response_format={"type": "json_object"},
            temperature=0.9,
        )
        import json as _json
        aufgabe_text = _json.loads(resp.choices[0].message.content).get("aufgabe", "")
        if not aufgabe_text:
            raise ValueError("Leere Antwort")
    except Exception as e:
        print(f"[M6] Aufgaben-Generierung fehlgeschlagen: {e}")
        fallback = {
            "A1": "Schreibe 3–4 Sätze über dich: Wie heißt du? Wo wohnst du? Was machst du gerne?",
            "A2": "Du warst letzte Woche beim Arzt. Schreibe eine kurze Nachricht (5–6 Sätze) an einen Freund: Warum warst du dort? Wie war es? Was hat der Arzt gesagt?",
            "B1": "Schreibe einen Text (8–10 Sätze) über deine Wohnsituation: Wo wohnst du? Was gefällt dir an deiner Wohnung oder deinem Wohnort? Was ist manchmal schwierig – zum Beispiel mit dem Vermieter, den Nachbarn oder den Kosten?",
            "B2": "Viele Menschen haben Schwierigkeiten, Beruf und Familie zu vereinbaren. Schreibe einen Meinungstext (12–15 Sätze): Was sind die größten Herausforderungen? Was hilft? Was wünschst du dir von Arbeitgebern oder dem Staat?",
            "C1": "Schreibe einen Essay (150–200 Wörter) zum Thema: 'In Deutschland ist es für viele Menschen sehr schwer, eine bezahlbare Wohnung zu finden.' Erkläre die Ursachen, beschreibe die Folgen für Betroffene und schlage mögliche Lösungen vor.",
        }
        aufgabe_text = fallback.get(niveau, fallback["B1"])

    modul = _get_modul(sess, ModulTyp.m6_schreiben)
    if modul:
        modul.set_roh_antworten({"aufgabe": aufgabe_text})
        modul.schwierigkeitsgrad = niveau
        modul.status = ModulStatus.laufend
        await db.commit()

    return {"aufgabe": aufgabe_text, "niveau": niveau}


@router.post("/api/m6/{token}/submit")
async def m6_submit(
    token: str,
    text: str = Form(""),
    bild: Optional[UploadFile] = File(None),
    db: AsyncSession = Depends(get_db),
):
    sess = await session_service.lade_session(db, token)
    if not sess:
        raise HTTPException(status_code=404, detail="Session nicht gefunden.")

    modul = _get_modul(sess, ModulTyp.m6_schreiben)
    if not modul:
        raise HTTPException(status_code=404, detail="M6 nicht gefunden.")

    bild_pfad = None
    if bild and bild.filename:
        content = await bild.read()
        max_bytes = settings.max_image_mb * 1024 * 1024
        if len(content) > max_bytes:
            raise HTTPException(status_code=413, detail=f"Bild zu groß (max. {settings.max_image_mb} MB).")
        ext = os.path.splitext(bild.filename)[1].lower() or ".jpg"
        bild_pfad = os.path.join(UPLOAD_DIR, f"m6_{token}_{secrets.token_hex(8)}{ext}")
        with open(bild_pfad, "wb") as f:
            f.write(content)
        modul.bild_pfad = bild_pfad

    cached = modul.get_roh_antworten()
    aufgabe = cached.get("aufgabe", "")

    analyse = await openai_service.analysiere_schreiben(
        text=text if text else None,
        bild_pfad=bild_pfad,
        aufgabe=aufgabe,
        niveau=modul.schwierigkeitsgrad or "B1",
    )

    # DSGVO: Bild löschen
    if settings.delete_image_after_analysis:
        await session_service.loesche_mediendateien(modul)

    modul.set_ki_analyse(analyse)
    raw_score = analyse.get("gesamt_score", 5.0)
    # GPT gibt 0-10 zurück → auf 0-100 normalisieren
    relativer_score = round(raw_score * 10, 1) if raw_score <= 10 else raw_score
    cefr_str = analyse.get("cefr_niveau", _score_to_cefr(relativer_score))
    cefr_str = cefr_str if cefr_str in CEFRNiveau._value2member_map_ else "B1"
    modul.gesamt_score = cefr_relativer_zu_absolut(relativer_score, cefr_str)
    modul.cefr_niveau = CEFRNiveau(cefr_str)
    modul.status = ModulStatus.abgeschlossen
    modul.abgeschlossen_am = datetime.now(timezone.utc)
    sess.laeuft_ab_am = datetime.now(timezone.utc) + timedelta(hours=2)
    await db.commit()

    if sess.alle_abgeschlossen():
        await session_service.berechne_gesamt_ergebnis(db, sess)

    return analyse


# ── Ergebnis ─────────────────────────────────────────────────────────────────

@router.get("/api/ergebnis/{token}")
async def ergebnis(token: str, db: AsyncSession = Depends(get_db)):
    sess = await session_service.lade_session(db, token)
    if not sess:
        raise HTTPException(status_code=404, detail="Session nicht gefunden.")

    module_data = []
    for m in sorted(sess.module, key=lambda x: x.reihenfolge):
        module_data.append({
            "modul": m.modul.value,
            "status": m.status.value,
            "cefr": m.cefr_niveau.value if m.cefr_niveau else None,
            "score": m.gesamt_score,
            "analyse": m.get_ki_analyse(),
        })

    return {
        "token": sess.token,
        "paket": sess.paket.value,
        "status": sess.status.value,
        "gesamt_score": sess.gesamt_score,
        "gesamt_niveau": sess.gesamt_niveau.value if sess.gesamt_niveau else None,
        "grob_niveau": sess.grob_niveau.value if sess.grob_niveau else None,
        "module": module_data,
        "abgeschlossen_am": sess.abgeschlossen_am.isoformat() if sess.abgeschlossen_am else None,
    }


# ── PDF-Export ────────────────────────────────────────────────────────────────

def _zeichne_radar(module_daten: list) -> object:
    """Zeichnet ein Spinnennetz-Diagramm (Radar-Chart) mit ReportLab."""
    import math
    from reportlab.graphics.shapes import Drawing, Polygon, Circle, Line, String, Group
    from reportlab.lib import colors

    n = len(module_daten)
    if n < 3:
        return None

    size = 200
    cx, cy = size / 2, size / 2
    r_max = size / 2 - 30  # Radius für 100%

    drawing = Drawing(size, size)

    # Hintergrund-Ringe (20, 40, 60, 80, 100)
    ring_farbe = colors.HexColor('#e5e7eb')
    for pct in [20, 40, 60, 80, 100]:
        r = r_max * pct / 100
        punkte = []
        for i in range(n):
            winkel = math.pi / 2 + 2 * math.pi * i / n
            punkte.extend([cx + r * math.cos(winkel), cy + r * math.sin(winkel)])
        poly = Polygon(punkte, strokeColor=ring_farbe, strokeWidth=0.5, fillColor=None)
        drawing.add(poly)

    # Achsen
    for i in range(n):
        winkel = math.pi / 2 + 2 * math.pi * i / n
        x_end = cx + r_max * math.cos(winkel)
        y_end = cy + r_max * math.sin(winkel)
        drawing.add(Line(cx, cy, x_end, y_end, strokeColor=ring_farbe, strokeWidth=0.5))

    # Datenpunkte
    data_punkte = []
    for i, (name, score) in enumerate(module_daten):
        r = r_max * min(score, 100) / 100
        winkel = math.pi / 2 + 2 * math.pi * i / n
        data_punkte.extend([cx + r * math.cos(winkel), cy + r * math.sin(winkel)])

    # Gefüllte Fläche
    if len(data_punkte) >= 6:
        poly_fill = Polygon(
            data_punkte,
            strokeColor=colors.HexColor('#2563eb'),
            strokeWidth=1.5,
            fillColor=colors.HexColor('#2563eb'),
            fillOpacity=0.2,
        )
        drawing.add(poly_fill)

    # Punkte auf den Achsen
    for i, (name, score) in enumerate(module_daten):
        r = r_max * min(score, 100) / 100
        winkel = math.pi / 2 + 2 * math.pi * i / n
        px = cx + r * math.cos(winkel)
        py = cy + r * math.sin(winkel)
        dot_farbe = colors.HexColor('#22c55e') if score >= 70 else colors.HexColor('#f59e0b') if score >= 40 else colors.HexColor('#ef4444')
        drawing.add(Circle(px, py, 4, fillColor=dot_farbe, strokeColor=colors.white, strokeWidth=1))

    # Achsenbeschriftungen
    for i, (name, score) in enumerate(module_daten):
        winkel = math.pi / 2 + 2 * math.pi * i / n
        label_r = r_max + 18
        lx = cx + label_r * math.cos(winkel)
        ly = cy + label_r * math.sin(winkel)
        # Textanker je nach Position
        if lx < cx - 5:
            anchor = 'end'
        elif lx > cx + 5:
            anchor = 'start'
        else:
            anchor = 'middle'
        s = String(lx, ly - 4, name, fontSize=7.5, fontName='Helvetica-Bold',
                   fillColor=colors.HexColor('#374151'), textAnchor=anchor)
        drawing.add(s)

    return drawing


@router.get("/api/export/pdf/{token}")
async def export_pdf(token: str, db: AsyncSession = Depends(get_db)):
    """Exportiert den vollständigen Einstufungsbericht als PDF mit Radar-Diagramm."""
    from fastapi.responses import StreamingResponse
    import io

    sess = await session_service.lade_session(db, token)
    if not sess:
        raise HTTPException(status_code=404, detail="Session nicht gefunden.")

    gesamt_score = round(sess.gesamt_score or 0)
    gesamt_niveau = sess.gesamt_niveau.value if sess.gesamt_niveau else "–"
    datum = datetime.now(timezone.utc).strftime("%d.%m.%Y")

    MODUL_NAMEN = {
        'm1_grammatik': 'Grammatik & Wortschatz',
        'm2_lesen': 'Lesen & Leseverstehen',
        'm3_hoerverstehen': 'Hörverstehen',
        'm4_vorlesen': 'Vorlesen',
        'm5_sprechen': 'Freies Sprechen',
        'm6_schreiben': 'Schreiben',
    }
    MODUL_KURZ = {
        'm1_grammatik': 'Grammatik',
        'm2_lesen': 'Lesen',
        'm3_hoerverstehen': 'Hören',
        'm4_vorlesen': 'Vorlesen',
        'm5_sprechen': 'Sprechen',
        'm6_schreiben': 'Schreiben',
    }

    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.lib import colors
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable, KeepTogether
        from reportlab.platypus.flowables import HRFlowable
        from reportlab.lib.enums import TA_CENTER, TA_LEFT
        from reportlab.graphics import renderPDF
        from reportlab.graphics.shapes import Drawing

        pdf_buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            pdf_buffer, pagesize=A4,
            leftMargin=2*cm, rightMargin=2*cm, topMargin=2*cm, bottomMargin=2*cm
        )
        styles = getSampleStyleSheet()
        story = []

        # ── Stile ────────────────────────────────────────────────────
        title_style = ParagraphStyle('title', parent=styles['Title'], fontSize=20,
            textColor=colors.HexColor('#1e3a5f'), spaceAfter=4)
        sub_style = ParagraphStyle('sub', parent=styles['Normal'], fontSize=9,
            textColor=colors.HexColor('#6b7280'), spaceAfter=4)
        ki_style = ParagraphStyle('ki', parent=styles['Normal'], fontSize=8,
            textColor=colors.HexColor('#0369a1'), spaceAfter=16,
            backColor=colors.HexColor('#f0f9ff'), borderPadding=(4, 6, 4, 6))
        h2_style = ParagraphStyle('h2', parent=styles['Heading2'], fontSize=13,
            textColor=colors.HexColor('#1e3a5f'), spaceBefore=16, spaceAfter=8)
        h3_style = ParagraphStyle('h3', parent=styles['Heading3'], fontSize=10,
            textColor=colors.HexColor('#374151'), spaceBefore=10, spaceAfter=4)
        body_style = ParagraphStyle('body', parent=styles['Normal'], fontSize=9,
            textColor=colors.HexColor('#374151'), spaceAfter=4)
        muted_style = ParagraphStyle('muted', parent=styles['Normal'], fontSize=8.5,
            textColor=colors.HexColor('#6b7280'), spaceAfter=4)
        skill_style = ParagraphStyle('skill', parent=styles['Normal'], fontSize=8,
            textColor=colors.HexColor('#374151'), spaceAfter=2, leftIndent=8)
        footer_style = ParagraphStyle('footer', parent=styles['Normal'], fontSize=7.5,
            textColor=colors.HexColor('#9ca3af'))

        # ── Kopfzeile ────────────────────────────────────────────────
        story.append(Paragraph('Einstufungsbericht – DaF Sprachdiagnostik', title_style))
        story.append(Paragraph(f'Erstellt am {datum}  |  Session: {token[:8]}…', sub_style))
        story.append(Paragraph('KI-generierter Bericht – gelegentliche Ungenauigkeiten möglich.', ki_style))
        story.append(HRFlowable(width='100%', thickness=1, color=colors.HexColor('#e5e7eb'), spaceAfter=12))

        # ── Gesamtergebnis-Box ────────────────────────────────────────
        cefr_farben = {'A1':'#dc2626','A2':'#ea580c','B1':'#d97706','B2':'#65a30d','C1':'#0891b2','C2':'#7c3aed'}
        cefr_farbe = cefr_farben.get(gesamt_niveau, '#374151')
        score_color_hex = '#22c55e' if gesamt_score >= 70 else '#f59e0b' if gesamt_score >= 40 else '#ef4444'

        score_data = [
            [Paragraph('<b>Gesamtscore</b>', body_style), Paragraph('<b>CEFR-Einstufung</b>', body_style)],
            [
                Paragraph(f'<font size="28" color="{score_color_hex}"><b>{gesamt_score}</b></font><font size="11" color="#6b7280">/100</font>', body_style),
                Paragraph(f'<font size="28" color="{cefr_farbe}"><b>{gesamt_niveau}</b></font>', body_style),
            ]
        ]
        score_table = Table(score_data, colWidths=[8.25*cm, 8.25*cm])
        score_table.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#f0f4ff')),
            ('BACKGROUND', (0,1), (-1,1), colors.HexColor('#f8fafc')),
            ('ALIGN', (0,0), (-1,-1), 'CENTER'),
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
            ('BOX', (0,0), (-1,-1), 0.8, colors.HexColor('#dbeafe')),
            ('INNERGRID', (0,0), (-1,-1), 0.5, colors.HexColor('#e5e7eb')),
            ('TOPPADDING', (0,0), (-1,-1), 10),
            ('BOTTOMPADDING', (0,0), (-1,-1), 10),
            ('ROUNDEDCORNERS', [4]),
        ]))
        story.append(score_table)
        story.append(Spacer(1, 20))

        # ── Kompetenzprofil: Radar + Balken nebeneinander ─────────────
        module_sortiert = sorted(sess.module, key=lambda x: x.reihenfolge)
        radar_daten = []
        for m in module_sortiert:
            m_score = round(m.gesamt_score or 0)
            kurz = MODUL_KURZ.get(m.modul.value, m.modul.value)
            radar_daten.append((kurz, m_score))

        story.append(Paragraph('Kompetenzprofil', h2_style))
        story.append(HRFlowable(width='100%', thickness=0.5, color=colors.HexColor('#e5e7eb'), spaceAfter=8))

        # Radar-Diagramm zeichnen
        radar_drawing = _zeichne_radar(radar_daten)

        # Balken-Tabelle (rechts neben Radar)
        balken_data = []
        for m in module_sortiert:
            m_score = round(m.gesamt_score or 0)
            m_cefr = m.cefr_niveau.value if m.cefr_niveau else '–'
            m_name = MODUL_NAMEN.get(m.modul.value, m.modul.value)
            s_color = '#22c55e' if m_score >= 70 else '#f59e0b' if m_score >= 40 else '#ef4444'
            balken_data.append([
                Paragraph(f'<b>{m_name}</b>', ParagraphStyle('bn', parent=styles['Normal'], fontSize=8)),
                Paragraph(f'<font color="{s_color}"><b>{m_score}/100</b></font>  <font color="#6b7280">{m_cefr}</font>',
                          ParagraphStyle('bs', parent=styles['Normal'], fontSize=8)),
            ])
        balken_table = Table(balken_data, colWidths=[5.5*cm, 3*cm])
        balken_table.setStyle(TableStyle([
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
            ('TOPPADDING', (0,0), (-1,-1), 5),
            ('BOTTOMPADDING', (0,0), (-1,-1), 5),
            ('ROWBACKGROUND', (0,0), (-1,-1), [colors.HexColor('#f8fafc'), colors.white]),
            ('BOX', (0,0), (-1,-1), 0.3, colors.HexColor('#e5e7eb')),
            ('INNERGRID', (0,0), (-1,-1), 0.2, colors.HexColor('#f3f4f6')),
        ]))

        if radar_drawing:
            viz_table = Table(
                [[radar_drawing, balken_table]],
                colWidths=[8.5*cm, 8*cm]
            )
            viz_table.setStyle(TableStyle([
                ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
                ('ALIGN', (0,0), (0,0), 'CENTER'),
            ]))
            story.append(viz_table)
        else:
            story.append(balken_table)
        story.append(Spacer(1, 20))

        # ── Kompetenzübersicht-Tabelle ───────────────────────────────
        story.append(Paragraph('Kompetenzübersicht', h2_style))
        story.append(HRFlowable(width='100%', thickness=0.5, color=colors.HexColor('#e5e7eb'), spaceAfter=8))

        uebersicht_data = [['Kompetenz', 'Score', 'CEFR', 'Bewertung']]
        uebersicht_style_cmds = [
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#1e3a5f')),
            ('TEXTCOLOR', (0,0), (-1,0), colors.white),
            ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
            ('FONTSIZE', (0,0), (-1,-1), 9),
            ('ALIGN', (1,0), (-1,-1), 'CENTER'),
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
            ('BOX', (0,0), (-1,-1), 0.5, colors.HexColor('#e5e7eb')),
            ('INNERGRID', (0,0), (-1,-1), 0.3, colors.HexColor('#f3f4f6')),
            ('TOPPADDING', (0,0), (-1,-1), 6),
            ('BOTTOMPADDING', (0,0), (-1,-1), 6),
        ]
        for i, m in enumerate(module_sortiert, 1):
            m_score = round(m.gesamt_score or 0)
            m_cefr = m.cefr_niveau.value if m.cefr_niveau else '–'
            m_name = MODUL_NAMEN.get(m.modul.value, m.modul.value)
            sterne = '★' * (5 if m_score >= 80 else 4 if m_score >= 65 else 3 if m_score >= 50 else 2 if m_score >= 35 else 1)
            sterne += '☆' * (5 - len(sterne))
            uebersicht_data.append([m_name, f'{m_score}/100', m_cefr, sterne])
            s_color = colors.HexColor('#22c55e') if m_score >= 70 else colors.HexColor('#f59e0b') if m_score >= 40 else colors.HexColor('#ef4444')
            uebersicht_style_cmds.append(('TEXTCOLOR', (1, i), (1, i), s_color))
            uebersicht_style_cmds.append(('FONTNAME', (1, i), (1, i), 'Helvetica-Bold'))
            if i % 2 == 0:
                uebersicht_style_cmds.append(('BACKGROUND', (0, i), (-1, i), colors.HexColor('#f8fafc')))

        uebersicht_table = Table(uebersicht_data, colWidths=[6*cm, 3*cm, 2.5*cm, 4.5*cm])
        uebersicht_table.setStyle(TableStyle(uebersicht_style_cmds))
        story.append(uebersicht_table)
        story.append(Spacer(1, 20))

        # ── Detaillierte Modul-Ergebnisse ─────────────────────────────
        story.append(Paragraph('Detaillierte Modul-Ergebnisse', h2_style))
        story.append(HRFlowable(width='100%', thickness=0.5, color=colors.HexColor('#e5e7eb'), spaceAfter=8))

        for m in module_sortiert:
            m_score = round(m.gesamt_score or 0)
            m_cefr = m.cefr_niveau.value if m.cefr_niveau else '–'
            m_name = MODUL_NAMEN.get(m.modul.value, m.modul.value)
            analyse = m.get_ki_analyse() or {}
            text_analyse = analyse.get('text_analyse', analyse)
            score_color_hex = '#22c55e' if m_score >= 70 else '#f59e0b' if m_score >= 40 else '#ef4444'

            modul_block = []
            modul_block.append(Paragraph(
                f'<b><font color="#1e3a5f">{m_name}</font></b>  '
                f'<font color="{score_color_hex}"><b>{m_score}/100</b></font>  '
                f'<font color="#6b7280">CEFR: <b>{m_cefr}</b></font>',
                h3_style
            ))

            # Gesamteinschätzung
            einschaetzung = (
                text_analyse.get('gesamteinschaetzung') or
                analyse.get('gesamteinschaetzung') or
                analyse.get('zusammenfassung') or ''
            )
            if einschaetzung:
                modul_block.append(Paragraph(einschaetzung, muted_style))

            # Skills-Tabelle
            alle_skills = {}
            for kategorie in ('grammatik', 'wortschatz', 'pragmatik', 'aussprache',
                              'rechtschreibung', 'kohärenz', 'aufgabenerfüllung'):
                kat_data = text_analyse.get(kategorie, {})
                if isinstance(kat_data, dict):
                    if 'score' in kat_data:  # Einzelner Skill
                        alle_skills[kategorie] = kat_data
                    else:
                        for skill_name, skill_data in kat_data.items():
                            if isinstance(skill_data, dict) and 'score' in skill_data:
                                alle_skills[skill_name] = skill_data
            for skill_name in ('satzbau', 'kohaerenz', 'argumentation', 'aufgabenerfullung',
                               'rechtschreibung', 'zeichensetzung', 'textstruktur'):
                if skill_name in text_analyse and isinstance(text_analyse[skill_name], dict):
                    alle_skills[skill_name] = text_analyse[skill_name]

            if alle_skills:
                skills_data = [['Kompetenz', 'Score', 'Begründung']]
                skills_style_cmds = [
                    ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#f3f4f6')),
                    ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
                    ('FONTSIZE', (0,0), (-1,-1), 8),
                    ('ALIGN', (1,0), (1,-1), 'CENTER'),
                    ('VALIGN', (0,0), (-1,-1), 'TOP'),
                    ('BOX', (0,0), (-1,-1), 0.3, colors.HexColor('#e5e7eb')),
                    ('INNERGRID', (0,0), (-1,-1), 0.2, colors.HexColor('#f3f4f6')),
                    ('TOPPADDING', (0,0), (-1,-1), 4),
                    ('BOTTOMPADDING', (0,0), (-1,-1), 4),
                    ('LEFTPADDING', (0,0), (-1,-1), 4),
                ]
                for j, (sname, sdata) in enumerate(alle_skills.items(), 1):
                    s_score = sdata.get('score', 0)
                    s_beg = sdata.get('begruendung', '')
                    label = sname.replace('_', ' ').replace('ae', 'ä').replace('oe', 'ö').replace('ue', 'ü').title()
                    skills_data.append([
                        label,
                        f'{s_score}/10',
                        s_beg[:100] + ('...' if len(s_beg) > 100 else '')
                    ])
                    s_color = colors.HexColor('#22c55e') if s_score >= 7 else colors.HexColor('#f59e0b') if s_score >= 4 else colors.HexColor('#ef4444')
                    skills_style_cmds.append(('TEXTCOLOR', (1, j), (1, j), s_color))
                    skills_style_cmds.append(('FONTNAME', (1, j), (1, j), 'Helvetica-Bold'))
                    if j % 2 == 0:
                        skills_style_cmds.append(('BACKGROUND', (0, j), (-1, j), colors.HexColor('#fafafa')))
                skills_table = Table(skills_data, colWidths=[4*cm, 1.5*cm, 11*cm])
                skills_table.setStyle(TableStyle(skills_style_cmds))
                modul_block.append(skills_table)
                modul_block.append(Spacer(1, 4))

            # Audio-Analyse (M4/M5)
            audio_analyse = analyse.get('audio_analyse')
            if audio_analyse and isinstance(audio_analyse, dict):
                audio_felder = [
                    ('Verständlichkeit', audio_analyse.get('verstaendlichkeit', {}).get('score') if isinstance(audio_analyse.get('verstaendlichkeit'), dict) else audio_analyse.get('verstaendlichkeit')),
                    ('Fluss', audio_analyse.get('fluss_fluency', {}).get('score') if isinstance(audio_analyse.get('fluss_fluency'), dict) else audio_analyse.get('fluss_fluency')),
                    ('Akzent', audio_analyse.get('akzent', {}).get('score') if isinstance(audio_analyse.get('akzent'), dict) else audio_analyse.get('akzent')),
                    ('Intonation', audio_analyse.get('intonation', {}).get('score') if isinstance(audio_analyse.get('intonation'), dict) else audio_analyse.get('intonation')),
                ]
                audio_felder = [(k, v) for k, v in audio_felder if isinstance(v, (int, float))]
                if audio_felder:
                    zus = audio_analyse.get('zusammenfassung', '')
                    if zus:
                        modul_block.append(Paragraph(f'<b>Aussprache-Analyse:</b> {zus}', skill_style))
                    audio_row = []
                    for label, val in audio_felder:
                        s_color = '#22c55e' if val >= 7 else '#f59e0b' if val >= 4 else '#ef4444'
                        audio_row.append(Paragraph(f'<b>{label}</b><br/><font color="{s_color}">{val}/10</font>',
                                                   ParagraphStyle('ac', parent=styles['Normal'], fontSize=8, alignment=1)))
                    audio_table = Table([audio_row], colWidths=[4*cm] * len(audio_row))
                    audio_table.setStyle(TableStyle([
                        ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#f0f9ff')),
                        ('BOX', (0,0), (-1,-1), 0.3, colors.HexColor('#bae6fd')),
                        ('INNERGRID', (0,0), (-1,-1), 0.2, colors.HexColor('#e0f2fe')),
                        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
                        ('TOPPADDING', (0,0), (-1,-1), 6),
                        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
                    ]))
                    modul_block.append(audio_table)
                    modul_block.append(Spacer(1, 4))

            # Einzelfragen-Details (M1/M2/M3)
            details = analyse.get('details', [])
            if details:
                modul_block.append(Paragraph('<b>Einzelfragen-Auswertung:</b>', skill_style))
                fragen_data = [['Frage', 'Antwort', 'Korrekt?']]
                fragen_style = [
                    ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#f3f4f6')),
                    ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
                    ('FONTSIZE', (0,0), (-1,-1), 7.5),
                    ('VALIGN', (0,0), (-1,-1), 'TOP'),
                    ('BOX', (0,0), (-1,-1), 0.3, colors.HexColor('#e5e7eb')),
                    ('INNERGRID', (0,0), (-1,-1), 0.2, colors.HexColor('#f3f4f6')),
                    ('TOPPADDING', (0,0), (-1,-1), 3),
                    ('BOTTOMPADDING', (0,0), (-1,-1), 3),
                    ('LEFTPADDING', (0,0), (-1,-1), 4),
                ]
                for k, d in enumerate(details, 1):
                    korrekt = d.get('ist_korrekt', False)
                    fragen_data.append([
                        d.get('frage', '')[:60] + ('...' if len(d.get('frage','')) > 60 else ''),
                        d.get('gewaehlt_text', str(d.get('gewaehlt', '')))[:40],
                        '✓' if korrekt else '✗',
                    ])
                    fragen_style.append(('TEXTCOLOR', (2, k), (2, k),
                        colors.HexColor('#22c55e') if korrekt else colors.HexColor('#ef4444')))
                    fragen_style.append(('FONTNAME', (2, k), (2, k), 'Helvetica-Bold'))
                    if k % 2 == 0:
                        fragen_style.append(('BACKGROUND', (0, k), (-1, k), colors.HexColor('#fafafa')))
                fragen_table = Table(fragen_data, colWidths=[9*cm, 5*cm, 1.5*cm])
                fragen_table.setStyle(TableStyle(fragen_style))
                modul_block.append(fragen_table)
                modul_block.append(Spacer(1, 4))

            # Stärken & Schwächen
            staerken = text_analyse.get('staerken') or analyse.get('staerken') or []
            schwaechen = text_analyse.get('schwaechen') or analyse.get('schwaechen') or []
            empfehlungen = text_analyse.get('empfehlungen') or analyse.get('empfehlungen') or []
            if staerken:
                modul_block.append(Paragraph(
                    '<font color="#22c55e"><b>✓ Stärken:</b></font> ' + ' · '.join(staerken), skill_style))
            if schwaechen:
                modul_block.append(Paragraph(
                    '<font color="#f59e0b"><b>→ Verbesserung:</b></font> ' + ' · '.join(schwaechen), skill_style))
            if empfehlungen:
                modul_block.append(Paragraph(
                    '<font color="#2563eb"><b>Empfehlung:</b></font> ' + ' · '.join(empfehlungen), skill_style))

            modul_block.append(Spacer(1, 8))
            modul_block.append(HRFlowable(width='100%', thickness=0.3,
                color=colors.HexColor('#e5e7eb'), spaceAfter=8))

            story.append(KeepTogether(modul_block[:4]))  # Titel + Einschätzung zusammenhalten
            for elem in modul_block[4:]:
                story.append(elem)

        # ── Footer ────────────────────────────────────────────────────
        story.append(Spacer(1, 20))
        story.append(HRFlowable(width='100%', thickness=0.5, color=colors.HexColor('#e5e7eb'), spaceAfter=6))
        story.append(Paragraph(
            f'DaF Sprachdiagnostik – Automatisch generierter Bericht  |  Erstellt am {datum}  |  '
            'KI-generierte Inhalte können gelegentliche Ungenauigkeiten enthalten.',
            footer_style
        ))

        doc.build(story)
        pdf_buffer.seek(0)
        return StreamingResponse(
            pdf_buffer,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename=einstufungsbericht_{token[:8]}.pdf"}
        )
    except Exception as e:
        import traceback
        print(f"[PDF-Export fehlgeschlagen]: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"PDF-Generierung fehlgeschlagen: {str(e)}")
