"""
Haupt-Router: Alle API-Endpunkte der DaF-Plattform.
"""
import json
import os
import secrets
import tempfile
from datetime import datetime, timezone
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
    if score >= 90: return "C2"
    if score >= 78: return "C1"
    if score >= 65: return "B2"
    if score >= 50: return "B1"
    if score >= 35: return "A2"
    return "A1"


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

@router.post("/api/zahlung/demo-bypass")
async def demo_bypass(
    token: str = Form(""),
    paket: str = Form("basis"),
    hilfssprache: str = Form("de"),
    waehrung: str = Form("CHF"),
    db: AsyncSession = Depends(get_db),
):
    """Demo-Bypass: Aktiviert eine Session ohne Stripe-Zahlung (nur für Testzwecke)."""
    if token:
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
    modul.gesamt_score = auswertung["score"]
    modul.cefr_niveau = CEFRNiveau(auswertung["cefr"])
    modul.status = ModulStatus.abgeschlossen
    modul.abgeschlossen_am = datetime.now(timezone.utc)

    sess.grob_niveau = CEFRNiveau(auswertung["cefr"])
    sess.status = SessionStatus.laufend
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
    modul.gesamt_score = auswertung["score"]
    modul.cefr_niveau = CEFRNiveau(auswertung["cefr"])
    modul.status = ModulStatus.abgeschlossen
    modul.abgeschlossen_am = datetime.now(timezone.utc)
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
    modul.gesamt_score = auswertung["score"]
    modul.cefr_niveau = CEFRNiveau(auswertung["cefr"])
    modul.status = ModulStatus.abgeschlossen
    modul.abgeschlossen_am = datetime.now(timezone.utc)
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
        "A1": ["Familie", "Essen", "Tiere", "Farben", "Schule"],
        "A2": ["Freizeit", "Einkaufen", "Wetter", "Reisen", "Arbeit"],
        "B1": ["Umwelt", "Technologie", "Gesundheit", "Kultur", "Sport"],
        "B2": ["Klimawandel", "Digitalisierung", "Migration", "Wirtschaft", "Bildung"],
        "C1": ["Künstliche Intelligenz", "Sprachpolitik", "Philosophie", "Globalisierung"],
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
            "A1": ["Ich heiße Anna.", "Ich wohne in Berlin.", "Das ist mein Haus."],
            "A2": ["Jeden Morgen trinke ich Kaffee.", "Mein Bruder arbeitet als Arzt.", "Das Wetter ist heute schön."],
            "B1": ["Die Digitalisierung verändert unsere Arbeitswelt grundlegend.", "Viele Menschen nutzen täglich soziale Medien.", "Gesunde Ernährung ist wichtig für das Wohlbefinden."],
            "B2": ["Die wirtschaftlichen Folgen des Klimawandels sind noch nicht vollständig absehbar.", "Bildung gilt als wichtigster Faktor für soziale Mobilität.", "Digitale Technologien eröffnen neue Möglichkeiten, bringen aber auch Risiken mit sich."],
            "C1": ["Die philosophische Frage nach dem freien Willen beschäftigt Denker seit Jahrhunderten.", "Globale Lieferketten erweisen sich in Krisenzeiten als besonders anfällig.", "Sprachliche Nuancen spiegeln kulturelle Wertvorstellungen wider."],
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
        modul.gesamt_score = round(raw_score * 10, 1) if raw_score <= 10 else raw_score
        cefr_str = audio_score.get("cefr_niveau", "B1")
    else:
        modul.gesamt_score = analyse.get("lesegenauigkeit", 50)
        cefr_str = _score_to_cefr(modul.gesamt_score)

    modul.cefr_niveau = CEFRNiveau(cefr_str) if cefr_str in CEFRNiveau._value2member_map_ else CEFRNiveau.B1
    modul.status = ModulStatus.abgeschlossen
    modul.abgeschlossen_am = datetime.now(timezone.utc)
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
- Konkretes, interessantes Thema (nicht generisch, nicht immer Klimawandel)
- 2-3 konkrete Leitfragen oder Sprechimpulse

