"""
Zentraler OpenAI-Service für alle KI-Analysen der Plattform.
Unterstützt: Whisper-Transkription, GPT-4.1 Textanalyse,
GPT-4o Audio-Analyse, GPT-4o Vision (Handschrift), OpenAI TTS.
"""
import base64
import json
import os
import re
import subprocess
import tempfile
from typing import Optional

from openai import AsyncOpenAI

from app.config import settings

client = AsyncOpenAI(api_key=settings.openai_api_key)


# ── Transkription ────────────────────────────────────────────────────────────

async def transkribiere_audio(audio_pfad: str, sprache: str = "de") -> dict:
    """Transkribiert eine Audiodatei mit Whisper gpt-4o-transcribe."""
    with open(audio_pfad, "rb") as f:
        result = await client.audio.transcriptions.create(
            model="gpt-4o-transcribe",
            file=f,
            language=sprache,
            response_format="json",
        )
    return {
        "text": result.text,
        "sprache": getattr(result, "language", sprache),
        "dauer": None,
    }


# ── Text-to-Speech ───────────────────────────────────────────────────────────

async def generiere_audio(text: str, stimme: str = "alloy", geschwindigkeit: float = 1.0) -> bytes:
    """Generiert Audio aus Text mit OpenAI TTS (für Hörverstehen-Modul)."""
    response = await client.audio.speech.create(
        model="tts-1",
        voice=stimme,
        input=text,
        speed=geschwindigkeit,
    )
    return response.content


# ── Sprechen-Analyse (M5) ────────────────────────────────────────────────────

async def analysiere_sprechen(
    audio_pfad: str,
    transkript: str,
    thema: str,
    niveau: str = "B1",
    modus: str = "tief",  # "schnell" oder "tief"
) -> dict:
    """
    Vollständige Sprechanalyse mit GPT-4.1 (Text) und optional GPT-4o Audio.
    """
    # Textbasierte Analyse (immer)
    text_analyse = await _analysiere_text_sprechen(transkript, thema, niveau)

    # Audio-Analyse nur im Tiefenmodus
    audio_analyse = None
    if modus == "tief":
        audio_analyse = await _analysiere_audio_sprechen(audio_pfad, thema, niveau)

    return {
        "transkript": transkript,
        "text_analyse": text_analyse,
        "audio_analyse": audio_analyse,
        "modus": modus,
    }


async def _analysiere_text_sprechen(transkript: str, thema: str, niveau: str) -> dict:
    """GPT-4.1 analysiert den transkribierten Text auf 12 Sprach-Skills."""
    prompt = f"""Du bist ein erfahrener DaF-Prüfer. Analysiere den folgenden deutschen Sprachbeitrag eines Lernenden.

Thema: {thema}
Erwartetes Niveau: {niveau}
Transkript:
\"\"\"
{transkript}
\"\"\"

Bewerte jeden der folgenden Skills auf einer Skala von 0–10 (0=nicht vorhanden, 10=perfekt).
Gib für jeden Score eine kurze Begründung (1–2 Sätze).

Antworte ausschließlich mit diesem JSON:
{{
  "grammatik": {{
    "verbzweitstellung": {{"score": 0, "begruendung": ""}},
    "nebensaetze": {{"score": 0, "begruendung": ""}},
    "kasus": {{"score": 0, "begruendung": ""}},
    "tempus": {{"score": 0, "begruendung": ""}},
    "praepositionen": {{"score": 0, "begruendung": ""}},
    "modalverben": {{"score": 0, "begruendung": ""}}
  }},
  "wortschatz": {{
    "umfang": {{"score": 0, "begruendung": ""}},
    "praezision": {{"score": 0, "begruendung": ""}},
    "register": {{"score": 0, "begruendung": ""}}
  }},
  "satzbau": {{"score": 0, "begruendung": ""}},
  "pragmatik": {{
    "kohaerenz": {{"score": 0, "begruendung": ""}},
    "argumentation": {{"score": 0, "begruendung": ""}}
  }},
  "staerken": ["", ""],
  "schwaechen": ["", ""],
  "empfehlungen": ["", ""],
  "cefr_niveau": "B1",
  "gesamt_score": 0.0,
  "gesamteinschaetzung": ""
}}"""

    response = await client.chat.completions.create(
        model="gpt-4.1",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.3,
    )
    return json.loads(response.choices[0].message.content)


