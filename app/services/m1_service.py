"""
M1-Service: Grammatik & Wortschatz (Multiple Choice).
Generiert niveau-adaptive Items via GPT-4.1 und wertet sie aus.
"""
import json
import re
from typing import Optional

from openai import AsyncOpenAI

from app.config import settings
from app.models.database import CEFRNiveau

client = AsyncOpenAI(api_key=settings.openai_api_key)

# Statische Fallback-Items falls GPT nicht verfügbar
FALLBACK_ITEMS = {
    "A2": [
        {"id": 1, "frage": "Ich ___ jeden Tag Kaffee.", "optionen": ["trinke", "trinkt", "trinkst", "trinken"], "korrekt": 0, "erklaerung": "1. Person Singular: ich trinke"},
        {"id": 2, "frage": "Das ist ___ Buch.", "optionen": ["ein", "eine", "einen", "einem"], "korrekt": 0, "erklaerung": "Nominativ Neutrum: ein Buch"},
        {"id": 3, "frage": "Wo ___ du gestern?", "optionen": ["warst", "bist", "hast", "wärst"], "korrekt": 0, "erklaerung": "sein im Präteritum: du warst"},
        {"id": 4, "frage": "Ich gehe ___ Supermarkt.", "optionen": ["in den", "in dem", "in die", "in das"], "korrekt": 0, "erklaerung": "Akkusativ maskulin: in den Supermarkt"},
        {"id": 5, "frage": "Er ___ nicht schlafen.", "optionen": ["kann", "könnte", "konnte", "kannte"], "korrekt": 0, "erklaerung": "Modalverb können: er kann"},
        {"id": 6, "frage": "Das Wetter ist heute sehr ___.", "optionen": ["schön", "schöne", "schöner", "schönen"], "korrekt": 0, "erklaerung": "Prädikativ: kein Adjektivendung"},
        {"id": 7, "frage": "Ich habe ___ Hunger.", "optionen": ["großen", "großem", "großer", "großes"], "korrekt": 0, "erklaerung": "Akkusativ maskulin nach haben: großen"},
        {"id": 8, "frage": "Sie ___ morgen nach Berlin.", "optionen": ["fährt", "fahrt", "fähren", "fahren"], "korrekt": 0, "erklaerung": "3. Person Singular Vokalwechsel: fährt"},
        {"id": 9, "frage": "Ich ___ gerne Musik.", "optionen": ["höre", "höre", "hören", "hörst"], "korrekt": 0, "erklaerung": "1. Person Singular: ich höre"},
        {"id": 10, "frage": "Das Haus ___ meinem Vater.", "optionen": ["gehört", "gehöre", "gehören", "gehörst"], "korrekt": 0, "erklaerung": "3. Person Singular: gehört"},
    ]
}


