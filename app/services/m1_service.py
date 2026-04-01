"""
M1-Service: Grammatik & Wortschatz – Adaptives CAT-System.

Ablauf:
1. start()        → 3 Einstiegsfragen (A2 / B1 / B2 gemischt)
2. naechste()     → nach jeder Antwort Niveau neu schätzen, nächste Frage generieren
3. werte_aus()    → Endauswertung mit GPT-Analyse

Jede Frage wird frisch von GPT generiert (zufälliger Grammatikfokus).
Kein Cache, keine Wiederholungen.

Verbesserungen v2:
- Authentizitätsprinzip: Sätze klingen wie aus echten Texten/Gesprächen
- Niedrigere Temperature (0.65) für präzisere grammatische Ausgaben
- Niveau-spezifische Verbotslisten gegen konstruierte Sätze
- Für C1/C2: kein zufälliges Thema mehr, nur Grammatikfokus
- Fokuspool nach Niveau gefiltert (keine C1-Konstruktionen auf A2)
"""
import json
import random
from typing import Optional

from openai import AsyncOpenAI

from app.config import settings
from app.models.database import CEFRNiveau

client = AsyncOpenAI(api_key=settings.openai_api_key)

# ── Konstanten ────────────────────────────────────────────────────────────────

NIVEAUS = ["A1", "A2", "B1", "B2", "C1", "C2"]

# Themen nur für A1–B2 (niedrige Niveaus brauchen Alltagskontext)
THEMEN_NIEDRIG = [
    "Alltag und Familie", "Arbeit und Beruf", "Reisen und Urlaub",
    "Gesundheit und Arzt", "Essen und Kochen", "Wohnen und Umzug",
    "Einkaufen", "Freizeit und Hobbys", "Schule und Studium",
    "Öffentliche Verkehrsmittel", "Wetter", "Freunde und Bekannte",
]

# Grammatikfokus nach Niveau – nur passende Strukturen pro Stufe
GRAMMATIK_NACH_NIVEAU = {
    "A1": [
        "sein/haben im Präsens (ich/du/er/wir/ihr/sie)",
        "Grundwortschatz: Nomen mit bestimmtem Artikel (der/die/das)",
        "Personalpronomen und einfache Verben im Präsens",
        "Zahlen, Farben, einfache Adjektive",
    ],
    "A2": [
        "Perfekt mit haben/sein (regelmäßige Verben)",
        "Perfekt mit haben/sein (unregelmäßige Verben)",
        "Akkusativ mit bestimmtem/unbestimmtem Artikel",
        "Dativ nach Präpositionen (in/an/auf/bei/mit/nach/von/zu)",
        "Modalverben im Präsens (können/müssen/wollen/dürfen/sollen)",
        "Trennbare Verben (aufmachen, anrufen, einladen…)",
        "Komparation: Komparativ und Superlativ",
    ],
    "B1": [
        "Kausalsätze mit weil/da (Verbstellung am Ende)",
        "Finalsätze mit damit/um…zu",
        "Konzessivsätze mit obwohl",
        "Temporalsätze mit als/wenn/während/nachdem",
        "Konjunktiv II: würde + Infinitiv (höfliche Bitten, Wünsche)",
        "Passiv Präsens (wird gemacht)",
        "Genitiv: des/der/eines/einer",
        "Relativsätze im Nominativ und Akkusativ",
        "Indirekte Rede mit dass",
    ],
    "B2": [
        "Konjunktiv II von starken Verben (wäre, hätte, käme, ginge…)",
        "Passiv Perfekt und Passiv Präteritum",
        "Relativsätze im Dativ und Genitiv",
        "Präpositionaladverbien (darauf, damit, worüber…)",
        "Erweiterte Partizipialkonstruktionen",
        "Konzessive Konstruktionen (trotzdem/dennoch/obwohl)",
        "Idiomatische Ausdrücke und Kollokationen",
        "Nominalisierung (das Lesen, die Entscheidung…)",
    ],
    "C1": [
        "Konzessiver Konjunktiv (wenngleich, wiewohl, auch wenn)",
        "Infinitivkonstruktionen mit zu (ohne es zu merken, anstatt zu…)",
        "Stilistische Variation: Nominalstil vs. Verbalstil",
        "Seltene Präpositionen mit Genitiv (anlässlich, infolge, hinsichtlich)",
        "Subjunktoren: sofern, insofern, insoweit, soweit",
        "Doppelkonjunktionen: sowohl…als auch, weder…noch, je…desto",
        "Fachvokabular und Register (formell vs. informell)",
    ],
    "C2": [
        "Konzessiver Konjunktiv mit mochte/möge (mochte er auch…)",
        "Archaische/literarische Konstruktionen (er sei, man nehme…)",
        "Nuancen zwischen bedeutungsähnlichen Präpositionen",
        "Stilistik: Ellipsen, Inversionen, Parenthesen",
        "Feine semantische Unterschiede (obwohl vs. obgleich vs. wenngleich)",
        "Komplexe Nominalphrasen mit mehreren Attributen",
    ],
}

