"""
M3-Service: Hörverstehen.
Generiert niveau-adaptive Hörtexte, spricht sie via OpenAI TTS ein
und erstellt Verständnisfragen.
"""
import json
import os
import tempfile
from openai import AsyncOpenAI
from app.config import settings

client = AsyncOpenAI(api_key=settings.openai_api_key)

# TTS-Stimmen nach Geschwindigkeit/Niveau
STIMMEN_NACH_NIVEAU = {
    "A1": ("alloy", 0.85),    # langsam, klar
    "A2": ("alloy", 0.90),
    "B1": ("nova", 1.0),
    "B2": ("nova", 1.05),
    "C1": ("onyx", 1.1),
    "C2": ("onyx", 1.15),
}


async def generiere_hoeraufgabe(niveau: str = "B1", hilfssprache: str = "de") -> dict:
    """Generiert einen Hörtext mit Fragen und TTS-Audio."""
    niveau_vorgaben = {
        "A1": "3–4 einfache Sätze, Alltagsdialog (Begrüßung, Einkauf)",
        "A2": "5–7 Sätze, einfacher Dialog oder kurze Ansage",
        "B1": "8–10 Sätze, Alltagsgespräch oder kurze Nachricht",
        "B2": "10–14 Sätze, Interview oder Radionachricht",
        "C1": "14–18 Sätze, Vortrag oder komplexes Interview",
        "C2": "18+ Sätze, akademischer Vortrag oder Diskussion",
    }

    prompt = f"""Erstelle eine Hörverstehen-Aufgabe für DaF-Lernende auf CEFR-Niveau {niveau}.

Vorgaben: {niveau_vorgaben.get(niveau, niveau_vorgaben['B1'])}

Antworte ausschließlich mit JSON:
{{
  "titel": "Titel der Höraufgabe",
  "hoertext": "Der vollständige Text der gesprochen wird (natürliche gesprochene Sprache)",
  "fragen": [
    {{
      "id": 1,
      "frage": "Frage zum Hörtext",
      "optionen": ["Option A", "Option B", "Option C"],
      "korrekt": 0,
      "erklaerung": "Begründung"
    }}
  ],
  "niveau": "{niveau}"
}}

Erstelle genau 4 Fragen. Verwende natürliche gesprochene Sprache im Hörtext."""

    try:
        response = await client.chat.completions.create(
            model="gpt-4.1",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.7,
        )
        aufgabe = json.loads(response.choices[0].message.content)
    except Exception as e:
        print(f"[M3] Aufgabe-Generierung fehlgeschlagen: {e}")
        aufgabe = _fallback_hoeraufgabe(niveau)

    # TTS-Audio generieren
    stimme, geschwindigkeit = STIMMEN_NACH_NIVEAU.get(niveau, ("nova", 1.0))
    try:
        tts_response = await client.audio.speech.create(
            model="tts-1",
            voice=stimme,
            input=aufgabe["hoertext"],
            speed=geschwindigkeit,
        )
        audio_bytes = tts_response.content
        # Als Base64 für Frontend
        import base64
        aufgabe["audio_b64"] = base64.b64encode(audio_bytes).decode()
        aufgabe["audio_format"] = "mp3"
    except Exception as e:
        print(f"[M3] TTS fehlgeschlagen: {e}")
        aufgabe["audio_b64"] = None

    return aufgabe


def _fallback_hoeraufgabe(niveau: str) -> dict:
    return {
        "titel": "Im Café",
        "hoertext": "Kellner: Guten Tag! Was darf ich Ihnen bringen? Kunde: Ich hätte gerne einen Kaffee und ein Stück Kuchen. Kellner: Welchen Kuchen möchten Sie? Wir haben Apfelkuchen und Schokoladenkuchen. Kunde: Den Apfelkuchen bitte. Kellner: Gerne. Das macht zusammen vier Euro fünfzig.",
        "fragen": [
            {"id": 1, "frage": "Was bestellt der Kunde?", "optionen": ["Tee und Kuchen", "Kaffee und Kuchen", "Wasser und Kuchen"], "korrekt": 1, "erklaerung": "Er bestellt Kaffee und Kuchen."},
            {"id": 2, "frage": "Welchen Kuchen wählt der Kunde?", "optionen": ["Schokoladenkuchen", "Käsekuchen", "Apfelkuchen"], "korrekt": 2, "erklaerung": "Er wählt den Apfelkuchen."},
            {"id": 3, "frage": "Wie viel kostet die Bestellung?", "optionen": ["3,50 €", "4,50 €", "5,00 €"], "korrekt": 1, "erklaerung": "Der Kellner sagt: vier Euro fünfzig."},
            {"id": 4, "frage": "Wo findet das Gespräch statt?", "optionen": ["Im Restaurant", "Im Café", "Im Supermarkt"], "korrekt": 1, "erklaerung": "Der Titel und Kontext zeigen: Im Café."},
        ],
        "niveau": niveau,
        "audio_b64": None,
    }
