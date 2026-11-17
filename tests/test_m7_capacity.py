import threading
import pytest
from app.modules.m7_capacity.storage import Store
from app.modules.m7_capacity.service import CapacityService
from app.errors import DomainError

# UTC 半开容量窗（JFK 覆盖两条链的 15:30/20:30 到达，LHR 覆盖 05:30/10:30/14:30 到达）
JFK_WIN = ("JFK", "2026-11-16T14:00:00Z", "2026-11-17T02:00:00Z")
LHR_WIN = ("LHR", "2026-11-16T22:00:00Z", "2026-11-17T16:00:00Z")


def chain(request_id, key, origin, first_dep, first_arr, second_dep, second_arr,
          third_dep, third_arr, middle_origin="JFK", middle_dest="LHR", final_dest="SFO"):
    """三段真实航段，两个转机点（默认 JFK、LHR），当地时刻由 M2/M3 换算 UTC。"""
    first_no, mid_no, last_no = f"AA{key}1", f"BA{key}2", f"VS{key}3"
    return {"request_id": request_id, "idempotency_key": key, "legs": [
        {"flight_no": first_no, "origin": origin, "destination": middle_origin,
         "departure_local": first_dep, "arrival_local": first_arr},
        {"flight_no": mid_no, "origin": middle_origin, "destination": middle_dest,
         "departure_local": second_dep, "arrival_local": second_arr},
        {"flight_no": last_no, "origin": middle_dest, "destination": final_dest,
         "departure_local": third_dep, "arrival_local": third_arr},
    ]}


# A: PVG->JFK(到15:30Z)->LHR(到00:30Z，次日05:30Z 后续)->SFO；两个转机点 JFK、LHR
CHAIN_A = chain("A", "key-A", "PVG",
                "2026-11-16T09:00", "2026-11-16T10:30",
                "2026-11-16T12:30", "2026-11-17T00:30",
                "2026-11-17T05:30", "2026-11-17T13:00")
# B: SFO->JFK(到20:30Z)->LHR(到10:30Z)->SFO
CHAIN_B = chain("B", "key-B", "SFO",
                "2026-11-16T07:00", "2026-11-16T15:30",
                "2026-11-16T18:00", "2026-11-17T05:30",
                "2026-11-17T10:30", "2026-11-17T18:00")
# C: SFO->JFK(到20:30Z)->LHR(到14:30Z)->SFO
CHAIN_C = chain("C", "key-C", "SFO",
                "2026-11-16T07:00", "2026-11-16T15:30",
                "2026-11-16T18:00", "2026-11-17T09:30",
                "2026-11-17T14:30", "2026-11-17T22:00")


@pytest.fixture()
def service(tmp_path):
    return CapacityService(Store(tmp_path / "m7.sqlite3"))


def seed_windows(service, jfk=2, lhr=2):
    service.put_window(*JFK_WIN, jfk)
    service.put_window(*LHR_WIN, lhr)


# --------------------------------------------------------------- 容量窗版本
def test_window_create_starts_at_version_one(service):
    view = service.put_window(*JFK_WIN, 3)
    assert view["version"] == 1 and view["limit"] == 3 and view["available"] == 3
    assert view["accepting_new"] is True


def test_shrink_increments_version_and_requires_expected_version(service):
    service.put_window(*JFK_WIN, 3)
    with pytest.raises(DomainError) as exc:
        service.put_window(*JFK_WIN, 1, expected_version=99)
    assert exc.value.code == "CAPACITY_VERSION_CONFLICT" and exc.value.status == 409
    view = service.put_window(*JFK_WIN, 1, expected_version=1)
    assert view["version"] == 2 and view["limit"] == 1


def test_bad_window_and_negative_limit_rejected(service):
    with pytest.raises(DomainError, match="BAD_WINDOW_RANGE"):
        service.put_window("JFK", "2026-11-16T18:00:00Z", "2026-11-16T14:00:00Z", 2)
    with pytest.raises(DomainError, match="BAD_LIMIT"):
        service.put_window("JFK", *JFK_WIN[1:], -1)


# --------------------------------------------------------------- 整链申请
def test_apply_holds_whole_chain_across_real_utc_legs(service):
    seed_windows(service)
    result = service.apply(CHAIN_A)
    assert result["status"] == "HELD"
    assert [b["airport"] for b in result["buckets"]] == ["JFK", "LHR"]
    snap = service.snapshot()
    for window in snap["windows"]:
        assert window["held"] == 1 and window["used"] == 1
    assert all(b["arrival_utc"].endswith("Z") for b in result["buckets"])  # UTC 统一 Z


def test_apply_idempotent_returns_same_request(service):
    seed_windows(service)
    first = service.apply(CHAIN_A)
    second = service.apply(CHAIN_A)
    assert first["request_id"] == second["request_id"]
    assert service.snapshot()["counts"]["HELD"] == 1


