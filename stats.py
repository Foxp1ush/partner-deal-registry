"""실적 통계: 기간별 추이, 파트너(담당자)별 비교, 단계별 퍼널, 기술 문의 통계.

보는 범위
- 총판 총괄 매니저: 전체. 비교 단위는 파트너.
- 파트너 관리자: 우리 회사만. 비교 단위는 담당자.
- 그 밖의 계정(영업 담당자, 총판 엔지니어)은 볼 수 없다.

숫자 기준
- 신규 등록 = 등록 시각 기준, 반려된 등록(중복)은 세지 않는다.
- 수주 = 수주 시각(won_at) 기준. 금액은 공급가액(VAT 별도), 0은 '금액 미입력'(금액 칸이 생기기 전 계약 고객).
- 퍼널 = 기간 안에 등록된 고객이 가장 멀리 간 단계. 보류·실패는 따로 센다.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import crm
import db
import tickets
from db import DB_PATH

# 단위 → (화면 이름, 기본으로 보여 줄 구간 수)
UNITS = {"day": ("일", 30), "week": ("주", 12), "month": ("월", 12), "quarter": ("분기", 8), "year": ("년", 5)}
FUNNEL_STAGES = crm.STAGES[:6]  # 명함 확보 → 첫 연락 → 견적·데모 → 구매 검토 → 구매 임박 → 수주
DROPPED = ("보류", "실패")


class StatsError(Exception):
    pass


# ---- 범위 ---------------------------------------------------------------

def _scope(user: dict) -> int | None:
    """볼 수 있는 파트너 id. 총괄 매니저는 None(전체)."""
    if tickets.is_admin_manager(user):
        return None
    if crm.is_manager(user):
        return user["partner_id"]
    raise StatsError("실적 통계는 총판 총괄 매니저와 파트너 관리자만 볼 수 있습니다.")


def _registrations(user: dict, path: Path) -> list[dict]:
    return [r for r in db.list_registrations(_scope(user), path=path) if r["status"] != "rejected"]


def _group_name(user: dict, path: Path):
    """비교 단위: 총판은 파트너 이름, 파트너 관리자는 담당자 이름."""
    if user["role"] == "admin":
        return lambda r: r["partner_name"]
    names = {s["username"]: s["display_name"] or s["username"] for s in db.list_staff(user["partner_id"], path=path)}
    return lambda r: names.get(r["owner"], r["owner"]) if r["owner"] else "담당 미지정"


def _day(ts: str) -> date | None:
    return date.fromisoformat(ts[:10]) if ts else None


def _in(ts: str, start: date, end: date) -> bool:
    d = _day(ts)
    return d is not None and start <= d <= end


# ---- 기간 단위 ------------------------------------------------------------

def period_start(d: date, unit: str) -> date:
    if unit == "day":
        return d
    if unit == "week":
        return d - timedelta(days=d.weekday())  # 월요일
    if unit == "month":
        return d.replace(day=1)
    if unit == "quarter":
        return date(d.year, (d.month - 1) // 3 * 3 + 1, 1)
    if unit == "year":
        return date(d.year, 1, 1)
    raise StatsError("알 수 없는 기간 단위입니다.")


def _previous(start: date, unit: str) -> date:
    """한 구간 앞의 시작일."""
    if unit in ("day", "week"):
        return start - timedelta(days=1 if unit == "day" else 7)
    return period_start(start - timedelta(days=1), unit)


def period_label(start: date, unit: str) -> str:
    if unit == "day":
        return start.strftime("%m-%d")
    if unit == "week":
        return f"{start.strftime('%m-%d')} 주"
    if unit == "month":
        return start.strftime("%Y-%m")
    if unit == "quarter":
        return f"{start.year} {(start.month - 1) // 3 + 1}분기"
    return f"{start.year}"


def periods(unit: str, count: int | None = None, today: date | None = None) -> list[date]:
    """오늘이 든 구간까지, 최근 count개 구간의 시작일 (오래된 것부터)."""
    count = count or UNITS[unit][1]
    starts = [period_start(today or date.today(), unit)]
    while len(starts) < count:
        starts.append(_previous(starts[-1], unit))
    return starts[::-1]


# ---- 통계 ---------------------------------------------------------------

def trend(user: dict, unit: str, count: int | None = None, today: date | None = None,
          path: Path = DB_PATH) -> list[dict]:
    """구간마다 신규 등록 수, 수주 건수, 수주 금액(공급가액)."""
    regs = _registrations(user, path)
    out = []
    for start in periods(unit, count, today):
        rows = [r for r in regs if _day(r["registered_at"]) and period_start(_day(r["registered_at"]), unit) == start]
        won = [r for r in regs if _day(r["won_at"]) and period_start(_day(r["won_at"]), unit) == start]
        out.append({"start": start.isoformat(), "period": period_label(start, unit), "registered": len(rows),
                    "won": len(won), "amount": sum(r["contract_amount"] for r in won)})
    return out


def summary(user: dict, start: date, end: date, path: Path = DB_PATH) -> dict:
    regs = _registrations(user, path)
    registered = sum(_in(r["registered_at"], start, end) for r in regs)
    won = [r for r in regs if _in(r["won_at"], start, end)]
    return {"registered": registered, "won": len(won), "amount": sum(r["contract_amount"] for r in won),
            "no_amount": sum(not r["contract_amount"] for r in won),
            "rate": len(won) / registered if registered else None}


def compare(user: dict, start: date, end: date, path: Path = DB_PATH) -> list[dict]:
    """파트너별(총판) 또는 담당자별(파트너 관리자) 등록·수주·금액·전환율. 금액이 큰 순."""
    name = _group_name(user, path)
    groups: dict[str, dict] = {}
    if user["role"] == "admin":  # 실적이 없는 파트너도 0으로 보이게
        for p in db.list_partners(path=path):
            groups[p["name"]] = {"name": p["name"], "registered": 0, "won": 0, "amount": 0}
    for r in _registrations(user, path):
        g = groups.setdefault(name(r), {"name": name(r), "registered": 0, "won": 0, "amount": 0})
        g["registered"] += _in(r["registered_at"], start, end)
        if _in(r["won_at"], start, end):
            g["won"] += 1
            g["amount"] += r["contract_amount"]
    rows = [{**g, "rate": g["won"] / g["registered"] if g["registered"] else None} for g in groups.values()]
    return sorted(rows, key=lambda g: (-g["amount"], -g["won"], g["name"]))


def funnel(user: dict, start: date, end: date, path: Path = DB_PATH) -> dict:
    """기간 안에 등록된 고객이 각 단계까지 몇 곳 도달했는지. {'stages': [{stage, count}], 'dropped': n}"""
    regs = [r for r in _registrations(user, path) if _in(r["registered_at"], start, end)]
    order = {s: i for i, s in enumerate(FUNNEL_STAGES)}
    furthest = {r["id"]: order.get(r["stage"], 0) for r in regs}
    for r in regs:
        if r["won_at"] or r["status"] in ("won", "lapsed"):
            furthest[r["id"]] = order["수주"]
    for ch in db.list_stage_changes(path=path):
        if ch["registration_id"] in furthest and ch["to_stage"] in order:
            furthest[ch["registration_id"]] = max(furthest[ch["registration_id"]], order[ch["to_stage"]])
    stages = [{"stage": s, "count": sum(v >= i for v in furthest.values())} for i, s in enumerate(FUNNEL_STAGES)]
    return {"stages": stages, "dropped": sum(r["stage"] in DROPPED for r in regs)}


def ticket_stats(user: dict, start: date, end: date, path: Path = DB_PATH) -> dict:
    """기간 안에 접수된 문의의 분류별 수, 첫 답변까지 평균 시간(시간), 지금 열린 문의 수."""
    scope = _scope(user)
    rows = db.list_tickets(scope, path=path)
    picked = [t for t in rows if _in(t["created_at"], start, end)]
    first = db.first_engineer_replies(path=path)
    hours = [(datetime.fromisoformat(first[t["id"]]) - datetime.fromisoformat(t["created_at"])).total_seconds() / 3600
             for t in picked if t["id"] in first]
    return {"by_category": [{"category": c, "count": sum(t["category"] == c for t in picked)}
                            for c in tickets.CATEGORIES],
            "total": len(picked), "avg_first_reply_hours": sum(hours) / len(hours) if hours else None,
            "open": sum(t["status"] != "closed" for t in rows)}


# ---- 금액 표시 ------------------------------------------------------------

def money(amount: int) -> str:
    """읽기 쉬운 금액: 1억 2,300만 원 / 4,500만 원 / 80,000원."""
    if amount >= 100_000_000:
        eok, man = divmod(amount, 100_000_000)
        man //= 10_000
        return f"{eok:,}억 {man:,}만 원" if man else f"{eok:,}억 원"
    if amount >= 10_000:
        return f"{amount // 10_000:,}만 원"
    return f"{amount:,}원"
