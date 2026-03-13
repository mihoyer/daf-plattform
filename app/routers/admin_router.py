"""Admin-Router: Passwortgeschütztes Dashboard für Lehrkräfte."""
import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models.database import TestSession, ModulErgebnis, ModulStatus, SessionStatus, get_db
from app.services.session_service import lade_session

router = APIRouter(prefix="/api/admin")


def _prüfe_admin(request: Request):
    token = request.cookies.get("admin_token") or request.headers.get("X-Admin-Token")
    if token != settings.admin_token:
        raise HTTPException(status_code=401, detail="Nicht autorisiert.")


@router.post("/login")
async def admin_login(request: Request):
    body = await request.json()
    passwort = body.get("passwort", "")
    if passwort != settings.admin_password:
        raise HTTPException(status_code=401, detail="Falsches Passwort.")
    import secrets as sec
    token = sec.token_hex(32)
    # Token in Settings temporär speichern (restartet sich bei Neustart)
    settings.admin_token = token
    response = JSONResponse({"status": "ok"})
    response.set_cookie("admin_token", token, httponly=True, samesite="strict", max_age=3600 * 8)
    return response


@router.post("/logout")
async def admin_logout():
    response = JSONResponse({"status": "ok"})
    response.delete_cookie("admin_token")
    return response


@router.get("/sessions")
async def liste_sessions(
    request: Request,
    seite: int = 1,
    limit: int = 20,
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    _prüfe_admin(request)
    offset = (seite - 1) * limit

    query = select(TestSession).options(selectinload(TestSession.module)).order_by(desc(TestSession.erstellt_am))
    if status:
        query = query.where(TestSession.status == status)

    result = await db.execute(query.offset(offset).limit(limit))
    sessions = result.scalars().all()

    count_result = await db.execute(select(func.count(TestSession.id)))
    total = count_result.scalar()

    return {
        "sessions": [_session_summary(s) for s in sessions],
        "total": total,
        "seite": seite,
        "seiten": (total + limit - 1) // limit,
    }


@router.get("/session/{token}")
async def session_detail(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    _prüfe_admin(request)
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
            "schwierigkeitsgrad": m.schwierigkeitsgrad,
        })

    return {
        **_session_summary(sess),
        "module": module_data,
    }


@router.get("/statistik")
async def statistik(request: Request, db: AsyncSession = Depends(get_db)):
    _prüfe_admin(request)

    total_result = await db.execute(select(func.count(TestSession.id)))
    total = total_result.scalar()

    abgeschlossen_result = await db.execute(
        select(func.count(TestSession.id)).where(TestSession.status == SessionStatus.abgeschlossen)
    )
    abgeschlossen = abgeschlossen_result.scalar()

    avg_result = await db.execute(
        select(func.avg(TestSession.gesamt_score)).where(TestSession.gesamt_score.isnot(None))
    )
    avg_score = avg_result.scalar()

    return {
        "gesamt_sessions": total,
        "abgeschlossene_sessions": abgeschlossen,
        "durchschnitt_score": round(float(avg_score), 1) if avg_score else None,
    }


@router.delete("/session/{token}")
async def loesche_session(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    _prüfe_admin(request)
    sess = await lade_session(db, token)
    if not sess:
        raise HTTPException(status_code=404, detail="Session nicht gefunden.")
    await db.delete(sess)
    await db.commit()
    return {"status": "gelöscht"}


def _session_summary(sess: TestSession) -> dict:
    return {
        "token": sess.token,
        "paket": sess.paket.value,
        "status": sess.status.value,
        "zahlungs_status": sess.zahlungs_status.value,
        "gesamt_score": sess.gesamt_score,
        "gesamt_niveau": sess.gesamt_niveau.value if sess.gesamt_niveau else None,
        "grob_niveau": sess.grob_niveau.value if sess.grob_niveau else None,
        "hilfssprache": sess.hilfssprache.value,
        "erstellt_am": sess.erstellt_am.isoformat(),
        "abgeschlossen_am": sess.abgeschlossen_am.isoformat() if sess.abgeschlossen_am else None,
        "modul_count": len(sess.module),
        "abgeschlossene_module": sum(1 for m in sess.module if m.status == ModulStatus.abgeschlossen),
    }
