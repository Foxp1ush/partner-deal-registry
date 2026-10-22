import io
from datetime import datetime, timedelta

import pytest
from PIL import Image

import auth
import db
import seed
import tickets


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "t.db"
    db.init(path)
    a = db.add_partner("A", path=path)
    b = db.add_partner("B", path=path)
    for username, role, pid, prole in [("a-boss", "partner", a, "manager"), ("a-1", "partner", a, "rep"),
                                       ("a-2", "partner", a, "rep"), ("b-boss", "partner", b, "manager"),
                                       ("eng1", "admin", None, "engineer"), ("eng2", "admin", None, "engineer"),
                                       ("boss", "admin", None, "manager")]:
        db.add_account(username, auth.hash_password("pw"), role, pid, username, prole, path=path)
    users = {u: auth.login(u, "pw", path=path)
             for u in ("a-boss", "a-1", "a-2", "b-boss", "eng1", "eng2", "boss")}
    r1 = db.add_registration(a, "샛별정밀", seed.fake_biz_no(1), "", "", "", "active", owner="a-1", path=path)
    r2 = db.add_registration(a, "누리금속", seed.fake_biz_no(2), "", "", "", "active", owner="a-2", path=path)
    return path, users, r1, r2


def png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (40, 30), "red").save(out, "PNG")
    return out.getvalue()


def new(env, who="a-1", reg="r1", **kw):
    path, u, r1, r2 = env
    rid = {"r1": r1, "r2": r2, None: None}[reg]
    return tickets.create(u[who], rid, kw.get("category", "오류"), kw.get("title", "실행 오류"),
                          kw.get("body", "켜면 꺼져요"), kw.get("images", ()), path=path)


def status(env, tid):
    return db.get_ticket(tid, path=env[0])["status"]


def test_visibility(env):
    path, u, r1, r2 = env
    t1 = new(env, "a-1", "r1")
    t2 = new(env, "a-2", "r2")
    assert [t["id"] for t in tickets.visible(u["a-1"], path=path)] == [t1]
    assert sorted(t["id"] for t in tickets.visible(u["a-boss"], path=path)) == [t1, t2]
    assert tickets.visible(u["b-boss"], path=path) == []
    with pytest.raises(tickets.TicketError):
        tickets.get(u["a-2"], t1, path=path)
    with pytest.raises(tickets.TicketError):
        tickets.get(u["b-boss"], t1, path=path)
    assert len(tickets.visible(u["eng1"], path=path)) == 2


def test_customer_must_be_mine_or_unset(env):
    path, u, r1, r2 = env
    with pytest.raises(tickets.TicketError):
        new(env, "a-1", "r2")  # a-2 담당 고객
    with pytest.raises(tickets.TicketError):
        new(env, "b-boss", "r1")  # 다른 파트너 고객
    tid = new(env, "a-1", None)
    assert db.get_ticket(tid, path=path)["customer_name"] == ""
    with pytest.raises(tickets.TicketError):
        new(env, "a-1", None, title=" ")
    with pytest.raises(tickets.TicketError):
        new(env, "a-1", None, category="없는 분류")
    with pytest.raises(tickets.TicketError):
        new(env, "eng1", None)  # 총판은 문의를 올리지 않는다


def test_flow(env):
    path, u, *_ = env
    tid = new(env)
    assert status(env, tid) == "open"
    assert [t["id"] for t in tickets.visible(u["eng1"], "unassigned", path)] == [tid]
    with pytest.raises(tickets.TicketError):
        tickets.reply(u["eng1"], tid, "답", path=path)  # 맡기 전
    tickets.claim(u["eng1"], tid, path=path)
    assert status(env, tid) == "in_progress"
    with pytest.raises(tickets.TicketError):
        tickets.claim(u["eng2"], tid, path=path)  # 이미 맡음
    with pytest.raises(tickets.TicketError):
        tickets.reply(u["eng2"], tid, "답", path=path)  # 담당 아님
    tickets.reply(u["eng1"], tid, "드라이버를 다시 설치해 보세요", path=path)
    assert status(env, tid) == "answered"
    assert tickets.counts(u["a-1"], path=path) == 1
    tickets.reply(u["a-1"], tid, "그래도 안 돼요", path=path)
    assert status(env, tid) == "in_progress"
    tickets.reply(u["eng1"], tid, "원격으로 확인했습니다", path=path)
    tickets.resolve(u["a-1"], tid, path=path)
    t = db.get_ticket(tid, path=path)
    assert (t["status"], t["closed_by"]) == ("closed", "a-1")
    with pytest.raises(tickets.TicketError):
        tickets.reply(u["a-1"], tid, "또 질문", path=path)  # 종료 후엔 다시 문의로
    tickets.reopen(u["a-1"], tid, "다시 같은 오류가 나요", path=path)
    t = db.get_ticket(tid, path=path)
    assert (t["status"], t["assignee"], t["closed_at"]) == ("in_progress", "eng1", "")
    sides = [m["side"] for m in tickets.messages(u["a-boss"], tid, path=path)]
    assert sides == ["partner", "engineer", "partner", "engineer", "partner"]
    assert any(e["kind"] == "ticket" for e in db.list_events(path=path))


