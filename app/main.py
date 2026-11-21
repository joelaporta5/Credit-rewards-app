"""FastAPI application entry point: `uvicorn app.main:app`."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.logging_setup import setup_logging
from app.routers import pages, plaid_routes, public
from app.security import LoginRequired

log = logging.getLogger("app")
LINK_PAGES = {"/connect", "/link/oauth-return"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    s = get_settings()
    problems = s.problems()
    if problems:
        for p in problems:
            log.error("CONFIG: %s", p)
        if s.app_env == "production":
            raise RuntimeError("Refusing to start with insecure configuration: " + "; ".join(problems))
    tasks = []
    if s.scheduler_enabled and s.app_env != "test":
        from app import scheduler

        tasks = scheduler.start()
    log.info("Rewards Autopilot started (env=%s, plaid=%s)", s.app_env, s.plaid_env)
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="Rewards Autopilot", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
app.include_router(public.router)
app.include_router(plaid_routes.router)
app.include_router(pages.router)


@app.exception_handler(LoginRequired)
async def _login_redirect(request: Request, exc: LoginRequired):
    nxt = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/login?next={quote(nxt)}", status_code=303)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("Cache-Control", "no-store")
    if request.url.path in LINK_PAGES:
        # Directives from Plaid's Link docs ("CSP directives"), only on the pages that open Link.
        csp = ("default-src 'self' https://cdn.plaid.com/; "
               "script-src 'self' 'unsafe-inline' https://cdn.plaid.com/link/v2/stable/link-initialize.js; "
               "style-src 'self' 'unsafe-inline'; frame-src https://cdn.plaid.com/; "
               "connect-src 'self' https://production.plaid.com/ https://sandbox.plaid.com/; img-src 'self' data: https:; "
               "frame-ancestors 'none'")
    else:
        csp = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; form-action 'self'"
    resp.headers.setdefault("Content-Security-Policy", csp)
    if get_settings().is_https:
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return resp
