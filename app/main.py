"""FastAPI 入口：公交刷卡票价核验台。"""

from __future__ import annotations

import os
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .billing import bill
from .database import Database, parse_dt

DB_PATH = os.environ.get("DB_PATH", "/data/bus.db")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

db = Database(DB_PATH)
app = FastAPI(title="公交刷卡票价核验台", version="1.0.0")


# ---------------------------------------------------------------- 模型
class StationIn(BaseModel):
    name: str = Field(min_length=1)
    zone: int = Field(ge=0)


class FareIn(BaseModel):
    zone_from: int = Field(ge=0)
    zone_to: int = Field(ge=0)
    price_yuan: float = Field(ge=0)


class SettingsIn(BaseModel):
    transfer_discount_yuan: float = Field(ge=0)
    daily_cap_yuan: float = Field(ge=0)


class SwipeIn(BaseModel):
    card: str = Field(min_length=1)
    station: str = Field(min_length=1)
    tap_time: str = Field(min_length=1)
    kind: Literal["IN", "OUT"]


def yuan_to_cents(v: float, label: str) -> int:
    cents = round(v * 100)
    if cents < 0:
        raise HTTPException(400, f"{label}不能为负")
    return cents


def err_400(msg: str) -> HTTPException:
    return HTTPException(status_code=400, detail=msg)


# ---------------------------------------------------------------- 配置接口
@app.get("/api/stations")
def get_stations():
    return db.list_stations()


@app.post("/api/stations", status_code=201)
def add_station(body: StationIn):
    db.upsert_station(body.name.strip(), body.zone)
    return {"ok": True}


@app.delete("/api/stations/{sid}", status_code=204)
def remove_station(sid: int):
    db.delete_station(sid)


@app.get("/api/fares")
def get_fares():
    out = []
    for r in db.list_fares():
        out.append({**r, "price_yuan": round(r["price_cents"] / 100, 2)})
    return out


@app.post("/api/fares", status_code=201)
def add_fare(body: FareIn):
    db.upsert_fare(body.zone_from, body.zone_to,
                   yuan_to_cents(body.price_yuan, "票价"))
    return {"ok": True}


@app.delete("/api/fares/{fid}", status_code=204)
def remove_fare(fid: int):
    db.delete_fare(fid)


@app.get("/api/settings")
def get_settings():
    s = db.get_settings()
    return {
        "transfer_discount_yuan": s["transfer_discount_cents"] / 100,
        "daily_cap_yuan": s["daily_cap_cents"] / 100,
    }


@app.put("/api/settings")
def set_settings(body: SettingsIn):
    db.update_settings(
        yuan_to_cents(body.transfer_discount_yuan, "换乘抵扣额"),
        yuan_to_cents(body.daily_cap_yuan, "每日封顶额"),
    )
    return {"ok": True}


# ---------------------------------------------------------------- 刷卡记录
@app.get("/api/swipes")
def get_swipes():
    return db.list_swipes()


@app.post("/api/swipes", status_code=201)
def add_swipe(body: SwipeIn):
    try:
        sid = db.add_swipe(body.card, body.station, body.tap_time, body.kind)
    except ValueError as e:
        raise err_400(str(e))
    return {"ok": True, "id": sid}


@app.post("/api/swipes/bulk", status_code=201)
def add_swipes_bulk(payload: dict):
    """批量录入。每行：卡号,站点,YYYY-MM-DD HH:MM,IN|OUT（首行可为表头）。

    任一行解析失败则整批拒绝，不写入任何记录。
    """
    text = (payload or {}).get("text", "")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise err_400("未解析到任何刷卡记录")

    created: list[int] = []
    parsed: list[tuple[str, str, str, str]] = []
    first = True
    for i, line in enumerate(lines, start=1):
        parts = [p.strip() for p in line.replace("\t", ",").split(",")]
        if len(parts) != 4:
            raise err_400(f"第 {i} 行字段数不是 4：{line}")
        card, station, tstr, kind = parts
        # 首行若时间无法解析，视为表头整体跳过（类型列可能是中文表头）
        if first:
            first = False
            try:
                parse_dt(tstr)
            except ValueError:
                continue
        kind_up = {"进站": "IN", "进": "IN", "IN": "IN",
                   "出站": "OUT", "出": "OUT", "OUT": "OUT"}.get(kind.upper())
        if kind_up is None:
            raise err_400(
                f"第 {i} 行刷卡类型「{kind}」无效，应为 IN/进站 或 OUT/出站")
        if not card or not station:
            raise err_400(f"第 {i} 行卡号或站点为空")
        try:
            parse_dt(tstr)
        except ValueError as e:
            raise err_400(f"第 {i} 行：{e}")
        parsed.append((card, station, tstr, kind_up))

    for card, station, tstr, kind_up in parsed:
        created.append(db.add_swipe(card, station, tstr, kind_up))
    return {"ok": True, "inserted": len(created), "ids": created}


@app.delete("/api/swipes/{sid}", status_code=204)
def remove_swipe(sid: int):
    db.delete_swipe(sid)


@app.delete("/api/swipes", status_code=204)
def clear_swipes():
    db.clear_all(include_config=False)


