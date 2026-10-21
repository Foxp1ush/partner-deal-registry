"""SQLite 저장소: 파트너, 영업권 등록, 이력 로그, 공지."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "data" / "registry.db"
PROTECTION_DAYS = 90
EXPIRING_SOON_DAYS = 14
CONTRACT_ALERT_DAYS = 60   # 계약 고객: 유지보수 종료 60일 전부터 연장 영업 알림
CONTRACT_MAX_DAYS = 365 * 3  # 계약 고객 보호는 오늘부터 최대 3년 (무기한 선점 방지)

SCHEMA = """
CREATE TABLE IF NOT EXISTS partners (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    active INTEGER NOT NULL DEFAULT 1  -- 0이면 계약 종료: 소속 계정 전부 로그인 불가
);
CREATE TABLE IF NOT EXISTS registrations (
    id INTEGER PRIMARY KEY,
    partner_id INTEGER NOT NULL REFERENCES partners(id),
    company_name TEXT NOT NULL,
    biz_no TEXT NOT NULL DEFAULT '',
    contact_name TEXT NOT NULL DEFAULT '',
    contact_phone TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    registered_at TEXT NOT NULL,      -- ISO datetime, 선점 기준 시각
    expires_on TEXT NOT NULL,         -- ISO date. 계약 고객(won)은 유지보수 종료일
    status TEXT NOT NULL,             -- active | pending | won(계약 고객) | expired | rejected | released
    review_reason TEXT NOT NULL DEFAULT '',
    owner TEXT NOT NULL DEFAULT '',   -- 담당 직원 계정(username). 비어 있으면 파트너 관리자만 봄
    stage TEXT NOT NULL DEFAULT '명함 확보',  -- CRM 영업 단계 (crm.STAGES)
    expected_close TEXT NOT NULL DEFAULT '',  -- 예상 구매일(ISO date). '구매 검토'(선택)·'구매 임박'(필수)일 때만
    license_start TEXT NOT NULL DEFAULT '',   -- 라이선스 시작일. 수주 시 필수 (유지보수 종료일 = 시작일 + 1년)
    ency_account TEXT NOT NULL DEFAULT '',    -- 고객사 ENCY 계정 이메일. 수주 시 필수, 총판이 이 이메일로 계정 발급
    ency_issued_at TEXT NOT NULL DEFAULT ''   -- 총판이 ENCY 계정을 발급한 시각. 비어 있으면 발급 대기
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,               -- register | pending | blocked | approve | reject | expire | ai_call | notice | edit
                                      -- won | renew (계약 고객)
                                      -- release | extend | transfer | rename | restore (관리자 조치)
    partner TEXT NOT NULL DEFAULT '',
    registration_id INTEGER,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS accounts (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,      -- auth.hash_password 결과 (salt 포함)
    role TEXT NOT NULL,               -- partner | admin
    partner_id INTEGER REFERENCES partners(id),  -- role=partner일 때만
    display_name TEXT NOT NULL DEFAULT '',
    partner_role TEXT NOT NULL DEFAULT 'manager',  -- manager(파트너 관리자) | rep(영업 담당자)
    active INTEGER NOT NULL DEFAULT 1,            -- 0이면 로그인 불가(퇴사 처리)
    must_change_pw INTEGER NOT NULL DEFAULT 0     -- 1이면 다음 로그인 때 비밀번호부터 바꿔야 함(임시 비밀번호)
);
-- CRM 활동 기록: 수정·삭제하지 않는다(정정은 새 기록으로). partner_id는 기록한 파트너 —
-- 등록이 다른 파트너로 이관돼도 이전 파트너의 기록은 새 파트너에게 보이지 않게 하려고 따로 둔다.
CREATE TABLE IF NOT EXISTS activities (
    id INTEGER PRIMARY KEY,
    registration_id INTEGER NOT NULL REFERENCES registrations(id),
    partner_id INTEGER NOT NULL REFERENCES partners(id),
    ts TEXT NOT NULL,                 -- 기록한 시각
    on_date TEXT NOT NULL,            -- 활동한 날짜
    author TEXT NOT NULL,             -- 기록한 계정
    kind TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
-- 총판이 파트너의 상세 영업 기록을 연 기록. 해당 파트너 관리자에게 공개된다.
CREATE TABLE IF NOT EXISTS access_log (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    viewer TEXT NOT NULL,
    partner_id INTEGER NOT NULL,
    registration_id INTEGER NOT NULL,
    reason TEXT NOT NULL
);
-- 등록 증빙 이미지(명함·사업자등록증). 파일은 data/evidence/에 두고 여기엔 파일 이름만. 지우지 않는다.
CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY,
    registration_id INTEGER NOT NULL REFERENCES registrations(id),
    kind TEXT NOT NULL,               -- card(명함) | cert(사업자등록증)
    file TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    uploader TEXT NOT NULL,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notices (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@contextmanager
def connect(path: Path = DB_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# 예전 DB 파일에 없는 열을 채워 넣는다 (CREATE TABLE IF NOT EXISTS는 기존 테이블을 바꾸지 않으므로)
_ADDED_COLUMNS = {
    "registrations": [("owner", "TEXT NOT NULL DEFAULT ''"), ("stage", "TEXT NOT NULL DEFAULT '명함 확보'"),
                      ("expected_close", "TEXT NOT NULL DEFAULT ''"), ("license_start", "TEXT NOT NULL DEFAULT ''"),
                      ("ency_account", "TEXT NOT NULL DEFAULT ''"), ("ency_issued_at", "TEXT NOT NULL DEFAULT ''")],
    "accounts": [("display_name", "TEXT NOT NULL DEFAULT ''"),
                 ("partner_role", "TEXT NOT NULL DEFAULT 'manager'"),
                 ("active", "INTEGER NOT NULL DEFAULT 1"),
                 ("must_change_pw", "INTEGER NOT NULL DEFAULT 0")],
    "partners": [("active", "INTEGER NOT NULL DEFAULT 1")],
}


FIRST_STAGE = "명함 확보"

# 2026-10-21 영업 단계 변경: 예전 8단계 → 총판 기준 Stage 0~4 + 수주·보류·실패 (crm.STAGES)
_OLD_STAGES = {"첫 접촉": "명함 확보", "니즈 파악": "첫 연락", "제안": "견적·데모", "견적": "견적·데모",
               "협상": "구매 검토", "갱신 협상": "구매 검토"}


def init(path: Path = DB_PATH) -> None:
    with connect(path) as c:
        c.executescript(SCHEMA)
        for table, cols in _ADDED_COLUMNS.items():
            have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols:
                if name not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        for old, new in _OLD_STAGES.items():
            c.execute("UPDATE registrations SET stage=? WHERE stage=?", (new, old))


def is_empty(path: Path = DB_PATH) -> bool:
    with connect(path) as c:
        return c.execute("SELECT COUNT(*) FROM partners").fetchone()[0] == 0


# ---- 파트너 -------------------------------------------------------------

def add_partner(name: str, path: Path = DB_PATH) -> int:
    with connect(path) as c:
        return c.execute("INSERT INTO partners(name) VALUES (?)", (name,)).lastrowid


def list_partners(path: Path = DB_PATH) -> list[dict]:
    with connect(path) as c:
        return [dict(r) for r in c.execute("SELECT * FROM partners ORDER BY name")]


# ---- 계정 ---------------------------------------------------------------

def add_account(username: str, password_hash: str, role: str, partner_id: int | None = None,
                display_name: str = "", partner_role: str = "manager", must_change_pw: bool = False,
                path: Path = DB_PATH) -> None:
    with connect(path) as c:
        c.execute(
            "INSERT INTO accounts(username, password_hash, role, partner_id, display_name, partner_role, must_change_pw) "
            "VALUES (?,?,?,?,?,?,?)",
            (username, password_hash, role, partner_id, display_name, partner_role, int(must_change_pw)),
        )


def set_password(username: str, password_hash: str, must_change: bool, path: Path = DB_PATH) -> None:
    with connect(path) as c:
        c.execute("UPDATE accounts SET password_hash=?, must_change_pw=? WHERE username=?",
                  (password_hash, int(must_change), username))


def set_partner_role(username: str, partner_role: str, path: Path = DB_PATH) -> None:
    with connect(path) as c:
        c.execute("UPDATE accounts SET partner_role=? WHERE username=?", (partner_role, username))


def set_partner_active(partner_id: int, active: bool, path: Path = DB_PATH) -> None:
    with connect(path) as c:
        c.execute("UPDATE partners SET active=? WHERE id=?", (int(active), partner_id))


def list_staff(partner_id: int, path: Path = DB_PATH) -> list[dict]:
    with connect(path) as c:
        return [dict(r) for r in c.execute(
            "SELECT username, display_name, partner_role, active, must_change_pw FROM accounts "
            "WHERE partner_id=? ORDER BY partner_role, username", (partner_id,))]


def set_account_active(username: str, active: bool, path: Path = DB_PATH) -> None:
    with connect(path) as c:
        c.execute("UPDATE accounts SET active=? WHERE username=?", (int(active), username))


def get_account(username: str, path: Path = DB_PATH) -> dict | None:
    with connect(path) as c:
        row = c.execute(
            "SELECT a.*, p.name AS partner_name, p.active AS partner_active FROM accounts a "
            "LEFT JOIN partners p ON p.id = a.partner_id "
            "WHERE a.username=?", (username,)
        ).fetchone()
    return dict(row) if row else None


def has_account(username: str, path: Path = DB_PATH) -> bool:
    with connect(path) as c:
        return c.execute("SELECT 1 FROM accounts WHERE username=?", (username,)).fetchone() is not None


# ---- 이력 로그 ----------------------------------------------------------

def log(kind: str, partner: str = "", registration_id: int | None = None,
        detail: str = "", ts: str | None = None, path: Path = DB_PATH) -> None:
    with connect(path) as c:
        c.execute(
            "INSERT INTO events(ts, kind, partner, registration_id, detail) VALUES (?,?,?,?,?)",
            (ts or now_iso(), kind, partner, registration_id, detail),
        )


def list_events(limit: int = 200, path: Path = DB_PATH) -> list[dict]:
    with connect(path) as c:
        return [dict(r) for r in c.execute("SELECT * FROM events ORDER BY ts DESC, id DESC LIMIT ?", (limit,))]


def extension_counts(path: Path = DB_PATH) -> dict[int, int]:
    """등록별 보호 기간 연장 횟수 (이력 로그의 extend 건수)."""
    with connect(path) as c:
        return {r[0]: r[1] for r in c.execute(
            "SELECT registration_id, COUNT(*) FROM events WHERE kind='extend' GROUP BY registration_id")}


def ai_calls_today(path: Path = DB_PATH) -> int:
    with connect(path) as c:
        return c.execute(
            "SELECT COUNT(*) FROM events WHERE kind='ai_call' AND substr(ts,1,10)=?",
            (date.today().isoformat(),),
        ).fetchone()[0]


# ---- 영업권 등록 --------------------------------------------------------

_REG_SELECT = """
SELECT r.*, p.name AS partner_name FROM registrations r
JOIN partners p ON p.id = r.partner_id
"""


def expire_overdue(path: Path = DB_PATH) -> int:
    """만료일(계약 고객은 계약 종료일)이 지난 등록을 expired로 바꾸고 로그를 남긴다."""
    today = date.today().isoformat()
    with connect(path) as c:
        rows = c.execute(
            _REG_SELECT + "WHERE r.status IN ('active','won') AND r.expires_on < ?", (today,)
        ).fetchall()
        for r in rows:
            c.execute("UPDATE registrations SET status='expired' WHERE id=?", (r["id"],))
            what = "유지보수 종료일 경과(연장 기록 없음)" if r["status"] == "won" else "보호기간 만료"
            c.execute(
                "INSERT INTO events(ts, kind, partner, registration_id, detail) VALUES (?,?,?,?,?)",
                (now_iso(), "expire", r["partner_name"], r["id"], f"{r['company_name']} {what}"),
            )
    return len(rows)


def holding_registrations(path: Path = DB_PATH) -> list[dict]:
    """선점 효력이 있는 등록(보호 중 + 검토 대기 + 계약 고객)."""
    with connect(path) as c:
        return [dict(r) for r in c.execute(
            _REG_SELECT + "WHERE r.status IN ('active','pending','won') ORDER BY r.registered_at"
        )]


def list_registrations(partner_id: int | None = None, statuses: tuple[str, ...] | None = None,
                       path: Path = DB_PATH) -> list[dict]:
    sql, args = _REG_SELECT + "WHERE 1=1", []
    if partner_id is not None:
        sql += " AND r.partner_id=?"
        args.append(partner_id)
    if statuses:
        sql += f" AND r.status IN ({','.join('?' * len(statuses))})"
        args.extend(statuses)
    sql += " ORDER BY r.registered_at DESC"
    with connect(path) as c:
        return [dict(r) for r in c.execute(sql, args)]


def expiring_soon(days: int = EXPIRING_SOON_DAYS, contract_days: int = CONTRACT_ALERT_DAYS,
                  path: Path = DB_PATH) -> list[dict]:
    """곧 끝나는 등록: 보호 중은 days일 안, 계약 고객은 contract_days일 안(갱신 영업 준비)."""
    limit = (date.today() + timedelta(days=days)).isoformat()
    contract_limit = (date.today() + timedelta(days=contract_days)).isoformat()
    with connect(path) as c:
        return [dict(r) for r in c.execute(
            _REG_SELECT + "WHERE (r.status='active' AND r.expires_on <= ?) OR (r.status='won' AND r.expires_on <= ?) "
                          "ORDER BY r.expires_on", (limit, contract_limit)
        )]


def add_registration(partner_id: int, company_name: str, biz_no: str, contact_name: str,
                     contact_phone: str, note: str, status: str, review_reason: str = "",
                     registered_at: datetime | None = None, owner: str = "", path: Path = DB_PATH) -> int:
    reg_at = registered_at or datetime.now()
    expires = (reg_at.date() + timedelta(days=PROTECTION_DAYS)).isoformat()
    with connect(path) as c:
        return c.execute(
            """INSERT INTO registrations(partner_id, company_name, biz_no, contact_name, contact_phone,
               note, registered_at, expires_on, status, review_reason, owner, stage)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (partner_id, company_name, biz_no, contact_name, contact_phone, note,
             reg_at.isoformat(timespec="seconds"), expires, status, review_reason, owner, FIRST_STAGE),
        ).lastrowid


_EDITABLE = ("biz_no", "contact_name", "contact_phone", "note", "ency_account")
ADMIN_EDITABLE = _EDITABLE + ("company_name", "partner_id", "expires_on", "status", "owner", "stage", "expected_close",
                                "license_start", "ency_issued_at")


def get_registration(registration_id: int, path: Path = DB_PATH) -> dict | None:
    with connect(path) as c:
        row = c.execute(_REG_SELECT + "WHERE r.id=?", (registration_id,)).fetchone()
    return dict(row) if row else None


def update_registration(registration_id: int, fields: dict, allowed: tuple[str, ...] = _EDITABLE,
                        path: Path = DB_PATH) -> None:
    """등록 정보 수정. 기본은 파트너가 바꿔도 되는 항목만. 관리자 조치는 allowed=ADMIN_EDITABLE."""
    cols = [k for k in fields if k in allowed]
    if not cols:
        return
    with connect(path) as c:
        c.execute(
            f"UPDATE registrations SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?",
            [fields[k] for k in cols] + [registration_id],
        )


def set_status(registration_id: int, status: str, path: Path = DB_PATH) -> None:
    with connect(path) as c:
        c.execute("UPDATE registrations SET status=? WHERE id=?", (status, registration_id))


# ---- CRM ----------------------------------------------------------------

def add_activity(registration_id: int, partner_id: int, author: str, kind: str, note: str,
                 on_date: str | None = None, ts: str | None = None, path: Path = DB_PATH) -> int:
    with connect(path) as c:
        return c.execute(
            "INSERT INTO activities(registration_id, partner_id, ts, on_date, author, kind, note) VALUES (?,?,?,?,?,?,?)",
            (registration_id, partner_id, ts or now_iso(), on_date or date.today().isoformat(), author, kind, note),
        ).lastrowid


def list_activities(registration_ids: list[int], partner_id: int | None = None, path: Path = DB_PATH) -> list[dict]:
    """partner_id를 주면 그 파트너가 남긴 기록만. (총판 상세 열람 때만 None)"""
    if not registration_ids:
        return []
    sql = (f"SELECT a.*, COALESCE(NULLIF(acc.display_name, ''), a.author) AS author_name FROM activities a "
           f"LEFT JOIN accounts acc ON acc.username = a.author "
           f"WHERE a.registration_id IN ({','.join('?' * len(registration_ids))})")
    args: list = list(registration_ids)
    if partner_id is not None:
        sql += " AND a.partner_id=?"
        args.append(partner_id)
    with connect(path) as c:
        return [dict(r) for r in c.execute(sql + " ORDER BY a.on_date DESC, a.id DESC", args)]


def log_access(viewer: str, partner_id: int, registration_id: int, reason: str, path: Path = DB_PATH) -> None:
    with connect(path) as c:
        c.execute("INSERT INTO access_log(ts, viewer, partner_id, registration_id, reason) VALUES (?,?,?,?,?)",
                  (now_iso(), viewer, partner_id, registration_id, reason))


def list_access_log(partner_id: int, path: Path = DB_PATH) -> list[dict]:
    with connect(path) as c:
        return [dict(r) for r in c.execute(
            "SELECT l.*, r.company_name FROM access_log l JOIN registrations r ON r.id = l.registration_id "
            "WHERE l.partner_id=? ORDER BY l.ts DESC, l.id DESC", (partner_id,))]


# ---- 등록 증빙 ----------------------------------------------------------

def add_evidence(registration_id: int, kind: str, file: str, size: int, uploader: str,
                 path: Path = DB_PATH) -> int:
    with connect(path) as c:
        return c.execute(
            "INSERT INTO evidence(registration_id, kind, file, bytes, uploader, ts) VALUES (?,?,?,?,?,?)",
            (registration_id, kind, file, size, uploader, now_iso()),
        ).lastrowid


def list_evidence(registration_id: int, path: Path = DB_PATH) -> list[dict]:
    with connect(path) as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM evidence WHERE registration_id=? ORDER BY ts, id", (registration_id,))]


def evidence_kinds(path: Path = DB_PATH) -> dict[int, set[str]]:
    """등록 id → 첨부된 증빙 종류 집합 (표에 증빙 수준을 보여 줄 때)."""
    out: dict[int, set[str]] = {}
    with connect(path) as c:
        for r in c.execute("SELECT registration_id, kind FROM evidence"):
            out.setdefault(r[0], set()).add(r[1])
    return out


# ---- 공지 ---------------------------------------------------------------

def add_notice(title: str, body: str, ts: str | None = None, path: Path = DB_PATH) -> None:
    with connect(path) as c:
        c.execute("INSERT INTO notices(ts, title, body) VALUES (?,?,?)", (ts or now_iso(), title, body))


def list_notices(path: Path = DB_PATH) -> list[dict]:
    with connect(path) as c:
        return [dict(r) for r in c.execute("SELECT * FROM notices ORDER BY ts DESC")]
