"""
M1-Service: Grammatik & Wortschatz – Adaptives CAT-System.

Ablauf:
1. start()        → 3 Einstiegsfragen (A2 / B1 / B2 gemischt)
2. naechste()     → nach jeder Antwort Niveau neu schätzen, nächste Frage generieren
3. werte_aus()    → Endauswertung mit GPT-Analyse

Jede Frage wird frisch von GPT generiert (zufälliges Thema + Grammatikfokus).
Kein Cache, keine Wiederholungen.
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

THEMEN = [
    "Alltag und Familie", "Arbeit und Beruf", "Reisen und Urlaub",
    "Gesundheit und Sport", "Umwelt und Natur", "Technologie und Medien",
    "Essen und Kochen", "Wohnen und Stadt", "Bildung und Schule",
    "Freizeit und Hobbys", "Einkaufen und Konsum", "Kultur und Gesellschaft",
    "Verkehr und Mobilität", "Politik und Gesellschaft", "Wissenschaft und Forschung",
]

GRAMMATIK_FOKUS = [
    "Verbkonjugation und Tempus (Präsens, Perfekt, Präteritum)",
    "Kasus (Nominativ, Akkusativ, Dativ, Genitiv) und Artikel",
    "Präpositionen mit Kasus",
    "Modalverben und Infinitivkonstruktionen",
    "Nebensätze und Konjunktionen (weil, dass, obwohl, wenn...)",
    "Adjektivdeklination und Komparation",
    "Passiv und Konjunktiv II",
    "Wortschatz und Kollokationen",
    "Trennbare und untrennbare Verben",
    "Relativsätze und Relativpronomen",
]

NIVEAU_BESCHREIBUNGEN = {
    "A1": "Sehr einfach: sein/haben, Grundwortschatz, einfachste Sätze",
    "A2": "Einfach: Präsens aller Verben, bestimmte/unbestimmte Artikel, einfache Präpositionen",
    "B1": "Mittel: Perfekt, Modalverben, Nebensätze, alle Kasus, erweiterter Wortschatz",
    "B2": "Fortgeschritten: Konjunktiv II, Passiv, komplexe Satzstrukturen, idiomatischer Ausdruck",
    "C1": "Sehr fortgeschritten: stilistische Feinheiten, seltene Konstruktionen, Fachvokabular",
    "C2": "Mastery: Nuancen, literarische Sprache, höchste sprachliche Präzision",
}

# Einstiegs-Niveaus: breite Streuung für schnelle Einschätzung
EINSTIEGS_NIVEAUS = ["A2", "B1", "B2"]


# ── Niveau-Schätzung (IRT-vereinfacht) ───────────────────────────────────────

def schaetze_niveau(antworten_verlauf: list[dict]) -> str:
    """
    Schätzt das aktuelle Niveau basierend auf dem bisherigen Antwortverlauf.
    antworten_verlauf: [{"niveau": "B1", "korrekt": True}, ...]
    
    Algorithmus: Gewichteter Durchschnitt der Niveau-Indizes,
    richtige Antworten ziehen nach oben, falsche nach unten.
    """
    if not antworten_verlauf:
        return "B1"

    niveau_punkte = 0.0
    gewicht_gesamt = 0.0

    for i, eintrag in enumerate(antworten_verlauf):
        # Neuere Antworten stärker gewichten
        gewicht = 1.0 + i * 0.3
        niveau_idx = NIVEAUS.index(eintrag["niveau"])
        
        if eintrag["korrekt"]:
            # Richtig → Niveau-Index + 1 (tendiert nach oben)
            ziel_idx = min(niveau_idx + 1, len(NIVEAUS) - 1)
        else:
            # Falsch → Niveau-Index - 1 (tendiert nach unten)
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
    Generiert genau eine MC-Frage für das angegebene Niveau.
    Zufälliges Thema + Grammatikfokus für maximale Variation.
    """
    thema = random.choice(THEMEN)
    fokus = random.choice(GRAMMATIK_FOKUS)

    hilfs_hinweis = ""
    if hilfssprache != "de":
        sprach_namen = {
            "en": "English", "tr": "Türkçe", "ar": "العربية",
            "uk": "Українська", "ru": "Русский", "fr": "Français",
            "it": "Italiano", "es": "Español"
        }
        sprach_name = sprach_namen.get(hilfssprache, hilfssprache)
        hilfs_hinweis = f'\nFüge ein Feld "hinweis_{hilfssprache}" mit einer kurzen Aufgabenerklärung auf {sprach_name} hinzu.'

    vermeidungs_hinweis = ""
    if bereits_verwendet:
        beispiele = ", ".join(bereits_verwendet[-5:])
        vermeidungs_hinweis = f'\nVermeide Fragen die diesen ähneln: {beispiele}'

    prompt = f"""Du bist ein DaF-Experte und Prüfungsautor. Erstelle genau EINE Multiple-Choice-Frage für CEFR-Niveau {niveau}.

Thema: {thema}
Grammatischer Schwerpunkt: {fokus}
Niveau {niveau}: {NIVEAU_BESCHREIBUNGEN[niveau]}
{vermeidungs_hinweis}
{hilfs_hinweis}

Strenge Anforderungen:
- Authentischer, natürlicher Satz zum Thema "{thema}"
- Genau 4 Antwortoptionen, genau EINE davon ist grammatisch und inhaltlich korrekt
- Die 3 Distraktoren sind FALSCH aber plausibel (typische Lernerfehler auf diesem Niveau)
- WICHTIG: Prüfe vor der Ausgabe nochmals: Ist optionen[korrekt] wirklich die einzig richtige Antwort?
- Stelle sicher dass der Satz mit der richtigen Option grammatisch und semantisch einwandfrei ist
- Kurze, präzise grammatische Erklärung warum die richtige Antwort korrekt ist

Antworte ausschließlich mit diesem JSON-Objekt:
{{
  "id": {item_id},
  "frage": "Lückensatz mit _____ als Lücke oder konkrete Frage auf Deutsch",
  "optionen": ["Option A", "Option B", "Option C", "Option D"],
  "korrekt": 0,
  "korrekte_antwort_text": "Die korrekte Option als Text (zur Selbstprüfung)",
  "erklaerung": "Grammatische Erklärung warum diese Antwort korrekt ist",
  "niveau": "{niveau}",
  "thema": "{thema}"
}}

korrekt ist der 0-basierte Index der richtigen Antwort in optionen[].
Beispiel: Wenn optionen[2] korrekt ist, dann korrekt=2 und korrekte_antwort_text=optionen[2]."""

    for versuch in range(3):  # Bis zu 3 Versuche
        try:
            response = await client.chat.completions.create(
                model="gpt-4.1",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0.95 if versuch == 0 else 0.7,
                max_tokens=500,
            )
            data = json.loads(response.choices[0].message.content)

            if not all(k in data for k in ("frage", "optionen", "korrekt")):
                continue

            data["id"] = item_id
            data["niveau"] = niveau
            optionen = data.get("optionen", [])
            korrekt_idx = data.get("korrekt", 0)
            korrekte_antwort_text = data.get("korrekte_antwort_text", "")

            # Stufe 1: Index-Text-Konsistenz prüfen
            if korrekte_antwort_text and 0 <= korrekt_idx < len(optionen):
                if optionen[korrekt_idx].strip() != korrekte_antwort_text.strip():
                    try:
                        echter_idx = next(
                            i for i, opt in enumerate(optionen)
                            if opt.strip() == korrekte_antwort_text.strip()
                        )
                        data["korrekt"] = echter_idx
                        korrekt_idx = echter_idx
                        print(f"[M1] Index-Korrektur: korrekt→{echter_idx} ('{korrekte_antwort_text}')")
                    except StopIteration:
                        pass

            # Stufe 2: Unabhängige grammatische Validierung durch zweiten GPT-Aufruf
            validiert = await _validiere_frage(data)
            if validiert is None:
                # Validierung fehlgeschlagen → nächster Versuch
                print(f"[M1] Validierung fehlgeschlagen (Versuch {versuch+1}), neu generieren...")
                continue

            return _mische_optionen(validiert)

        except Exception as e:
            print(f"[M1] Fragen-Generierung Versuch {versuch+1} fehlgeschlagen: {e}")

    # Fallback: eine zufällige Frage aus dem Fallback-Pool
    return _fallback_frage(niveau, item_id)


