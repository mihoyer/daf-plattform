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
    """Berechnet Gesamtscore und CEFR-Niveau aus allen Modul-Ergebnissen."""
    abgeschlossene = [
        m for m in session.module
        if m.status == ModulStatus.abgeschlossen and m.gesamt_score is not None
    ]
    if not abgeschlossene:
        return

    gesamt_score = sum(m.gesamt_score for m in abgeschlossene) / len(abgeschlossene)
    session.gesamt_score = round(gesamt_score, 1)

    # CEFR aus Score ableiten
    if gesamt_score >= 90:
        session.gesamt_niveau = CEFRNiveau.C2
    elif gesamt_score >= 78:
        session.gesamt_niveau = CEFRNiveau.C1
    elif gesamt_score >= 65:
        session.gesamt_niveau = CEFRNiveau.B2
    elif gesamt_score >= 50:
        session.gesamt_niveau = CEFRNiveau.B1
    elif gesamt_score >= 35:
        session.gesamt_niveau = CEFRNiveau.A2
    else:
        session.gesamt_niveau = CEFRNiveau.A1

    session.status = SessionStatus.abgeschlossen
    session.abgeschlossen_am = datetime.now(timezone.utc)
    await db.commit()
