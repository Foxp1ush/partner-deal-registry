"""등록 처리 흐름: 판정 규칙(matching)과 저장소(db)를 묶어 등록 결과를 결정한다.

결과
- invalid: 사업자등록번호 형식 오류 → 저장하지 않음
- blocked: 같은 회사가 이미 선점 중 → 저장하지 않음
- pending: 비슷한 회사가 있음 → 관리자 승인 대기로 저장 (선점 효력은 유지)
- active : 걸리는 것 없음 → 바로 등록

※ 실제 운영 대비용. 아직 app.py는 이 모듈을 쓰지 않는다(데모 화면은 자체 흐름 사용).
  app.py와 다른 점: AI가 '다른 회사'라 해도 관리자 검토로 보냄(app.py는 바로 등록), AI 한도 50회(app.py는 30회).
  연결할 때 이 차이를 어느 쪽으로 맞출지 먼저 정한다.
"""

from __future__ import annotations

from pathlib import Path

import ai_extract
import db
import matching
from db import DB_PATH

AI_DAILY_LIMIT = 50  # 하루 AI 호출 상한 (비용 관리)


def ai_ready(path: Path = DB_PATH) -> bool:
    return ai_extract.available() and db.ai_calls_today(path=path) < AI_DAILY_LIMIT


def _partner_name(partner_id: int, path: Path) -> str:
    return next((p["name"] for p in db.list_partners(path=path) if p["id"] == partner_id), "")


def submit(partner_id: int, company_name: str, biz_no: str = "", contact_name: str = "",
           contact_phone: str = "", note: str = "", use_ai: bool = True, path: Path = DB_PATH) -> dict:
    company_name = (company_name or "").strip()
    biz_no = (biz_no or "").strip()
    partner = _partner_name(partner_id, path)
    result = {"outcome": "", "message": "", "registration_id": None, "similar": [], "ai": []}

    if not company_name:
        return {**result, "outcome": "invalid", "message": "회사명을 입력해 주세요."}
    if biz_no and not matching.is_valid_biz_no(biz_no):
        return {**result, "outcome": "invalid", "message": "사업자등록번호가 올바르지 않습니다. 다시 확인해 주세요."}
    biz_no = matching.format_biz_no(biz_no) if biz_no else ""

    db.expire_overdue(path=path)
    conflicts = matching.find_conflicts(company_name, biz_no, db.holding_registrations(path=path))

    if conflicts["exact"]:
        row, reason = conflicts["exact"][0]
        mine = row["partner_id"] == partner_id
        message = ("이미 등록하신 고객사입니다." if mine
                   else "다른 파트너가 이미 선점 중인 고객사입니다. 등록할 수 없습니다.")
        db.log("blocked", partner, row["id"], f"{company_name} 차단 ({reason}, 기존: {row['partner_name']})", path=path)
        return {**result, "outcome": "blocked", "message": f"{message} ({reason})"}

    if conflicts["similar"]:
        similar = conflicts["similar"]
        lines = [f"비슷한 기존 등록: {row['company_name']} ({row['partner_name']}, 유사도 {score})"
                 for row, score in similar]
        ai_judgments = []
        if use_ai and ai_ready(path):
            try:
                ai_judgments = ai_extract.judge_same_company(company_name, biz_no, [row for row, _ in similar])
                db.log("ai_call", partner, None, f"동일회사 판단: {company_name}", path=path)
            except Exception as e:  # AI 실패는 등록을 막지 않는다
                db.log("ai_call", partner, None, f"동일회사 판단 실패: {e}", path=path)
        for j in ai_judgments:
            if 0 <= j["index"] < len(similar):
                verdict = "같은 회사 가능성 높음" if j["likely_same"] else "다른 회사로 보임"
                lines.append(f"AI 의견({similar[j['index']][0]['company_name']}): {verdict} — {j['reason']}")
        rid = db.add_registration(partner_id, company_name, biz_no, contact_name, contact_phone, note,
                                  "pending", review_reason="\n".join(lines), path=path)
        db.log("pending", partner, rid, f"{company_name} 승인 대기 (비슷한 회사 {len(similar)}건)", path=path)
        return {**result, "outcome": "pending", "registration_id": rid,
                "similar": [row["company_name"] for row, _ in similar], "ai": ai_judgments,
                "message": "비슷한 이름의 고객사가 있어 관리자 확인 후 등록됩니다. 그동안에도 선점 효력은 유지됩니다."}

    rid = db.add_registration(partner_id, company_name, biz_no, contact_name, contact_phone, note,
                              "active", path=path)
    db.log("register", partner, rid, f"{company_name} 등록", path=path)
    return {**result, "outcome": "active", "registration_id": rid,
            "message": f"등록되었습니다. {db.PROTECTION_DAYS}일간 영업권이 보호됩니다."}


def _decide(registration_id: int, status: str, kind: str, label: str, path: Path) -> None:
    row = next(r for r in db.list_registrations(path=path) if r["id"] == registration_id)
    db.set_status(registration_id, status, path=path)
    db.log(kind, row["partner_name"], registration_id, f"{row['company_name']} {label}", path=path)


def approve(registration_id: int, path: Path = DB_PATH) -> None:
    _decide(registration_id, "active", "approve", "승인", path)


def reject(registration_id: int, path: Path = DB_PATH) -> None:
    _decide(registration_id, "rejected", "reject", "반려", path)


def extract_card(image_bytes: bytes, media_type: str, partner: str = "", path: Path = DB_PATH) -> dict:
    """명함·사업자등록증 이미지에서 입력값을 뽑는다. 호출 이력을 남긴다."""
    if not ai_ready(path):
        raise RuntimeError("AI를 사용할 수 없습니다(API 키 없음 또는 오늘 한도 초과). 직접 입력해 주세요.")
    try:
        data = ai_extract.extract_from_image(image_bytes, media_type)
    except Exception as e:
        db.log("ai_call", partner, None, f"이미지 추출 실패: {e}", path=path)
        raise
    db.log("ai_call", partner, None, f"이미지 추출: {data.get('company_name', '')}", path=path)
    return data