# ---------------------------------------------------------------- 核验
@app.post("/api/billing/run")
def run_billing():
    swipes, station_zone, fares, settings = db.billing_inputs()
    trips, errors = bill(
        swipes, station_zone, fares,
        settings["transfer_discount_cents"],
        settings["daily_cap_cents"],
    )
    if errors:
        return {
            "ok": False,
            "errors": [
                {"code": e.code, "message": e.message,
                 "swipe_ids": e.swipe_ids}
                for e in errors
            ],
            "trips": [],
            "summary": [],
        }

    summary_map: dict[tuple, dict] = {}
    trip_dicts = []
    for tr in trips:
        if tr.is_transfer:
            discount_source = (
                f"换乘优惠：距上次出站 {tr.gap_minutes} 分钟（≤90 分钟），"
                f"抵扣 {tr.discount / 100:.2f} 元")
        elif tr.gap_minutes is not None:
            discount_source = f"无换乘优惠：距上次出站 {tr.gap_minutes} 分钟（>90 分钟）"
        else:
            discount_source = "无换乘优惠：本卡首段行程"
        trip_dicts.append({
            "index": tr.index,
            "card": tr.card,
            "in_id": tr.in_id,
            "out_id": tr.out_id,
            "in_station": tr.in_station,
            "out_station": tr.out_station,
            "in_zone": tr.in_zone,
            "out_zone": tr.out_zone,
            "in_time": tr.in_time.strftime("%Y-%m-%d %H:%M"),
            "out_time": tr.out_time.strftime("%Y-%m-%d %H:%M"),
            "service_day": tr.service_day,
            "base_fare_yuan": round(tr.base_fare / 100, 2),
            "discount_yuan": round(tr.discount / 100, 2),
            "discount_source": discount_source,
            "charged_yuan": round(tr.charged / 100, 2),
            "capped": tr.capped,
            "daily_total_yuan": round(tr.daily_total / 100, 2),
        })
        key = (tr.card, tr.service_day)
        s = summary_map.setdefault(key, {
            "card": tr.card, "service_day": tr.service_day,
            "trip_count": 0, "base_total_yuan": 0.0,
            "discount_total_yuan": 0.0, "charged_total_yuan": 0.0,
            "capped": False,
        })
        s["trip_count"] += 1
        s["base_total_yuan"] += tr.base_fare / 100
        s["discount_total_yuan"] += tr.discount / 100
        s["charged_total_yuan"] += tr.charged / 100
        s["capped"] = s["capped"] or tr.capped

    summary = [
        {**v,
         "base_total_yuan": round(v["base_total_yuan"], 2),
         "discount_total_yuan": round(v["discount_total_yuan"], 2),
         "charged_total_yuan": round(v["charged_total_yuan"], 2)}
        for v in sorted(summary_map.values(),
                        key=lambda x: (x["card"], x["service_day"]))
    ]
    return {"ok": True, "errors": [], "trips": trip_dicts, "summary": summary}


# ---------------------------------------------------------------- 演示数据
@app.post("/api/demo", status_code=201)
def load_demo():
    """清空并载入演示数据（保留参数为演示值）。"""
    db.clear_all(include_config=True)
    for name, zone in [("中央公园", 1), ("人民广场", 1), ("科技园", 2),
                       ("大学城", 3), ("机场", 4)]:
        db.upsert_station(name, zone)
    # 票价矩阵（方向无关）
    for z1, z2, price in [(1, 2, 3.0), (1, 3, 4.0), (1, 4, 8.0),
                          (2, 3, 3.0), (2, 4, 6.0), (3, 4, 5.0),
                          (1, 1, 2.0), (2, 2, 2.0), (3, 3, 2.0), (4, 4, 2.0)]:
        db.upsert_fare(z1, z2, yuan_to_cents(price, "票价"))
    db.update_settings(yuan_to_cents(1.0, "抵扣"), yuan_to_cents(10.0, "封顶"))

    rows = [
        # 卡 A001：换乘优惠（间隔 65 分钟）
        ("A001", "中央公园", "2026-09-18 08:00", "IN"),
        ("A001", "科技园",   "2026-09-18 08:35", "OUT"),
        ("A001", "科技园",   "2026-09-18 09:40", "IN"),
        ("A001", "大学城",   "2026-09-18 10:20", "OUT"),
        # 卡 A001：第三次行程触发当日封顶 10 元
        ("A001", "人民广场", "2026-09-18 13:00", "IN"),
        ("A001", "机场",     "2026-09-18 14:00", "OUT"),
        # 卡 A002：跨 04:00 运营日（03:30 进站归属前一运营日）
        ("A002", "大学城",   "2026-09-19 03:30", "IN"),
        ("A002", "机场",     "2026-09-19 04:20", "OUT"),
        ("A002", "机场",     "2026-09-19 04:50", "IN"),
        ("A002", "科技园",   "2026-09-19 05:30", "OUT"),
    ]
    for card, station, t, kind in rows:
        db.add_swipe(card, station, t, kind)
    return {"ok": True, "inserted": len(rows)}


# ---------------------------------------------------------------- 前端
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
