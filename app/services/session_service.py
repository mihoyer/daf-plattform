"""
Session-Service: Erstellt und verwaltet anonyme Test-Sessions.
Keine Speicherung personenbezogener Daten (DSGVO-konform).
"""
import os
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models.database import (
    CEFRNiveau, GutscheinCode, Hilfssprache, ModulErgebnis, ModulStatus,
    ModulTyp, PAKET_MODULE, PaketTyp, SessionStatus, TestSession,
    ZahlungsStatus,
)


def _generate_token(length: int = 32) -> str:
    """Generiert einen kryptographisch sicheren, URL-sicheren Token."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


async def erstelle_session(
    db: AsyncSession,
    paket: PaketTyp,
    hilfssprache: Hilfssprache,
    waehrung: str = "CHF",
    grob_niveau: Optional[CEFRNiveau] = None,
) -> TestSession:
    """Erstellt eine neue anonyme Test-Session mit den zugehörigen Modul-Slots."""
    token = _generate_token()

    # Ablaufdatum berechnen
    laeuft_ab = None
    if settings.session_expiry_days > 0:
        laeuft_ab = datetime.now(timezone.utc) + timedelta(days=settings.session_expiry_days)

    session = TestSession(
        token=token,
        paket=paket,
        hilfssprache=hilfssprache,
        waehrung=waehrung,
        grob_niveau=grob_niveau,
        status=SessionStatus.offen,
        zahlungs_status=ZahlungsStatus.demo if paket == PaketTyp.demo else ZahlungsStatus.ausstehend,
        laeuft_ab_am=laeuft_ab,
    )
    db.add(session)
    await db.flush()  # ID generieren

    # Modul-Slots anlegen
    module = PAKET_MODULE.get(paket, [])
    for i, modul_typ in enumerate(module):
        modul = ModulErgebnis(
            session_id=session.id,
            modul=modul_typ,
            reihenfolge=i,
            status=ModulStatus.ausstehend,
        )
        db.add(modul)

    await db.commit()
    await db.refresh(session)
    return session


async def lade_session(db: AsyncSession, token: str) -> Optional[TestSession]:
    """Lädt eine Session anhand des Tokens inkl. aller Module."""
    result = await db.execute(
        select(TestSession)
        .where(TestSession.token == token)
        .options(selectinload(TestSession.module))
    )
    session = result.scalar_one_or_none()
    if session is None:
        return None

    # Abgelaufene Sessions markieren
    if (
        session.laeuft_ab_am
        and datetime.now(timezone.utc) > session.laeuft_ab_am
        and session.status not in (SessionStatus.abgeschlossen, SessionStatus.abgelaufen)
    ):
        session.status = SessionStatus.abgelaufen
        await db.commit()

    return session


async def aktiviere_session_nach_zahlung(
    db: AsyncSession,
    stripe_payment_intent_id: str,
) -> Optional[TestSession]:
    """Aktiviert eine Session nach erfolgreicher Stripe-Zahlung."""
    result = await db.execute(
        select(TestSession)
        .where(TestSession.stripe_payment_intent_id == stripe_payment_intent_id)
        .options(selectinload(TestSession.module))
    )
    session = result.scalar_one_or_none()
    if session:
        session.zahlungs_status = ZahlungsStatus.bezahlt
        session.status = SessionStatus.laufend
        await db.commit()
    return session


async def validiere_gutschein(
    db: AsyncSession,
    code: str,
) -> Optional[GutscheinCode]:
    """Prüft ob ein Gutscheincode gültig ist."""
    result = await db.execute(
        select(GutscheinCode).where(GutscheinCode.code == code.upper())
    )
    gutschein = result.scalar_one_or_none()
    if gutschein and gutschein.ist_gueltig():
        return gutschein
    return None


async def loesche_mediendateien(modul: ModulErgebnis) -> None:
    """Löscht Audio- und Bilddateien nach der Analyse (DSGVO)."""
    if modul.audio_pfad and os.path.exists(modul.audio_pfad):
        try:
            os.remove(modul.audio_pfad)
        except OSError:
            pass
        modul.audio_pfad = None

    if modul.bild_pfad and os.path.exists(modul.bild_pfad):
        try:
            os.remove(modul.bild_pfad)
        except OSError:
            pass
        modul.bild_pfad = None


async def berechne_gesamt_ergebnis(db: AsyncSession, session: TestSession) -> None:
    """
    Berechnet Gesamtscore und CEFR-Niveau aus allen Modul-Ergebnissen.

    Methodik:
    1. Gewichteter Score: Jedes Modul trägt unterschiedlich stark zum Gesamtscore bei.
       - M1 Grammatik:      10 %
       - M2 Lesen:          20 %
       - M3 Hören:          20 %
       - M4 Vorlesen:       10 %
       - M5 Freies Sprechen: 20 %
       - M6 Schreiben:      20 %
    2. Basis-Niveau: Wird aus dem gewichteten Score abgeleitet.
    3. K.O.-Kriterium: Das Gesamtniveau darf maximal eine Stufe über dem
       schlechtesten produktiven Modul (Sprechen oder Schreiben) liegen.
    """
    abgeschlossene = [
        m for m in session.module
        if m.status == ModulStatus.abgeschlossen and m.gesamt_score is not None
    ]
    if not abgeschlossene:
        return

    CEFR_WERT = {"A1": 1, "A2": 2, "B1": 3, "B2": 4, "C1": 5, "C2": 6}
    CEFR_VON_WERT = {1: CEFRNiveau.A1, 2: CEFRNiveau.A2, 3: CEFRNiveau.B1,
                     4: CEFRNiveau.B2, 5: CEFRNiveau.C1, 6: CEFRNiveau.C2}

    # Gewichtung pro Modul-Typ
    GEWICHT = {
        ModulTyp.m1_grammatik:  0.10,
        ModulTyp.m2_lesen:      0.20,
        ModulTyp.m3_hoerverstehen: 0.20,
        ModulTyp.m4_vorlesen:   0.10,
        ModulTyp.m5_sprechen:   0.20,
        ModulTyp.m6_schreiben:  0.20,
    }

    # Normierte Scores (alle auf 0-100 Skala)
    modul_scores = {}  # {ModulTyp: normierter_score}
    for m in abgeschlossene:
        score = m.gesamt_score
        # Scores <= 15 sind wahrscheinlich auf 0-10 Skala -> auf 0-100 skalieren
        if score is not None and score <= 15:
            score = score * 10
        modul_scores[m.modul] = score

    # Gewichteten Gesamt-Score berechnen
    gewichtete_summe = 0.0
    gesamtgewicht = 0.0
    for modul_typ, score in modul_scores.items():
        gewicht = GEWICHT.get(modul_typ, 1.0 / len(modul_scores))
        gewichtete_summe += score * gewicht
        gesamtgewicht += gewicht

    # Falls nicht alle Module vorhanden: Gewichte normalisieren
    gesamt_score = gewichtete_summe / gesamtgewicht if gesamtgewicht > 0 else 0.0
    session.gesamt_score = round(gesamt_score, 1)

    # Basis-Niveau aus gewichtetem Score ableiten
    # Absolute Skala: A1=0-14, A2=15-34, B1=35-59, B2=60-79, C1/C2=80-100
    def score_zu_niveau(s: float) -> int:
        """Gibt CEFR-Wert (1-6) für einen absoluten Score (0-100) zurück."""
        if s >= 93: return 6  # C2
        if s >= 80: return 5  # C1
        if s >= 60: return 4  # B2
        if s >= 35: return 3  # B1
        if s >= 15: return 2  # A2
        return 1              # A1

    basis_niveau_wert = score_zu_niveau(gesamt_score)

    # K.O.-Kriterium: Schlechtestes produktives Modul bestimmen
    # Produktive Kernmodule: Sprechen (M5) und Schreiben (M6)
    ko_module = [ModulTyp.m5_sprechen, ModulTyp.m6_schreiben]
    ko_niveau_werte = []
    for modul_typ in ko_module:
        score = modul_scores.get(modul_typ)
        if score is not None:
            ko_niveau_werte.append(score_zu_niveau(score))

    if ko_niveau_werte:
        schlechtestes_ko = min(ko_niveau_werte)
        # Gesamtniveau darf maximal eine Stufe über dem schlechtesten K.O.-Modul liegen
        max_erlaubtes_niveau = schlechtestes_ko + 1
        endniveau_wert = min(basis_niveau_wert, max_erlaubtes_niveau)
    else:
        endniveau_wert = basis_niveau_wert

    endniveau_wert = max(1, min(6, endniveau_wert))
    session.gesamt_niveau = CEFR_VON_WERT[endniveau_wert]

    session.status = SessionStatus.abgeschlossen
    session.abgeschlossen_am = datetime.now(timezone.utc)
    await db.commit()
