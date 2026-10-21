"""2026-10-21 정한 정책: 수주 후 계약 기간 보호, 이관 시 기록 비공개, 연장 횟수 표시(제한 없음)."""

from datetime import date, timedelta

import pytest

import accounts
import admin
import auth
import crm
import db
import matching
import seed


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "t.db"
    db.init(path)
    a = db.add_partner("A", path=path)
    b = db.add_partner("B", path=path)
    for username, pid, role in [("a-boss", a, "manager"), ("a-1", a, "rep"), ("b-boss", b, "manager")]:
        db.add_account(username, auth.hash_password("pw"), "partner", pid, username, role, path=path)
    users = {u: auth.login(u, "pw", path=path) for u in ("a-boss", "a-1", "b-boss")}
    r1 = db.add_registration(a, "샛별정밀", seed.fake_biz_no(1), "", "", "", "active", owner="a-1", path=path)
    return path, users, a, b, r1


def in_days(n: int) -> str:
    return (date.today() + timedelta(days=n)).isoformat()


def status(path, rid):
    return db.get_registration(rid, path=path)["status"]


# ---- ① 수주 후 계약 기간 동안 보호 -------------------------------------------

def test_won_holds_until_contract_end(env):
    path, u, a, b, r1 = env
    crm.set_stage(u["a-1"], r1, "수주", contract_end=in_days(365), path=path)
    reg = db.get_registration(r1, path=path)
    assert reg["status"] == "won" and reg["expires_on"] == in_days(365)
    # 다른 파트너는 여전히 같은 회사로 막힌다
    found = matching.find_conflicts("(주)샛별정밀", "", db.holding_registrations(path=path))
    assert found["exact"]
    assert any(e["kind"] == "won" for e in db.list_events(path=path))
    assert "계약 종료일" in crm.timeline(u["a-1"], r1, path=path)[0]["note"]


def test_won_needs_valid_contract_end(env):
    path, u, a, b, r1 = env
    with pytest.raises(crm.CrmError):
        crm.set_stage(u["a-1"], r1, "수주", path=path)  # 종료일 없음
    with pytest.raises(crm.CrmError):
        crm.set_stage(u["a-1"], r1, "수주", contract_end=date.today().isoformat(), path=path)
    with pytest.raises(crm.CrmError):
        crm.set_stage(u["a-1"], r1, "수주", contract_end=in_days(db.CONTRACT_MAX_DAYS + 1), path=path)
    assert status(path, r1) == "active"
    crm.set_stage(u["a-1"], r1, "견적·데모", path=path)  # 다른 단계는 종료일이 필요 없다
    assert status(path, r1) == "active"


def test_pending_cannot_be_won(env):
    path, u, a, b, r1 = env
    db.set_status(r1, "pending", path=path)
    with pytest.raises(crm.CrmError):
        crm.set_stage(u["a-1"], r1, "수주", contract_end=in_days(30), path=path)


def test_renew_extends_contract(env):
    path, u, a, b, r1 = env
    with pytest.raises(crm.CrmError):
        crm.renew_contract(u["a-1"], r1, in_days(400), path=path)  # 아직 계약 고객 아님
    crm.set_stage(u["a-1"], r1, "수주", contract_end=in_days(30), path=path)
    with pytest.raises(crm.CrmError):
        crm.renew_contract(u["a-1"], r1, in_days(20), path=path)  # 지금보다 앞당길 수 없음
    assert crm.renew_contract(u["a-1"], r1, in_days(395), path=path) == in_days(395)
    assert db.get_registration(r1, path=path)["expires_on"] == in_days(395)
    assert any(e["kind"] == "renew" for e in db.list_events(path=path))
    with pytest.raises(crm.CrmError):
        crm.renew_contract(u["b-boss"], r1, in_days(500), path=path)  # 남의 고객


def test_won_expires_after_contract_end(env):
    path, u, a, b, r1 = env
    crm.set_stage(u["a-1"], r1, "수주", contract_end=in_days(30), path=path)
    db.update_registration(r1, {"expires_on": in_days(-1)}, allowed=db.ADMIN_EDITABLE, path=path)
    assert db.expire_overdue(path=path) == 1
    assert status(path, r1) == "expired"
    assert "계약 종료일 경과" in db.list_events(path=path)[0]["detail"]


def test_won_alert_window(env):
    path, u, a, b, r1 = env
    crm.set_stage(u["a-1"], r1, "수주", contract_end=in_days(45), path=path)
    assert [r["id"] for r in db.expiring_soon(path=path)] == [r1]  # 계약 고객은 60일 안이면 알림
    crm.renew_contract(u["a-1"], r1, in_days(400), path=path)
    assert db.expiring_soon(path=path) == []


def test_admin_actions_on_won(env):
    path, u, a, b, r1 = env
    crm.set_stage(u["a-1"], r1, "수주", contract_end=in_days(200), path=path)
    with pytest.raises(admin.ActionError):
        admin.extend(r1, 30, "연장", path=path)  # 계약 고객은 연장 대상 아님
    admin.transfer(r1, b, "고객 요청", path=path)
    reg = db.get_registration(r1, path=path)
    assert reg["partner_id"] == b and reg["status"] == "won" and reg["expires_on"] == in_days(200)
    admin.release(r1, "계약 해지", path=path)
    assert status(path, r1) == "released"


def test_end_contract_releases_won(env):
    path, u, a, b, r1 = env
    crm.set_stage(u["a-1"], r1, "수주", contract_end=in_days(200), path=path)
    db.add_account("boss", auth.hash_password("pw"), "admin", path=path)
    assert accounts.end_contract(auth.login("boss", "pw", path=path), a, "계약 종료", True, path=path) == 1
    assert status(path, r1) == "released"


# ---- ② 이관 시 이전 파트너 기록은 비공개·보존 ----------------------------------

def test_transfer_keeps_previous_notes_private_but_preserved(env):
    path, u, a, b, r1 = env
    crm.add_activity(u["a-1"], r1, "미팅", "A의 메모", path=path)
    admin.transfer(r1, b, "담당 변경", path=path)
    assert crm.timeline(u["b-boss"], r1, path=path) == []
    # 지워지지 않고 남아 있다: 총판이 사유를 남기고 열면 A가 쓴 기록이 보인다
    notes = [x["note"] for x in db.list_activities([r1], path=path)]
    assert "A의 메모" in notes


# ---- ③ 연장 횟수: 제한 없음, 등록별·고객사 누적 표시 ---------------------------

def test_extension_counts_per_registration_and_customer(env):
    path, u, a, b, r1 = env
    for _ in range(3):  # 제한 없이 연장된다
        admin.extend(r1, 30, "고객 일정 지연", path=path)
    assert admin.extension_counts(path=path)[r1] == (3, 3)
    # 만료 후 다른 파트너가 같은 회사(표기만 다름)를 다시 등록해 1회 연장 → 고객사 누적에 합산
    db.set_status(r1, "expired", path=path)
    r2 = db.add_registration(b, "샛별정밀 주식회사", "", "", "", "", "active", path=path)
    admin.extend(r2, 30, "예산 심의 지연", path=path)
    counts = admin.extension_counts(path=path)
    assert counts[r2] == (1, 4) and counts[r1] == (3, 4)
    other = db.add_registration(a, "누리금속", seed.fake_biz_no(2), "", "", "", "active", path=path)
    assert admin.extension_counts(path=path)[other] == (0, 0)
