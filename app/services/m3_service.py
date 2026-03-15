"""
M3-Service: Hörverstehen.
Generiert niveau-adaptive Hörtexte, spricht sie via OpenAI TTS ein
und erstellt Verständnisfragen. Jede Generierung verwendet ein zufälliges
Thema, damit nie zweimal dieselbe Aufgabe erscheint.
"""
import base64
import json
import random
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

# Themenpool pro Niveau – wird zufällig gezogen, damit jede Aufgabe anders ist
THEMEN_POOL = {
    "A1": [
        "Begrüßung und Vorstellung auf der Straße",
        "Einkauf im Supermarkt",
        "Im Café bestellen",
        "Wegbeschreibung in der Stadt",
        "Telefongespräch mit einem Freund",
        "Wetter und Kleidung",
        "Zahlen und Uhrzeiten",
        "Familie vorstellen",
    ],
    "A2": [
        "Verabredung zum Sport treffen",
        "Arzttermin vereinbaren",
        "Reisebuchung am Schalter",
        "Wochenende planen",
        "Im Restaurant reklamieren",
        "Busfahrplan erfragen",
        "Neuer Job: erster Tag",
        "Geburtstagsfeier organisieren",
    ],
    "B1": [
        "Diskussion über Homeoffice-Vor- und Nachteile",
        "Radiosendung über nachhaltige Ernährung",
        "Gespräch über Urlaubserfahrungen",
        "Interview mit einem Sportler",
        "Nachrichtenbericht über ein Stadtfest",
        "Telefonat über Wohnungssuche",
        "Diskussion über soziale Medien",
        "Bericht über ein ehrenamtliches Projekt",
    ],
    "B2": [
        "Radiopodcast über künstliche Intelligenz im Alltag",
        "Interview mit einer Unternehmerin über Work-Life-Balance",
        "Diskussionsrunde über Klimapolitik",
        "Reportage über urbane Landwirtschaft",
        "Gespräch über Bildungssystem-Reformen",
        "Podcast über psychische Gesundheit am Arbeitsplatz",
        "Nachrichtensendung über internationale Migration",
        "Interview über Digitalisierung im Gesundheitswesen",
    ],
    "C1": [
        "Akademischer Vortrag über Spracherwerbstheorien",
        "Diskussion über ethische Fragen der Gentechnik",
        "Radiodebatte über Demokratie und soziale Medien",
        "Vortrag über postkoloniale Literatur",
        "Podiumsdiskussion über Wirtschaftsungleichheit",
        "Interview mit einem Philosophen über freien Willen",
        "Wissenschaftssendung über Quantencomputing",
        "Diskussion über Urbanisierung und soziale Segregation",
    ],
    "C2": [
        "Philosophisches Seminar über Erkenntnistheorie",
        "Literarische Analyse eines modernen Romans",
        "Wissenschaftliche Debatte über Klimamodelle",
        "Vortrag über die Geschichte der deutschen Sprache",
        "Diskussion über postmoderne Kunsttheorie",
        "Radiogespräch über Transhumanismus",
        "Analyse politischer Rhetorik",
        "Vortrag über Neuroplastizität und Sprachenlernen",
    ],
}