# Einstiegs-Niveaus: breite Streuung für schnelle Einschätzung
EINSTIEGS_NIVEAUS = ["A2", "B1", "B2"]

# Niveau-spezifische Verbotslisten für den Prompt
VERBOTE_NACH_NIVEAU = {
    "A1": "Keine Nebensätze. Keine Perfekt-Formen. Keine Kasus-Konstruktionen.",
    "A2": "Keine Nebensätze. Keine Konjunktiv-Formen. Maximal ein grammatischer Fokus pro Satz.",
    "B1": "Keine Doppel-Nebensätze. Kein Konjunktiv II von starken Verben. Kein Passiv Perfekt.",
    "B2": "Keine literarischen Konstruktionen. Kein konzessiver Konjunktiv mit 'mochte'. Sätze bleiben verständlich.",
    "C1": "Keine archaischen Formen. Kein 'er sei' / 'man nehme'. Sätze müssen im modernen Schriftdeutsch vorkommen.",
    "C2": "Nur Konstruktionen, die in anspruchsvollen Sachtexten oder Literatur tatsächlich vorkommen.",
}

# Authentizitäts-Kontext nach Niveau
KONTEXT_NACH_NIVEAU = {
    "A1": "einfaches Alltagsgespräch (Begrüßung, Vorstellung, Einkauf)",
    "A2": "kurze Alltagssituation (Gespräch, SMS, einfache E-Mail)",
    "B1": "Alltagstext (Zeitungsnotiz, E-Mail, Gespräch unter Kollegen)",
    "B2": "Zeitungsartikel, Sachtext, formelles Schreiben oder Interview",
    "C1": "Qualitätsjournalismus, Sachbuch, Fachtext oder Essay",
    "C2": "anspruchsvoller Sachtext, wissenschaftlicher Artikel oder Literatur",
}


# ── Niveau-Schätzung (IRT-vereinfacht) ───────────────────────────────────────

def schaetze_niveau(antworten_verlauf: list[dict]) -> str:
    """
    Schätzt das aktuelle Niveau basierend auf dem bisherigen Antwortverlauf.
    antworten_verlauf: [{"niveau": "B1", "korrekt": True}, ...]
    """
    if not antworten_verlauf:
        return "B1"

    niveau_punkte = 0.0
    gewicht_gesamt = 0.0

    for i, eintrag in enumerate(antworten_verlauf):
        gewicht = 1.0 + i * 0.3
        niveau_idx = NIVEAUS.index(eintrag["niveau"])

        if eintrag["korrekt"]:
            ziel_idx = min(niveau_idx + 1, len(NIVEAUS) - 1)
        else:
            ziel_idx = max(niveau_idx - 1, 0)

        niveau_punkte += ziel_idx * gewicht
        gewicht_gesamt += gewicht

    geschaetzter_idx = round(niveau_punkte / gewicht_gesamt)
    geschaetzter_idx = max(0, min(geschaetzter_idx, len(NIVEAUS) - 1))
    return NIVEAUS[geschaetzter_idx]


# ── GPT-Generierung ───────────────────────────────────────────────────────────