async def _analysiere_audio_sprechen(audio_pfad: str, thema: str, niveau: str) -> Optional[dict]:
    """GPT-4o Audio analysiert die Aufnahme direkt auf Aussprache/Prosodie."""
    try:
        # WebM → WAV konvertieren
        wav_pfad = audio_pfad.replace(".webm", ".wav").replace(".mp4", ".wav")
        if not wav_pfad.endswith(".wav"):
            wav_pfad = audio_pfad + ".wav"

        result = subprocess.run(
            ["ffmpeg", "-y", "-i", audio_pfad, "-ar", "16000", "-ac", "1", wav_pfad],
            capture_output=True, timeout=30
        )
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg Fehler: {result.stderr.decode()}")

        with open(wav_pfad, "rb") as f:
            audio_b64 = base64.b64encode(f.read()).decode()

        # WAV-Datei sofort löschen
        try:
            os.remove(wav_pfad)
        except OSError:
            pass

        prompt = f"""Du bist ein DaF-Aussprache-Experte. Höre dir die folgende Sprachaufnahme an und analysiere die Aussprache.

Thema: {thema}, Erwartetes Niveau: {niveau}

Bewerte auf einer Skala 0–10 und antworte NUR mit JSON (kein Markdown):
{{
  "verstaendlichkeit": {{"score": 0, "begruendung": ""}},
  "fluss_fluency": {{"score": 0, "begruendung": ""}},
  "akzent": {{"score": 0, "begruendung": ""}},
  "intonation": {{"score": 0, "begruendung": ""}},
  "phonetische_fehler": [],
  "gesamt_score": 0.0,
  "zusammenfassung": ""
}}"""

        response = await client.chat.completions.create(
            model="gpt-4o-audio-preview",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "input_audio", "input_audio": {"data": audio_b64, "format": "wav"}},
                ]
            }],
            temperature=0.3,
        )

        content = response.choices[0].message.content
        # JSON aus Text extrahieren
        match = re.search(r'\{.*\}', content, re.DOTALL)
        if match:
            return json.loads(match.group())
        return json.loads(content)

    except Exception as e:
        print(f"[Audio-Aussprache-Analyse fehlgeschlagen]: {e}")
        return None


# ── Vorlesen-Analyse (M4) ────────────────────────────────────────────────────

async def analysiere_vorlesen(audio_pfad: str, vorlesetext: str, niveau: str = "B1") -> dict:
    """Analysiert eine Vorlese-Aufnahme auf Aussprache, Prosodie und Lesegenauigkeit."""
    # Transkript erstellen
    transkript_data = await transkribiere_audio(audio_pfad)
    transkript = transkript_data["text"]

    # Audio-Analyse
    audio_analyse = await _analysiere_audio_vorlesen(audio_pfad, vorlesetext, niveau)

    # Lesegenauigkeit (Textvergleich)
    genauigkeit = _berechne_lesegenauigkeit(vorlesetext, transkript)

    return {
        "vorlesetext": vorlesetext,
        "transkript": transkript,
        "lesegenauigkeit": genauigkeit,
        "audio_analyse": audio_analyse,
    }


async def _analysiere_audio_vorlesen(audio_pfad: str, vorlesetext: str, niveau: str) -> Optional[dict]:
    """GPT-4o Audio analysiert die Vorlese-Aufnahme."""
    try:
        wav_pfad = audio_pfad + ".wav"
        subprocess.run(
            ["ffmpeg", "-y", "-i", audio_pfad, "-ar", "16000", "-ac", "1", wav_pfad],
            capture_output=True, timeout=30, check=True
        )
        with open(wav_pfad, "rb") as f:
            audio_b64 = base64.b64encode(f.read()).decode()
        try:
            os.remove(wav_pfad)
        except OSError:
            pass

        prompt = f"""Du bist ein DaF-Experte für Lautlesekompetenz. Der Teilnehmer sollte folgenden Text vorlesen:

"{vorlesetext}"

Analysiere die Aufnahme und antworte NUR mit JSON:
{{
  "aussprache": {{"score": 0, "begruendung": ""}},
  "prosodie_betonung": {{"score": 0, "begruendung": ""}},
  "rhythmus": {{"score": 0, "begruendung": ""}},
  "fluessigkeit": {{"score": 0, "begruendung": ""}},
  "lesegenauigkeit_hoer": {{"score": 0, "begruendung": ""}},
  "fehlerarten": [],
  "gesamt_score": 0.0,
  "cefr_niveau": "B1",
  "zusammenfassung": ""
}}"""

        response = await client.chat.completions.create(
            model="gpt-4o-audio-preview",
            messages=[{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "input_audio", "input_audio": {"data": audio_b64, "format": "wav"}},
            ]}],
            temperature=0.3,
        )
        content = response.choices[0].message.content
        match = re.search(r'\{.*\}', content, re.DOTALL)
        if match:
            return json.loads(match.group())
        return json.loads(content)
    except Exception as e:
        print(f"[Vorlesen-Audio-Analyse fehlgeschlagen]: {e}")
        return None


