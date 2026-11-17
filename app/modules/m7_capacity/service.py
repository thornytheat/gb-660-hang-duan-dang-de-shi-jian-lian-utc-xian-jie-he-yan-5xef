"""M7 中转保障容量领域服务。

容量版本：每个机场 UTC 时窗维护单调递增 version，缩容与确认在同一把锁、同一事务内
按当前版本裁决；调用方携带 expected_version，过期即 CAPACITY_VERSION_CONFLICT。

申请状态机：
  HELD       整链所有转机点都拿到未确认占位
  WAITLISTED 至少一个窗容量不足，整链候补，不占任何容量
  CONFIRMED  全程保障兑现，占位升级为已确认
  CANCELLED  撤销，释放其全部有效占位，中断后续跑
  REJECTED   衔接无效等输入问题，不占容量
欠额：缩容后已确认数超过新上限，差额记为欠额；欠额清偿前该窗暂停一切新增占位。
"""
import json
import uuid
from .storage import Store, VersionConflict
from .chains import build_chain, transfer_buckets, parse_utc, utc_key
from app.errors import DomainError

ACTIVE_STATES = ("HELD", "CONFIRMED")
TERMINAL_STATES = ("CANCELLED", "REJECTED")


class CapacityService:
    def __init__(self, store: Store | None = None):
        self.store = store or Store()

    # ------------------------------------------------------------------ 工具
    def _window_view(self, db, row) -> dict:
        active = self.store.active_slots(db, row["airport"], row["window_start"], row["window_end"])
        confirmed = sum(1 for slot in active if slot["state"] == "CONFIRMED")
        held = sum(1 for slot in active if slot["state"] == "HELD")
        limit = row["limit_slots"]
        deficit = max(0, confirmed - limit)
        return {
            "airport": row["airport"],
            "window_start": row["window_start"],
            "window_end": row["window_end"],
            "limit": limit,
            "version": row["version"],
            "confirmed": confirmed,
            "held": held,
            "used": confirmed + held,
            "available": max(0, limit - confirmed - held),
            "deficit": deficit,
            "accepting_new": deficit == 0,
        }

    def _request_view(self, row) -> dict:
        return {
            "request_id": row["request_id"],
            "status": row["status"],
            "created_seq": row["created_seq"],
            "decided_version": row["decided_version"],
            "reason_code": row["reason_code"],
            "chain": json.loads(row["chain_json"]),
            "buckets": json.loads(row["buckets_json"]),
        }

    def _resolve_buckets(self, db, links: list[dict]) -> list[dict]:
        resolved = []
        for link in links:
            window = self.store.find_window(db, link["airport"], link["arrival_utc"])
            if window is None:
                raise DomainError("CAPACITY_WINDOW_NOT_FOUND",
                                  f"{link['airport']} 在 {link['arrival_utc']} 无容量窗", 422)
            resolved.append({**link, "window_start": window["window_start"],
                             "window_end": window["window_end"],
                             "window_version": window["version"]})
        return resolved

    # ----------------------------------------------------------- 容量窗管理
    def put_window(self, airport: str, window_start: str, window_end: str,
                   limit_slots: int, expected_version: int | None = None) -> dict:
        airport = airport.strip().upper()
        start = utc_key(parse_utc(window_start))
        end = utc_key(parse_utc(window_end))
        if end <= start:
            raise DomainError("BAD_WINDOW_RANGE", "容量窗结束必须晚于开始（UTC 半开区间）")
        if limit_slots < 0:
            raise DomainError("BAD_LIMIT", "容量上限不能为负")
        with self.store.lock, self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                try:
                    row = self.store.upsert_window(db, airport, start, end, limit_slots,
                                                   expected_version)
                except VersionConflict as exc:
                    raise DomainError("CAPACITY_VERSION_CONFLICT",
                                      f"容量版本已变更，当前版本 {exc.current_version}",
                                      409)
                created = row["version"] == 1
                if not created:
                    self._reconcile_after_shrink(db, row)
                db.commit()
                return self._window_view(db, self.store.get_window(db, airport, start, end))
            except Exception:
                db.rollback()
                raise

    def _reconcile_after_shrink(self, db, window) -> None:
        """缩容：释放未确认占位（整链转候补），已确认超额单列欠额。"""
        view = self._window_view(db, window)
        # 1) 未确认占位超出新上限的部分释放，对应申请整链转候补
        active = self.store.active_slots(db, window["airport"], window["window_start"], window["window_end"])
        overflow = max(0, view["used"] - window["limit_slots"])
        for slot in active:
            if overflow <= 0:
                break
            if slot["state"] != "HELD":
                continue
            self.store.set_slot_state(db, slot["request_id"], slot["airport"],
                                      slot["window_start"], slot["window_end"],
                                      "RELEASED_SHRINK", window["version"])
            self.store.log(db, "SLOT_RELEASED_SHRINK", request_id=slot["request_id"],
                           airport=slot["airport"], start=slot["window_start"],
                           end=slot["window_end"], to_version=window["version"])
            overflow -= 1
            request = self.store.get_request(db, slot["request_id"])
            if request and request["status"] == "HELD":
                self.store.update_status(db, request["request_id"], "WAITLISTED",
                                         window["version"], "RELEASED_BY_SHRINK")
                self.store.log(db, "REQUEST_WAITLISTED", request_id=request["request_id"],
                               to_version=window["version"], detail={"reason": "SHRINK"})
        # 2) 已确认超额 -> 欠额（仅统计，不毁约）
        view = self._window_view(db, window)
        if view["deficit"] > 0:
            self.store.log(db, "DEFICIT_OPENED", airport=window["airport"],
                           start=window["window_start"], end=window["window_end"],
                           to_version=window["version"],
                           detail={"deficit": view["deficit"], "limit": window["limit_slots"],
                                   "confirmed": view["confirmed"]})
        # 3) 尝试用释放出的空间清偿欠额并自动回填候补
        self._autofill(db, window)

    # ------------------------------------------------------------- 保障申请
    def apply(self, payload: dict) -> dict:
        idem_key = str(payload.get("idempotency_key") or payload.get("request_id") or
                       _stable_key(payload)).strip()
        request_id = str(payload.get("request_id") or f"REQ-{uuid.uuid4().hex[:12]}")
        with self.store.lock, self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                existing = self.store.get_request_by_idem(db, idem_key)
                if existing:
                    db.commit()
                    return self._request_view(existing)
                seq = self.store.next_request_seq(db)
                try:
                    legs = build_chain(payload["legs"])
                    links = transfer_buckets(legs)
                    buckets = self._resolve_buckets(db, links)
                except DomainError:
                    raise  # 容量窗缺失等领域错误直接上抛，不落到 REJECTED
                except ValueError as exc:
                    # 衔接无效 / 航链问题：拒绝且不占容量
                    self._record_rejection(db, request_id, idem_key, seq, payload, str(exc))
                    db.commit()
                    return self._request_view(self.store.get_request(db, request_id))
                # 欠额清偿前暂停新增占位；容量不足整链候补
                versions = sorted({b["window_version"] for b in buckets})
                blocked = [b for b in buckets
                           if self._window_view(db, self.store.get_window(
                               db, b["airport"], b["window_start"], b["window_end"]))["deficit"] > 0]
                insufficient = [b for b in buckets
                                if self._window_view(db, self.store.get_window(
                                    db, b["airport"], b["window_start"], b["window_end"]))["available"] < 1]
                if blocked or insufficient:
                    reason = "WINDOW_DEFICIT_BLOCKED" if blocked else "CAPACITY_INSUFFICIENT"
                    self.store.insert_request(db, request_id, idem_key, "WAITLISTED", seq,
                                              legs, buckets, max(versions), reason)
                    self.store.log(db, "REQUEST_WAITLISTED", request_id=request_id,
                                   to_version=max(versions), detail={"reason": reason})
                    db.commit()
                    return self._request_view(self.store.get_request(db, request_id))
                # 整链获配：每个转机点占一个未确认占位
                for order, bucket in enumerate(buckets, 1):
                    window = self.store.get_window(db, bucket["airport"],
                                                   bucket["window_start"], bucket["window_end"])
                    self.store.insert_slot(db, request_id, bucket["airport"],
                                           bucket["window_start"], bucket["window_end"],
                                           "HELD", window["version"], order)
                    self.store.log(db, "SLOT_HELD", request_id=request_id,
                                   airport=bucket["airport"], start=bucket["window_start"],
                                   end=bucket["window_end"], to_version=window["version"])
                self.store.insert_request(db, request_id, idem_key, "HELD", seq, legs,
                                          buckets, max(versions), None)
                self.store.log(db, "REQUEST_HELD", request_id=request_id,
                               to_version=max(versions))
                db.commit()
                return self._request_view(self.store.get_request(db, request_id))
            except Exception:
                db.rollback()
                raise

    def _record_rejection(self, db, request_id: str, idem_key: str, seq: int,
                          payload: dict, reason: str) -> None:
        if self.store.get_request(db, request_id):
            return
        legs = payload.get("legs", [])
        self.store.insert_request(db, request_id, idem_key, "REJECTED", seq, legs, [],
                                  0, reason)
        self.store.log(db, "REQUEST_REJECTED", request_id=request_id, detail={"reason": reason})

    # ----------------------------------------------------------------- 确认
    def confirm(self, request_id: str, expected_versions: dict | None = None) -> dict:
        with self.store.lock, self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                request = self.store.get_request(db, request_id)
                if request is None:
                    raise DomainError("REQUEST_NOT_FOUND", f"申请 {request_id} 不存在", 404)
                current = self._request_view(request)
                if current["status"] in TERMINAL_STATES:
                    raise DomainError("REQUEST_NOT_ACTIVE",
                                      f"申请已处于 {current['status']}，无法确认", 409)
                # 同一容量版本裁决：调用方版本过期则拒绝整笔确认
                for bucket in current["buckets"]:
                    window = self.store.get_window(db, bucket["airport"],
                                                   bucket["window_start"], bucket["window_end"])
                    if expected_versions:
                        token = f"{bucket['airport']}|{bucket['window_start']}|{bucket['window_end']}"
                        if token in expected_versions and expected_versions[token] != window["version"]:
                            raise DomainError("CAPACITY_VERSION_CONFLICT",
                                              f"{token} 当前版本 {window['version']}", 409)
                if current["status"] == "CONFIRMED":
                    return current  # 幂等
                if current["status"] == "WAITLISTED":
                    # 候补尚未获配，不能确认
                    raise DomainError("REQUEST_STILL_WAITLISTED",
                                      "申请仍在候补，未获配占位，无法确认", 409)
                decided = 0
                for slot in self.store.slots_of(db, request_id):
                    window = self.store.get_window(db, slot["airport"], slot["window_start"],
                                                   slot["window_end"])
                    view = self._window_view(db, window)
                    if view["deficit"] > 0 or view["available"] < 0:
                        raise DomainError("WINDOW_DEFICIT_BLOCKED",
                                          f"{slot['airport']} 窗存在欠额，暂停确认", 409)
                    self.store.set_slot_state(db, request_id, slot["airport"],
                                              slot["window_start"], slot["window_end"],
                                              "CONFIRMED", window["version"])
                    self.store.log(db, "SLOT_CONFIRMED", request_id=request_id,
                                   airport=slot["airport"], start=slot["window_start"],
                                   end=slot["window_end"], to_version=window["version"])
                    decided = max(decided, window["version"])
                self.store.update_status(db, request_id, "CONFIRMED", decided)
                self.store.log(db, "REQUEST_CONFIRMED", request_id=request_id,
                               to_version=decided)
                db.commit()
                return self._request_view(self.store.get_request(db, request_id))
            except Exception:
                db.rollback()
                raise

    # ----------------------------------------------------------------- 撤销
    def cancel(self, request_id: str, reason: str = "USER_CANCEL") -> dict:
        with self.store.lock, self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                request = self.store.get_request(db, request_id)
                if request is None:
                    raise DomainError("REQUEST_NOT_FOUND", f"申请 {request_id} 不存在", 404)
                current = self._request_view(request)
                if current["status"] == "CANCELLED":
                    return current  # 重试不重复归还
                if current["status"] == "REJECTED":
                    raise DomainError("REQUEST_NOT_ACTIVE", "申请已拒绝，无需撤销", 409)
                touched_windows = []
                for slot in self.store.slots_of(db, request_id):
                    if slot["state"] in ACTIVE_STATES:
                        self.store.release_slot(db, request_id, slot["airport"],
                                                slot["window_start"], slot["window_end"],
                                                "RELEASED_CANCEL")
                        self.store.log(db, "SLOT_RELEASED_CANCEL", request_id=request_id,
                                       airport=slot["airport"], start=slot["window_start"],
                                       end=slot["window_end"])
                        touched_windows.append((slot["airport"], slot["window_start"],
                                                slot["window_end"]))
                self.store.update_status(db, request_id, "CANCELLED",
                                         request["decided_version"], reason)
                self.store.log(db, "REQUEST_CANCELLED", request_id=request_id,
                               detail={"reason": reason})
                # 中断后续跑：释放后按 FIFO 回填候补（欠额先清偿）
                seen = set()
                for airport, start, end in touched_windows:
                    key = (airport, start, end)
                    if key in seen:
                        continue
                    seen.add(key)
                    self._autofill(db, self.store.get_window(db, airport, start, end))
                db.commit()
                return self._request_view(self.store.get_request(db, request_id))
            except Exception:
                db.rollback()
                raise

    # ------------------------------------------------- 欠额清偿与候补自动回填
    def _autofill(self, db, window) -> None:
        """按 FIFO 用空闲容量回填候补整链；欠额窗在欠额清偿前不接受新增占位。

        欠额 = 已确认 - 上限，只有已确认占位被撤销（RELEASED_CANCEL）使确认数回落到
        上限以内时欠额才会清偿；在此之前候补不获配。
        """
        if window is None:
            return
        for candidate in self.store.list_waitlist(db):
            view = self._window_view(db, window)
            if view["deficit"] > 0:
                return  # 欠额未清偿，暂停新增占位
            buckets = json.loads(candidate["buckets_json"])
            if not buckets:
                continue
            if not self._chain_fits(db, buckets):
                continue
            decided = 0
            for order, bucket in enumerate(buckets, 1):
                win = self.store.get_window(db, bucket["airport"], bucket["window_start"],
                                            bucket["window_end"])
                self.store.insert_slot(db, candidate["request_id"], bucket["airport"],
                                       bucket["window_start"], bucket["window_end"],
                                       "HELD", win["version"], order)
                self.store.log(db, "SLOT_HELD", request_id=candidate["request_id"],
                               airport=bucket["airport"], start=bucket["window_start"],
                               end=bucket["window_end"], to_version=win["version"])
                decided = max(decided, win["version"])
            self.store.update_status(db, candidate["request_id"], "HELD", decided,
                                     "AUTO_FILLED_FROM_WAITLIST")
            self.store.log(db, "REQUEST_HELD", request_id=candidate["request_id"],
                           to_version=decided, detail={"reason": "AUTOFILL"})

    def _chain_fits(self, db, buckets: list[dict]) -> bool:
        """整链判定：所有转机窗都欠额为零且各有空位，才允许获配。"""
        for bucket in buckets:
            window = self.store.get_window(db, bucket["airport"], bucket["window_start"],
                                           bucket["window_end"])
            if window is None:
                return False
            view = self._window_view(db, window)
            if view["deficit"] > 0 or view["available"] < 1:
                return False
        return True

    # ----------------------------------------------------------------- 回读
    def snapshot(self) -> dict:
        with self.store.lock, self.store.connect() as db:
            windows = [self._window_view(db, row) for row in self.store.list_windows(db)]
            requests = [self._request_view(row) for row in self.store.list_requests(db)]
            deficits = [
                {"airport": w["airport"], "window_start": w["window_start"],
                 "window_end": w["window_end"], "deficit": w["deficit"],
                 "limit": w["limit"], "confirmed": w["confirmed"],
                 "accepting_new": w["accepting_new"]}
                for w in windows if w["deficit"] > 0
            ]
            events = [dict(row) for row in self.store.list_events(db)]
            counts = {"HELD": 0, "WAITLISTED": 0, "CONFIRMED": 0, "CANCELLED": 0,
                      "REJECTED": 0}
            for request in requests:
                counts[request["status"]] = counts.get(request["status"], 0) + 1
            return {
                "windows": windows,
                "requests": requests,
                "counts": counts,
                "deficits": deficits,
                "progress": {
                    "held": counts["HELD"],
                    "confirmed": counts["CONFIRMED"],
                    "waitlisted": counts["WAITLISTED"],
                    "released": sum(1 for e in events if e["kind"].startswith("SLOT_RELEASED")),
                    "cancelled": counts["CANCELLED"],
                    "rejected": counts["REJECTED"],
                },
                "events": events,
            }


def _stable_key(payload: dict) -> str:
    legs = payload.get("legs", [])
    return "chain:" + "|".join(
        f"{leg.get('flight_no','')}:{leg.get('departure_local','')}" for leg in legs
    )
