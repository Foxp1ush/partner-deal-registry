"""계정 발급·관리. 회원가입 없이 '초대' 방식으로만 계정이 생긴다.

총판 관리자 → 파트너사 등록 + 파트너 관리자 계정 발급, 관리자 비밀번호 재설정, 계약 종료(일괄 차단)
파트너 관리자 → 자기 회사 직원 계정 생성(crm.add_staff), 관리자 지정·해제, 직원 비밀번호 재설정
모든 사람 → 자기 비밀번호 변경

임시 비밀번호는 시스템이 만들어 한 번만 보여 주고, 그 계정은 다음 로그인 때 비밀번호부터 바꿔야 한다.
비밀번호 원문은 어디에도 저장·기록하지 않는다(이력 로그에도 남기지 않음).
"""

from __future__ import annotations

import secrets
from pathlib import Path

import admin
import auth
import db
from db import DB_PATH

MIN_PASSWORD_LEN = 8
_TEMP_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # 헷갈리는 글자(l, 1, o, 0, i) 제외


class AccountError(Exception):
    """할 수 없는 요청. 메시지를 그대로 화면에 보여 준다."""


def temp_password() -> str:
    return "-".join("".join(secrets.choice(_TEMP_ALPHABET) for _ in range(4)) for _ in range(3))


def _check_new_username(username: str, path: Path) -> str:
    username = (username or "").strip()
    if not username:
        raise AccountError("아이디를 입력해 주세요.")
    if db.has_account(username, path=path):
        raise AccountError("이미 있는 아이디입니다.")
    return username


def _account(username: str, path: Path) -> dict:
    acc = db.get_account(username, path=path)
    if acc is None:
        raise AccountError("계정을 찾을 수 없습니다.")
    return acc


def _need_admin(actor: dict) -> None:
    if actor["role"] != "admin":
        raise AccountError("총판 관리자만 할 수 있습니다.")


def _need_partner_manager(actor: dict, target: dict) -> None:
    if not (actor["role"] == "partner" and actor["partner_role"] == "manager"):
        raise AccountError("파트너 관리자만 할 수 있습니다.")
    if target["partner_id"] != actor["partner_id"]:
        raise AccountError("우리 회사 직원 계정이 아닙니다.")


# ---- 총판 관리자 --------------------------------------------------------

def create_partner(actor: dict, partner_name: str, manager_username: str, manager_name: str,
                   path: Path = DB_PATH) -> str:
    """새 파트너사와 그 관리자 계정을 만든다. 임시 비밀번호를 돌려준다."""
    _need_admin(actor)
    partner_name = (partner_name or "").strip()
    if not partner_name:
        raise AccountError("파트너사 이름을 입력해 주세요.")
    if any(p["name"] == partner_name for p in db.list_partners(path=path)):
        raise AccountError("이미 등록된 파트너사입니다.")
    username = _check_new_username(manager_username, path)
    pid = db.add_partner(partner_name, path=path)
    pw = temp_password()
    db.add_account(username, auth.hash_password(pw), "partner", pid, (manager_name or "").strip(),
                   "manager", must_change_pw=True, path=path)
    db.log("account", partner_name, None, f"파트너사 등록, 관리자 계정 {username} 발급", path=path)
    return pw


def add_manager(actor: dict, partner_id: int, username: str, display_name: str, path: Path = DB_PATH) -> str:
    """기존 파트너사에 관리자 계정을 추가 발급한다(관리자 교체·추가). 임시 비밀번호를 돌려준다."""
    _need_admin(actor)
    partner = next((p for p in db.list_partners(path=path) if p["id"] == partner_id), None)
    if partner is None:
        raise AccountError("파트너사를 찾을 수 없습니다.")
    username = _check_new_username(username, path)
    pw = temp_password()
    db.add_account(username, auth.hash_password(pw), "partner", partner_id, (display_name or "").strip(),
                   "manager", must_change_pw=True, path=path)
    db.log("account", partner["name"], None, f"관리자 계정 {username} 추가 발급", path=path)
    return pw


def end_contract(actor: dict, partner_id: int, reason: str, release_registrations: bool,
                 path: Path = DB_PATH) -> int:
    """계약 종료: 소속 계정 전부 로그인 차단. 선택하면 보호 중인 영업권도 해제한다. 해제한 건수를 돌려준다."""
    _need_admin(actor)
    reason = (reason or "").strip()
    if not reason:
        raise AccountError("사유를 입력해 주세요.")
    partner = next((p for p in db.list_partners(path=path) if p["id"] == partner_id), None)
    if partner is None or not partner["active"]:
        raise AccountError("이미 계약 종료된 파트너사입니다.")
    db.set_partner_active(partner_id, False, path=path)
    released = 0
    if release_registrations:
        for r in db.list_registrations(partner_id, statuses=("active", "pending", "won"), path=path):
            admin.release(r["id"], f"계약 종료 — {reason}", path=path)
            released += 1
    db.log("account", partner["name"], None, f"계약 종료(계정 일괄 차단, 영업권 해제 {released}건) — {reason}", path=path)
    return released


