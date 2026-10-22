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

수주 정책 (ENCY CAM 라이선스)
- 가망 고객의 '영업 보호'(90일, deal registration)와 계약 고객의 '담당 파트너'(partner of record)는 다르다.
  계약 고객은 이미 산 고객이라 보호 '기간'이 없다. 판 파트너가 담당으로 남고, 유지보수·구독 종료일은
  연장·갱신 영업 시점일 뿐이다. 담당이 바뀌는 건 고객 요청(총판 이관), 파트너 계약 종료, 구독 미갱신 때뿐이다.
- 수주 때 라이선스 종류·라이선스 시작일·고객사 ENCY 계정 이메일을 꼭 받는다. 계약일과 시작일은 다를 수 있다.
- 영구 라이선스: 시작일부터 1년은 유지보수(최신 버전 업데이트)가 무료. 그 뒤엔 계속 쓸 수 있지만 업데이트는 안 되고,
  고객이 2~3년 뒤에 유지보수 비용을 내고 다시 가입하는 경우가 많다.
  → 유지보수가 끝나면 'lapsed(유지보수 만료·사용 중)'. 담당 파트너는 그대로, 재가입 영업 대상.
- 1년·3년 구독: 구독이 끝나면 쓸 수 없다. 끝나면 'lapsed(갱신 유예)'로 90일 동안 담당 유지, 그 뒤 갱신이 없으면 만료.
- 다른 파트너가 담당 고객을 등록하려 하면 자동 차단 대신 총판 검토로 간다(고객이 파트너 변경을 원할 수 있음).
- 유지보수 연장·재가입, 구독 갱신은 renew_contract()로 종료일을 늦춘다.

