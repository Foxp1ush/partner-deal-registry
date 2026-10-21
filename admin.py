"""총판 관리자 조치: 해제, 기간 연장, 이관, 회사명 수정, 반려 되돌리기.

모든 조치는 사유를 받아 이력에 남긴다(분쟁 시 근거).
다른 파트너의 선점과 겹치게 되는 조치는 막는다.
기간 연장은 횟수 제한이 없다. 대신 등록별·고객사별 누적 연장 횟수를 보여 주고 총판이 판단한다.
계약 고객(won)은 계약 종료일로 보호되므로 기간 연장 대상이 아니다.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import db
import matching
from db import ADMIN_EDITABLE, DB_PATH

ACTOR = "총판 관리자"


class ActionError(Exception):
    """조치를 할 수 없을 때. 메시지를 그대로 화면에 보여 준다."""


def _load(registration_id: int, allowed: tuple[str, ...], path: Path) -> dict:
    row = db.get_registration(registration_id, path=path)
    if row is None:
        raise ActionError("등록을 찾을 수 없습니다.")
    if row["status"] not in allowed:
        raise ActionError(f"현재 상태({row['status']})에서는 할 수 없는 조치입니다.")
    return row


def _need_reason(reason: str) -> str:
    reason = (reason or "").strip()
    if not reason:
        raise ActionError("사유를 입력해 주세요. 이력에 남아 분쟁 시 근거가 됩니다.")
    return reason


def _exact_conflict(company_name: str, biz_no: str, holdings: list[dict]) -> str | None:
    found = matching.find_conflicts(company_name, biz_no, holdings)["exact"]
    if not found:
        return None
    row, why = found[0]
    return f"{row['partner_name']}의 '{row['company_name']}'와 같은 회사입니다({why})"


def _holdings_except(registration_id: int, path: Path) -> list[dict]:
    return [h for h in db.holding_registrations(path=path) if h["id"] != registration_id]


def _save(row: dict, fields: dict, kind: str, detail: str, partner: str | None = None, path: Path = DB_PATH) -> None:
    db.update_registration(row["id"], fields, allowed=ADMIN_EDITABLE, path=path)
    db.log(kind, partner or row["partner_name"], row["id"], f"[{ACTOR}] {detail}", path=path)


def release(registration_id: int, reason: str, path: Path = DB_PATH) -> None:
    """보호 중·검토 대기·계약 고객 등록을 해제한다. 해제되면 다른 파트너가 등록할 수 있다."""
    row = _load(registration_id, ("active", "pending", "won"), path)
    reason = _need_reason(reason)
    _save(row, {"status": "released"}, "release", f"{row['company_name']} 등록 해제 — {reason}", path=path)


def extend(registration_id: int, days: int, reason: str, path: Path = DB_PATH) -> str:
    """보호 중인 등록의 만료일을 늘린다. 만료된 건은 그사이 다른 파트너가 잡았을 수 있어 대상이 아니다."""
    row = _load(registration_id, ("active",), path)
    reason = _need_reason(reason)
    if days <= 0:
        raise ActionError("연장 일수는 1일 이상이어야 합니다.")
    new_exp = (date.fromisoformat(row["expires_on"]) + timedelta(days=days)).isoformat()
    _save(row, {"expires_on": new_exp}, "extend",
          f"{row['company_name']} 보호기간 {days}일 연장 ({row['expires_on']}→{new_exp}) — {reason}", path=path)
    return new_exp


def transfer(registration_id: int, to_partner_id: int, reason: str, path: Path = DB_PATH) -> None:
    """다른 파트너에게 이관한다. 보호 기간(계약 고객은 계약 종료일)은 그대로 유지한다."""
    row = _load(registration_id, ("active", "won"), path)
    reason = _need_reason(reason)
    target = next((p for p in db.list_partners(path=path) if p["id"] == to_partner_id), None)
    if target is None:
        raise ActionError("받을 파트너를 찾을 수 없습니다.")
    if not target["active"]:
        raise ActionError("계약 종료된 파트너사에는 이관할 수 없습니다.")
    if to_partner_id == row["partner_id"]:
        raise ActionError("이미 이 파트너의 등록입니다.")
    target_holdings = [h for h in _holdings_except(registration_id, path) if h["partner_id"] == to_partner_id]
    if msg := _exact_conflict(row["company_name"], row["biz_no"], target_holdings):
        raise ActionError(f"이관할 수 없습니다. 받는 파트너에게 이미 등록이 있습니다: {msg}")
    detail = f"{row['company_name']} 이관 {row['partner_name']}→{target['name']} — {reason}"
    # 담당 직원은 받는 파트너의 관리자가 새로 정한다. 이전 파트너의 CRM 기록은 새 파트너에게 보이지 않는다(crm.py).
    _save(row, {"partner_id": to_partner_id, "owner": ""}, "transfer", detail, partner=target["name"], path=path)
    db.log("transfer", row["partner_name"], row["id"], f"[{ACTOR}] {detail}", path=path)  # 넘겨준 쪽 이력에도


def rename(registration_id: int, new_name: str, reason: str, confirm_similar: bool = False,
           path: Path = DB_PATH) -> list[str]:
    """회사명을 바꾼다. 다른 등록과 같아지면 막고, 비슷하기만 하면 confirm_similar=True일 때만 진행한다.

    반환: 확인이 필요한 비슷한 등록 목록 (비어 있으면 변경 완료)
    """
    row = _load(registration_id, ("active", "pending", "won"), path)
    new_name = (new_name or "").strip()
    if not new_name:
        raise ActionError("새 회사명을 입력해 주세요.")
    reason = _need_reason(reason)
    if new_name == row["company_name"]:
        raise ActionError("지금과 같은 회사명입니다.")
    others = _holdings_except(registration_id, path)
    found = matching.find_conflicts(new_name, row["biz_no"], others)
    if found["exact"]:
        other, why = found["exact"][0]
        raise ActionError(f"바꿀 수 없습니다. {other['partner_name']}의 '{other['company_name']}'와 같은 회사가 됩니다({why}).")
    similar = [f"{o['company_name']} ({o['partner_name']}, 유사도 {s})" for o, s in found["similar"]]
    if similar and not confirm_similar:
        return similar
    _save(row, {"company_name": new_name}, "rename",
          f"회사명 수정 '{row['company_name']}'→'{new_name}' — {reason}", path=path)
    return []


def _same_customer(a: dict, b: dict) -> bool:
    na, nb = matching.normalize_biz_no(a["biz_no"]), matching.normalize_biz_no(b["biz_no"])
    return bool(na and na == nb) or matching.normalize_name(a["company_name"]) == matching.normalize_name(b["company_name"])


def extension_counts(path: Path = DB_PATH) -> dict[int, tuple[int, int]]:
    """등록 id → (이 등록의 연장 횟수, 같은 고객사의 모든 등록 누적 연장 횟수).

    누적은 파트너와 상관없이 같은 고객사(사업자번호 또는 법인 표기를 뺀 회사명이 같음)의 모든 등록을 합친다.
    """
    per_reg = db.extension_counts(path=path)
    regs = db.list_registrations(path=path)
    return {r["id"]: (per_reg.get(r["id"], 0), sum(per_reg.get(o["id"], 0) for o in regs if _same_customer(r, o)))
            for r in regs}


def restore(registration_id: int, reason: str, path: Path = DB_PATH) -> None:
    """반려를 취소하고 보호 중으로 되돌린다."""
    row = _load(registration_id, ("rejected",), path)
    reason = _need_reason(reason)
    if row["expires_on"] < date.today().isoformat():
        raise ActionError("보호 기간이 이미 지나 되돌릴 수 없습니다. 파트너가 새로 등록해야 합니다.")
    if msg := _exact_conflict(row["company_name"], row["biz_no"], db.holding_registrations(path=path)):
        raise ActionError(f"되돌릴 수 없습니다. 그사이 등록된 건이 있습니다: {msg}")
    _save(row, {"status": "active"}, "restore", f"{row['company_name']} 반려 취소 → 보호 중 — {reason}", path=path)