def resume_contract(actor: dict, partner_id: int, path: Path = DB_PATH) -> None:
    _need_admin(actor)
    partner = next((p for p in db.list_partners(path=path) if p["id"] == partner_id), None)
    if partner is None or partner["active"]:
        raise AccountError("계약 종료 상태가 아닙니다.")
    db.set_partner_active(partner_id, True, path=path)
    db.log("account", partner["name"], None, "계약 재개(계정 로그인 다시 허용)", path=path)


# ---- 비밀번호 -----------------------------------------------------------

def reset_password(actor: dict, username: str, path: Path = DB_PATH) -> str:
    """임시 비밀번호로 재설정. 총판은 모든 파트너 계정, 파트너 관리자는 자기 회사 직원만."""
    target = _account(username, path)
    if target["role"] == "admin":
        raise AccountError("총판 관리자 계정은 여기서 재설정할 수 없습니다.")
    if actor["role"] != "admin":
        _need_partner_manager(actor, target)
        if username == actor["username"]:
            raise AccountError("내 비밀번호는 '비밀번호 변경'에서 바꿔 주세요.")
    pw = temp_password()
    db.set_password(username, auth.hash_password(pw), must_change=True, path=path)
    db.log("account", target["partner_name"] or "", None,
           f"{username} 비밀번호 재설정 (by {actor['username']})", path=path)
    return pw


def change_own_password(user: dict, current: str, new: str, confirm: str, path: Path = DB_PATH) -> None:
    acc = _account(user["username"], path)
    if not auth.check_password(current or "", acc["password_hash"]):
        raise AccountError("현재 비밀번호가 맞지 않습니다.")
    if new != confirm:
        raise AccountError("새 비밀번호 두 칸이 서로 다릅니다.")
    if len(new or "") < MIN_PASSWORD_LEN:
        raise AccountError(f"새 비밀번호는 {MIN_PASSWORD_LEN}자 이상이어야 합니다.")
    if new == current:
        raise AccountError("지금과 다른 비밀번호를 정해 주세요.")
    if user["username"].lower() in new.lower():
        raise AccountError("비밀번호에 아이디를 넣을 수 없습니다.")
    db.set_password(user["username"], auth.hash_password(new), must_change=False, path=path)


# ---- 파트너 관리자 ------------------------------------------------------

def add_staff(actor: dict, username: str, display_name: str, path: Path = DB_PATH) -> str:
    """우리 회사 영업 담당자 계정을 만든다. 임시 비밀번호를 돌려준다."""
    if not (actor["role"] == "partner" and actor["partner_role"] == "manager"):
        raise AccountError("파트너 관리자만 할 수 있습니다.")
    username = _check_new_username(username, path)
    pw = temp_password()
    db.add_account(username, auth.hash_password(pw), "partner", actor["partner_id"],
                   (display_name or "").strip(), "rep", must_change_pw=True, path=path)
    db.log("account", actor["partner_name"], None, f"직원 계정 {username} 발급 (by {actor['username']})", path=path)
    return pw


def set_role(actor: dict, username: str, partner_role: str, path: Path = DB_PATH) -> None:
    """직원을 관리자로 지정하거나 해제한다. 활성 관리자가 한 명도 안 남게 되는 해제는 막는다."""
    target = _account(username, path)
    _need_partner_manager(actor, target)
    if partner_role not in ("manager", "rep"):
        raise AccountError("알 수 없는 역할입니다.")
    if not target["active"]:
        raise AccountError("퇴사 처리된 계정입니다.")
    if target["partner_role"] == partner_role:
        return
    if partner_role == "rep":
        managers = [s for s in db.list_staff(actor["partner_id"], path=path)
                    if s["active"] and s["partner_role"] == "manager" and s["username"] != username]
        if not managers:
            raise AccountError("관리자가 한 명도 남지 않게 됩니다. 다른 직원을 먼저 관리자로 지정해 주세요.")
    db.set_partner_role(username, partner_role, path=path)
    label = "관리자 지정" if partner_role == "manager" else "관리자 해제"
    db.log("account", actor["partner_name"], None, f"{username} {label} (by {actor['username']})", path=path)