def _berechne_lesegenauigkeit(original: str, transkript: str) -> float:
    """Einfache wortbasierte Lesegenauigkeit."""
    orig_woerter = set(original.lower().split())
    trans_woerter = set(transkript.lower().split())
    if not orig_woerter:
        return 0.0
    treffer = len(orig_woerter & trans_woerter)
    return round(treffer / len(orig_woerter) * 100, 1)


## ── Lesen-Analyse (M2) ────────────────────────────────────────────

async def analysiere_lesen(
    lesetext: str,
    fragen: list[dict],
    antworten: dict,
    niveau: str = "B1",
) -> dict:
    """Wertet Leseverstehen-Antworten aus mit GPT-basierter Qualitätsbewertung."""
    korrekt = 0
    details = []
    for frage in fragen:
        fid = str(frage["id"])
        gewaehlt = antworten.get(fid, -1)
        ist_korrekt = gewaehlt == frage.get("korrekt", -99)
        if ist_korrekt:
            korrekt += 1
        korrekte_option = frage.get("optionen", [])
        korrekte_option_text = korrekte_option[frage.get("korrekt", 0)] if korrekte_option else ""
        gewaehlt_text = korrekte_option[gewaehlt] if 0 <= gewaehlt < len(korrekte_option) else "(keine Antwort)"
        details.append({
            "id": frage["id"],
            "frage": frage["frage"],
            "gewaehlt": gewaehlt,
            "gewaehlt_text": gewaehlt_text,
            "korrekt": frage.get("korrekt"),
            "korrekt_text": korrekte_option_text,
            "ist_korrekt": ist_korrekt,
            "erklaerung": frage.get("erklaerung", ""),
        })

    total = len(fragen)
    rohprozent = (korrekt / total * 100) if total > 0 else 0

    # GPT bewertet die Antwortqualität und gibt einen kalibrierten Score
    try:
        details_text = "\n".join(
            f"Frage {d['id']}: '{d['frage']}' → Antwort: '{d['gewaehlt_text']}' ({'korrekt' if d['ist_korrekt'] else 'falsch, richtig wäre: ' + d['korrekt_text']})"
            for d in details
        )
        prompt = f"""Du bist ein DaF-Prüfer. Bewerte das Leseverstehen eines Lernenden auf CEFR-Niveau {niveau}.

{f'Lesetext: {lesetext[:500]}...' if lesetext else ''}

Antworten ({korrekt}/{total} korrekt):
{details_text}

Gib eine faire, pädagogisch kalibrierte Bewertung. Berücksichtige:
- Schwierigkeit des Textes und der Fragen für Niveau {niveau}
- Qualität der falschen Antworten (knappe Fehler vs. komplette Missverständnisse)
- Rohprozent: {rohprozent:.0f}%

Antworte NUR mit JSON:
{
  "gesamt_score": 0.0,
  "cefr_niveau": "B1",
  "gesamteinschaetzung": "",
  "staerken": [],
  "schwaechen": [],
  "empfehlungen": []
}"""
        response = await client.chat.completions.create(
            model="gpt-4.1",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.3,
        )
        gpt_bewertung = json.loads(response.choices[0].message.content)
        score = float(gpt_bewertung.get("gesamt_score", rohprozent))
        cefr = gpt_bewertung.get("cefr_niveau", "B1")
    except Exception as e:
        print(f"[Lesen-GPT-Bewertung fehlgeschlagen]: {e}")
        score = rohprozent
        gpt_bewertung = {}
        if rohprozent >= 80:
            cefr = "B2"
        elif rohprozent >= 55:
            cefr = "B1"
        elif rohprozent >= 35:
            cefr = "A2"
        else:
            cefr = "A1"

    return {
        "score": round(score, 1),
        "korrekt": korrekt,
        "total": total,
        "cefr": cefr,
        "details": details,
        "gesamteinschaetzung": gpt_bewertung.get("gesamteinschaetzung", ""),
        "staerken": gpt_bewertung.get("staerken", []),
        "schwaechen": gpt_bewertung.get("schwaechen", []),
        "empfehlungen": gpt_bewertung.get("empfehlungen", []),
    }


# ── Hörverstehen-Analyse (M3) ──────────────────────────────────────────

async def analysiere_hoerverstehen(
    fragen: list[dict],
    antworten: dict,
    niveau: str = "B1",
) -> dict:
    """Wertet Hörverstehen-Antworten mit GPT-basierter Qualitätsbewertung aus."""
    return await analysiere_lesen(
        lesetext="",
        fragen=fragen,
        antworten=antworten,
        niveau=niveau,
    )


# ── Schreiben-Analyse (M6) ───────────────────────────────────────────────────

