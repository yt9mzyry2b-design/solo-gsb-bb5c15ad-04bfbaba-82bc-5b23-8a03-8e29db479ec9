"""FastAPI 入口：API + 原生静态页面。"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .core import (
    Config,
    parse_fares_text,
    parse_records_text,
    parse_stations_text,
    verify,
)
from .schemas import ConfigIn, TextIn

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="公交刷卡票价核验台",
    description="录入站点分区、票价矩阵、换乘抵扣、每日封顶与刷卡记录，"
                "按卡号配对进出站并核验计价。",
    version="1.0.0",
    lifespan=lifespan,
)


def _parse_error_response(errors: list[dict], status: int = 422) -> JSONResponse:
    return JSONResponse(status_code=status,
                        content={"ok": False, "errors": errors})


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@app.get("/api/config")
def get_config():
    with db.connect() as conn:
        cfg = db.get_config(conn)
    return {
        "transfer_minutes": cfg.transfer_minutes,
        "transfer_discount": cfg.transfer_discount,
        "daily_cap": cfg.daily_cap,
        "day_split_hour": cfg.day_split_hour,
    }


@app.put("/api/config")
def put_config(body: ConfigIn):
    cfg = Config(
        transfer_minutes=body.transfer_minutes,
        transfer_discount=body.transfer_discount,
        daily_cap=body.daily_cap,
        day_split_hour=body.day_split_hour,
    )
    with db.connect() as conn:
        db.update_config(conn, cfg)
        # 规则参数变化后，旧账单可能已不再成立，标记为失效
        db.clear_bills(conn)
    return {"ok": True}


# ---------------------------------------------------------------------------
# 站点分区
# ---------------------------------------------------------------------------

@app.get("/api/stations")
def get_stations():
    with db.connect() as conn:
        return {"items": db.list_stations(conn)}


@app.put("/api/stations")
def put_stations(body: TextIn):
    stations, errors = parse_stations_text(body.text)
    if errors:
        return _parse_error_response(errors)
    with db.connect() as conn:
        db.replace_stations(conn, stations)
        db.clear_bills(conn)
    return {"ok": True, "count": len(stations)}


# ---------------------------------------------------------------------------
# 票价矩阵
# ---------------------------------------------------------------------------

@app.get("/api/fares")
def get_fares():
    with db.connect() as conn:
        return {"items": db.list_fares(conn)}


@app.put("/api/fares")
def put_fares(body: TextIn):
    fares, errors = parse_fares_text(body.text)
    if errors:
        return _parse_error_response(errors)
    with db.connect() as conn:
        db.replace_fares(conn, fares)
        db.clear_bills(conn)
    return {"ok": True, "count": len(fares)}


# ---------------------------------------------------------------------------
# 刷卡记录
# ---------------------------------------------------------------------------

@app.get("/api/records")
def get_records():
    with db.connect() as conn:
        return {"items": db.list_records(conn)}


@app.put("/api/records")
def put_records(body: TextIn):
    default_year = datetime.now().year
    records, errors = parse_records_text(body.text, default_year=default_year)
    if errors:
        return _parse_error_response(errors)
    with db.connect() as conn:
        db.replace_records(conn, records)
        db.clear_bills(conn)
    return {"ok": True, "count": len(records)}


# ---------------------------------------------------------------------------
# 核验
# ---------------------------------------------------------------------------

@app.post("/api/verify")
def run_verify():
    """执行全量核验。

    成功：清空旧账单并写入新账单（同一事务）。
    失败：返回 422 与全部定位错误，不写入/保留任何账单（无部分账单）。
    """
    with db.connect() as conn:
        cfg = db.get_config(conn)
        stations, fares, records = db.load_domain(conn)
        result = verify(stations, fares, records, cfg)
        if not result.ok:
            db.clear_bills(conn)
            return JSONResponse(
                status_code=422,
                content={"ok": False,
                         "errors": result.errors,
                         "bill_count": 0})
        db.save_bills(conn, result.bills)
        return {"ok": True,
                "bill_count": len(result.bills),
                "bills": result.bills}


@app.get("/api/bills")
def get_bills():
    with db.connect() as conn:
        bills = db.list_bills(conn)
        last = db.get_meta(conn, "last_verified_at")
    return {"items": bills, "count": len(bills), "last_verified_at": last}


@app.get("/api/health")
def health():
    return {"ok": True}


# ---------------------------------------------------------------------------
# 原生前端（静态资源）
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