async def generiere_eine_frage(
    niveau: str,
    hilfssprache: str = "de",
    bereits_verwendet: list[str] | None = None,
    item_id: int = 1,
) -> dict:
    """
    Generiert genau eine Lückentext-Frage für das angegebene Niveau.
    Der Lernende gibt die Antwort als Freitext ein (kein Multiple-Choice).
    Authentizitätsprinzip: Satz klingt wie aus einem echten Text.
    """
    # Grammatikfokus aus dem niveau-spezifischen Pool
    fokus_pool = GRAMMATIK_NACH_NIVEAU.get(niveau, GRAMMATIK_NACH_NIVEAU["B1"])
    fokus = random.choice(fokus_pool)

    # Thema nur für A1–B2 (höhere Niveaus: Fokus reicht als Kontext)
    if niveau in ("A1", "A2", "B1", "B2"):
        thema = random.choice(THEMEN_NIEDRIG)
        thema_zeile = f"THEMA: {thema} (der Satz soll aus diesem Alltagsbereich stammen)"
    else:
        thema_zeile = "THEMA: frei wählbar – wähle ein Thema, das zum Grammatikfokus passt"

    kontext = KONTEXT_NACH_NIVEAU.get(niveau, "Alltagstext")
    verbote = VERBOTE_NACH_NIVEAU.get(niveau, "")

    hilfs_hinweis = ""
    if hilfssprache != "de":
        sprach_namen = {
            "en": "English", "tr": "Türkçe", "ar": "العربية",
            "uk": "Українська", "ru": "Русский", "fr": "Français",
            "it": "Italiano", "es": "Español", "fa": "فارسی",
            "zh": "中文", "pl": "Polski",
        }
        sprach_name = sprach_namen.get(hilfssprache, hilfssprache)
        hilfs_hinweis = f'\nFüge ein Feld "hinweis_{hilfssprache}" mit einer kurzen Aufgabenerklärung auf {sprach_name} hinzu.'

    vermeidungs_hinweis = ""
    if bereits_verwendet:
        beispiele = " | ".join(bereits_verwendet[-4:])
        vermeidungs_hinweis = f'\nVERMEIDE Fragen, die diesen ähneln: {beispiele}'

    prompt = f"""Du bist DaF-Prüfungsautor mit Erfahrung beim Goethe-Institut. Erstelle EINE Lückentext-Aufgabe (Freitexteingabe, kein Multiple-Choice).

NIVEAU: {niveau} (CEFR)
GRAMMATIK-FOKUS: {fokus}
{thema_zeile}
KONTEXT: Der Satz soll klingen wie aus einem echten {kontext} – nicht wie ein konstruierter Lehrbuchsatz.

VERBOTE FÜR DIESES NIVEAU:
{verbote}
{vermeidungs_hinweis}

QUALITÄTSREGELN (alle einhalten):
1. Der Satz mit der richtigen Antwort muss so klingen, wie ihn ein Muttersprachler tatsächlich schreiben würde.
2. PFLICHT: Im Feld "frage" MUSS exakt die Zeichenkette _____ (fünf Unterstriche) als Lücke vorkommen. Ohne _____ ist die Aufgabe ungültig.
3. Die Lücke testet gezielt den angegebenen Grammatikfokus.
4. Die korrekte Antwort ist eine einzelne Wortform (1–4 Wörter), grammatisch und semantisch EINDEUTIG korrekt.
5. Kein Satz, der mehrere Deutungen oder mehrere mögliche Antworten zulässt.
6. Die Erklärung nennt die Grammatikregel präzise (1–2 Sätze).
{hilfs_hinweis}

WICHTIG: Das Feld "frage" MUSS _____ enthalten. Beispiel: "Er ist _____ nach Hause gegangen."

Antworte NUR mit diesem JSON (kein Text davor oder danach):
{{
  "id": {item_id},
  "frage": "Vollständiger Satz mit _____ als Lücke – MUSS _____ enthalten!",
  "korrekte_antwort_text": "Die einzig korrekte Wortform (z.B. 'hatte', 'dem', 'weil')",
  "erklaerung": "Warum diese Form korrekt ist (1–2 präzise Sätze, Grammatikregel nennen)",
  "niveau": "{niveau}",
  "thema": "Thema des Satzes"
}}"""

    for versuch in range(3):
        try:
            response = await client.chat.completions.create(
                model="gpt-4.1",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Du bist ein erfahrener DaF-Prüfungsautor. "
                            "Du erstellst ausschließlich grammatisch einwandfreie, "
                            "natürlich klingende Lückensatz-Aufgaben. "
                            "Konstruierte oder unnatürliche Sätze lehnst du ab."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.65,  # Präzision vor Kreativität
                max_tokens=500,
            )
            data = json.loads(response.choices[0].message.content)

            if not all(k in data for k in ("frage", "korrekte_antwort_text")):
                continue

            # Sicherheitscheck: Lücke muss im Satz vorhanden sein
            if "_____" not in data.get("frage", ""):
                print(f"[M1] Kein _____ im Satz (Versuch {versuch+1}), neu generieren: {data.get('frage', '')[:80]}")
                continue

            # Kompatibilitäts-Felder für Backend (erwartet optionen/korrekt)
            korrekte_antwort = data.get("korrekte_antwort_text", "").strip()
            data["optionen"] = [korrekte_antwort, "–", "–", "–"]
            data["korrekt"] = 0
            data["id"] = item_id
            data["niveau"] = niveau
            optionen = data.get("optionen", [])
            korrekt_idx = data.get("korrekt", 0)
            # Index-Text-Konsistenz: bei Freitext immer Index 0
            data["korrekt"] = 0

            # Grammatische Validierung
            validiert = await _validiere_frage(data)
            if validiert is None:
                print(f"[M1] Validierung fehlgeschlagen (Versuch {versuch+1}), neu generieren...")
                continue

            return _mische_optionen(validiert)

        except Exception as e:
            print(f"[M1] Fragen-Generierung Versuch {versuch+1} fehlgeschlagen: {e}")

    return _fallback_frage(niveau, item_id)


