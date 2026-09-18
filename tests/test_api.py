"""FastAPI 端到端测试。运行：python -m pytest -q"""

import os
import tempfile

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DB_PATH"] = _tmp.name

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)


def setup_demo_config():
    for name, zone in [("S1", 1), ("S2", 2), ("S3", 3)]:
        client.post("/api/stations", json={"name": name, "zone": zone})
    for z1, z2, p in [(1, 2, 3.0), (2, 3, 3.0), (1, 3, 4.0)]:
        client.post("/api/fares",
                    json={"zone_from": z1, "zone_to": z2, "price_yuan": p})
    client.put("/api/settings",
               json={"transfer_discount_yuan": 1.0, "daily_cap_yuan": 10.0})


def test_full_flow_discount_and_cap():
    setup_demo_config()
    bulk = """卡号,站点,时间,类型
C,S1,2026-09-18 08:00,IN
C,S2,2026-09-18 08:30,OUT
C,S2,2026-09-18 09:40,IN
C,S3,2026-09-18 10:10,OUT
C,S3,2026-09-18 11:00,IN
C,S1,2026-09-18 11:40,OUT
C,S1,2026-09-18 13:00,IN
C,S2,2026-09-18 13:30,OUT
"""
    r = client.post("/api/swipes/bulk", json={"text": bulk})
    assert r.status_code == 201 and r.json()["inserted"] == 8

    r = client.post("/api/billing/run")
    data = r.json()
    assert data["ok"] is True
    charged = [t["charged_yuan"] for t in data["trips"]]
    # 3.00, 2.00, 3.00, 2.00
    assert charged == [3.0, 2.0, 3.0, 2.0]
    assert data["summary"][0]["charged_total_yuan"] == 10.0
    assert "换乘优惠" in data["trips"][1]["discount_source"]


def test_bulk_bad_line_rejects_entire_batch():
    before = len(client.get("/api/swipes").json())
    text = "D,S1,2026-09-18 08:00,IN\nD,S2,not-a-time,OUT\n"
    r = client.post("/api/swipes/bulk", json={"text": text})
    assert r.status_code == 400 and "第 2 行" in r.json()["detail"]
    after = len(client.get("/api/swipes").json())
    assert before == after  # 整批不写入


def test_missing_out_blocks_billing_with_ids():
    client.delete("/api/swipes")
    client.post("/api/swipes", json={
        "card": "X", "station": "S1",
        "tap_time": "2026-09-18 08:00", "kind": "IN"})
    r = client.post("/api/billing/run").json()
    assert r["ok"] is False
    assert r["trips"] == []
    codes = {e["code"] for e in r["errors"]}
    assert "missing_check_out" in codes
    assert all(e["swipe_ids"] for e in r["errors"])


def test_duplicate_and_reverse_detected():
    client.delete("/api/swipes")
    bulk = """X,S1,2026-09-18 09:00,IN
X,S1,2026-09-18 09:00,IN
X,S2,2026-09-18 08:30,OUT
"""
    client.post("/api/swipes/bulk", json={"text": bulk})
    r = client.post("/api/billing/run").json()
    codes = {e["code"] for e in r["errors"]}
    assert {"duplicate_tap", "time_reversed", "missing_check_out"} <= codes


def test_demo_endpoint():
    r = client.post("/api/demo")
    assert r.status_code == 201
    data = client.post("/api/billing/run").json()
    assert data["ok"] is True and len(data["trips"]) == 5
    days = {(t["card"], t["service_day"]) for t in data["trips"]}
    assert ("A002", "2026-09-18") in days  # 03:30 进站归属前一运营日
    assert ("A002", "2026-09-19") in days


def test_index_page():
    r = client.get("/")
    assert r.status_code == 200 and "公交刷卡票价核验台" in r.text