async def analysiere_schreiben(
    text: Optional[str] = None,
    bild_pfad: Optional[str] = None,
    aufgabe: str = "",
    niveau: str = "B1",
) -> dict:
    """
    Analysiert einen geschriebenen Text oder ein Handschrift-Foto.
    Bei Handschrift: GPT-4o Vision erkennt und bewertet den Text.
    """
    if bild_pfad and os.path.exists(bild_pfad):
        return await _analysiere_handschrift(bild_pfad, aufgabe, niveau)
    elif text:
        return await _analysiere_schreibtext(text, aufgabe, niveau)
    else:
        raise ValueError("Weder Text noch Bild angegeben.")


async def _analysiere_schreibtext(text: str, aufgabe: str, niveau: str) -> dict:
    """GPT-4.1 analysiert einen getippten Text."""
    prompt = f"""Du bist ein DaF-Schreibexperte. Analysiere den folgenden deutschen Text eines Lernenden.

Aufgabe: {aufgabe}
Erwartetes Niveau: {niveau}
Text:
\"\"\"
{text}
\"\"\"

Antworte ausschließlich mit JSON:
{{
  "grammatik": {{"score": 0, "begruendung": ""}},
  "wortschatz": {{"score": 0, "begruendung": ""}},
  "satzbau": {{"score": 0, "begruendung": ""}},
  "kohaerenz": {{"score": 0, "begruendung": ""}},
  "aufgabenerfullung": {{"score": 0, "begruendung": ""}},
  "rechtschreibung": {{"score": 0, "begruendung": ""}},
  "staerken": [],
  "schwaechen": [],
  "empfehlungen": [],
  "cefr_niveau": "B1",
  "gesamt_score": 0.0,
  "gesamteinschaetzung": ""
}}"""

    response = await client.chat.completions.create(
        model="gpt-4.1",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.3,
    )
    return json.loads(response.choices[0].message.content)


async def _analysiere_handschrift(bild_pfad: str, aufgabe: str, niveau: str) -> dict:
    """GPT-4o Vision liest Handschrift und analysiert den Text."""
    with open(bild_pfad, "rb") as f:
        bild_b64 = base64.b64encode(f.read()).decode()

    # Dateityp ermitteln
    ext = os.path.splitext(bild_pfad)[1].lower()
    mime = {"jpg": "image/jpeg", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".webp": "image/webp"}.get(ext, "image/jpeg")

    prompt = f"""Du bist ein DaF-Schreibexperte. Lies den handgeschriebenen deutschen Text im Bild und analysiere ihn.

Aufgabe: {aufgabe}
Erwartetes Niveau: {niveau}

Schritt 1: Transkribiere den handgeschriebenen Text vollständig.
Schritt 2: Analysiere den transkribierten Text.

Antworte ausschließlich mit JSON:
{{
  "transkript": "vollständiger transkribierter Text",
  "grammatik": {{"score": 0, "begruendung": ""}},
  "wortschatz": {{"score": 0, "begruendung": ""}},
  "satzbau": {{"score": 0, "begruendung": ""}},
  "kohaerenz": {{"score": 0, "begruendung": ""}},
  "aufgabenerfullung": {{"score": 0, "begruendung": ""}},
  "rechtschreibung": {{"score": 0, "begruendung": ""}},
  "lesbarkeit": {{"score": 0, "begruendung": ""}},
  "staerken": [],
  "schwaechen": [],
  "empfehlungen": [],
  "cefr_niveau": "B1",
  "gesamt_score": 0.0,
  "gesamteinschaetzung": ""
}}"""

    response = await client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{bild_b64}", "detail": "high"}},
        ]}],
        response_format={"type": "json_object"},
        temperature=0.3,
    )
    return json.loads(response.choices[0].message.content)


# ── Zusatzfragen-Generator ───────────────────────────────────────────────────

async def generiere_zusatzfragen(analyse: dict, modul: str, niveau: str) -> list[dict]:
    """Generiert gezielte Zusatzfragen basierend auf erkannten Schwächen."""
    schwaechen = analyse.get("schwaechen", [])
    if not schwaechen:
        return []

    prompt = f"""Basierend auf folgenden Schwächen eines DaF-Lernenden auf Niveau {niveau}:
{json.dumps(schwaechen, ensure_ascii=False)}

Generiere 2–3 gezielte Nachfragen, die diese Schwächen genauer diagnostizieren.
Antworte mit JSON-Array:
[{{"frage": "...", "ziel": "...", "hinweis": "..."}}]"""

    try:
        response = await client.chat.completions.create(
            model="gpt-4.1",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.5,
        )
        data = json.loads(response.choices[0].message.content)
        if isinstance(data, list):
            return data
        for key in ("fragen", "zusatzfragen", "items"):
            if key in data:
                return data[key]
    except Exception as e:
        print(f"[Zusatzfragen] Fehler: {e}")
    return []