async def generiere_einstiegsfragen(hilfssprache: str = "de") -> list[dict]:
    """
    Generiert 3 Einstiegsfragen mit breiter Niveau-Streuung (A2, B1, B2).
    """
    import asyncio
    einstiegs_niveaus = EINSTIEGS_NIVEAUS.copy()
    random.shuffle(einstiegs_niveaus)

    aufgaben = [
        generiere_eine_frage(niveau, hilfssprache, item_id=i + 1)
        for i, niveau in enumerate(einstiegs_niveaus)
    ]
    fragen = await asyncio.gather(*aufgaben)
    return list(fragen)


# ── Öffentliche Service-Funktionen ────────────────────────────────────────────

async def starte_adaptiven_test(hilfssprache: str = "de") -> dict:
    """Startet den adaptiven Test. Gibt 3 Einstiegsfragen zurück."""
    fragen = await generiere_einstiegsfragen(hilfssprache)
    return {
        "fragen": fragen,
        "geschaetztes_niveau": "B1",
        "antworten_verlauf": [],
        "naechste_id": len(fragen) + 1,
        "gesamt_fragen": 10,
        "phase": "einstieg",
    }


async def naechste_frage(
    zustand: dict,
    item_id: int,
    gewaehlt: int,
    hilfssprache: str = "de",
) -> dict:
    """Verarbeitet eine Antwort und generiert die nächste adaptive Frage."""
    antworten_verlauf = zustand.get("antworten_verlauf", [])
    naechste_id = zustand.get("naechste_id", 4)
    alle_fragen = zustand.get("alle_fragen", [])

    aktuelle_frage = next(
        (f for f in alle_fragen if f["id"] == item_id), None
    )

    korrekt = False
    fragen_niveau = "B1"
    if aktuelle_frage:
        korrekt = gewaehlt == aktuelle_frage.get("korrekt", -1)
        fragen_niveau = aktuelle_frage.get("niveau", "B1")

    antworten_verlauf.append({
        "item_id": item_id,
        "niveau": fragen_niveau,
        "korrekt": korrekt,
        "gewaehlt": gewaehlt,
    })

    neues_niveau = schaetze_niveau(antworten_verlauf)
    bereits_verwendet = [f.get("frage", "") for f in alle_fragen]

    neue_frage = await generiere_eine_frage(
        neues_niveau, hilfssprache, bereits_verwendet, naechste_id
    )

    return {
        "frage": neue_frage,
        "geschaetztes_niveau": neues_niveau,
        "antworten_verlauf": antworten_verlauf,
        "naechste_id": naechste_id + 1,
        "korrekt": korrekt,
    }