Antworte NUR mit JSON: {{\"thema\": \"Kurzer Titel\", \"aufgabe\": \"Die vollst\u00e4ndige Aufgabenstellung mit Leitfragen\"}}"""}],
            response_format={"type": "json_object"},
            temperature=0.9,
        )
        import json as _json
        data = _json.loads(resp.choices[0].message.content)
        thema = data.get("aufgabe") or data.get("thema", "Freies Sprechen")
    except Exception as e:
        print(f"[M5] Thema-Generierung fehlgeschlagen: {e}")
        import random
        fallback = {
            "A1": "Stell dich vor: Wie hei\u00dft du? Wo wohnst du? Was machst du gerne?",
            "A2": "Erz\u00e4hl von deiner Woche: Was hast du gemacht? Was war sch\u00f6n? Was war schwierig?",
            "B1": "Beschreibe eine Person, die du bewunderst: Wer ist das? Warum bewunderst du sie? Was hast du von ihr gelernt?",
            "B2": "Diskutiere: Sollte das Studium in Deutschland kostenlos sein? Welche Argumente gibt es daf\u00fcr und dagegen? Was ist deine Meinung?",
            "C1": "Analysiere: Inwiefern ver\u00e4ndert k\u00fcnstliche Intelligenz unsere Vorstellung von Kreativit\u00e4t und k\u00fcnstlerischem Ausdruck? Ber\u00fccksichtige verschiedene Perspektiven.",
        }
        thema = fallback.get(niveau, fallback["B1"])

    modul = _get_modul(sess, ModulTyp.m5_sprechen)
    if modul:
        modul.schwierigkeitsgrad = niveau
        modul.set_roh_antworten({"thema": thema})
        modul.status = ModulStatus.laufend
        await db.commit()

    return {"thema": thema, "niveau": niveau, "dauer_sekunden": 90}


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
    gesamt_score = text_analyse.get("gesamt_score", 5.0)
    # GPT gibt 0-10 zurück → auf 0-100 normalisieren
    if gesamt_score <= 10:
        gesamt_score = round(gesamt_score * 10, 1)
    modul.gesamt_score = gesamt_score
    cefr_str = text_analyse.get("cefr_niveau", _score_to_cefr(gesamt_score))
    modul.cefr_niveau = CEFRNiveau(cefr_str) if cefr_str in CEFRNiveau._value2member_map_ else CEFRNiveau.B1
    modul.status = ModulStatus.abgeschlossen
    modul.abgeschlossen_am = datetime.now(timezone.utc)
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
- Konkretes, interessantes Thema (nicht generisch)
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
            "A2": "Du hast letzte Woche ein Konzert besucht. Schreibe eine kurze Nachricht (5–6 Sätze) an einen Freund: Was war das für ein Konzert? Wie war die Musik? Was hat dir gefallen oder nicht gefallen?",
            "B1": "Viele junge Menschen verbringen viel Zeit mit sozialen Medien. Schreibe einen Text (8–10 Sätze): Welche Vor- und Nachteile siehst du? Wie nutzt du selbst soziale Medien? Was würdest du anderen empfehlen?",
            "B2": "In vielen Ländern wird diskutiert, ob das Schulfach 'Digitale Kompetenz' Pflicht sein sollte. Schreibe einen Meinungstext (12–15 Sätze): Welche Argumente gibt es dafür und dagegen? Wie ist deine Meinung und warum?",
            "C1": "Analysiere in einem Essay (150–200 Wörter) die Aussage: 'Künstliche Intelligenz wird den Arbeitsmarkt stärker verändern als die Industrialisierung.' Berücksichtige verschiedene Perspektiven und belege deine Argumentation.",
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
    modul.gesamt_score = round(raw_score * 10, 1) if raw_score <= 10 else raw_score
    cefr_str = analyse.get("cefr_niveau", _score_to_cefr(modul.gesamt_score))
    modul.cefr_niveau = CEFRNiveau(cefr_str) if cefr_str in CEFRNiveau._value2member_map_ else CEFRNiveau.B1
    modul.status = ModulStatus.abgeschlossen
    modul.abgeschlossen_am = datetime.now(timezone.utc)
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