예상 구매일
- '구매 임박'(1개월 안)은 날짜가 단계의 뜻이라 필수. '구매 검토'는 결정권자 검토처럼 시기를 모를 수 있어 비워도 된다.
"""

from __future__ import annotations

import re
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
    "수주": "계약을 체결한 상태 (담당 고객으로 계속 관리, 보호 기간 없음)",
    "보류": "고객 사정으로 영업이 멈춘 상태 (예산 동결, 담당자 공석 등)",
    "실패": "고객이 다른 제품을 택했거나 구매하지 않기로 한 상태",
}
FORECAST_STAGES = ("구매 검토", "구매 임박")  # 예상 구매일을 받는 단계
EXPECTED_REQUIRED = ("구매 임박",)          # 예상 구매일이 꼭 있어야 하는 단계
IMMINENT_DAYS = 31  # '구매 임박'의 예상 구매일 상한
LICENSE_TYPES = {k: v[0] for k, v in db.LICENSE_TYPES.items()}  # perpetual → '영구', sub1 → '1년 구독', …
ACTIVITY_KINDS = ["미팅", "전화", "메일", "데모", "견적 제출", "계약", "갱신", "정정", "기타"]
NOTE_MAX = 500
NOTE_GUIDE = "고객사 내부 사정 같은 민감한 내용은 적지 마세요. 사실 위주로 짧게 남겨 주세요."


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


def search(rows: list[dict], query: str = "", stages: list[str] | None = None) -> list[dict]:
    """영업 관리의 고객사 검색. 회사명(법인 표기·띄어쓰기 무시), 사업자번호·연락처 숫자, 고객 담당자 이름으로 찾는다.

    stages를 주면 그 단계의 고객만. 검색어가 비어 있으면 단계 조건만 적용한다.
    """
    q = (query or "").strip()
    qn, qd = matching.normalize_name(q), re.sub(r"\D", "", q)
    out = []
    for r in rows:
        if stages and r["stage"] not in stages:
            continue
        if q and not ((qn and qn in matching.normalize_name(r["company_name"]))
                      or (len(qd) >= 3 and (qd in matching.normalize_biz_no(r["biz_no"])
                                            or qd in re.sub(r"\D", "", r["contact_phone"])))
                      or q in r["contact_name"]):
            continue
        out.append(r)
    return out


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
    """유지보수 종료일 검사 (수주·유지보수 연장 공통)."""
    if not contract_end:
        raise CrmError("유지보수 종료일을 입력해 주세요. 그날까지 영업권이 유지됩니다.")
    end = date.fromisoformat(str(contract_end))
    today = date.today()
    if end <= today:
        raise CrmError("유지보수 종료일은 오늘 이후여야 합니다.")
    if after and end <= date.fromisoformat(after):
        raise CrmError(f"새 유지보수 종료일은 지금 종료일({after})보다 뒤여야 합니다.")
    if end > today + timedelta(days=db.CONTRACT_MAX_DAYS):
        raise CrmError(f"유지보수 종료일은 오늘부터 최대 {db.CONTRACT_MAX_DAYS // 365}년까지 입력할 수 있습니다.")
    return end.isoformat()


def license_end(license_type: str, license_start: str) -> tuple[str, str]:
    """(라이선스 시작일, 보호 종료일). 영구는 시작일 + 1년(무상 유지보수), 구독은 시작일 + 구독 기간."""
    start = date.fromisoformat(str(license_start))
    return start.isoformat(), (start + timedelta(days=db.LICENSE_TYPES[license_type][1])).isoformat()


def end_label(row: dict) -> str:
    """종료일의 이름: 영구는 '유지보수 종료', 구독은 '구독 종료'."""
    return "유지보수 종료" if db.is_perpetual(row) else "구독 종료"


def grace_end(row: dict) -> str:
    """구독이 끝난 고객의 갱신 유예 마지막 날 (종료일 + 90일)."""
    return (date.fromisoformat(row["expires_on"]) + timedelta(days=db.SUB_GRACE_DAYS)).isoformat()


def relation(row: dict) -> str:
    """이 고객과 우리의 관계를 한 줄로. 가망 고객은 '영업 보호 기간', 계약 고객은 '담당 고객 + 다음 영업 시점'."""
    d = (date.fromisoformat(row["expires_on"]) - date.today()).days
    if row["status"] in ("active", "pending"):
        return f"영업 보호 ~{row['expires_on']} (D-{d})"
    if row["status"] == "won":
        return f"담당 고객 · {end_label(row)} {row['expires_on']} (연장 영업 D-{d})"
    if row["status"] == "lapsed":
        if db.is_perpetual(row):
            return f"담당 고객 · 유지보수 만료 {row['expires_on']} (재가입 영업 대상)"
        return f"담당 고객 · 구독 종료 {row['expires_on']} (갱신 유예 ~{grace_end(row)})"
    return "-"


def renewal_default(row: dict, years: int = 1) -> str:
    """연장 기본 종료일. 유지보수 중·구독 중이면 지금 종료일 + n년, 유지보수가 이미 끝났으면(재가입) 오늘 + n년."""
    base = date.fromisoformat(row["expires_on"])
    if row["status"] == "lapsed" or base < date.today():
        base = date.today()
    return (base + timedelta(days=365 * years)).isoformat()


VAT_NOTE = "(공급가액, VAT 10% 별도)"
AMOUNT_MAX = 10_000_000_000  # 100억 원. 자릿수를 잘못 넣는 실수를 막는 상한


def won_text(amount: int) -> str:
    """금액을 '12,000,000원'처럼. 0은 미입력(금액 칸이 생기기 전의 계약 고객)."""
    return f"{amount:,}원" if amount else "금액 미입력"


def check_amount(amount) -> int:
    """수주 계약 금액(공급가액 · VAT 별도). 1원 이상 100억 원 이하의 정수."""
    try:
        value = int(amount)
    except (TypeError, ValueError):
        raise CrmError(f"계약 금액을 입력해 주세요 {VAT_NOTE}.") from None
    if value <= 0:
        raise CrmError(f"계약 금액을 입력해 주세요 {VAT_NOTE}.")
    if value > AMOUNT_MAX:
        raise CrmError("계약 금액이 너무 큽니다. 자릿수를 확인해 주세요.")
    return value


_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def check_ency_account(email: str | None) -> str:
    """고객사 ENCY 계정 이메일 확인. 총판이 이 이메일로 ENCY 계정을 발급한다."""
    email = (email or "").strip()
    if not email:
        raise CrmError("고객사가 사용할 ENCY 계정 이메일을 입력해 주세요. 총판이 이 이메일로 계정을 발급합니다.")
    if not _EMAIL.match(email):
        raise CrmError("ENCY 계정 이메일 형식이 올바르지 않습니다 (예: cam@company.co.kr).")
    return email


def _check_expected_close(stage: str, expected_close: str | None) -> str:
    if not expected_close:
        if stage in EXPECTED_REQUIRED:
            raise CrmError(f"'{stage}' 단계에는 예상 구매일을 입력해 주세요.")
        return ""  # '구매 검토'는 시기를 몰라도 된다
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
              expected_close: str | None = None, license_start: str | None = None,
              ency_account: str | None = None, license_type: str | None = None,
              contract_amount: int | None = None, path: Path = DB_PATH) -> None:
    """영업 단계를 바꾼다.

    - '구매 임박'은 예상 구매일이 필요하고, '구매 검토'는 선택이다(같은 단계에서 날짜만 바꾸는 것도 가능).
    - '수주'로 바꾸면 계약 고객이 된다. 필수: 라이선스 종류, 라이선스 시작일, 고객사 ENCY 계정 이메일,
      계약 금액(공급가액 · VAT 별도).
      종료일은 영구 = 시작일 + 1년(무상 유지보수), 구독 = 시작일 + 구독 기간.
      contract_end를 직접 주면 그 날짜를 쓴다(이미 연장한 고객 등).
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
        note = (f"{stage} 예상 구매일 {row['expected_close'] or '미입력'} → {exp or '미입력'}" if stage == row["stage"]
                else f"{note} (예상 구매일 {exp})" if exp else note)
    elif stage == row["stage"]:
        return
    else:
        fields["expected_close"] = ""
    if stage == "수주" and row["status"] not in ("won", "lapsed"):  # lapsed는 '유지보수 재가입'으로 처리
        if row["status"] == "pending":
            raise CrmError("총판 검토가 끝난 뒤에 수주 처리할 수 있습니다.")
        if row["status"] != "active":
            raise CrmError("보호 기간이 끝난 고객입니다. 다시 등록한 뒤 수주 처리해 주세요.")
        if license_type not in db.LICENSE_TYPES:
            raise CrmError("라이선스 종류(영구 / 1년 구독 / 3년 구독)를 골라 주세요.")
        if not license_start:
            raise CrmError("라이선스 시작일을 입력해 주세요. 영구는 유지보수 종료일(시작일 + 1년), "
                           "구독은 구독 종료일까지 영업권이 유지됩니다.")
        email = check_ency_account(ency_account)
        amount = check_amount(contract_amount)
        start, term_end = license_end(license_type, license_start)
        kind = LICENSE_TYPES[license_type]
        what = "유지보수 종료일" if license_type == "perpetual" else "구독 종료일"
        try:
            end = _check_contract_end(contract_end or term_end)
        except CrmError:
            if contract_end:
                raise
            if date.fromisoformat(term_end) <= date.today():
                raise CrmError(f"라이선스 시작일({start})로부터 {kind} 기간이 이미 지났습니다. "
                               "시작일을 다시 확인해 주세요.") from None
            raise CrmError(f"라이선스 시작일이 너무 먼 미래입니다. {what}이 오늘부터 "
                           f"{db.CONTRACT_MAX_DAYS // 365}년을 넘을 수 없습니다.") from None
        fields |= {"status": "won", "expires_on": end, "license_start": start, "ency_account": email,
                   "license_type": license_type, "contract_amount": amount, "won_at": db.now_iso()}
        note += (f" ({kind}, 계약 금액 {won_text(amount)} {VAT_NOTE}, 라이선스 시작 {start}, "
                 f"{what} {end}까지 영업권 유지, ENCY 계정 {email})")
        db.log("won", user["partner_name"], registration_id,
               f"{row['company_name']} 수주 — {kind}, 계약 금액 {won_text(amount)} {VAT_NOTE}, "
               f"라이선스 시작 {start}, {what} {end}까지 보호, "
               f"ENCY 계정 발급 요청 {email} (기존 만료일 {row['expires_on']})", path=path)
    db.update_registration(registration_id, fields, allowed=ADMIN_EDITABLE, path=path)
    db.add_activity(registration_id, user["partner_id"], user["username"], "단계 변경", note, path=path)
    if stage != row["stage"]:
        db.add_stage_change(registration_id, user["partner_id"], row["stage"], stage, user["username"], path=path)


