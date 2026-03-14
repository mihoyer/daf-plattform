"""
M2-Service: Lesen & Leseverstehen.
Generiert niveau-adaptive Lesetexte und Verständnisfragen via GPT-4.1.
"""
import json
import random
from openai import AsyncOpenAI
from app.config import settings

client = AsyncOpenAI(api_key=settings.openai_api_key)

# Themenpool für abwechslungsreiche Texte
THEMEN_POOL = {
    "A1": ["Familie und Zuhause", "Essen und Trinken", "Zahlen und Farben", "Tiere", "Körper und Gesundheit"],
    "A2": ["Freizeit und Hobbys", "Einkaufen und Geld", "Arbeit und Berufe", "Reisen und Verkehr", "Wetter und Jahreszeiten", "Freundschaft"],
    "B1": ["Umwelt und Natur", "Medien und Technologie", "Bildung und Schule", "Gesundheit und Ernährung", "Kultur und Traditionen", "Sport und Fitness", "Städte und Wohnen"],
    "B2": ["Digitalisierung und Gesellschaft", "Klimawandel", "Globalisierung", "Arbeitswelt im Wandel", "Migration und Integration", "Wissenschaft und Forschung"],
    "C1": ["Philosophie und Ethik", "Wirtschaft und Politik", "Künstliche Intelligenz", "Sprachpolitik", "Literatur und Kunst", "Demografischer Wandel"],
    "C2": ["Erkenntnistheorie", "Postmoderne Gesellschaft", "Sprachphilosophie", "Kulturkritik"],
}


async def generiere_leseaufgabe(niveau: str = "B1", hilfssprache: str = "de") -> dict:
    """Generiert einen Lesetext mit 5 Verständnisfragen für das angegebene Niveau."""
    niveau_vorgaben = {
        "A1": "50–80 Wörter, sehr einfache Sätze, Alltagsthemen (Familie, Wohnung, Essen)",
        "A2": "80–120 Wörter, einfache Sätze, Alltagsthemen (Freizeit, Einkaufen, Arbeit)",
        "B1": "120–180 Wörter, mittlere Komplexität, gesellschaftliche Themen",
        "B2": "180–250 Wörter, komplexere Sätze, abstrakte Themen",
        "C1": "250–350 Wörter, anspruchsvolle Sprache, Fachtexte oder Meinungsartikel",
        "C2": "350+ Wörter, literarische oder wissenschaftliche Texte",
    }
    vorgabe = niveau_vorgaben.get(niveau, niveau_vorgaben["B1"])
    thema = random.choice(THEMEN_POOL.get(niveau, THEMEN_POOL["B1"]))

    hilfs_hinweis = ""
    if hilfssprache != "de":
        sprach_namen = {"en": "English", "tr": "Türkçe", "ar": "العربية",
                        "uk": "Українська", "ru": "Русский", "fr": "Français",
                        "it": "Italiano", "es": "Español"}
        hilfs_hinweis = f'\nFüge ein Feld "anweisung_{hilfssprache}" hinzu mit der Aufgabenanweisung auf {sprach_namen.get(hilfssprache, hilfssprache)}.'

    prompt = f"""Erstelle eine Leseverstehen-Aufgabe für DaF-Lernende auf CEFR-Niveau {niveau}.

Thema: {thema}
Vorgaben für den Text: {vorgabe}
{hilfs_hinweis}

Antworte ausschließlich mit JSON:
{{
  "titel": "Titel des Textes",
  "text": "Der vollständige Lesetext auf Deutsch",
  "fragen": [
    {{
      "id": 1,
      "typ": "multiple_choice",
      "frage": "Frage zum Text",
      "optionen": ["Option A", "Option B", "Option C", "Option D"],
      "korrekt": 0,
      "erklaerung": "Warum diese Antwort korrekt ist"
    }}
  ],
  "vorlese_saetze": ["Satz 1 aus dem Text zum Vorlesen", "Satz 2"],
  "niveau": "{niveau}"
}}

Erstelle genau 5 Fragen. Fragen-Typen: globales Verständnis, Detailverständnis, Inferenz, Wortschatz, Grammatik im Kontext."""

    try:
        response = await client.chat.completions.create(
            model="gpt-4.1",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.7,
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        print(f"[M2] Leseaufgabe-Generierung fehlgeschlagen: {e}")
        return _fallback_leseaufgabe(niveau)


def _fallback_leseaufgabe(niveau: str) -> dict:
    return {
        "titel": "Ein Tag in der Stadt",
        "text": "Maria wohnt in Berlin. Sie arbeitet als Lehrerin. Jeden Morgen fährt sie mit der U-Bahn zur Schule. Die Schule ist nicht weit von ihrer Wohnung. Nach der Arbeit kauft sie im Supermarkt ein. Abends liest sie gerne Bücher oder schaut Filme.",
        "fragen": [
            {"id": 1, "typ": "multiple_choice", "frage": "Wo wohnt Maria?", "optionen": ["München", "Hamburg", "Berlin", "Wien"], "korrekt": 2, "erklaerung": "Im Text steht: Maria wohnt in Berlin."},
            {"id": 2, "typ": "multiple_choice", "frage": "Was ist Marias Beruf?", "optionen": ["Ärztin", "Lehrerin", "Köchin", "Verkäuferin"], "korrekt": 1, "erklaerung": "Sie arbeitet als Lehrerin."},
            {"id": 3, "typ": "multiple_choice", "frage": "Wie fährt Maria zur Arbeit?", "optionen": ["Mit dem Auto", "Mit dem Fahrrad", "Mit der U-Bahn", "Zu Fuß"], "korrekt": 2, "erklaerung": "Sie fährt mit der U-Bahn."},
            {"id": 4, "typ": "multiple_choice", "frage": "Was macht Maria nach der Arbeit?", "optionen": ["Sie geht ins Kino", "Sie kauft ein", "Sie kocht", "Sie schläft"], "korrekt": 1, "erklaerung": "Nach der Arbeit kauft sie ein."},
            {"id": 5, "typ": "multiple_choice", "frage": "Was macht Maria am Abend?", "optionen": ["Sport", "Kochen", "Lesen oder Filme schauen", "Schlafen"], "korrekt": 2, "erklaerung": "Abends liest sie oder schaut Filme."},
        ],
        "vorlese_saetze": ["Maria wohnt in Berlin.", "Sie arbeitet als Lehrerin."],
        "niveau": niveau,
    }