def test_insufficient_capacity_waitlists_whole_chain_without_holding(service):
    seed_windows(service, jfk=1, lhr=1)
    service.apply(CHAIN_A)
    waitlisted = service.apply(CHAIN_B)
    assert waitlisted["status"] == "WAITLISTED"
    assert waitlisted["reason_code"] == "CAPACITY_INSUFFICIENT"
    snap = service.snapshot()
    # 候补整链不占任何容量
    assert all(w["used"] <= 1 for w in snap["windows"])


def test_invalid_connection_rejected_and_holds_nothing(service):
    seed_windows(service)
    bad = {"request_id": "X", "idempotency_key": "key-X", "legs": [
        {"flight_no": "MU5101", "origin": "PVG", "destination": "JFK",
         "departure_local": "2026-11-16T09:00", "arrival_local": "2026-11-16T10:30"},
        # JFK 仅停留 30 分钟，低于 90 分钟最短衔接 -> 衔接无效拒绝
        {"flight_no": "BA900", "origin": "JFK", "destination": "LHR",
         "departure_local": "2026-11-16T11:00", "arrival_local": "2026-11-16T23:00"},
        {"flight_no": "VS900", "origin": "LHR", "destination": "SFO",
         "departure_local": "2026-11-17T08:00", "arrival_local": "2026-11-17T16:00"},
    ]}
    result = service.apply(bad)
    assert result["status"] == "REJECTED"
    assert result["reason_code"] == "CONNECTION_TOO_SHORT"
    assert service.snapshot()["counts"]["REJECTED"] == 1
    assert all(w["used"] == 0 for w in service.snapshot()["windows"])


def test_apply_without_capacity_window_rejected(service):
    service.put_window(*LHR_WIN, 2)  # 缺 JFK 窗
    with pytest.raises(DomainError, match="CAPACITY_WINDOW_NOT_FOUND"):
        service.apply(CHAIN_A)


# --------------------------------------------------------------- 确认与版本
def test_confirm_upgrades_held_to_confirmed(service):
    seed_windows(service)
    service.apply(CHAIN_A)
    confirmed = service.confirm("A")
    assert confirmed["status"] == "CONFIRMED"
    snap = service.snapshot()
    assert snap["counts"]["CONFIRMED"] == 1
    assert all(w["confirmed"] == 1 for w in snap["windows"])


def test_confirm_uses_same_capacity_version_and_rejects_stale(service):
    seed_windows(service)
    service.apply(CHAIN_A)
    service.put_window(*JFK_WIN, 2, expected_version=1)  # 产生 v2
    token = "JFK|2026-11-16T14:00:00Z|2026-11-17T02:00:00Z"
    with pytest.raises(DomainError, match="CAPACITY_VERSION_CONFLICT"):
        service.confirm("A", {token: 1})
    ok = service.confirm("A", {token: 2})  # 携带当前版本即可确认
    assert ok["status"] == "CONFIRMED"


def test_confirm_is_idempotent(service):
    seed_windows(service)
    service.apply(CHAIN_A)
    service.confirm("A")
    again = service.confirm("A")
    assert again["status"] == "CONFIRMED"
    assert service.snapshot()["counts"]["CONFIRMED"] == 1


def test_cannot_confirm_waitlisted(service):
    seed_windows(service, jfk=1, lhr=10)
    service.apply(CHAIN_A)
    service.apply(CHAIN_B)  # JFK 不足 -> 候补
    with pytest.raises(DomainError, match="REQUEST_STILL_WAITLISTED"):
        service.confirm("B")


# --------------------------------------------------------------- 缩容与欠额
def test_shrink_releases_unconfirmed_holds_to_waitlist(service):
    seed_windows(service, jfk=2, lhr=2)
    service.apply(CHAIN_A)
    service.apply(CHAIN_B)
    view = service.put_window(*JFK_WIN, 1, expected_version=1)  # 超额的未确认占位释放
    assert view["held"] == 1
    statuses = {r["request_id"]: r["status"] for r in service.snapshot()["requests"]}
    assert sorted(statuses.values()).count("WAITLISTED") == 1
    assert "HELD" in statuses.values()


def test_shrink_keeps_confirmed_and_books_deficit(service):
    seed_windows(service, jfk=2, lhr=2)
    service.apply(CHAIN_A)
    service.apply(CHAIN_B)
    service.confirm("A")
    service.confirm("B")  # JFK 已确认 2
    view = service.put_window(*JFK_WIN, 1, expected_version=1)
    assert view["confirmed"] == 2 and view["deficit"] == 1
    assert view["accepting_new"] is False  # 欠额清偿前暂停新增占位
    assert all(r["status"] == "CONFIRMED" for r in service.snapshot()["requests"])  # 不毁约


def test_new_apply_blocked_while_deficit_open(service):
    seed_windows(service, jfk=2, lhr=2)
    service.apply(CHAIN_A)
    service.apply(CHAIN_B)
    service.confirm("A")
    service.confirm("B")
    service.put_window(*JFK_WIN, 1, expected_version=1)
    result = service.apply(CHAIN_C)
    assert result["status"] == "WAITLISTED"
    assert result["reason_code"] == "WINDOW_DEFICIT_BLOCKED"


