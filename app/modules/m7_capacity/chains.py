"""真实航段接入：复用 M4 的 UTC 换算与 M5 的衔接判定，不改出票与当地时刻解析。"""
from datetime import datetime, timezone
from app.modules.m4_itinerary.builder import build_leg, order_legs
from app.modules.m4_itinerary.validate import validate_chain
from app.modules.m5_connections.rules import required_minutes, connection_result
from app.modules.m5_connections.windows import waiting_minutes
from app.modules.m2_airports.registry import airport_version

MAX_CONNECTION_MINUTES = 720


def parse_utc(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise ValueError("UTC_TIME_REQUIRES_OFFSET")
    return moment.astimezone(timezone.utc)


def utc_key(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def build_chain(rows: list[dict]) -> list[dict]:
    """把真实航段行（M1 采信后的当地时刻结构）换算为带 UTC 的有序航链。"""
    if not rows:
        raise ValueError("EMPTY_CHAIN")
    legs = [build_leg(dict(row)) for row in rows]
    legs = order_legs(legs)
    if not validate_chain(legs):
        raise ValueError("CHAIN_NOT_CONTINUOUS")
    return legs


def check_connection(prev: dict, nxt: dict) -> dict:
    """对相邻航段做 M5 衔接判定；衔接无效整链拒绝。"""
    airport = prev["destination"]
    if nxt["origin"] != airport:
        raise ValueError("CHAIN_NOT_CONTINUOUS")
    arrival = parse_utc(prev["arrival_utc"])
    departure = parse_utc(nxt["departure_utc"])
    wait = waiting_minutes(arrival, departure)
    day = arrival.date().isoformat()
    arrival_terminal = prev.get("arrival_terminal") or airport_version(airport, day)["terminal"]
    departure_terminal = nxt.get("departure_terminal") or airport_version(airport, day)["terminal"]
    required = required_minutes(airport, arrival_terminal, departure_terminal)
    result = connection_result(wait, required, MAX_CONNECTION_MINUTES)
    if result["status"] != "VALID":
        raise ValueError(f"CONNECTION_{result['status']}")
    return {
        "airport": airport,
        "arrival_utc": utc_key(arrival),
        "departure_utc": utc_key(departure),
        "wait_minutes": wait,
        "required_minutes": required,
        "arrival_terminal": arrival_terminal,
        "departure_terminal": departure_terminal,
    }


def transfer_buckets(legs: list[dict]) -> list[dict]:
    """每个转机点按到达 UTC 落入半开容量窗；单航段直飞不占用中转容量。"""
    buckets = []
    for index in range(1, len(legs)):
        link = check_connection(legs[index - 1], legs[index])
        buckets.append(link)
    return buckets
