"""
Stripe-Service für Zahlungsintegration.
Läuft im Testmodus (sk_test_...) – keine echten Zahlungen.
"""
import stripe
from typing import Optional

from app.config import settings
from app.models.database import PAKET_PREISE_CHF, PAKET_PREISE_EUR, PaketTyp

stripe.api_key = settings.stripe_secret_key

# Stripe-Produkt-/Preis-IDs (werden beim ersten Aufruf gecacht)
_produkt_ids: dict[str, str] = {}


def _paket_name(paket: PaketTyp) -> str:
    namen = {
        PaketTyp.basis: "DaF Einstufungstest – Basis",
        PaketTyp.standard: "DaF Einstufungstest – Standard",
        PaketTyp.premium: "DaF Einstufungstest – Premium",
        PaketTyp.b2b: "DaF Einstufungstest – B2B",
        PaketTyp.demo: "DaF Einstufungstest – Demo",
    }
    return namen.get(paket, "DaF Einstufungstest")


def _paket_beschreibung(paket: PaketTyp) -> str:
    beschreibungen = {
        PaketTyp.basis: "Grammatik/Wortschatz + Lesen & Leseverstehen",
        PaketTyp.standard: "Basis + Hörverstehen + Vorlesen",
        PaketTyp.premium: "Alle 6 Module: Vollständige Sprachdiagnostik",
        PaketTyp.b2b: "Vollständige Sprachdiagnostik (B2B-Lizenz)",
        PaketTyp.demo: "Demo-Version",
    }
    return beschreibungen.get(paket, "")


async def erstelle_payment_intent(
    paket: PaketTyp,
    waehrung: str = "CHF",
    session_token: str = "",
) -> dict:
    """
    Erstellt einen Stripe PaymentIntent für das gewählte Paket.
    Gibt client_secret und payment_intent_id zurück.
    """
    if waehrung.upper() == "EUR":
        betrag_decimal = PAKET_PREISE_EUR.get(paket, 0.0)
    else:
        betrag_decimal = PAKET_PREISE_CHF.get(paket, 0.0)

    # Stripe erwartet Betrag in Rappen/Cent (Integer)
    betrag_rappen = int(betrag_decimal * 100)

    if betrag_rappen == 0:
        # Demo oder B2B – kein Stripe nötig
        return {"client_secret": None, "payment_intent_id": None, "betrag": 0}

    intent = stripe.PaymentIntent.create(
        amount=betrag_rappen,
        currency=waehrung.lower(),
        automatic_payment_methods={"enabled": True},
        description=f"{_paket_name(paket)} – {_paket_beschreibung(paket)}",
        metadata={
            "paket": paket.value,
            "session_token": session_token,
            "plattform": "daf-einstufungstest",
        },
    )

    return {
        "client_secret": intent.client_secret,
        "payment_intent_id": intent.id,
        "betrag": betrag_decimal,
        "waehrung": waehrung.upper(),
    }


async def verarbeite_webhook(payload: bytes, sig_header: str) -> Optional[dict]:
    """
    Verarbeitet Stripe-Webhook-Events.
    Gibt das Event zurück oder None bei ungültiger Signatur.
    """
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.stripe_webhook_secret
        )
        return event
    except (stripe.error.SignatureVerificationError, ValueError):
        return None


def get_publishable_key() -> str:
    return settings.stripe_publishable_key