@router.get("/api/export/pdf/{token}")
async def export_pdf(token: str, db: AsyncSession = Depends(get_db)):
    """Exportiert den Einstufungsbericht als PDF."""
    from fastapi.responses import StreamingResponse
    import io

    sess = await session_service.lade_session(db, token)
    if not sess:
        raise HTTPException(status_code=404, detail="Session nicht gefunden.")

    # HTML für PDF aufbauen
    module_html = ""
    for m in sorted(sess.module, key=lambda x: x.reihenfolge):
        score = round(m.gesamt_score or 0)
        cefr = m.cefr_niveau.value if m.cefr_niveau else "–"
        analyse = m.get_ki_analyse() or {}
        zusammenfassung = analyse.get("zusammenfassung", analyse.get("feedback", ""))
        module_html += f"""
        <div style="margin-bottom:1.5rem; padding:1rem; border:1px solid #e5e7eb; border-radius:8px;">
          <h3 style="margin:0 0 .5rem;">{m.modul.value.replace('_', ' ').title()}</h3>
          <p style="margin:.25rem 0; color:#6b7280;">Score: {score}/100 &nbsp;|&nbsp; CEFR: {cefr}</p>
          {f'<p style="margin:.5rem 0; font-size:.9rem;">{zusammenfassung}</p>' if zusammenfassung else ''}
        </div>"""

    gesamt_score = round(sess.gesamt_score or 0)
    gesamt_niveau = sess.gesamt_niveau.value if sess.gesamt_niveau else "–"
    datum = datetime.now(timezone.utc).strftime("%d.%m.%Y")

    html = f"""<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="UTF-8">
  <title>Einstufungsbericht – DaF Sprachdiagnostik</title>
  <style>
    body {{ font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 2rem; color: #1f2937; }}
    h1 {{ color: #1e3a5f; }} h2 {{ color: #374151; border-bottom: 1px solid #e5e7eb; padding-bottom:.5rem; }}
    .score-box {{ display:inline-block; background:#f0f4ff; border-radius:8px; padding:1rem 2rem; margin:.5rem; text-align:center; }}
    .score-big {{ font-size:2.5rem; font-weight:800; color:#2563eb; }}
    footer {{ margin-top:3rem; font-size:.8rem; color:#9ca3af; border-top:1px solid #e5e7eb; padding-top:1rem; }}
  </style>
</head>
<body>
  <h1>Einstufungsbericht – DaF Sprachdiagnostik</h1>
  <p style="color:#6b7280;">Erstellt am {datum} &nbsp;|&nbsp; Session: {token[:8]}…</p>
  <div style="margin:1.5rem 0;">
    <div class="score-box"><div class="score-big">{gesamt_score}</div><div>Gesamtscore</div></div>
    <div class="score-box"><div class="score-big">{gesamt_niveau}</div><div>CEFR-Niveau</div></div>
  </div>
  <h2>Modul-Ergebnisse</h2>
  {module_html if module_html else '<p style="color:#6b7280;">Keine abgeschlossenen Module.</p>'}
  <footer>DaF Sprachdiagnostik – Automatisch generierter Bericht. Alle Daten werden nach dem Export gelöscht.</footer>
</body>
</html>"""

    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.lib import colors
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
        from reportlab.lib.enums import TA_CENTER, TA_LEFT

        pdf_buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            pdf_buffer, pagesize=A4,
            leftMargin=2*cm, rightMargin=2*cm, topMargin=2*cm, bottomMargin=2*cm
        )
        styles = getSampleStyleSheet()
        story = []

        # Titel
        title_style = ParagraphStyle('title', parent=styles['Title'], fontSize=20, textColor=colors.HexColor('#1e3a5f'), spaceAfter=6)
        sub_style = ParagraphStyle('sub', parent=styles['Normal'], fontSize=10, textColor=colors.HexColor('#6b7280'), spaceAfter=20)
        story.append(Paragraph('Einstufungsbericht – DaF Sprachdiagnostik', title_style))
        story.append(Paragraph(f'Erstellt am {datum}  |  Session: {token[:8]}…', sub_style))
        story.append(HRFlowable(width='100%', thickness=1, color=colors.HexColor('#e5e7eb'), spaceAfter=16))

        # Gesamtscore-Tabelle
        score_data = [['Gesamtscore', 'CEFR-Niveau'], [f'{gesamt_score}/100', gesamt_niveau]]
        score_table = Table(score_data, colWidths=[8*cm, 8*cm])
        score_table.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#f0f4ff')),
            ('TEXTCOLOR', (0,0), (-1,0), colors.HexColor('#374151')),
            ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
            ('FONTSIZE', (0,0), (-1,0), 10),
            ('FONTSIZE', (0,1), (-1,1), 22),
            ('FONTNAME', (0,1), (-1,1), 'Helvetica-Bold'),
            ('TEXTCOLOR', (0,1), (0,1), colors.HexColor('#2563eb')),
            ('ALIGN', (0,0), (-1,-1), 'CENTER'),
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
            ('ROWBACKGROUND', (0,1), (-1,1), colors.HexColor('#f8fafc')),
            ('BOX', (0,0), (-1,-1), 0.5, colors.HexColor('#e5e7eb')),
            ('INNERGRID', (0,0), (-1,-1), 0.5, colors.HexColor('#e5e7eb')),
            ('TOPPADDING', (0,0), (-1,-1), 10),
            ('BOTTOMPADDING', (0,0), (-1,-1), 10),
        ]))
        story.append(score_table)
        story.append(Spacer(1, 20))

        # Modul-Ergebnisse
        h2_style = ParagraphStyle('h2', parent=styles['Heading2'], fontSize=13, textColor=colors.HexColor('#374151'), spaceBefore=12, spaceAfter=8)
        body_style = ParagraphStyle('body', parent=styles['Normal'], fontSize=9, textColor=colors.HexColor('#374151'), spaceAfter=4)
        muted_style = ParagraphStyle('muted', parent=styles['Normal'], fontSize=9, textColor=colors.HexColor('#6b7280'), spaceAfter=4)

        # Kompetenzübersicht als Tabelle (Balkendiagramm-Ersatz)
        story.append(Paragraph('Kompetenzübersicht', h2_style))
        story.append(HRFlowable(width='100%', thickness=0.5, color=colors.HexColor('#e5e7eb'), spaceAfter=8))

        MODUL_NAMEN = {
            'm1_grammatik': 'Grammatik & Wortschatz',
            'm2_lesen': 'Lesen & Leseverstehen',
            'm3_hoerverstehen': 'Hörverstehen',
            'm4_vorlesen': 'Vorlesen',
            'm5_sprechen': 'Freies Sprechen',
            'm6_schreiben': 'Schreiben',
        }

        # Kompetenz-Übersichtstabelle
        from reportlab.graphics.shapes import Drawing, Rect, String, Group
        from reportlab.graphics import renderPDF

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
        for i, m in enumerate(sorted(sess.module, key=lambda x: x.reihenfolge), 1):
            m_score = round(m.gesamt_score or 0)
            m_cefr = m.cefr_niveau.value if m.cefr_niveau else '–'
            m_name = MODUL_NAMEN.get(m.modul.value, m.modul.value)
            bewertung = '★★★★★' if m_score >= 80 else '★★★★☆' if m_score >= 65 else '★★★☆☆' if m_score >= 50 else '★★☆☆☆' if m_score >= 35 else '★☆☆☆☆'
            uebersicht_data.append([m_name, f'{m_score}/100', m_cefr, bewertung])
            score_color = colors.HexColor('#22c55e') if m_score >= 70 else colors.HexColor('#f59e0b') if m_score >= 40 else colors.HexColor('#ef4444')
            uebersicht_style_cmds.append(('TEXTCOLOR', (1, i), (1, i), score_color))
            uebersicht_style_cmds.append(('FONTNAME', (1, i), (1, i), 'Helvetica-Bold'))
            if i % 2 == 0:
                uebersicht_style_cmds.append(('BACKGROUND', (0, i), (-1, i), colors.HexColor('#f8fafc')))

        uebersicht_table = Table(uebersicht_data, colWidths=[6*cm, 3*cm, 2.5*cm, 4.5*cm])
        uebersicht_table.setStyle(TableStyle(uebersicht_style_cmds))
        story.append(uebersicht_table)
        story.append(Spacer(1, 20))

        # Detaillierte Modul-Ergebnisse
        story.append(Paragraph('Detaillierte Modul-Ergebnisse', h2_style))
        story.append(HRFlowable(width='100%', thickness=0.5, color=colors.HexColor('#e5e7eb'), spaceAfter=8))

        skill_style = ParagraphStyle('skill', parent=styles['Normal'], fontSize=8, textColor=colors.HexColor('#374151'), spaceAfter=2, leftIndent=10)

        for m in sorted(sess.module, key=lambda x: x.reihenfolge):
            m_score = round(m.gesamt_score or 0)
            m_cefr = m.cefr_niveau.value if m.cefr_niveau else '–'
            m_name = MODUL_NAMEN.get(m.modul.value, m.modul.value)
            analyse = m.get_ki_analyse() or {}

            score_color_hex = '#22c55e' if m_score >= 70 else '#f59e0b' if m_score >= 40 else '#ef4444'
            story.append(Paragraph(
                f'<b><font color="#1e3a5f">{m_name}</font></b> — '
                f'<font color="{score_color_hex}"><b>{m_score}/100</b></font> | CEFR: <b>{m_cefr}</b>',
                body_style
            ))

            # Gesamteinschätzung
            einschaetzung = (
                analyse.get('gesamteinschaetzung') or
                analyse.get('text_analyse', {}).get('gesamteinschaetzung') or
                analyse.get('zusammenfassung') or ''
            )
            if einschaetzung:
                story.append(Paragraph(einschaetzung, muted_style))

            # Skills als Tabelle (falls vorhanden)
            text_analyse = analyse.get('text_analyse', analyse)
            alle_skills = {}
            for kategorie in ('grammatik', 'wortschatz', 'pragmatik', 'aussprache'):
                kat_data = text_analyse.get(kategorie, {})
                if isinstance(kat_data, dict):
                    for skill_name, skill_data in kat_data.items():
                        if isinstance(skill_data, dict) and 'score' in skill_data:
                            alle_skills[skill_name] = skill_data
            # Einzelne Skills auf oberster Ebene
            for skill_name in ('satzbau', 'kohaerenz', 'argumentation'):
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
                    skills_data.append([sname.replace('_', ' ').title(), f'{s_score}/10', s_beg[:80] + ('...' if len(s_beg) > 80 else '')])
                    s_color = colors.HexColor('#22c55e') if s_score >= 7 else colors.HexColor('#f59e0b') if s_score >= 4 else colors.HexColor('#ef4444')
                    skills_style_cmds.append(('TEXTCOLOR', (1, j), (1, j), s_color))
                    skills_style_cmds.append(('FONTNAME', (1, j), (1, j), 'Helvetica-Bold'))
                    if j % 2 == 0:
                        skills_style_cmds.append(('BACKGROUND', (0, j), (-1, j), colors.HexColor('#fafafa')))

                skills_table = Table(skills_data, colWidths=[4*cm, 1.5*cm, 10.5*cm])
                skills_table.setStyle(TableStyle(skills_style_cmds))
                story.append(skills_table)
                story.append(Spacer(1, 4))

            # Stärken & Schwächen
            staerken = analyse.get('staerken') or text_analyse.get('staerken') or []
            schwaechen = analyse.get('schwaechen') or text_analyse.get('schwaechen') or []
            empfehlungen = analyse.get('empfehlungen') or text_analyse.get('empfehlungen') or []
            if staerken:
                story.append(Paragraph(f'<font color="#22c55e">✓ Stärken:</font> {", ".join(staerken)}', skill_style))
            if schwaechen:
                story.append(Paragraph(f'<font color="#f59e0b">→ Verbesserung:</font> {", ".join(schwaechen)}', skill_style))
            if empfehlungen:
                story.append(Paragraph(f'<font color="#2563eb">📌 Empfehlung:</font> {", ".join(empfehlungen)}', skill_style))

            story.append(Spacer(1, 10))
            story.append(HRFlowable(width='100%', thickness=0.3, color=colors.HexColor('#f3f4f6'), spaceAfter=8))

        # Footer
        story.append(Spacer(1, 20))
        footer_style = ParagraphStyle('footer', parent=styles['Normal'], fontSize=8, textColor=colors.HexColor('#9ca3af'))
        story.append(HRFlowable(width='100%', thickness=0.5, color=colors.HexColor('#e5e7eb'), spaceAfter=6))
        story.append(Paragraph('DaF Sprachdiagnostik – Automatisch generierter Bericht.', footer_style))

        doc.build(story)
        pdf_buffer.seek(0)
        return StreamingResponse(
            pdf_buffer,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename=einstufungsbericht_{token[:8]}.pdf"}
        )
    except Exception as e:
        print(f"[PDF-Export fehlgeschlagen]: {e}")
        # Fallback: HTML zurückgeben
        return StreamingResponse(
            io.BytesIO(html.encode("utf-8")),
            media_type="text/html",
            headers={"Content-Disposition": f"attachment; filename=einstufungsbericht_{token[:8]}.html"}
        )
