from datetime import date, datetime, timedelta

import pytest

import auth
import crm
import db
import seed
import stats
import tickets


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "t.db"
    db.init(path)
    a = db.add_partner("A", path=path)
    b = db.add_partner("B", path=path)
    for username, role, pid, prole in [("a-boss", "partner", a, "manager"), ("a-1", "partner", a, "rep"),
                                       ("b-boss", "partner", b, "manager"),
                                       ("boss", "admin", None, "manager"), ("eng", "admin", None, "engineer")]:
        db.add_account(username, auth.hash_password("pw"), role, pid, username, prole, path=path)
    users = {u: auth.login(u, "pw", path=path) for u in ("a-boss", "a-1", "b-boss", "boss", "eng")}
    return path, users, a, b


def reg(path, pid, name, serial, days_ago=0, owner="a-1"):
    at = datetime.now() - timedelta(days=days_ago)
    return db.add_registration(pid, name, seed.fake_biz_no(serial), "", "", "", "active",
                               registered_at=at.replace(microsecond=0), owner=owner, path=path)


def won(user, rid, amount, path):
    crm.set_stage(user, rid, "수주", license_type="perpetual", license_start=date.today().isoformat(),
                  ency_account="cam@x.example", contract_amount=amount, path=path)


def test_won_requires_supply_amount(env):
    path, u, a, b = env
    r = reg(path, a, "샛별정밀", 1)
    for bad in (None, 0, -5, "abc", stats.crm.AMOUNT_MAX + 1):
        with pytest.raises(crm.CrmError):
            won(u["a-1"], r, bad, path)
    won(u["a-1"], r, 12_000_000, path)
    row = db.get_registration(r, path=path)
    assert row["contract_amount"] == 12_000_000 and row["won_at"]
    assert any("VAT 10% 별도" in e["detail"] for e in db.list_events(path=path) if e["kind"] == "won")


def test_stage_changes_recorded(env):
    path, u, a, b = env
    r = reg(path, a, "샛별정밀", 1)
    crm.set_stage(u["a-1"], r, "첫 연락", path=path)
    crm.set_stage(u["a-1"], r, "견적·데모", path=path)
    assert [(c["from_stage"], c["to_stage"]) for c in db.list_stage_changes(path=path)] == \
        [("명함 확보", "첫 연락"), ("첫 연락", "견적·데모")]


def test_period_boundaries():
    assert stats.period_start(date(2026, 10, 22), "week") == date(2026, 10, 19)  # 월요일
    assert stats.period_start(date(2026, 10, 22), "quarter") == date(2026, 10, 1)
    assert stats.period_label(date(2026, 7, 1), "quarter") == "2026 3분기"
    assert stats.periods("quarter", 3, today=date(2026, 2, 10)) == [date(2025, 7, 1), date(2025, 10, 1), date(2026, 1, 1)]
    assert stats.periods("year", 2, today=date(2026, 1, 1)) == [date(2025, 1, 1), date(2026, 1, 1)]
    assert stats.periods("month", 2, today=date(2026, 1, 31)) == [date(2025, 12, 1), date(2026, 1, 1)]
    assert len(stats.periods("day")) == 30 and len(stats.periods("year")) == 5


@pytest.mark.parametrize("unit", list(stats.UNITS))
def test_trend_totals_match(env, unit):
    path, u, a, b = env
    r1, r2 = reg(path, a, "샛별정밀", 1), reg(path, b, "누리금속", 2, owner="b-boss")
    reg(path, a, "오래된회사", 3, days_ago=4000)  # 어느 구간에도 안 들어감
    won(u["a-1"], r1, 10_000_000, path)
    won(u["b-boss"], r2, 5_000_000, path)
    rows = stats.trend(u["boss"], unit, path=path)
    assert len(rows) == stats.UNITS[unit][1]
    assert sum(x["registered"] for x in rows) == 2 and sum(x["won"] for x in rows) == 2
    assert sum(x["amount"] for x in rows) == 15_000_000 and rows[-1]["amount"] == 15_000_000


def test_scope_and_compare(env):
    path, u, a, b = env
    r1, r2 = reg(path, a, "샛별정밀", 1), reg(path, b, "누리금속", 2, owner="b-boss")
    won(u["a-1"], r1, 10_000_000, path)
    won(u["b-boss"], r2, 5_000_000, path)
    today = date.today()
    assert [(g["name"], g["amount"]) for g in stats.compare(u["boss"], today, today, path)] == \
        [("A", 10_000_000), ("B", 5_000_000)]
    mine = stats.compare(u["a-boss"], today, today, path)  # 파트너 관리자: 우리 회사 담당자별
    assert [(g["name"], g["won"]) for g in mine] == [("a-1", 1)]
    assert stats.summary(u["a-boss"], today, today, path)["amount"] == 10_000_000  # B 금액은 안 보임
    for who in ("a-1", "eng"):
        with pytest.raises(stats.StatsError):
            stats.summary(u[who], today, today, path)


def test_funnel_never_grows(env):
    path, u, a, b = env
    r1, r2, r3 = reg(path, a, "샛별정밀", 1), reg(path, a, "누리금속", 2), reg(path, a, "하람", 3)
    crm.set_stage(u["a-1"], r1, "견적·데모", path=path)
    won(u["a-1"], r2, 1_000_000, path)
    crm.set_stage(u["a-1"], r3, "첫 연락", path=path)
    crm.set_stage(u["a-1"], r3, "실패", path=path)  # 첫 연락까지 갔다가 실패
    f = stats.funnel(u["a-boss"], date.today(), date.today(), path)
    counts = [s["count"] for s in f["stages"]]
    assert counts == [3, 3, 2, 1, 1, 1] and f["dropped"] == 1
    assert all(x >= y for x, y in zip(counts, counts[1:]))


def test_ticket_stats(env):
    path, u, a, b = env
    eng = u["eng"]
    tid = tickets.create(u["a-1"], None, "오류", "멈춤", "켜면 멈춰요", path=path)
    tickets.create(u["a-1"], None, "설치", "설치", "설치가 안 돼요", path=path)
    tickets.claim(eng, tid, path=path)
    db.update_ticket(tid, {"updated_at": db.now_iso()}, path=path)
    created = datetime.fromisoformat(db.get_ticket(tid, path=path)["created_at"])
    db.add_ticket_message(tid, "eng", "engineer", "답", ts=(created + timedelta(hours=3)).isoformat(timespec="seconds"),
                          path=path)
    s = stats.ticket_stats(u["boss"], date.today(), date.today(), path)
    assert s["total"] == 2 and s["open"] == 2 and round(s["avg_first_reply_hours"]) == 3
    assert {c["category"]: c["count"] for c in s["by_category"]}["오류"] == 1
    assert stats.ticket_stats(u["b-boss"], date.today(), date.today(), path)["total"] == 0  # 다른 파트너 문의는 안 셈


def test_money():
    assert stats.money(123_000_000) == "1억 2,300만 원"
    assert stats.money(45_000_000) == "4,500만 원"
    assert stats.money(200_000_000) == "2억 원"
    assert stats.money(80_000) == "8만 원"