def renew_label(row: dict) -> str:
    """연장 동작의 이름: 영구 = 유지보수 연장(유지보수 중) / 유지보수 재가입(만료 후), 구독 = 구독 갱신."""
    if not db.is_perpetual(row):
        return "구독 갱신"
    return "유지보수 재가입" if row["status"] == "lapsed" else "유지보수 연장"


def renew_contract(user: dict, registration_id: int, new_end: str, path: Path = DB_PATH) -> str:
    """유지보수 연장·재가입, 구독 갱신: 종료일을 늦춘다. 재가입이면 다시 계약 고객(won)이 된다. 새 종료일을 돌려준다."""
    row = _get_own(user, registration_id, path)
    if row["status"] not in ("won", "lapsed"):
        raise CrmError("계약 고객만 연장할 수 있습니다. 먼저 단계를 '수주'로 바꿔 주세요.")
    action, label = renew_label(row), end_label(row)
    end = _check_contract_end(new_end, after=row["expires_on"])
    db.update_registration(registration_id, {"expires_on": end, "status": "won"}, allowed=ADMIN_EDITABLE, path=path)
    db.add_activity(registration_id, user["partner_id"], user["username"], "갱신",
                    f"{action}: {label}일 {row['expires_on']} → {end}", path=path)
    db.log("renew", user["partner_name"], registration_id,
           f"{row['company_name']} {action} — {label}일 {row['expires_on']}→{end}", path=path)
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
    """파트너별 구매 예상 건수: 1개월 안(Stage 4) / 3개월 안(Stage 3) / Stage 3 시기 미정 / 예상일 지남."""
    out = {p["name"]: {"1개월 안 (Stage 4)": 0, "3개월 안 (Stage 3)": 0, "시기 미정 (Stage 3)": 0, "예상일 지남": 0}
           for p in db.list_partners(path=path)}
    for r in _live_customers(path):
        if r["stage"] not in FORECAST_STAGES:
            continue
        if is_overdue(r):
            key = "예상일 지남"
        elif r["stage"] == "구매 임박":
            key = "1개월 안 (Stage 4)"
        else:
            key = "3개월 안 (Stage 3)" if r["expected_close"] else "시기 미정 (Stage 3)"
        out[r["partner_name"]][key] += 1
    return out


