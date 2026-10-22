"""기술 문의(티켓): 고객사의 기술 문의를 파트너가 올리고, 총판 엔지니어가 답한다.

흐름: open(미배정) → [엔지니어: 내가 맡기] → in_progress → [엔지니어 답변] → answered
      → [파트너: 해결됨] 또는 답변 후 AUTO_CLOSE_DAYS일 동안 반응 없음 → closed
- answered에서 파트너가 다시 질문하면 in_progress로 돌아간다.
- closed도 파트너가 '다시 문의'하면 열린다(담당 엔지니어는 그대로).

공개 범위
- 파트너 영업 담당자: 내가 올린 문의 / 파트너 관리자: 우리 회사 문의 전체. 다른 파트너의 문의는 보이지 않는다.
- 총판 엔지니어: 모든 문의. 답변은 내가 맡은 건만.
- 총판 총괄 매니저: 모든 문의(읽기)와 담당 엔지니어 지정·변경.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import crm
import db
import evidence
from db import DB_PATH

CATEGORIES = ["설치", "라이선스", "사용법", "오류", "기타"]
STATUS_LABEL = {"open": "접수(미배정)", "in_progress": "처리 중", "answered": "답변 완료", "closed": "종료"}
AUTO_CLOSE_DAYS = 7
TITLE_MAX = 100
BODY_MAX = 3000
MAX_IMAGES = 5


class TicketError(Exception):
    pass


# ---- 역할 ---------------------------------------------------------------

def is_engineer(user: dict) -> bool:
    return user["role"] == "admin" and user.get("partner_role") == "engineer"


def is_admin_manager(user: dict) -> bool:
    return user["role"] == "admin" and user.get("partner_role") == "manager"


def _need_partner(user: dict) -> None:
    if user["role"] != "partner":
        raise TicketError("파트너 계정만 할 수 있습니다.")


def _need_engineer(user: dict) -> None:
    if not is_engineer(user):
        raise TicketError("총판 엔지니어만 할 수 있습니다.")


# ---- 조회 ---------------------------------------------------------------

def _can_see(user: dict, t: dict) -> bool:
    if user["role"] == "admin":
        return True
    return t["partner_id"] == user["partner_id"] and (crm.is_manager(user) or t["author"] == user["username"])


def visible(user: dict, scope: str = "all", path: Path = DB_PATH) -> list[dict]:
    """볼 수 있는 문의(최근 갱신 순). 총판은 scope로 unassigned(미배정) / mine(내 담당, 종료 제외) / all."""
    if user["role"] == "partner":
        return [t for t in db.list_tickets(user["partner_id"], path=path) if _can_see(user, t)]
    rows = db.list_tickets(path=path)
    if scope == "unassigned":
        return [t for t in rows if not t["assignee"] and t["status"] != "closed"]
    if scope == "mine":
        return [t for t in rows if t["assignee"] == user["username"] and t["status"] != "closed"]
    return rows


def search(rows: list[dict], query: str, path: Path = DB_PATH) -> list[dict]:
    """rows(이미 볼 수 있는 문의) 안에서 찾는다. 제목·대화 내용·고객사·파트너·분류·번호.

    띄어 쓴 낱말은 모두 들어 있어야 한다(예: '라이선스 설치'). 대소문자는 가리지 않는다.
    """
    words = (query or "").lower().replace("#", " ").split()
    if not words:
        return rows
    texts = db.ticket_texts(path=path)
    out = []
    for t in rows:
        hay = " ".join([str(t["id"]), t["title"], t["customer_name"], t["partner_name"], t["category"],
                        texts.get(t["id"], "")]).lower()
        if all(w in hay for w in words):
            out.append(t)
    return out


def get(user: dict, ticket_id: int, path: Path = DB_PATH) -> dict:
    t = db.get_ticket(ticket_id, path=path)
    if t is None or not _can_see(user, t):
        raise TicketError("문의를 찾을 수 없습니다.")
    return t


def messages(user: dict, ticket_id: int, path: Path = DB_PATH) -> list[dict]:
    """대화(오래된 것부터). 각 메시지에 files(첨부 이미지 목록, file_path 포함)를 붙인다."""
    get(user, ticket_id, path)
    folder = files_dir(path)
    by_msg: dict[int, list[dict]] = {}
    for f in db.list_ticket_files(ticket_id, path=path):
        by_msg.setdefault(f["message_id"], []).append({**f, "file_path": folder / f["file"]})
    return [{**m, "files": by_msg.get(m["id"], [])} for m in db.list_ticket_messages(ticket_id, path=path)]


def counts(user: dict, path: Path = DB_PATH) -> int:
    """메뉴 숫자 배지. 파트너: 답변 완료(확인할 것) / 엔지니어: 미배정 + 내 담당 처리 중 / 총괄 매니저: 미배정."""
    if user["role"] == "partner":
        return sum(t["status"] == "answered" for t in visible(user, path=path))
    unassigned = len(visible(user, "unassigned", path))
    if is_engineer(user):
        return unassigned + sum(t["status"] == "in_progress" for t in visible(user, "mine", path))
    return unassigned


# ---- 첨부 이미지 ----------------------------------------------------------

def files_dir(path: Path = DB_PATH) -> Path:
    return path.parent / "tickets"


def _shrink_all(images) -> list[bytes]:
    """저장하기 전에 전부 줄여 본다. 하나라도 못 읽으면 아무것도 저장하지 않는다."""
    images = [b for b in (images or []) if b]
    if len(images) > MAX_IMAGES:
        raise TicketError(f"이미지는 한 번에 {MAX_IMAGES}장까지 올릴 수 있습니다.")
    try:
        return [evidence.shrink(b) for b in images]
    except evidence.EvidenceError as e:
        raise TicketError(str(e)) from e


def _save_files(ticket_id: int, message_id: int, shrunk: list[bytes], uploader: str, path: Path) -> None:
    if not shrunk:
        return
    folder = files_dir(path)
    folder.mkdir(parents=True, exist_ok=True)
    for i, data in enumerate(shrunk):
        name = f"{ticket_id}_{message_id}_{i}_{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.jpg"
        (folder / name).write_bytes(data)
        db.add_ticket_file(ticket_id, message_id, name, len(data), uploader, path=path)


def _add_message(t: dict, user: dict, side: str, body: str, images, path: Path) -> None:
    body = (body or "").strip()
    if not body:
        raise TicketError("내용을 입력해 주세요.")
    if len(body) > BODY_MAX:
        raise TicketError(f"내용은 {BODY_MAX}자까지 쓸 수 있습니다.")
    shrunk = _shrink_all(images)
    mid = db.add_ticket_message(t["id"], user["username"], side, body, path=path)
    _save_files(t["id"], mid, shrunk, user["username"], path)


def _log(t: dict, detail: str, path: Path) -> None:
    db.log("ticket", t["partner_name"], t["registration_id"], f"#{t['id']} {t['title']} — {detail}", path=path)


# ---- 동작 ---------------------------------------------------------------

def create(user: dict, registration_id: int | None, category: str, title: str, body: str, images=(),
           path: Path = DB_PATH) -> int:
    """문의를 올린다. 고객사는 내가 볼 수 있는 고객 중에서만 고르고, None이면 '고객사 미지정'."""
    _need_partner(user)
    title = (title or "").strip()
    if not title:
        raise TicketError("제목을 입력해 주세요.")
    if len(title) > TITLE_MAX:
        raise TicketError(f"제목은 {TITLE_MAX}자까지 쓸 수 있습니다.")
    if category not in CATEGORIES:
        raise TicketError("분류를 골라 주세요.")
    customer = ""
    if registration_id is not None:
        mine = {r["id"]: r for r in crm.visible_registrations(user, path=path)}
        if registration_id not in mine:
            raise TicketError("내 고객 목록에 있는 고객사만 고를 수 있습니다.")
        customer = mine[registration_id]["company_name"]
    if not (body or "").strip():
        raise TicketError("내용을 입력해 주세요.")
    _shrink_all(images)  # 이미지가 잘못됐으면 문의를 만들기 전에 막는다
    tid = db.add_ticket(user["partner_id"], registration_id, customer, user["username"], category, title, path=path)
    t = db.get_ticket(tid, path=path)
    _add_message(t, user, "partner", body, images, path)
    _log(t, f"문의 접수 ({user['username']}, {customer or '고객사 미지정'})", path)
    return tid


def claim(user: dict, ticket_id: int, path: Path = DB_PATH) -> None:
    """엔지니어가 미배정 문의를 맡는다."""
    _need_engineer(user)
    t = get(user, ticket_id, path)
    if t["assignee"]:
        raise TicketError("이미 다른 엔지니어가 맡은 문의입니다.")
    if t["status"] == "closed":
        raise TicketError("종료된 문의입니다.")
    db.update_ticket(ticket_id, {"assignee": user["username"], "status": "in_progress",
                                 "updated_at": db.now_iso()}, path=path)
    _log(t, f"담당 {user['username']}", path)


def assign(user: dict, ticket_id: int, engineer: str, path: Path = DB_PATH) -> None:
    """총괄 매니저가 담당 엔지니어를 정하거나 바꾼다."""
    if not is_admin_manager(user):
        raise TicketError("총판 총괄 매니저만 담당자를 지정할 수 있습니다.")
    t = get(user, ticket_id, path)
    if engineer not in {e["username"] for e in db.list_engineers(path=path)}:
        raise TicketError("일하고 있는 엔지니어를 골라 주세요.")
    if t["status"] == "closed":
        raise TicketError("종료된 문의입니다.")
    status = "in_progress" if t["status"] == "open" else t["status"]
    db.update_ticket(ticket_id, {"assignee": engineer, "status": status, "updated_at": db.now_iso()}, path=path)
    _log(t, f"담당 지정 {t['assignee'] or '없음'} → {engineer} (by {user['username']})", path)


def reply(user: dict, ticket_id: int, body: str, images=(), path: Path = DB_PATH) -> None:
    """답장. 엔지니어가 답하면 answered, 파트너가 답하면 다시 처리 중(미배정이면 open 그대로)."""
    t = get(user, ticket_id, path)
    if t["status"] == "closed":
        raise TicketError("종료된 문의입니다. '다시 문의'로 열어 주세요.")
    now = db.now_iso()
    if is_engineer(user):
        if t["assignee"] != user["username"]:
            raise TicketError("내가 맡은 문의에만 답할 수 있습니다. 먼저 [내가 맡기]를 눌러 주세요.")
        _add_message(t, user, "engineer", body, images, path)
        db.update_ticket(ticket_id, {"status": "answered", "answered_at": now, "updated_at": now}, path=path)
        _log(t, f"답변 ({user['username']})", path)
        return
    _need_partner(user)
    _add_message(t, user, "partner", body, images, path)
    db.update_ticket(ticket_id, {"status": "in_progress" if t["assignee"] else "open", "updated_at": now}, path=path)
    _log(t, f"추가 질문 ({user['username']})", path)


def resolve(user: dict, ticket_id: int, path: Path = DB_PATH) -> None:
    """파트너가 해결됐다고 확인하고 종료한다."""
    _need_partner(user)
    t = get(user, ticket_id, path)
    if t["status"] == "closed":
        raise TicketError("이미 종료된 문의입니다.")
    now = db.now_iso()
    db.update_ticket(ticket_id, {"status": "closed", "closed_at": now, "closed_by": user["username"],
                                 "updated_at": now}, path=path)
    _log(t, f"해결 확인·종료 ({user['username']})", path)


def reopen(user: dict, ticket_id: int, body: str, images=(), path: Path = DB_PATH) -> None:
    """종료된 문의를 다시 연다. 무엇이 안 됐는지 내용을 꼭 남긴다."""
    _need_partner(user)
    t = get(user, ticket_id, path)
    if t["status"] != "closed":
        raise TicketError("종료된 문의만 다시 열 수 있습니다.")
    _add_message(t, user, "partner", body, images, path)
    db.update_ticket(ticket_id, {"status": "in_progress" if t["assignee"] else "open", "closed_at": "",
                                 "closed_by": "", "updated_at": db.now_iso()}, path=path)
    _log(t, f"다시 문의 ({user['username']})", path)


def auto_close(today: datetime | None = None, path: Path = DB_PATH) -> int:
    """답변 완료 뒤 AUTO_CLOSE_DAYS일 동안 파트너 반응이 없으면 종료한다. 종료한 건수를 돌려준다."""
    cutoff = ((today or datetime.now()) - timedelta(days=AUTO_CLOSE_DAYS)).isoformat(timespec="seconds")
    n = 0
    for t in db.list_tickets(path=path):
        if t["status"] == "answered" and t["answered_at"] and t["answered_at"] <= cutoff:
            now = db.now_iso()
            db.update_ticket(t["id"], {"status": "closed", "closed_at": now, "closed_by": "auto",
                                       "updated_at": now}, path=path)
            _log(t, f"답변 후 {AUTO_CLOSE_DAYS}일 반응 없음 — 자동 종료", path)
            n += 1
    return n
