"""
Datenbankmodell für die DaF-Sprachdiagnostik-Plattform.
Verwendet SQLAlchemy 2.0 async mit PostgreSQL.
"""
import json
import uuid
from datetime import datetime, timezone
from enum import Enum as PyEnum
from typing import Optional

from sqlalchemy import (
    Boolean, DateTime, Enum, Float, ForeignKey,
    Integer, String, Text, func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.config import settings


# ── Engine & Session ─────────────────────────────────────────────────────────

engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    pool_pre_ping=True,
)

AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# ── Enums ────────────────────────────────────────────────────────────────────

class PaketTyp(str, PyEnum):
    basis = "basis"
    standard = "standard"
    premium = "premium"
    b2b = "b2b"
    demo = "demo"  # kostenlos für Tests


class Hilfssprache(str, PyEnum):
    de = "de"
    en = "en"
    tr = "tr"
    ar = "ar"
    uk = "uk"
    ru = "ru"
    fr = "fr"
    it = "it"
    es = "es"


class CEFRNiveau(str, PyEnum):
    A1 = "A1"
    A2 = "A2"
    B1 = "B1"
    B2 = "B2"
    C1 = "C1"
    C2 = "C2"
    unbekannt = "unbekannt"


class SessionStatus(str, PyEnum):
    offen = "offen"
    laufend = "laufend"
    abgeschlossen = "abgeschlossen"
    abgelaufen = "abgelaufen"
    fehler = "fehler"


class ModulTyp(str, PyEnum):
    m1_grammatik = "m1_grammatik"
    m2_lesen = "m2_lesen"
    m3_hoerverstehen = "m3_hoerverstehen"
    m4_vorlesen = "m4_vorlesen"
    m5_sprechen = "m5_sprechen"
    m6_schreiben = "m6_schreiben"


class ModulStatus(str, PyEnum):
    ausstehend = "ausstehend"
    laufend = "laufend"
    abgeschlossen = "abgeschlossen"
    fehler = "fehler"
    uebersprungen = "uebersprungen"


class ZahlungsStatus(str, PyEnum):
    ausstehend = "ausstehend"
    bezahlt = "bezahlt"
    fehlgeschlagen = "fehlgeschlagen"
    erstattet = "erstattet"
    demo = "demo"  # kein Stripe, kostenlos


# ── Paket-Konfiguration ──────────────────────────────────────────────────────

PAKET_MODULE = {
    PaketTyp.basis: [ModulTyp.m1_grammatik, ModulTyp.m2_lesen],
    PaketTyp.standard: [ModulTyp.m1_grammatik, ModulTyp.m2_lesen, ModulTyp.m3_hoerverstehen, ModulTyp.m4_vorlesen],
    PaketTyp.premium: [ModulTyp.m1_grammatik, ModulTyp.m2_lesen, ModulTyp.m3_hoerverstehen, ModulTyp.m4_vorlesen, ModulTyp.m5_sprechen, ModulTyp.m6_schreiben],
    PaketTyp.b2b: [ModulTyp.m1_grammatik, ModulTyp.m2_lesen, ModulTyp.m3_hoerverstehen, ModulTyp.m4_vorlesen, ModulTyp.m5_sprechen, ModulTyp.m6_schreiben],
    PaketTyp.demo: [ModulTyp.m1_grammatik, ModulTyp.m5_sprechen],
}

PAKET_PREISE_CHF = {
    PaketTyp.basis: 6.00,
    PaketTyp.standard: 10.00,
    PaketTyp.premium: 14.00,
    PaketTyp.b2b: 0.00,  # individuell
    PaketTyp.demo: 0.00,
}

PAKET_PREISE_EUR = {
    PaketTyp.basis: 6.00,
    PaketTyp.standard: 10.00,
    PaketTyp.premium: 14.00,
    PaketTyp.b2b: 0.00,
    PaketTyp.demo: 0.00,
}


# ── ORM-Modelle ──────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