async def generiere_items(niveau: str = "B1", hilfssprache: str = "de") -> list[dict]:
    """
    Generiert 10 adaptive MC-Items für das angegebene CEFR-Niveau.
    Aufgabenanweisungen werden in der Hilfssprache ergänzt wenn nicht Deutsch.
    """
    hilfs_hinweis = ""
    if hilfssprache != "de":
        sprach_namen = {
            "en": "English", "tr": "Türkçe", "ar": "العربية",
            "uk": "Українська", "ru": "Русский", "fr": "Français",
            "it": "Italiano", "es": "Español"
        }
        sprach_name = sprach_namen.get(hilfssprache, hilfssprache)
        hilfs_hinweis = f'\nFüge für jede Frage ein Feld "hinweis_{hilfssprache}" hinzu mit einer kurzen Erklärung der Aufgabe auf {sprach_name}.'

    prompt = f"""Du bist ein DaF-Experte. Erstelle genau 10 Multiple-Choice-Items zum Testen von Grammatik und Wortschatz auf CEFR-Niveau {niveau}.

Anforderungen:
- Jedes Item hat genau 4 Antwortoptionen (A, B, C, D)
- Genau eine Option ist korrekt
- Die Distraktoren sind plausibel (typische Lernerfehler)
- Themen: Verbkonjugation, Kasus, Artikel, Präpositionen, Wortschatz, Tempus
- Niveau {niveau}: {_niveau_beschreibung(niveau)}
{hilfs_hinweis}

Antworte ausschließlich mit einem JSON-Array:
[
  {{
    "id": 1,
    "frage": "Lückensatz oder Frage auf Deutsch",
    "optionen": ["Option A", "Option B", "Option C", "Option D"],
    "korrekt": 0,
    "erklaerung": "Kurze grammatische Erklärung auf Deutsch"
  }},
  ...
]

korrekt ist der 0-basierte Index der richtigen Antwort."""

    try:
        response = await client.chat.completions.create(
            model="gpt-4.1",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.7,
        )
        content = response.choices[0].message.content
        data = json.loads(content)
        # GPT gibt manchmal {"items": [...]} zurück
        if isinstance(data, dict):
            for key in ("items", "fragen", "aufgaben"):
                if key in data:
                    data = data[key]
                    break
        if isinstance(data, list) and len(data) >= 5:
            return data[:10]
    except Exception as e:
        print(f"[M1] Item-Generierung fehlgeschlagen: {e}")

    # Fallback
    return FALLBACK_ITEMS.get(niveau, FALLBACK_ITEMS["A2"])


def _niveau_beschreibung(niveau: str) -> str:
    beschreibungen = {
        "A1": "Sehr einfach: Grundverben (sein, haben), einfache Sätze, Zahlen, Farben",
        "A2": "Einfach: Präsens aller Verben, Artikel, einfache Präpositionen, Alltagswortschatz",
        "B1": "Mittel: Perfekt, Modalverben, Nebensätze, Kasus, erweiterter Wortschatz",
        "B2": "Fortgeschritten: Konjunktiv, Passiv, komplexe Satzstrukturen, idiomatischer Ausdruck",
        "C1": "Sehr fortgeschritten: Stilistische Feinheiten, seltene Konstruktionen, Fachvokabular",
        "C2": "Mastery: Nuancen, literarische Sprache, höchste Präzision",
    }
    return beschreibungen.get(niveau, "Mittleres Niveau")


async def werte_aus(items: list[dict], antworten: dict[str, int]) -> dict:
    """
    Wertet die MC-Antworten aus und berechnet Score + CEFR-Niveau.
    antworten: {"1": 2, "2": 0, ...} (item_id -> gewählter Index)
    """
    korrekt = 0
    total = len(items)
    details = []

    for item in items:
        item_id = str(item["id"])
        gewaehlt = antworten.get(item_id, -1)
        ist_korrekt = gewaehlt == item.get("korrekt", -99)
        if ist_korrekt:
            korrekt += 1
        details.append({
            "id": item["id"],
            "frage": item["frage"],
            "gewaehlt": gewaehlt,
            "korrekt": item.get("korrekt"),
            "ist_korrekt": ist_korrekt,
            "erklaerung": item.get("erklaerung", ""),
        })

    prozent = (korrekt / total * 100) if total > 0 else 0
    score = round(prozent, 1)

    # Grob-Niveau aus Prozent
    if prozent >= 85:
        grob_niveau = "hoch"
        naechstes_niveau = "B2"
    elif prozent >= 40:
        grob_niveau = "mittel"
        naechstes_niveau = "B1"
    else:
        grob_niveau = "niedrig"
        naechstes_niveau = "A2"

    # CEFR-Einstufung
    if prozent >= 90:
        cefr = CEFRNiveau.C1
    elif prozent >= 75:
        cefr = CEFRNiveau.B2
    elif prozent >= 55:
        cefr = CEFRNiveau.B1
    elif prozent >= 35:
        cefr = CEFRNiveau.A2
    else:
        cefr = CEFRNiveau.A1

    return {
        "score": score,
        "korrekt": korrekt,
        "total": total,
        "prozent": prozent,
        "cefr": cefr.value,
        "grob_niveau": grob_niveau,
        "naechstes_niveau": naechstes_niveau,
        "details": details,
    }
