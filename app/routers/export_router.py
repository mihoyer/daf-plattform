"""Export-Router: PDF und JSON-Export der Einstufungsberichte."""
import json
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_db, ModulStatus
from app.services.session_service import lade_session
from app.services.pdf_service import erstelle_pdf

router = APIRouter(prefix="/api/export")


@router.get("/{token}/pdf")
async def export_pdf(token: str, db: AsyncSession = Depends(get_db)):
    sess = await lade_session(db, token)
    if not sess:
        raise HTTPException(status_code=404, detail="Session nicht gefunden.")

    pdf_bytes = await erstelle_pdf(sess)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=einstufungsbericht_{token[:8]}.pdf"},
    )


@router.get("/{token}/json")
async def export_json(token: str, db: AsyncSession = Depends(get_db)):
    sess = await lade_session(db, token)
    if not sess:
        raise HTTPException(status_code=404, detail="Session nicht gefunden.")

    module_data = []
    for m in sorted(sess.module, key=lambda x: x.reihenfolge):
        module_data.append({
            "modul": m.modul.value,
            "status": m.status.value,
            "cefr": m.cefr_niveau.value if m.cefr_niveau else None,
            "score": m.gesamt_score,
            "analyse": m.get_ki_analyse(),
        })

    bericht = {
        "meta": {
            "export_version": "2.0",
            "plattform": "DaF Sprachdiagnostik",
            "dsgvo_hinweis": "Dieser Bericht enthält keine personenbezogenen Daten.",
        },
        "session": {
            "token_kurz": token[:8] + "...",
            "paket": sess.paket.value,
            "hilfssprache": sess.hilfssprache.value,
            "erstellt_am": sess.erstellt_am.isoformat(),
            "abgeschlossen_am": sess.abgeschlossen_am.isoformat() if sess.abgeschlossen_am else None,
        },
        "ergebnis": {
            "gesamt_score": sess.gesamt_score,
            "gesamt_niveau": sess.gesamt_niveau.value if sess.gesamt_niveau else None,
            "grob_niveau": sess.grob_niveau.value if sess.grob_niveau else None,
        },
        "module": module_data,
    }

    return Response(
        content=json.dumps(bericht, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename=einstufungsbericht_{token[:8]}.json"},
    )