def customer_ids(row: dict, path: Path = DB_PATH) -> list[int]:
    """모든 파트너를 통틀어 같은 고객사인 등록 (related_ids의 전체 파트너판). 이관 전 기록까지 찾을 때 쓴다."""
    nb, nn = matching.normalize_biz_no(row["biz_no"]), matching.normalize_name(row["company_name"])
    return [r["id"] for r in db.list_registrations(path=path)
            if (nb and matching.normalize_biz_no(r["biz_no"]) == nb) or matching.normalize_name(r["company_name"]) == nn]


def admin_open_detail(viewer: str, registration_id: int, reason: str, path: Path = DB_PATH) -> list[dict]:
    """총판의 상세 열람. 사유가 있어야 열리고, 열람 기록이 기록을 쓴 파트너 모두의 관리자에게 보인다.

    이관된 고객이면 이전 파트너가 남긴 기록도 함께 보여 준다(분쟁 확인용). 새 파트너 화면에는 여전히 보이지 않는다.
    각 기록에 partner_name(기록을 쓴 파트너)을 붙인다.
    """
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise CrmError("열람 사유를 5자 이상 적어 주세요. 이 사유는 해당 파트너에게 공개됩니다.")
    row = db.get_registration(registration_id, path=path)
    if row is None:
        raise CrmError("등록을 찾을 수 없습니다.")
    items = db.list_activities(customer_ids(row, path), path=path)
    names = {p["id"]: p["name"] for p in db.list_partners(path=path)}
    for pid in dict.fromkeys([row["partner_id"]] + [a["partner_id"] for a in items]):
        db.log_access(viewer, pid, registration_id, reason, path=path)
    return [{**a, "partner_name": names.get(a["partner_id"], "")} for a in items]
