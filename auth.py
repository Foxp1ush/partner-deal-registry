"""로그인: 비밀번호는 원문 대신 salt를 섞은 해시(PBKDF2)로만 저장한다."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from pathlib import Path

import db
from db import DB_PATH

_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _ITERATIONS).hex()
    return f"{salt}${digest}"


def check_password(password: str, stored: str) -> bool:
    salt, digest = stored.split("$", 1)
    test = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _ITERATIONS).hex()
    return hmac.compare_digest(test, digest)


_SESSION_KEYS = ("username", "role", "partner_id", "partner_name", "display_name", "partner_role", "must_change_pw")


def _usable(acc: dict | None) -> bool:
    """퇴사 처리된 계정, 계약 종료된 파트너사의 계정은 쓸 수 없다."""
    return acc is not None and bool(acc["active"]) and (acc["partner_id"] is None or bool(acc["partner_active"]))


def login(username: str, password: str, path: Path = DB_PATH) -> dict | None:
    """맞으면 계정 dict, 틀리거나 쓸 수 없는 계정이면 None."""
    acc = db.get_account((username or "").strip(), path=path)
    if not _usable(acc) or not check_password(password or "", acc["password_hash"]):
        return None
    return {k: acc[k] for k in _SESSION_KEYS}


def refresh(user: dict, path: Path = DB_PATH) -> dict | None:
    """화면을 그릴 때마다 계정 상태를 다시 읽는다. 로그인 중에 퇴사·계약 종료되면 None(즉시 로그아웃),
    역할이 바뀌었으면 바뀐 권한이 바로 적용된다."""
    acc = db.get_account(user["username"], path=path)
    return {k: acc[k] for k in _SESSION_KEYS} if _usable(acc) else None


def still_valid(user: dict, path: Path = DB_PATH) -> bool:
    return refresh(user, path) is not None