async def werte_aus(alle_fragen: list[dict], antworten: dict[str, int]) -> dict:
    """Endauswertung: berechnet Score, CEFR und detaillierte Analyse."""
    korrekt_count = 0
    total = len(alle_fragen)
    details = []
    antworten_verlauf = []

    for frage in alle_fragen:
        item_id = str(frage["id"])
        gewaehlt = antworten.get(item_id, -1)
        ist_korrekt = gewaehlt == frage.get("korrekt", -99)
        if ist_korrekt:
            korrekt_count += 1

        antworten_verlauf.append({
            "niveau": frage.get("niveau", "B1"),
            "korrekt": ist_korrekt,
        })

        korrekt_text = frage.get("korrekte_antwort_text") or (
            frage.get("optionen", [])[frage.get("korrekt", 0)]
            if frage.get("optionen") else "–"
        )
        details.append({
            "id": frage["id"],
            "frage": frage["frage"],
            "optionen": frage.get("optionen", []),
            "gewaehlt": gewaehlt,
            "gewaehlt_text": (frage.get("optionen", [])[gewaehlt]
                               if 0 <= gewaehlt < len(frage.get("optionen", []))
                               else "–"),
            "korrekt": frage.get("korrekt"),
            "korrekt_text": korrekt_text,
            "korrekte_antwort_text": korrekt_text,
            "ist_korrekt": ist_korrekt,
            "erklaerung": frage.get("erklaerung", ""),
            "niveau": frage.get("niveau", "B1"),
            "thema": frage.get("thema", ""),
        })

    prozent = (korrekt_count / total * 100) if total > 0 else 0

    adaptives_niveau = schaetze_niveau(antworten_verlauf)

    if prozent >= 90:
        prozent_cefr = "C1"
    elif prozent >= 75:
        prozent_cefr = "B2"
    elif prozent >= 55:
        prozent_cefr = "B1"
    elif prozent >= 35:
        prozent_cefr = "A2"
    else:
        prozent_cefr = "A1"

    adaptiv_idx = NIVEAUS.index(adaptives_niveau)
    prozent_idx = NIVEAUS.index(prozent_cefr)
    final_idx = round((adaptiv_idx + prozent_idx) / 2)
    final_cefr = NIVEAUS[final_idx]

    # Relativer Score (0-100) wird im main_router zu absolutem Score umgerechnet
    score = round(prozent, 1)

    return {
        "score": score,
        "korrekt": korrekt_count,
        "total": total,
        "prozent": prozent,
        "cefr": final_cefr,
        "adaptives_niveau": adaptives_niveau,
        "details": details,
        "staerken": _analysiere_staerken(details),
        "schwaechen": _analysiere_schwaechen(details),
    }


# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

