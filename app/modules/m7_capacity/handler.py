from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from app.web import page
from app.errors import DomainError
from .service import CapacityService

router = APIRouter()

# 演示与测试共享一个服务实例（进程内 SQLite + 锁）
service = CapacityService()


class WindowIn(BaseModel):
    airport: str
    window_start: str
    window_end: str
    limit: int | None = None
    limit_slots: int | None = None
    expected_version: int | None = None

    def resolved_limit(self) -> int:
        value = self.limit if self.limit is not None else self.limit_slots
        if value is None:
            raise DomainError("BAD_LIMIT", "缺少容量上限 limit", 422)
        return value


class ConfirmIn(BaseModel):
    expected_versions: dict | None = None


class CancelIn(BaseModel):
    reason: str = "USER_CANCEL"


@router.get("/capacity", response_class=HTMLResponse)
def view(request: Request):
    rows = [
        {"key": "JFK|2026-11-16 窗", "status": "容量版本", "value": "v1"},
        {"key": "LHR|2026-11-16 窗", "status": "欠额", "value": "0"},
        {"key": "整链保障", "status": "占位/确认", "value": "API"},
    ]
    return page(request, "m7.html", "中转保障容量", "/api/capacity/demo", rows)


@router.put("/api/capacity/windows")
def put_window(payload: WindowIn):
    return service.put_window(payload.airport, payload.window_start, payload.window_end,
                              payload.resolved_limit(), payload.expected_version)


@router.post("/api/capacity/apply")
def apply(payload: dict):
    return service.apply(payload)


@router.post("/api/capacity/requests/{request_id}/confirm")
def confirm(request_id: str, payload: ConfirmIn | None = None):
    versions = payload.expected_versions if payload else None
    return service.confirm(request_id, versions)


@router.post("/api/capacity/requests/{request_id}/cancel")
def cancel(request_id: str, payload: CancelIn | None = None):
    reason = payload.reason if payload else "USER_CANCEL"
    return service.cancel(request_id, reason)


@router.get("/api/capacity/snapshot")
def snapshot():
    return service.snapshot()


@router.get("/api/capacity/requests/{request_id}")
def get_request(request_id: str):
    store = service.store
    with store.lock, store.connect() as db:
        row = store.get_request(db, request_id)
        if row is None:
            raise DomainError("REQUEST_NOT_FOUND", f"申请 {request_id} 不存在", 404)
        return service._request_view(row)


# 演示数据：两条三航段整链（PVG/SFO→JFK→LHR→SFO），转机点 JFK、LHR
DEMO_WINDOWS = [
    {"airport": "JFK", "window_start": "2026-11-16T14:00:00Z", "window_end": "2026-11-17T02:00:00Z", "limit": 1},
    {"airport": "LHR", "window_start": "2026-11-16T22:00:00Z", "window_end": "2026-11-17T16:00:00Z", "limit": 2},
]

DEMO_CHAINS = [
    {"request_id": "DEMO-A", "idempotency_key": "demo-chain-a", "legs": [
        {"flight_no": "MU5101", "origin": "PVG", "destination": "JFK",
         "departure_local": "2026-11-16T09:00", "arrival_local": "2026-11-16T10:30"},
        {"flight_no": "BA178", "origin": "JFK", "destination": "LHR",
         "departure_local": "2026-11-16T12:30", "arrival_local": "2026-11-17T00:30"},
        {"flight_no": "VS251", "origin": "LHR", "destination": "SFO",
         "departure_local": "2026-11-17T05:30", "arrival_local": "2026-11-17T13:00"},
    ]},
    {"request_id": "DEMO-B", "idempotency_key": "demo-chain-b", "legs": [
        {"flight_no": "UA857", "origin": "SFO", "destination": "JFK",
         "departure_local": "2026-11-16T07:00", "arrival_local": "2026-11-16T15:30"},
        {"flight_no": "BA202", "origin": "JFK", "destination": "LHR",
         "departure_local": "2026-11-16T18:00", "arrival_local": "2026-11-17T05:30"},
        {"flight_no": "VS253", "origin": "LHR", "destination": "SFO",
         "departure_local": "2026-11-17T10:30", "arrival_local": "2026-11-17T18:00"},
    ]},
]


def seed_demo() -> dict:
    """灌入演示容量窗并提交两条整链申请，供页面一键演示。"""
    for window in DEMO_WINDOWS:
        try:
            service.put_window(window["airport"], window["window_start"],
                               window["window_end"], window["limit"])
        except DomainError as exc:
            if exc.code != "CAPACITY_VERSION_CONFLICT":
                raise
    results = [service.apply(chain) for chain in DEMO_CHAINS]
    return {"seeded_windows": len(DEMO_WINDOWS), "requests": results}


@router.post("/api/capacity/demo")
def demo():
    return seed_demo()