async def generiere_hoeraufgabe(niveau: str = "B1", hilfssprache: str = "de") -> dict:
    """Generiert einen Hörtext mit Fragen und TTS-Audio. Jedes Mal ein anderes Thema."""
    niveau_vorgaben = {
        "A1": "3–4 einfache Sätze, Alltagsdialog",
        "A2": "5–7 Sätze, einfacher Dialog oder kurze Ansage",
        "B1": "8–10 Sätze, Alltagsgespräch oder kurze Nachricht",
        "B2": "10–14 Sätze, Interview oder Radionachricht",
        "C1": "14–18 Sätze, Vortrag oder komplexes Interview",
        "C2": "18+ Sätze, akademischer Vortrag oder Diskussion",
    }

    # Zufälliges Thema aus dem Pool wählen
    themen = THEMEN_POOL.get(niveau, THEMEN_POOL["B1"])
    thema = random.choice(themen)

    # Zufälligen Seed für zusätzliche Variation
    variation_seed = random.randint(1000, 9999)

    prompt = f"""Erstelle eine Hörverstehen-Aufgabe für DaF-Lernende auf CEFR-Niveau {niveau}.

Thema: {thema}
Vorgaben: {niveau_vorgaben.get(niveau, niveau_vorgaben['B1'])}
Variante #{variation_seed} – erstelle eine einzigartige, neue Aufgabe zu diesem Thema.

Antworte ausschließlich mit JSON:
{{
  "titel": "Titel der Höraufgabe",
  "hoertext": "Der vollständige Text der gesprochen wird (natürliche gesprochene Sprache, passend zum Thema)",
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

Erstelle genau 4 Fragen. Verwende natürliche gesprochene Sprache im Hörtext.
Alle 4 Antwortoptionen müssen plausibel klingen – vermeide offensichtlich falsche Optionen."""

    try:
        response = await client.chat.completions.create(
            model="gpt-4.1",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.95,  # Hohe Temperatur für maximale Variation
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
        aufgabe["audio_b64"] = base64.b64encode(audio_bytes).decode()
        aufgabe["audio_format"] = "mp3"
    except Exception as e:
        print(f"[M3] TTS fehlgeschlagen: {e}")
        aufgabe["audio_b64"] = None

    return aufgabe


def _fallback_hoeraufgabe(niveau: str) -> dict:
    """Verschiedene Fallback-Aufgaben je nach Niveau."""
    fallbacks = {
        "A1": {
            "titel": "Im Café",
            "hoertext": "Kellner: Guten Tag! Was darf ich Ihnen bringen? Kunde: Ich hätte gerne einen Kaffee und ein Stück Kuchen. Kellner: Welchen Kuchen möchten Sie? Wir haben Apfelkuchen und Schokoladenkuchen. Kunde: Den Apfelkuchen bitte. Kellner: Gerne. Das macht zusammen vier Euro fünfzig.",
            "fragen": [
                {"id": 1, "frage": "Was bestellt der Kunde?", "optionen": ["Tee und Kuchen", "Kaffee und Kuchen", "Wasser und Kuchen"], "korrekt": 1, "erklaerung": "Er bestellt Kaffee und Kuchen."},
                {"id": 2, "frage": "Welchen Kuchen wählt der Kunde?", "optionen": ["Schokoladenkuchen", "Käsekuchen", "Apfelkuchen"], "korrekt": 2, "erklaerung": "Er wählt den Apfelkuchen."},
                {"id": 3, "frage": "Wie viel kostet die Bestellung?", "optionen": ["3,50 €", "4,50 €", "5,00 €"], "korrekt": 1, "erklaerung": "Der Kellner sagt: vier Euro fünfzig."},
                {"id": 4, "frage": "Wo findet das Gespräch statt?", "optionen": ["Im Restaurant", "Im Café", "Im Supermarkt"], "korrekt": 1, "erklaerung": "Der Titel und Kontext zeigen: Im Café."},
            ],
        },
        "B1": {
            "titel": "Gespräch über Homeoffice",
            "hoertext": "Anna: Seit drei Monaten arbeite ich jetzt im Homeoffice. Am Anfang fand ich es toll – kein Pendeln, mehr Zeit für mich. Aber inzwischen vermisse ich die Kollegen. Mark: Das kenne ich. Ich arbeite zwei Tage pro Woche im Büro und den Rest zuhause. Das ist für mich die beste Lösung. Anna: Wie schaffst du es, zuhause konzentriert zu bleiben? Mark: Ich habe mir ein richtiges Arbeitszimmer eingerichtet und halte feste Arbeitszeiten ein. Das hilft enorm.",
            "fragen": [
                {"id": 1, "frage": "Wie lange arbeitet Anna schon im Homeoffice?", "optionen": ["Einen Monat", "Drei Monate", "Ein Jahr"], "korrekt": 1, "erklaerung": "Anna sagt: seit drei Monaten."},
                {"id": 2, "frage": "Was vermisst Anna im Homeoffice?", "optionen": ["Die Kantine", "Die Kollegen", "Das Büro"], "korrekt": 1, "erklaerung": "Anna sagt, sie vermisst die Kollegen."},
                {"id": 3, "frage": "Wie oft arbeitet Mark im Büro?", "optionen": ["Jeden Tag", "Einmal pro Woche", "Zweimal pro Woche"], "korrekt": 2, "erklaerung": "Mark sagt: zwei Tage pro Woche."},
                {"id": 4, "frage": "Was hilft Mark beim konzentrierten Arbeiten?", "optionen": ["Musik hören", "Feste Arbeitszeiten", "Kurze Pausen"], "korrekt": 1, "erklaerung": "Mark nennt feste Arbeitszeiten als wichtigen Faktor."},
            ],
        },
    }
    fb = fallbacks.get(niveau, fallbacks.get("B1"))
    fb["niveau"] = niveau
    fb["audio_b64"] = None
    return fb