async def _validiere_frage(item: dict) -> Optional[dict]:
    """
    Zweiter GPT-Aufruf zur grammatischen Validierung.
    Prüft: Ist die markierte Antwort eindeutig korrekt? Klingt der Satz natürlich?
    """
    frage = item.get("frage", "")
    optionen = item.get("optionen", [])
    korrekt_idx = item.get("korrekt", 0)
    niveau = item.get("niveau", "B1")

    if not frage or not optionen or korrekt_idx >= len(optionen):
        return None

    optionen_text = "\n".join(f"{i}. {opt}" for i, opt in enumerate(optionen))
    korrekte_option = optionen[korrekt_idx]

    val_prompt = f"""Du bist Deutschlehrer und Grammatikexperte. Prüfe diese DaF-Lückentext-Aufgabe (Niveau {niveau}).

Lückensatz: {frage}
Korrekte Antwort: "{korrekte_option}"

Beantworte VIER Fragen:
1. Ist "{korrekte_option}" grammatisch und semantisch die EINZIG richtige Antwort für diese Lücke?
2. Klingt der vollständige Satz mit "{korrekte_option}" natürlich (wie ein Muttersprachler ihn schreiben würde)?
3. Gibt es keine andere plausible Antwort, die ebenfalls korrekt wäre?
4. Ist die Lücke sinnvoll gesetzt? Prüfe: Steht nach der Lücke kein Wort, das zur korrekten Antwort gehört oder sie verdoppelt (z.B. Lücke=\"der\" gefolgt von \"der\", oder Lücke=\"Eskalation\" gefolgt von \"der\" wäre ok). Die Lücke darf nicht redundant oder sinnlos wirken.

ANTWORTE mit JSON:
{{
  "korrekt": true/false,
  "natuerlich": true/false,
  "luecke_sinnvoll": true/false,
  "richtiger_index": 0,
  "richtige_antwort": "{korrekte_option}",
  "erklaerung": "Kurze Begründung (Grammatikregel nennen)",
  "frage_korrekt": true/false
}}"""

    try:
        response = await client.chat.completions.create(
            model="gpt-4.1",
            messages=[
                {
                    "role": "system",
                    "content": "Du bist ein strenger Grammatikprüfer. Du lehnst Aufgaben ab, die unnatürlich klingen oder mehrdeutig sind.",
                },
                {"role": "user", "content": val_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
            max_tokens=300,
        )
        val = json.loads(response.choices[0].message.content)

        # Frage fehlerhaft, unnatürlich oder Lücke sinnlos → verwerfen
        if not val.get("frage_korrekt", True):
            print(f"[M1] Frage verworfen (fehlerhaft): {frage[:60]}")
            return None
        if not val.get("natuerlich", True):
            print(f"[M1] Frage verworfen (unnatürlich): {frage[:60]}")
            return None
        if not val.get("luecke_sinnvoll", True):
            print(f"[M1] Frage verworfen (Lücke sinnlos/redundant): {frage[:60]}")
            return None

        # Bei Freitext: korrekte Antwort ggf. aus Validierung übernehmen
        if not val.get("korrekt", True):
            richtige_antwort = val.get("richtige_antwort", "").strip()
            if richtige_antwort:
                print(f"[M1] Validierung korrigiert Antwort: '{item.get('korrekte_antwort_text')}' → '{richtige_antwort}'")
                item = dict(item)
                item["korrekte_antwort_text"] = richtige_antwort
                item["optionen"] = [richtige_antwort, "–", "–", "–"]
                item["korrekt"] = 0
                item["erklaerung"] = val.get("erklaerung", item.get("erklaerung", ""))
            else:
                print(f"[M1] Frage verworfen (keine gültige Korrektur): {frage[:60]}")
                return None
        else:
            if val.get("erklaerung"):
                item = dict(item)
                item["erklaerung"] = val["erklaerung"]

        return item

    except Exception as e:
        print(f"[M1] Validierungs-Aufruf fehlgeschlagen: {e}")
        return item  # Bei Fehler: Item trotzdem zurückgeben


def _mische_optionen(item: dict) -> dict:
    """Bei Freitext-Aufgaben: kein Mischen nötig, korrekte Antwort bleibt bei Index 0."""
    item = dict(item)
    # Sicherstellen dass korrekte_antwort_text gesetzt ist
    if not item.get("korrekte_antwort_text") and item.get("optionen"):
        item["korrekte_antwort_text"] = item["optionen"][0]
    item["korrekt"] = 0
    return item


def _fallback_frage(niveau: str, item_id: int) -> dict:
    """Notfall-Fallback – handverlesene, geprüfte Fragen pro Niveau."""
    # Mehrere Fallbacks pro Niveau für etwas Variation
    fallbacks = {
        "A1": [
            {"frage": "Ich _____ aus Deutschland.", "optionen": ["komme", "kommst", "kommt", "kommen"], "korrekt": 0, "erklaerung": "1. Person Singular Präsens: ich komme."},
            {"frage": "Das _____ mein Bruder.", "optionen": ["ist", "bin", "bist", "sind"], "korrekt": 0, "erklaerung": "3. Person Singular von 'sein': ist."},
        ],
        "A2": [
            {"frage": "Gestern _____ ich ins Kino gegangen.", "optionen": ["bin", "habe", "war", "wurde"], "korrekt": 0, "erklaerung": "Perfekt mit 'sein' bei Bewegungsverben (gehen)."},
            {"frage": "Kannst du _____ helfen?", "optionen": ["mir", "mich", "mein", "ich"], "korrekt": 0, "erklaerung": "Nach 'helfen' steht der Dativ: mir."},
        ],
        "B1": [
            {"frage": "Er kommt nicht zur Party, _____ er krank ist.", "optionen": ["weil", "dass", "ob", "wenn"], "korrekt": 0, "erklaerung": "'weil' leitet einen Kausalsatz ein; das Verb steht am Ende."},
            {"frage": "Sie hat versprochen, _____ sie pünktlich kommt.", "optionen": ["dass", "ob", "weil", "wenn"], "korrekt": 0, "erklaerung": "Nach 'versprechen' folgt ein dass-Satz."},
        ],
        "B2": [
            {"frage": "Wenn ich mehr Zeit _____, würde ich öfter reisen.", "optionen": ["hätte", "habe", "hatte", "haben"], "korrekt": 0, "erklaerung": "Konjunktiv II im Konditionalsatz: hätte."},
            {"frage": "Das Paket _____ gestern geliefert.", "optionen": ["wurde", "hat", "ist", "wird"], "korrekt": 0, "erklaerung": "Passiv Präteritum: wurde + Partizip II."},
        ],
        "C1": [
            {"frage": "_____ seiner langjährigen Erfahrung wurde er für die Stelle ausgewählt.", "optionen": ["Aufgrund", "Wegen", "Durch", "Infolge"], "korrekt": 0, "erklaerung": "'aufgrund' + Genitiv drückt einen Grund aus; hier am natürlichsten."},
            {"frage": "Das Projekt scheiterte, _____ alle Beteiligten ihr Bestes gegeben hatten.", "optionen": ["obwohl", "weil", "sodass", "damit"], "korrekt": 0, "erklaerung": "'obwohl' leitet einen konzessiven Nebensatz ein."},
        ],
        "C2": [
            {"frage": "Er bestand darauf, die Angelegenheit _____ zu regeln.", "optionen": ["selbst", "selber", "allein", "eigenständig"], "korrekt": 0, "erklaerung": "Im formellen Schriftdeutsch ist 'selbst' die stilistisch präziseste Wahl."},
        ],
    }
    pool = fallbacks.get(niveau, fallbacks["B1"])
    base = random.choice(pool).copy()
    base["id"] = item_id
    base["niveau"] = niveau
    base["thema"] = "Grammatik"
    return _mische_optionen(base)


def _analysiere_staerken(details: list[dict]) -> list[str]:
    """Leitet Stärken aus den korrekten Antworten ab."""
    korrekte_niveaus = [d["niveau"] for d in details if d["ist_korrekt"]]
    staerken = []
    if "C1" in korrekte_niveaus or "C2" in korrekte_niveaus:
        staerken.append("Beherrschung anspruchsvoller C1/C2-Strukturen")
    if "B2" in korrekte_niveaus:
        staerken.append("Sichere Anwendung komplexer B2-Grammatik")
    if korrekte_niveaus.count("B1") >= 2:
        staerken.append("Solide Grundgrammatik auf B1-Niveau")
    if korrekte_niveaus.count("A2") >= 1:
        staerken.append("Sichere Basis in elementaren Strukturen")
    return staerken or ["Grundkenntnisse vorhanden"]


def _analysiere_schwaechen(details: list[dict]) -> list[str]:
    """Leitet Schwächen aus den falschen Antworten ab."""
    falsche_niveaus = [d["niveau"] for d in details if not d["ist_korrekt"]]
    schwaechen = []
    if "A2" in falsche_niveaus:
        schwaechen.append("Grundlegende Grammatikstrukturen (A2) noch unsicher")
    if "B1" in falsche_niveaus:
        schwaechen.append("Mittelstufen-Grammatik (B1) ausbaufähig")
    if "B2" in falsche_niveaus:
        schwaechen.append("Komplexe Strukturen (B2) noch nicht gefestigt")
    if "C1" in falsche_niveaus:
        schwaechen.append("Anspruchsvolle C1-Konstruktionen noch unsicher")
    return schwaechen or ["Einzelne Lücken in spezifischen Strukturen"]
