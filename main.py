"""
DaF Sprachdiagnostik-Plattform – Hauptanwendung
FastAPI + PostgreSQL + Stripe + OpenAI
"""
import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, update

from app.config import settings
from app.models.database import AsyncSessionLocal, SessionStatus, TestSession, init_db
from app.routers.main_router import router as main_router
from app.routers.admin_router import router as admin_router
from app.routers.export_router import router as export_router
from app.routers.kandidaten_router import router as kandidaten_router


async def _timeout_worker():
    """Markiert alle laufenden Sessions als 'abgelaufen', wenn laeuft_ab_am überschritten."""
    while True:
        try:
            async with AsyncSessionLocal() as db:
                now = datetime.now(timezone.utc)
                stmt = (
                    update(TestSession)
                    .where(
                        TestSession.status == SessionStatus.laufend,
                        TestSession.laeuft_ab_am != None,
                        TestSession.laeuft_ab_am < now,
                    )
                    .values(status=SessionStatus.abgelaufen)
                )
                result = await db.execute(stmt)
                if result.rowcount:
                    print(f"[Timeout] {result.rowcount} Session(s) als abgelaufen markiert.")
                await db.commit()
        except Exception as e:
            print(f"[Timeout] Fehler: {e}")
        await asyncio.sleep(900)  # alle 15 Minuten prüfen


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    task = asyncio.create_task(_timeout_worker())
    yield
    task.cancel()


app = FastAPI(
    title="DaF Sprachdiagnostik-Plattform",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/api/docs" if settings.debug else None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files & Templates
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# API-Router
app.include_router(main_router)
app.include_router(admin_router)
app.include_router(export_router)
app.include_router(kandidaten_router)


# ── Frontend-Routen ──────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "settings": settings})


@app.get("/paket", response_class=HTMLResponse)
async def paket(request: Request):
    # Testgruppen-Modus: Direktzugang gesperrt, nur via QR-Code (/k/{code})
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/", status_code=302)


@app.get("/zahlung", response_class=HTMLResponse)
async def zahlung(request: Request):
    return templates.TemplateResponse("zahlung.html", {"request": request, "settings": settings})


@app.get("/test/{token}", response_class=HTMLResponse)
async def test_start(request: Request, token: str):
    return templates.TemplateResponse("test.html", {"request": request, "token": token, "settings": settings})


@app.get("/m1/{token}", response_class=HTMLResponse)
async def m1_seite(request: Request, token: str):
    return templates.TemplateResponse("m1_grammatik.html", {"request": request, "token": token})


@app.get("/m2/{token}", response_class=HTMLResponse)
async def m2_seite(request: Request, token: str):
    return templates.TemplateResponse("m2_lesen.html", {"request": request, "token": token})


@app.get("/m3/{token}", response_class=HTMLResponse)
async def m3_seite(request: Request, token: str):
    return templates.TemplateResponse("m3_hoerverstehen.html", {"request": request, "token": token})


@app.get("/m4/{token}", response_class=HTMLResponse)
async def m4_seite(request: Request, token: str):
    return templates.TemplateResponse("m4_vorlesen.html", {"request": request, "token": token})


@app.get("/m5/{token}", response_class=HTMLResponse)
async def m5_seite(request: Request, token: str):
    return templates.TemplateResponse("m5_sprechen.html", {"request": request, "token": token})


@app.get("/m6/{token}", response_class=HTMLResponse)
async def m6_seite(request: Request, token: str):
    return templates.TemplateResponse("m6_schreiben.html", {"request": request, "token": token})


@app.get("/ergebnis/{token}", response_class=HTMLResponse)
async def ergebnis_seite(request: Request, token: str):
    return templates.TemplateResponse("ergebnis.html", {"request": request, "token": token})


@app.get("/dsgvo", response_class=HTMLResponse)
async def dsgvo(request: Request):
    from datetime import datetime
    datum = datetime.now().strftime("%d.%m.%Y")
    return templates.TemplateResponse("dsgvo.html", {"request": request, "datum": datum})


@app.get("/admin", response_class=HTMLResponse)
async def admin_redirect(request: Request):
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/admin/login")


@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login(request: Request):
    return templates.TemplateResponse("admin_login.html", {"request": request})


@app.get("/admin/dashboard", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    return templates.TemplateResponse("admin_dashboard.html", {"request": request})


@app.get("/admin/testgruppe", response_class=HTMLResponse)
async def testgruppe_dashboard(request: Request):
    return templates.TemplateResponse("testgruppe_dashboard.html", {"request": request})


@app.get("/admin/session/{token}", response_class=HTMLResponse)
async def admin_session_detail(request: Request, token: str):
    return templates.TemplateResponse("admin_session_detail.html", {"request": request, "token": token})
