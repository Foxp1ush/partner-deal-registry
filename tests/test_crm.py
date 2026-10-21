from datetime import datetime, timedelta

import pytest

import admin
import auth
import crm
import db
import seed


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "t.db"
    db.init(path)
    a = db.add_partner("A", path=path)
    b = db.add_partner("B", path=path)
    for username, pid, role in [("a-boss", a, "manager"), ("a-1", a, "rep"), ("a-2", a, "rep"), ("b-boss", b, "manager")]:
        db.add_account(username, auth.hash_password("pw"), "partner", pid, username, role, path=path)
    users = {u: auth.login(u, "pw", path=path) for u in ("a-boss", "a-1", "a-2", "b-boss")}
    r1 = db.add_registration(a, "샛별정밀", seed.fake_biz_no(1), "", "", "", "active", owner="a-1", path=path)
    r2 = db.add_registration(a, "누리금속", seed.fake_biz_no(2), "", "", "", "active", owner="a-2", path=path)
    return path, users, a, b, r1, r2


def ids(rows):
    return sorted(r["id"] for r in rows)


def test_rep_sees_only_own(env):
    path, u, a, b, r1, r2 = env
    assert ids(crm.visible_registrations(u["a-1"], path=path)) == [r1]
    assert ids(crm.visible_registrations(u["a-boss"], path=path)) == [r1, r2]
    assert crm.visible_registrations(u["b-boss"], path=path) == []
    with pytest.raises(crm.CrmError):
        crm.add_activity(u["a-1"], r2, "전화", "남의 고객", path=path)
    with pytest.raises(crm.CrmError):
        crm.timeline(u["b-boss"], r1, path=path)


def test_activity_and_stage(env):
    path, u, a, b, r1, r2 = env
    crm.add_activity(u["a-1"], r1, "미팅", "첫 미팅", path=path)
    crm.set_stage(u["a-1"], r1, "견적·데모", path=path)
    t = crm.timeline(u["a-1"], r1, path=path)
    assert {x["kind"] for x in t} == {"미팅", "단계 변경"}
    assert db.get_registration(r1, path=path)["stage"] == "견적·데모"
    with pytest.raises(crm.CrmError):
        crm.add_activity(u["a-1"], r1, "미팅", "  ", path=path)
    with pytest.raises(crm.CrmError):
        crm.add_activity(u["a-1"], r1, "미팅", "x" * (crm.NOTE_MAX + 1), path=path)


def test_history_continues_across_reregistration(env):
    path, u, a, b, r1, r2 = env
    old = db.add_registration(a, "(주)샛별정밀", seed.fake_biz_no(1), "", "", "", "expired",
                              registered_at=datetime.now() - timedelta(days=700), owner="a-1", path=path)
    db.add_activity(old, a, "a-1", "계약", "2년 전 첫 계약", on_date="2024-10-01", path=path)
    notes = [x["note"] for x in crm.timeline(u["a-1"], r1, path=path)]
    assert "2년 전 첫 계약" in notes


def test_transfer_hides_previous_partner_notes(env):
    path, u, a, b, r1, r2 = env
    crm.add_activity(u["a-1"], r1, "미팅", "A의 비밀 메모", path=path)
    admin.transfer(r1, b, "담당 변경", path=path)
    reg = db.get_registration(r1, path=path)
    assert reg["owner"] == ""
    assert crm.timeline(u["b-boss"], r1, path=path) == []  # B는 A의 기록을 못 봄
    assert crm.visible_registrations(u["a-1"], path=path) == []


def test_deactivate_hands_over(env):
    path, u, a, b, r1, r2 = env
    with pytest.raises(crm.CrmError):
        crm.deactivate(u["a-1"], "a-2", "a-1", path=path)  # 영업 담당자는 못 함
    with pytest.raises(crm.CrmError):
        crm.deactivate(u["a-boss"], "b-boss", "a-1", path=path)  # 다른 파트너 직원
    assert crm.deactivate(u["a-boss"], "a-1", "a-2", path=path) == 1
    assert auth.login("a-1", "pw", path=path) is None
    assert ids(crm.visible_registrations(u["a-2"], path=path)) == [r1, r2]
    assert crm.timeline(u["a-2"], r1, path=path)[0]["kind"] == "담당자 변경"
    with pytest.raises(crm.CrmError):
        crm.reassign(u["a-boss"], r1, "a-1", path=path)  # 퇴사자에게 배정 불가


def test_admin_sees_aggregate_and_logged_detail(env):
    path, u, a, b, r1, r2 = env
    crm.set_stage(u["a-1"], r1, "첫 연락", path=path)
    assert crm.pipeline(path=path)["A"]["첫 연락"] == 1
    with pytest.raises(crm.CrmError):
        crm.admin_open_detail("admin", r1, "짧음", path=path)
    assert db.list_access_log(a, path=path) == []
    detail = crm.admin_open_detail("admin", r1, "선점 분쟁 확인", path=path)
    assert detail and detail[0]["kind"] == "단계 변경"
    log = db.list_access_log(a, path=path)
    assert log[0]["reason"] == "선점 분쟁 확인" and log[0]["company_name"] == "샛별정밀"
    assert db.list_access_log(b, path=path) == []