async def generiere_einstiegsfragen(hilfssprache: str = "de") -> list[dict]:
    """
    Generiert 3 Einstiegsfragen mit breiter Niveau-Streuung (A2, B1, B2).
    Reihenfolge wird gemischt damit das Niveau nicht vorhersehbar ist.
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
    """
    Startet den adaptiven Test.
    Gibt 3 Einstiegsfragen zurück + initialen Zustand.
    """
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
    """
    Verarbeitet eine Antwort und generiert die nächste adaptive Frage.
    
    zustand: {antworten_verlauf, naechste_id, bereits_gefragt_fragen}
    item_id: ID der beantworteten Frage
    gewaehlt: Index der gewählten Option
    """
    antworten_verlauf = zustand.get("antworten_verlauf", [])
    naechste_id = zustand.get("naechste_id", 4)
    bereits_gefragt = zustand.get("bereits_gefragt_fragen", [])
    alle_fragen = zustand.get("alle_fragen", [])

    # Aktuelle Frage aus dem Verlauf finden
    aktuelle_frage = next(
        (f for f in alle_fragen if f["id"] == item_id), None
    )
    
    korrekt = False
    fragen_niveau = "B1"
    if aktuelle_frage:
        korrekt = gewaehlt == aktuelle_frage.get("korrekt", -1)
        fragen_niveau = aktuelle_frage.get("niveau", "B1")

    # Antwortverlauf aktualisieren
    antworten_verlauf.append({
        "item_id": item_id,
        "niveau": fragen_niveau,
        "korrekt": korrekt,
        "gewaehlt": gewaehlt,
    })

    # Neues Niveau schätzen
    neues_niveau = schaetze_niveau(antworten_verlauf)

    # Bereits verwendete Fragen-Texte für Vermeidung
    bereits_verwendet = [f.get("frage", "") for f in alle_fragen]

    # Nächste Frage generieren
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
    """
    Endauswertung: berechnet Score, CEFR und detaillierte Analyse.
    antworten: {"1": 2, "2": 0, ...} (item_id → gewählter Index)
    """
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

        details.append({
            "id": frage["id"],
            "frage": frage["frage"],
            "optionen": frage.get("optionen", []),
            "gewaehlt": gewaehlt,
            "korrekt": frage.get("korrekt"),
            "ist_korrekt": ist_korrekt,
            "erklaerung": frage.get("erklaerung", ""),
            "niveau": frage.get("niveau", "B1"),
            "thema": frage.get("thema", ""),
        })

    prozent = (korrekt_count / total * 100) if total > 0 else 0
    score = round(prozent, 1)

    # CEFR aus adaptivem Verlauf + Prozent kombinieren
    adaptives_niveau = schaetze_niveau(antworten_verlauf)
    
    # Prozent-basiertes CEFR als Kontrolle
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

    # Kombiniertes CEFR: Durchschnitt aus adaptivem und prozentbasiertem Niveau
    adaptiv_idx = NIVEAUS.index(adaptives_niveau)
    prozent_idx = NIVEAUS.index(prozent_cefr)
    final_idx = round((adaptiv_idx + prozent_idx) / 2)
    final_cefr = NIVEAUS[final_idx]

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
    Zweiter unabhängiger GPT-Aufruf zur grammatischen Validierung.
    Gibt das korrigierte Item zurück, oder None wenn die Frage unbrauchbar ist.
    """
    frage = item.get("frage", "")
    optionen = item.get("optionen", [])
    korrekt_idx = item.get("korrekt", 0)
    niveau = item.get("niveau", "B1")

    if not frage or not optionen or korrekt_idx >= len(optionen):
        return None

    optionen_text = "\n".join(f"{i}. {opt}" for i, opt in enumerate(optionen))
    korrekte_option = optionen[korrekt_idx]

    val_prompt = f"""Du bist ein Deutschlehrer und Grammatikexperte. Prüfe diese DaF-Aufgabe auf Korrektheit.

Frage: {frage}
Optionen:
{optionen_text}
Als korrekt markiert: {korrekt_idx}. "{korrekte_option}"
Niveau: {niveau}

Prüfe:
1. Ist "{korrekte_option}" grammatisch und semantisch die EINZIG richtige Antwort?
2. Sind die anderen Optionen wirklich falsch?
3. Ist die Frage selbst grammatisch korrekt?

Antworte mit JSON:
{{
  "korrekt": true/false,  // true wenn die markierte Antwort stimmt
  "richtiger_index": {korrekt_idx},  // korrekter Index (kann abweichen wenn falsch markiert)
  "richtige_antwort": "{korrekte_option}",  // die tatsächlich korrekte Option
  "erklaerung": "Kurze Begründung",
  "frage_korrekt": true/false  // false wenn die Frage selbst fehlerhaft ist
}}"""

    try:
        response = await client.chat.completions.create(
            model="gpt-4.1",
            messages=[{"role": "user", "content": val_prompt}],
            response_format={"type": "json_object"},
            temperature=0.1,  # Niedrig für konsistente Validierung
            max_tokens=300,
        )
        val = json.loads(response.choices[0].message.content)

        # Frage selbst fehlerhaft → verwerfen
        if not val.get("frage_korrekt", True):
            print(f"[M1] Frage verworfen (fehlerhaft): {frage[:60]}")
            return None

        # Falscher Index → korrigieren
        if not val.get("korrekt", True):
            richtiger_idx = val.get("richtiger_index", korrekt_idx)
            richtige_antwort = val.get("richtige_antwort", "")
            # Index aus Text ableiten falls möglich
            if richtige_antwort:
                try:
                    richtiger_idx = next(
                        i for i, opt in enumerate(optionen)
                        if opt.strip() == richtige_antwort.strip()
                    )
                except StopIteration:
                    pass
            if 0 <= richtiger_idx < len(optionen):
                print(f"[M1] Validierung korrigiert: {korrekt_idx}→{richtiger_idx} ('{optionen[richtiger_idx]}')")
                item = dict(item)
                item["korrekt"] = richtiger_idx
                item["erklaerung"] = val.get("erklaerung", item.get("erklaerung", ""))
            else:
                # Kein gültiger Index gefunden → verwerfen
                print(f"[M1] Frage verworfen (kein gültiger korrekt-Index): {frage[:60]}")
                return None
        else:
            # Validierung bestätigt – Erklärung ggf. verbessern
            if val.get("erklaerung"):
                item = dict(item)
                item["erklaerung"] = val["erklaerung"]

        return item

    except Exception as e:
        print(f"[M1] Validierungs-Aufruf fehlgeschlagen: {e}")
        # Bei Validierungsfehler: Item trotzdem zurückgeben (besser als Fallback)
        return item


