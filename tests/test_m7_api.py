import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.modules.m7_capacity.handler import service
from app.modules.m7_capacity.storage import Store
from app.modules.m7_capacity.service import CapacityService


@pytest.fixture(autouse=True)
def isolated_service(tmp_path, monkeypatch):
    """每个用例使用独立 SQLite 文件，避免演示数据互相污染。"""
    fresh = CapacityService(Store(tmp_path / "m7-api.sqlite3"))
    monkeypatch.setattr("app.modules.m7_capacity.handler.service", fresh)
    return fresh

JFK = {"airport": "JFK", "window_start": "2026-11-16T14:00:00Z",
       "window_end": "2026-11-17T02:00:00Z", "limit": 1}
LHR = {"airport": "LHR", "window_start": "2026-11-16T22:00:00Z",
       "window_end": "2026-11-17T16:00:00Z", "limit": 2}

CHAIN = {"request_id": "WEB-A", "idempotency_key": "web-a", "legs": [
    {"flight_no": "MU5101", "origin": "PVG", "destination": "JFK",
     "departure_local": "2026-11-16T09:00", "arrival_local": "2026-11-16T10:30"},
    {"flight_no": "BA178", "origin": "JFK", "destination": "LHR",
     "departure_local": "2026-11-16T12:30", "arrival_local": "2026-11-17T00:30"},
    {"flight_no": "VS251", "origin": "LHR", "destination": "SFO",
     "departure_local": "2026-11-17T05:30", "arrival_local": "2026-11-17T13:00"},
]}


def test_page_renders():
    response = TestClient(app).get("/capacity")
    assert response.status_code == 200 and "panel" in response.text


def test_full_capacity_lifecycle_over_http():
    client = TestClient(app)
    # 1) 建容量窗（UTC 时窗容量调整）
    r = client.put("/api/capacity/windows", json=JFK)
    assert r.status_code == 200 and r.json()["version"] == 1
    client.put("/api/capacity/windows", json=LHR)

    # 2) 航链保障申请 -> 整链获配占位
    r = client.post("/api/capacity/apply", json=CHAIN)
    assert r.status_code == 200 and r.json()["status"] == "HELD"

    # 3) 确认全程保障
    r = client.post("/api/capacity/requests/WEB-A/confirm", json={})
    assert r.json()["status"] == "CONFIRMED"

    # 4) 缩容到 0：已确认超额 -> 欠额，窗暂停新增
    r = client.put("/api/capacity/windows", json={**JFK, "limit": 0, "expected_version": 1})
    body = r.json()
    assert body["deficit"] == 1 and body["accepting_new"] is False

    # 5) 欠额清偿前新增申请被挂候补
    r = client.post("/api/capacity/apply",
                    json={**CHAIN, "request_id": "WEB-C", "idempotency_key": "web-c"})
    assert r.json()["status"] == "WAITLISTED"
    assert r.json()["reason_code"] == "WINDOW_DEFICIT_BLOCKED"

    # 6) 容量版本冲突：旧版本号被拒（409）
    r = client.put("/api/capacity/windows", json={**JFK, "limit": 0, "expected_version": 1})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "CAPACITY_VERSION_CONFLICT"

    # 7) 撤销已确认 -> 释放、欠额清偿、候补 FIFO 回填
    client.put("/api/capacity/windows", json={**JFK, "limit": 1, "expected_version": 2})
    r = client.post("/api/capacity/requests/WEB-A/cancel", json={"reason": "PLAN_CHANGE"})
    assert r.json()["status"] == "CANCELLED"
    # 重试撤销不重复归还
    client.post("/api/capacity/requests/WEB-A/cancel", json={})
    snap = client.get("/api/capacity/snapshot").json()
    jfk = next(w for w in snap["windows"] if w["airport"] == "JFK")
    assert jfk["deficit"] == 0
    web_c = next(r for r in snap["requests"] if r["request_id"] == "WEB-C")
    assert web_c["status"] == "HELD"
    releases = [e for e in snap["events"] if e["kind"] == "SLOT_RELEASED_CANCEL"]
    assert len(releases) == 2  # JFK + LHR 各一次，重试未重复归还

    # 8) 回读：占位/确认/候补/释放进度与各时窗欠额
    assert snap["progress"]["released"] >= 2
    assert all("deficit" in w for w in snap["windows"])
    assert snap["deficits"] == []


def test_invalid_connection_rejected_over_http():
    client = TestClient(app)
    client.put("/api/capacity/windows", json=JFK)
    client.put("/api/capacity/windows", json=LHR)
    bad = {**CHAIN, "request_id": "WEB-BAD", "idempotency_key": "web-bad", "legs": [
        {"flight_no": "MU5101", "origin": "PVG", "destination": "JFK",
         "departure_local": "2026-11-16T09:00", "arrival_local": "2026-11-16T10:30"},
        {"flight_no": "BA900", "origin": "JFK", "destination": "LHR",
         "departure_local": "2026-11-16T11:00", "arrival_local": "2026-11-16T23:00"},
        {"flight_no": "VS900", "origin": "LHR", "destination": "SFO",
         "departure_local": "2026-11-17T08:00", "arrival_local": "2026-11-17T16:00"},
    ]}
    r = client.post("/api/capacity/apply", json=bad)
    assert r.json()["status"] == "REJECTED"
    assert r.json()["reason_code"] == "CONNECTION_TOO_SHORT"


def test_demo_endpoint_seeds_and_is_idempotent():
    client = TestClient(app)
    first = client.post("/api/capacity/demo", json={}).json()
    second = client.post("/api/capacity/demo", json={}).json()
    assert len(first["requests"]) == 2
    # 幂等：第二次不产生新申请
    assert second["requests"][0]["request_id"] == first["requests"][0]["request_id"]
    snap = client.get("/api/capacity/snapshot").json()
    assert snap["counts"]["HELD"] + snap["counts"]["WAITLISTED"] == 2


def test_unknown_request_returns_404():
    client = TestClient(app)
    r = client.get("/api/capacity/requests/NOPE")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "REQUEST_NOT_FOUND"


def test_limit_accepts_both_field_names():
    client = TestClient(app)
    r1 = client.put("/api/capacity/windows", json=JFK)
    r2 = client.put("/api/capacity/windows",
                    json={"airport": "SFO", "window_start": "2026-11-17T00:00:00Z",
                          "window_end": "2026-11-17T08:00:00Z", "limit_slots": 5})  # 兼容命名
    assert r1.json()["limit"] == 1 and r2.json()["limit"] == 5


def test_half_open_window_boundary_uses_arrival_utc(tmp_path, monkeypatch):
    # 到达 UTC 恰好等于窗结束时刻 -> 不命中该半开窗（TRAP-23）
    from app.modules.m7_capacity.service import CapacityService
    from app.modules.m7_capacity.storage import Store
    svc = CapacityService(Store(tmp_path / "boundary.sqlite3"))
    monkeypatch.setattr("app.modules.m7_capacity.handler.service", svc)
    client = TestClient(app)
    client.put("/api/capacity/windows",
               json={"airport": "JFK", "window_start": "2026-11-16T14:00:00Z",
                     "window_end": "2026-11-16T15:30:00Z", "limit": 2})
    client.put("/api/capacity/windows", json=LHR)
    # A 链 JFK 到达 15:30Z，恰为窗结束 -> 无窗命中而报错
    r = client.post("/api/capacity/apply", json=CHAIN)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "CAPACITY_WINDOW_NOT_FOUND"