def test_deficit_cleared_after_confirmed_cancel_then_waitlist_autofills(service):
    seed_windows(service, jfk=1, lhr=2)
    service.apply(CHAIN_A)
    service.confirm("A")                  # JFK 已确认 1，上限 1
    service.put_window(*JFK_WIN, 0, expected_version=1)  # 缩到 0 -> 欠额 1，暂停新增
    blocked = service.apply(CHAIN_B)
    assert blocked["status"] == "WAITLISTED" and blocked["reason_code"] == "WINDOW_DEFICIT_BLOCKED"
    service.put_window(*JFK_WIN, 1, expected_version=2)  # 容量回升，欠额待清偿
    service.cancel("A")                   # 撤销已确认 -> 欠额清偿，释放触发 FIFO 回填 B
    snap = service.snapshot()
    jfk = next(w for w in snap["windows"] if w["airport"] == "JFK")
    assert jfk["deficit"] == 0 and jfk["accepting_new"] is True
    b = next(r for r in snap["requests"] if r["request_id"] == "B")
    assert b["status"] == "HELD"


# --------------------------------------------------------------- 撤销语义
def test_cancel_releases_slots_and_autofills_fifo(service):
    seed_windows(service, jfk=1, lhr=2)
    service.apply(CHAIN_A)
    service.apply(CHAIN_B)  # JFK 不足 -> 候补
    service.cancel("A")     # 中断后续跑：释放即回填
    snap = service.snapshot()
    assert snap["counts"]["CANCELLED"] == 1
    b = next(r for r in snap["requests"] if r["request_id"] == "B")
    assert b["status"] == "HELD"


def test_cancel_retry_does_not_double_release(service):
    seed_windows(service)
    service.apply(CHAIN_A)
    service.cancel("A")
    again = service.cancel("A")  # 重试
    assert again["status"] == "CANCELLED"
    release_events = [e for e in service.snapshot()["events"]
                      if e["kind"] == "SLOT_RELEASED_CANCEL"]
    assert len(release_events) == 2  # A 有 JFK/LHR 两个占位，仅各释放一次


def test_cancel_waitlisted_marks_cancelled_without_slots(service):
    seed_windows(service, jfk=1, lhr=1)
    service.apply(CHAIN_A)
    service.apply(CHAIN_B)  # 候补
    result = service.cancel("B")
    assert result["status"] == "CANCELLED"
    assert service.snapshot()["progress"]["released"] == 0


# --------------------------------------------------------------- 回读
def test_snapshot_reports_progress_deficits_and_events(service):
    seed_windows(service)
    service.apply(CHAIN_A)
    service.confirm("A")
    snap = service.snapshot()
    assert snap["progress"]["confirmed"] == 1
    assert snap["progress"]["held"] == 0
    assert snap["deficits"] == []
    kinds = {e["kind"] for e in snap["events"]}
    assert {"WINDOW_CREATE", "SLOT_HELD", "SLOT_CONFIRMED", "REQUEST_CONFIRMED"} <= kinds


def test_waitlist_autofill_respects_fifo_order(service):
    # JFK/LHR 各 1：A 获配，B、C 依次候补；撤销 A 后最早的 B 先获配
    service.put_window(*JFK_WIN, 1)
    service.put_window(*LHR_WIN, 1)
    service.apply(CHAIN_A)
    service.apply(CHAIN_B)
    service.apply(CHAIN_C)
    statuses = {r["request_id"]: r["status"] for r in service.snapshot()["requests"]}
    assert statuses == {"A": "HELD", "B": "WAITLISTED", "C": "WAITLISTED"}
    service.cancel("A")
    statuses = {r["request_id"]: r["status"] for r in service.snapshot()["requests"]}
    assert statuses["A"] == "CANCELLED"
    assert statuses["B"] == "HELD"       # 最早候补先回填
    assert statuses["C"] == "WAITLISTED"  # 仅释放一个整链容量，C 继续候补


# --------------------------------------------------------------- 并发裁决
def test_concurrent_shrink_and_confirm_are_serialized_on_one_version(service):
    seed_windows(service)
    service.apply(CHAIN_A)
    errors = []

    def shrink():
        try:
            service.put_window(*JFK_WIN, 1, expected_version=1)
        except DomainError as exc:
            errors.append(exc.code)

    def confirm():
        try:
            service.confirm("A")
        except DomainError as exc:
            errors.append(exc.code)

    threads = [threading.Thread(target=shrink), threading.Thread(target=confirm)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 同一锁内串行，按同一容量版本给出明确裁决，无脏写/重复计数
    snap = service.snapshot()
    jfk = next(w for w in snap["windows"] if w["airport"] == "JFK")
    assert jfk["version"] == 2
    assert jfk["confirmed"] + jfk["held"] <= max(jfk["limit"], jfk["confirmed"])
    assert snap["counts"]["CONFIRMED"] + snap["counts"]["HELD"] >= 1