def _mische_optionen(item: dict) -> dict:
    """Mischt die Antwortoptionen zufällig und passt den korrekt-Index an."""
    item = dict(item)
    optionen = list(item.get("optionen", []))
    korrekt_idx = item.get("korrekt", 0)
    if 0 <= korrekt_idx < len(optionen):
        korrekte_antwort = optionen[korrekt_idx]
        indizes = list(range(len(optionen)))
        random.shuffle(indizes)
        item["optionen"] = [optionen[i] for i in indizes]
        item["korrekt"] = item["optionen"].index(korrekte_antwort)
    return item


def _fallback_frage(niveau: str, item_id: int) -> dict:
    """Notfall-Fallback falls GPT komplett ausfällt."""
    fallbacks = {
        "A1": {"frage": "Ich ___ Peter.", "optionen": ["heiße", "heißt", "heißen", "heiß"], "korrekt": 0, "erklaerung": "1. Person Singular: ich heiße"},
        "A2": {"frage": "Gestern ___ ich ins Kino gegangen.", "optionen": ["bin", "habe", "war", "wurde"], "korrekt": 0, "erklaerung": "Perfekt mit 'sein' bei Bewegungsverben"},
        "B1": {"frage": "Er sagte, ___ er morgen komme.", "optionen": ["dass", "das", "ob", "weil"], "korrekt": 0, "erklaerung": "Indirekter Satz mit 'dass'"},
        "B2": {"frage": "Wenn ich mehr Zeit ___, würde ich reisen.", "optionen": ["hätte", "habe", "hatte", "haben"], "korrekt": 0, "erklaerung": "Konjunktiv II in Konditionalsätzen"},
        "C1": {"frage": "Die Entscheidung, ___ er sich widersetzt hatte, wurde revidiert.", "optionen": ["der", "die", "das", "dem"], "korrekt": 0, "erklaerung": "Relativpronomen im Dativ nach 'widersetzen'"},
        "C2": {"frage": "___ er auch noch so fleißig lernte, reichte es nicht.", "optionen": ["Mochte", "Möge", "Mag", "Möchte"], "korrekt": 0, "erklaerung": "Konzessiver Konjunktiv: 'mochte... auch'"},
    }
    base = fallbacks.get(niveau, fallbacks["B1"]).copy()
    base["id"] = item_id
    base["niveau"] = niveau
    base["thema"] = "Grammatik"
    return _mische_optionen(base)


def _analysiere_staerken(details: list[dict]) -> list[str]:
    """Leitet Stärken aus den korrekten Antworten ab."""
    korrekte_niveaus = [d["niveau"] for d in details if d["ist_korrekt"]]
    staerken = []
    if "B2" in korrekte_niveaus or "C1" in korrekte_niveaus:
        staerken.append("Beherrschung komplexer Grammatikstrukturen")
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
    return schwaechen or ["Einzelne Lücken in spezifischen Strukturen"]
