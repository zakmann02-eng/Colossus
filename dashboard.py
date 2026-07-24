from __future__ import annotations

import csv
import json
import os
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import scan_log

_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "colossus")
_HTML = Path(__file__).parent / "dashboard.html"

app = FastAPI(docs_url=None, redoc_url=None)
_security = HTTPBasic()


def _auth(creds: HTTPBasicCredentials = Depends(_security)) -> None:
    if not secrets.compare_digest(creds.password.encode(), _PASSWORD.encode()):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(_auth)])
async def index() -> str:
    return _HTML.read_text()


@app.get("/api/positions", dependencies=[Depends(_auth)])
async def api_positions() -> dict:
    p = Path("positions.json")
    return json.loads(p.read_text()) if p.exists() else {}


@app.get("/api/reserve", dependencies=[Depends(_auth)])
async def api_reserve() -> dict:
    p = Path("reserve.json")
    return json.loads(p.read_text()) if p.exists() else {"reserve_usd": 0.0}


@app.get("/api/trades", dependencies=[Depends(_auth)])
async def api_trades() -> list:
    p = Path("trade_log.csv")
    if not p.exists():
        return []
    rows = []
    with p.open(newline="") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    return list(reversed(rows[-50:]))


@app.get("/api/scan-feed", dependencies=[Depends(_auth)])
async def api_scan_feed() -> list:
    return list(scan_log.events)