def test_assign_only_by_admin_manager(env):
    path, u, *_ = env
    tid = new(env)
    with pytest.raises(tickets.TicketError):
        tickets.assign(u["eng1"], tid, "eng2", path=path)
    with pytest.raises(tickets.TicketError):
        tickets.assign(u["boss"], tid, "a-1", path=path)  # 엔지니어가 아님
    tickets.assign(u["boss"], tid, "eng2", path=path)
    t = db.get_ticket(tid, path=path)
    assert (t["assignee"], t["status"]) == ("eng2", "in_progress")
    assert tickets.counts(u["eng2"], path=path) == 1 and tickets.counts(u["eng1"], path=path) == 0
    with pytest.raises(tickets.TicketError):
        tickets.reply(u["boss"], tid, "매니저 답", path=path)


def test_auto_close(env):
    path, u, *_ = env
    old, fresh = new(env), new(env, title="두 번째")
    for tid in (old, fresh):
        tickets.claim(u["eng1"], tid, path=path)
        tickets.reply(u["eng1"], tid, "답", path=path)
    db.update_ticket(old, {"answered_at": (datetime.now() - timedelta(days=8)).isoformat(timespec="seconds")},
                     path=path)
    assert tickets.auto_close(path=path) == 1
    assert db.get_ticket(old, path=path)["closed_by"] == "auto"
    assert status(env, fresh) == "answered"


def test_images(env):
    path, u, *_ = env
    tid = new(env, images=[png(), png()])
    msgs = tickets.messages(u["a-1"], tid, path=path)
    assert len(msgs[0]["files"]) == 2 and all(f["file_path"].exists() for f in msgs[0]["files"])
    with pytest.raises(tickets.TicketError):
        new(env, images=[b"not an image"])
    assert len(db.list_tickets(path=path)) == 1  # 잘못된 이미지면 문의도 만들지 않는다
    with pytest.raises(tickets.TicketError):
        new(env, images=[png()] * (tickets.MAX_IMAGES + 1))


def test_search_includes_closed_and_respects_visibility(env):
    path, u, *_ = env
    t1 = new(env, "a-1", "r1", title="라이선스 설치 오류", category="라이선스")
    t2 = new(env, "a-2", "r2", title="공구 경로", body="라이선스 키 입력 화면이 안 떠요")
    tickets.claim(u["eng1"], t1, path=path)
    tickets.reply(u["eng1"], t1, "재설치 후 키를 다시 넣으세요", path=path)
    tickets.resolve(u["a-1"], t1, path=path)  # 종료된 문의도 찾는다
    everyone = tickets.visible(u["eng1"], "all", path)
    assert sorted(t["id"] for t in tickets.search(everyone, "라이선스", path)) == [t1, t2]
    assert [t["id"] for t in tickets.search(everyone, "재설치 키", path)] == [t1]  # 대화 내용, 낱말 모두
    assert [t["id"] for t in tickets.search(everyone, f"#{t2}", path)] == [t2]
    assert [t["id"] for t in tickets.search(everyone, "누리금속", path)] == [t2]  # 고객사
    assert tickets.search(everyone, "없는말", path) == []
    # 영업 담당자는 자기 문의 안에서만 찾는다
    assert [t["id"] for t in tickets.search(tickets.visible(u["a-1"], path=path), "라이선스", path)] == [t1]
