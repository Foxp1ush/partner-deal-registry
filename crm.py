"""CRM: 고객사별 영업 단계와 활동 이력. 누가 무엇을 볼 수 있는지도 여기서 정한다.

보는 범위
- 영업 담당자(rep): 자기가 담당한 고객만
- 파트너 관리자(manager): 자기 파트너의 모든 고객 + 직원 관리 + 총판 열람 기록
- 총판 관리자(admin): 평소엔 단계별 집계만. 상세 이력은 사유를 남기고 열람하며, 그 기록은 파트너에게 공개된다

이력 원칙
- 활동 기록은 수정·삭제하지 않는다. 잘못 적었으면 '정정' 기록을 추가한다.
- 같은 파트너가 같은 고객을 다시 등록해도(예: 만료 후 갱신 영업) 이전 이력이 한 타임라인으로 이어진다.
- 기록은 쓴 파트너의 것이다. 등록이 다른 파트너로 이관되거나 파트너 계약이 끝나도 새 파트너에게는
  보이지 않고, 지우지 않고 보존한다(분쟁 근거. 총판은 사유를 남기고만 열람).

수주 정책
- 단계를 '수주'로 바꿀 때 계약 종료일을 받고 '계약 고객(won)'이 된다. 종료일까지 선점이 유지된다(90일 아님).
- 계약을 갱신하면 종료일을 늦춘다. 종료일이 지나도 갱신 기록이 없으면 만료되어 다른 파트너가 등록할 수 있다.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import db
import matching
from db import ADMIN_EDITABLE, DB_PATH

# 영업 단계. 앞의 다섯 개는 총판 기준 Stage 0~4 (파트너 화면에는 이름과 기준만 보이고, 숫자는 총판 화면에만).
# 기준은 파트너마다 해석이 갈리지 않도록 '있었던 일'로 적는다. 예: 첫 연락은 통화가 실제로 연결됐을 때.
STAGES = ["명함 확보", "첫 연락", "견적·데모", "구매 검토", "구매 임박", "수주", "보류", "실패"]
STAGE_CODE = {"명함 확보": "Stage 0", "첫 연락": "Stage 1", "견적·데모": "Stage 2",
              "구매 검토": "Stage 3", "구매 임박": "Stage 4"}
STAGE_GUIDE = {
    "명함 확보": "명함을 주고받았거나 명함만 전달한 상태 (문자·메일만 보낸 경우도 여기)",
    "첫 연락": "받은 명함으로 고객 담당자와 통화가 연결된 상태",
    "견적·데모": "고객이 견적서를 요청했거나 현장 기술 데모를 요청한 상태",
    "구매 검토": "3개월 안에 구매가 예상되거나, 구매팀장·대표 등 결정권자가 검토 중인 상태",
    "구매 임박": "1개월 안에 구매가 확정될 상태",
    "수주": "계약을 체결한 상태 (계약 종료일까지 영업권 유지)",
    "보류": "고객 사정으로 영업이 멈춘 상태 (예산 동결, 담당자 공석 등)",
    "실패": "고객이 다른 제품을 택했거나 구매하지 않기로 한 상태",
}
FORECAST_STAGES = ("구매 검토", "구매 임박")  # 예상 구매일을 받는 단계
IMMINENT_DAYS = 31  # '구매 임박'의 예상 구매일 상한
ACTIVITY_KINDS = ["미팅", "전화", "메일", "데모", "견적 제출", "계약", "갱신", "정정", "기타"]
NOTE_MAX = 500
NOTE_GUIDE = "단가·할인율·고객사 내부 사정 같은 민감한 내용은 적지 마세요. 사실 위주로 짧게 남겨 주세요."


class CrmError(Exception):
    """할 수 없는 요청. 메시지를 그대로 화면에 보여 준다."""


def is_manager(user: dict) -> bool:
    return user["role"] == "partner" and user.get("partner_role") == "manager"


def _customer_key(r: dict) -> tuple:
    nb = matching.normalize_biz_no(r["biz_no"])
    return (r["partner_id"], nb) if nb else (r["partner_id"], "name", matching.normalize_name(r["company_name"]))


def latest_per_customer(rows: list[dict]) -> list[dict]:
    """같은 고객사의 여러 등록(예: 만료 후 재등록) 중 가장 최근 것만. 이력은 timeline()이 합쳐서 보여 준다."""
    latest: dict[tuple, dict] = {}
    for r in rows:
        k = _customer_key(r)
        if k not in latest or r["registered_at"] > latest[k]["registered_at"]:
            latest[k] = r
    return sorted(latest.values(), key=lambda r: r["registered_at"], reverse=True)


def visible_registrations(user: dict, path: Path = DB_PATH) -> list[dict]:
    """로그인한 파트너 직원이 볼 수 있는 고객(고객사별 최신 등록). 반려·해제된 건은 영업 대상이 아니라 뺀다."""
    rows = [r for r in db.list_registrations(user["partner_id"], path=path)
            if r["status"] not in ("rejected", "released")]
    if not is_manager(user):
        rows = [r for r in rows if r["owner"] == user["username"]]
    return latest_per_customer(rows)


def _get_own(user: dict, registration_id: int, path: Path) -> dict:
    row = next((r for r in visible_registrations(user, path) if r["id"] == registration_id), None)
    if row is None:
        raise CrmError("볼 수 있는 고객사가 아닙니다.")
    return row


def related_ids(row: dict, path: Path = DB_PATH) -> list[int]:
    """같은 파트너가 같은 고객사로 남긴 모든 등록 (사업자번호 또는 법인 표기를 뺀 회사명이 같으면 같은 고객)."""
    nb, nn = matching.normalize_biz_no(row["biz_no"]), matching.normalize_name(row["company_name"])
    return [r["id"] for r in db.list_registrations(row["partner_id"], path=path)
            if (nb and matching.normalize_biz_no(r["biz_no"]) == nb) or matching.normalize_name(r["company_name"]) == nn]


def timeline(user: dict, registration_id: int, path: Path = DB_PATH) -> list[dict]:
    row = _get_own(user, registration_id, path)
    return db.list_activities(related_ids(row, path), partner_id=user["partner_id"], path=path)


def add_activity(user: dict, registration_id: int, kind: str, note: str, on_date: str | None = None,
                 path: Path = DB_PATH) -> int:
    _get_own(user, registration_id, path)
    note = (note or "").strip()
    if kind not in ACTIVITY_KINDS:
        raise CrmError("활동 유형을 골라 주세요.")
    if not note:
        raise CrmError("내용을 입력해 주세요.")
    if len(note) > NOTE_MAX:
        raise CrmError(f"내용은 {NOTE_MAX}자까지 적을 수 있습니다.")
    return db.add_activity(registration_id, user["partner_id"], user["username"], kind, note, on_date, path=path)


def _check_contract_end(contract_end: str | None, after: str | None = None) -> str:
    if not contract_end:
        raise CrmError("계약 종료일을 입력해 주세요. 그날까지 영업권이 유지됩니다.")
    end = date.fromisoformat(str(contract_end))
    today = date.today()
    if end <= today:
        raise CrmError("계약 종료일은 오늘 이후여야 합니다.")
    if after and end <= date.fromisoformat(after):
        raise CrmError(f"새 계약 종료일은 지금 종료일({after})보다 뒤여야 합니다.")
    if end > today + timedelta(days=db.CONTRACT_MAX_DAYS):
        raise CrmError(f"계약 종료일은 오늘부터 최대 {db.CONTRACT_MAX_DAYS // 365}년까지 입력할 수 있습니다.")
    return end.isoformat()


def _check_expected_close(stage: str, expected_close: str | None) -> str:
    if not expected_close:
        raise CrmError(f"'{stage}' 단계에는 예상 구매일을 입력해 주세요.")
    d = date.fromisoformat(str(expected_close))
    if d < date.today():
        raise CrmError("예상 구매일은 오늘 이후여야 합니다.")
    if stage == "구매 임박" and d > date.today() + timedelta(days=IMMINENT_DAYS):
        raise CrmError("'구매 임박'은 1개월 안에 구매가 확정될 때입니다. 더 늦다면 '구매 검토'를 골라 주세요.")
    return d.isoformat()


def is_overdue(row: dict) -> bool:
    """예상 구매일이 지났는데 아직 '구매 검토'·'구매 임박'에 머물러 있는지."""
    return (row["stage"] in FORECAST_STAGES and bool(row.get("expected_close"))
            and row["expected_close"] < date.today().isoformat())


def set_stage(user: dict, registration_id: int, stage: str, contract_end: str | None = None,
              expected_close: str | None = None, path: Path = DB_PATH) -> None:
    """영업 단계를 바꾼다.

    - '구매 검토'·'구매 임박'은 예상 구매일이 필요하다(같은 단계에서 날짜만 바꾸는 것도 가능).
    - '수주'로 바꾸면 계약 종료일까지 영업권을 유지하는 계약 고객이 된다.
    """
    row = _get_own(user, registration_id, path)
    if stage not in STAGES:
        raise CrmError("알 수 없는 단계입니다.")
    fields, note = {"stage": stage}, f"{row['stage']} → {stage}"
    if stage in FORECAST_STAGES:
        exp = _check_expected_close(stage, expected_close)
        if stage == row["stage"] and exp == row["expected_close"]:
            return
        fields["expected_close"] = exp
        note = (f"{stage} 예상 구매일 {row['expected_close'] or '미입력'} → {exp}" if stage == row["stage"]
                else f"{note} (예상 구매일 {exp})")
    elif stage == row["stage"]:
        return
    else:
        fields["expected_close"] = ""
    if stage == "수주" and row["status"] != "won":
        if row["status"] == "pending":
            raise CrmError("총판 검토가 끝난 뒤에 수주 처리할 수 있습니다.")
        if row["status"] != "active":
            raise CrmError("보호 기간이 끝난 고객입니다. 다시 등록한 뒤 수주 처리해 주세요.")
        end = _check_contract_end(contract_end)
        fields |= {"status": "won", "expires_on": end}
        note += f" (계약 종료일 {end}까지 영업권 유지)"
        db.log("won", user["partner_name"], registration_id,
               f"{row['company_name']} 수주 — 계약 종료일 {end}까지 보호 (기존 만료일 {row['expires_on']})", path=path)
    db.update_registration(registration_id, fields, allowed=ADMIN_EDITABLE, path=path)
    db.add_activity(registration_id, user["partner_id"], user["username"], "단계 변경", note, path=path)


def renew_contract(user: dict, registration_id: int, new_end: str, path: Path = DB_PATH) -> str:
    """계약 고객의 계약을 갱신해 계약 종료일을 늦춘다. 새 종료일을 돌려준다."""
    row = _get_own(user, registration_id, path)
    if row["status"] != "won":
        raise CrmError("계약 고객만 갱신할 수 있습니다. 먼저 단계를 '수주'로 바꿔 주세요.")
    end = _check_contract_end(new_end, after=row["expires_on"])
    db.update_registration(registration_id, {"expires_on": end}, allowed=ADMIN_EDITABLE, path=path)
    db.add_activity(registration_id, user["partner_id"], user["username"], "갱신",
                    f"계약 갱신: 종료일 {row['expires_on']} → {end}", path=path)
    db.log("renew", user["partner_name"], registration_id,
           f"{row['company_name']} 계약 갱신 — 종료일 {row['expires_on']}→{end}", path=path)
    return end


# ---- 직원 관리 (파트너 관리자) --------------------------------------------

def _need_manager(user: dict) -> None:
    if not is_manager(user):
        raise CrmError("파트너 관리자만 할 수 있습니다.")


def _staff(user: dict, username: str, path: Path) -> dict:
    s = next((s for s in db.list_staff(user["partner_id"], path=path) if s["username"] == username), None)
    if s is None:
        raise CrmError("우리 회사 직원 계정이 아닙니다.")
    return s


def reassign(user: dict, registration_id: int, new_owner: str, path: Path = DB_PATH) -> None:
    """고객 담당자를 바꾼다. 이력은 그대로 남고, 새 담당자가 이어서 본다."""
    _need_manager(user)
    row = _get_own(user, registration_id, path)
    target = _staff(user, new_owner, path)
    if not target["active"]:
        raise CrmError("퇴사 처리된 직원에게는 맡길 수 없습니다.")
    if row["owner"] == new_owner:
        return
    db.update_registration(registration_id, {"owner": new_owner}, allowed=ADMIN_EDITABLE, path=path)
    db.add_activity(registration_id, user["partner_id"], user["username"], "담당자 변경",
                    f"{row['owner'] or '미지정'} → {new_owner}", path=path)


def deactivate(user: dict, username: str, handover_to: str, path: Path = DB_PATH) -> int:
    """퇴사 처리: 로그인을 막고 담당 고객을 다른 직원에게 넘긴다. 넘긴 고객 수를 돌려준다."""
    _need_manager(user)
    leaving = _staff(user, username, path)
    if username == user["username"]:
        raise CrmError("자기 자신은 퇴사 처리할 수 없습니다.")
    if not leaving["active"]:
        raise CrmError("이미 퇴사 처리된 계정입니다.")
    if handover_to == username:
        raise CrmError("인계받을 직원을 다른 사람으로 골라 주세요.")
    target = _staff(user, handover_to, path)
    if not target["active"]:
        raise CrmError("퇴사 처리된 직원에게는 인계할 수 없습니다.")
    db.set_account_active(username, False, path=path)
    # 반려·만료된 건까지 전부 넘긴다 (이력 조회 권한이 퇴사자에게 남지 않도록)
    moved = [r for r in db.list_registrations(user["partner_id"], path=path) if r["owner"] == username]
    for r in moved:
        db.update_registration(r["id"], {"owner": handover_to}, allowed=ADMIN_EDITABLE, path=path)
        db.add_activity(r["id"], user["partner_id"], user["username"], "담당자 변경",
                        f"{username} → {handover_to} (퇴사 인계)", path=path)
    db.log("handover", user["partner_name"], None,
           f"{username} 퇴사 처리, 담당 고객 {len(moved)}곳 → {handover_to}", path=path)
    return len(moved)


# ---- 총판 (집계·사유 열람) ------------------------------------------------

def stage_label(stage: str) -> str:
    """총판 화면용 이름: 'Stage 2 견적·데모'. 0~4 밖의 단계(수주·보류·실패)는 이름 그대로."""
    return f"{STAGE_CODE[stage]} {stage}" if stage in STAGE_CODE else stage


def _live_customers(path: Path) -> list[dict]:
    rows = [r for r in db.list_registrations(path=path) if r["status"] not in ("rejected", "released")]
    return latest_per_customer(rows)


def pipeline(path: Path = DB_PATH) -> dict[str, dict[str, int]]:
    """파트너별·단계별 고객 수. 총판은 평소 이것만 본다(고객사명·메모 없음)."""
    out: dict[str, dict[str, int]] = {p["name"]: {s: 0 for s in STAGES} for p in db.list_partners(path=path)}
    for r in _live_customers(path):
        out[r["partner_name"]][r["stage"]] += 1
    return out


def forecast(path: Path = DB_PATH) -> dict[str, dict[str, int]]:
    """파트너별 구매 예상 건수: 1개월 안(Stage 4) / 3개월 안·결정권자 검토(Stage 3) / 예상일 지남."""
    out = {p["name"]: {"1개월 안 (Stage 4)": 0, "3개월 안 (Stage 3)": 0, "예상일 지남": 0}
           for p in db.list_partners(path=path)}
    for r in _live_customers(path):
        if r["stage"] not in FORECAST_STAGES:
            continue
        if is_overdue(r):
            out[r["partner_name"]]["예상일 지남"] += 1
        else:
            out[r["partner_name"]]["1개월 안 (Stage 4)" if r["stage"] == "구매 임박" else "3개월 안 (Stage 3)"] += 1
    return out


def admin_open_detail(viewer: str, registration_id: int, reason: str, path: Path = DB_PATH) -> list[dict]:
    """총판의 상세 열람. 사유가 있어야 열리고, 열람 기록이 해당 파트너 관리자에게 보인다."""
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise CrmError("열람 사유를 5자 이상 적어 주세요. 이 사유는 해당 파트너에게 공개됩니다.")
    row = db.get_registration(registration_id, path=path)
    if row is None:
        raise CrmError("등록을 찾을 수 없습니다.")
    db.log_access(viewer, row["partner_id"], registration_id, reason, path=path)
    return db.list_activities(related_ids(row, path), partner_id=row["partner_id"], path=path)