class TestSession(Base):
    """Anonyme Test-Session. Kein Personenbezug."""
    __tablename__ = "test_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    paket: Mapped[PaketTyp] = mapped_column(Enum(PaketTyp), nullable=False, default=PaketTyp.demo)
    hilfssprache: Mapped[Hilfssprache] = mapped_column(Enum(Hilfssprache), nullable=False, default=Hilfssprache.de)
    waehrung: Mapped[str] = mapped_column(String(3), nullable=False, default="CHF")
    grob_niveau: Mapped[Optional[CEFRNiveau]] = mapped_column(Enum(CEFRNiveau), nullable=True)
    gesamt_niveau: Mapped[Optional[CEFRNiveau]] = mapped_column(Enum(CEFRNiveau), nullable=True)
    gesamt_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[SessionStatus] = mapped_column(Enum(SessionStatus), nullable=False, default=SessionStatus.offen)
    zahlungs_status: Mapped[ZahlungsStatus] = mapped_column(Enum(ZahlungsStatus), nullable=False, default=ZahlungsStatus.ausstehend)
    stripe_payment_intent_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    erstellt_am: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    abgeschlossen_am: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    laeuft_ab_am: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    module: Mapped[list["ModulErgebnis"]] = relationship("ModulErgebnis", back_populates="session", cascade="all, delete-orphan")

    def get_aktives_modul(self) -> Optional["ModulErgebnis"]:
        for m in self.module:
            if m.status == ModulStatus.laufend:
                return m
        return None

    def get_naechstes_modul(self) -> Optional["ModulErgebnis"]:
        for m in self.module:
            if m.status == ModulStatus.ausstehend:
                return m
        return None

    def alle_abgeschlossen(self) -> bool:
        return all(
            m.status in (ModulStatus.abgeschlossen, ModulStatus.uebersprungen, ModulStatus.fehler)
            for m in self.module
        )


class ModulErgebnis(Base):
    """Ergebnis eines einzelnen Test-Moduls innerhalb einer Session."""
    __tablename__ = "modul_ergebnisse"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(Integer, ForeignKey("test_sessions.id"), nullable=False)
    modul: Mapped[ModulTyp] = mapped_column(Enum(ModulTyp), nullable=False)
    reihenfolge: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[ModulStatus] = mapped_column(Enum(ModulStatus), nullable=False, default=ModulStatus.ausstehend)
    schwierigkeitsgrad: Mapped[Optional[str]] = mapped_column(String(2), nullable=True)  # A1, A2, B1, B2, C1, C2
    roh_antworten_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ki_analyse_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cefr_niveau: Mapped[Optional[CEFRNiveau]] = mapped_column(Enum(CEFRNiveau), nullable=True)
    gesamt_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    audio_pfad: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    bild_pfad: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    erstellt_am: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    abgeschlossen_am: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    session: Mapped["TestSession"] = relationship("TestSession", back_populates="module")

    def get_roh_antworten(self) -> dict:
        if self.roh_antworten_json:
            return json.loads(self.roh_antworten_json)
        return {}

    def set_roh_antworten(self, data: dict):
        self.roh_antworten_json = json.dumps(data, ensure_ascii=False)

    def get_ki_analyse(self) -> dict:
        if self.ki_analyse_json:
            return json.loads(self.ki_analyse_json)
        return {}

    def set_ki_analyse(self, data: dict):
        self.ki_analyse_json = json.dumps(data, ensure_ascii=False)


class AdminUser(Base):
    """Admin-Benutzer für das Dashboard."""
    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    erstellt_am: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GutscheinCode(Base):
    """B2B-Gutscheincodes für Institutionen."""
    __tablename__ = "gutschein_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    paket: Mapped[PaketTyp] = mapped_column(Enum(PaketTyp), nullable=False)
    max_nutzungen: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    genutzt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    aktiv: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notiz: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    erstellt_am: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    laeuft_ab_am: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    def ist_gueltig(self) -> bool:
        if not self.aktiv:
            return False
        if self.genutzt >= self.max_nutzungen:
            return False
        if self.laeuft_ab_am and datetime.now(timezone.utc) > self.laeuft_ab_am:
            return False
        return True
