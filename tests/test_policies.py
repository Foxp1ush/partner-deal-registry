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

MANAGER = {"role": "admin", "partner_role": "manager", "username": "admin"}  # 총판 총괄 매니저


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


def won(start: int = 0, email: str = "cam@satbyeol.example", kind: str = "perpetual") -> dict:
    """수주 필수 정보: 라이선스 종류, 라이선스 시작일(오늘부터 start일 뒤), 고객사 ENCY 계정 이메일, 계약 금액."""
    return {"license_start": in_days(start), "ency_account": email, "license_type": kind,
            "contract_amount": 12_000_000}


# ---- ① 수주 후 계약 기간 동안 보호 -------------------------------------------

def test_won_holds_until_contract_end(env):
    path, u, a, b, r1 = env
    crm.set_stage(u["a-1"], r1, "수주", **won(), contract_end=in_days(365), path=path)
    reg = db.get_registration(r1, path=path)
    assert reg["status"] == "won" and reg["expires_on"] == in_days(365)
    # 다른 파트너는 여전히 같은 회사로 막힌다
    found = matching.find_conflicts("(주)샛별정밀", "", db.holding_registrations(path=path))
    assert found["exact"]
    assert any(e["kind"] == "won" for e in db.list_events(path=path))
    assert "유지보수 종료일" in crm.timeline(u["a-1"], r1, path=path)[0]["note"]


def test_won_from_license_start(env):
    # ENCY: 라이선스 시작일 + 1년(무상 유지보수)까지 보호. ENCY 계정 이메일도 함께 저장
    path, u, a, b, r1 = env
    crm.set_stage(u["a-1"], r1, "수주", **won(), path=path)
    reg = db.get_registration(r1, path=path)
    assert reg["status"] == "won" and reg["license_start"] == in_days(0) and reg["expires_on"] == in_days(365)
    assert reg["ency_account"] == "cam@satbyeol.example"
    assert "cam@satbyeol.example" in next(e for e in db.list_events(path=path) if e["kind"] == "won")["detail"]


def test_won_requires_license_start_and_email(env):
    path, u, a, b, r1 = env
    with pytest.raises(crm.CrmError, match="라이선스 종류"):
        crm.set_stage(u["a-1"], r1, "수주", license_start=in_days(0), ency_account="cam@satbyeol.example", path=path)
    with pytest.raises(crm.CrmError, match="라이선스 시작일"):
        crm.set_stage(u["a-1"], r1, "수주", ency_account="cam@satbyeol.example", license_type="perpetual", path=path)
    with pytest.raises(crm.CrmError, match="ENCY 계정 이메일"):
        crm.set_stage(u["a-1"], r1, "수주", license_start=in_days(0), license_type="perpetual", path=path)
    for bad in ("cam", "cam@", "cam@satbyeol", "c am@satbyeol.example"):
        with pytest.raises(crm.CrmError, match="형식"):
            crm.set_stage(u["a-1"], r1, "수주", **won(email=bad), path=path)
    assert status(path, r1) == "active"


def test_ency_issue_checklist(env):
    path, u, a, b, r1 = env
    assert admin.ency_queue(path=path) == ([], [])
    with pytest.raises(admin.ActionError):
        admin.mark_ency_issued(MANAGER, r1, path=path)  # 아직 수주 전
    crm.set_stage(u["a-1"], r1, "수주", **won(), path=path)
    waiting, done = admin.ency_queue(path=path)
    assert [r["id"] for r in waiting] == [r1] and done == []
    admin.mark_ency_issued(MANAGER, r1, path=path)
    waiting, done = admin.ency_queue(path=path)
    assert waiting == [] and [r["id"] for r in done] == [r1] and done[0]["ency_issued_at"]
    with pytest.raises(admin.ActionError):
        admin.mark_ency_issued(MANAGER, r1, path=path)  # 두 번 누를 수 없음
    with pytest.raises(admin.ActionError):
        admin.undo_ency_issued(MANAGER, r1, "  ", path=path)  # 취소는 사유 필수
    admin.undo_ency_issued(MANAGER, r1, "다른 고객사로 잘못 표시", path=path)
    assert [r["id"] for r in admin.ency_queue(path=path)[0]] == [r1]
    assert sum(e["kind"] == "ency_issued" for e in db.list_events(path=path)) == 2


def test_won_license_start_later_than_contract(env):
    # 계약일과 라이선스 시작일이 다를 수 있다: 두 달 뒤 시작 → 그로부터 1년
    path, u, a, b, r1 = env
    crm.set_stage(u["a-1"], r1, "수주", **won(60), path=path)
    reg = db.get_registration(r1, path=path)
    assert reg["license_start"] == in_days(60) and reg["expires_on"] == in_days(425)


def test_won_license_start_too_old(env):
    # 무상 유지보수 1년이 이미 지난 시작일은 막는다
    path, u, a, b, r1 = env
    with pytest.raises(crm.CrmError, match="이미 지났습니다"):
        crm.set_stage(u["a-1"], r1, "수주", **won(-400), path=path)
    assert status(path, r1) == "active"


def test_won_needs_valid_contract_end(env):
    path, u, a, b, r1 = env
    with pytest.raises(crm.CrmError):
        crm.set_stage(u["a-1"], r1, "수주", **won(), contract_end=date.today().isoformat(), path=path)
    with pytest.raises(crm.CrmError):
        crm.set_stage(u["a-1"], r1, "수주", **won(), contract_end=in_days(db.CONTRACT_MAX_DAYS + 1), path=path)
    assert status(path, r1) == "active"
    crm.set_stage(u["a-1"], r1, "견적·데모", path=path)  # 다른 단계는 종료일이 필요 없다
    assert status(path, r1) == "active"


def test_pending_cannot_be_won(env):
    path, u, a, b, r1 = env
    db.set_status(r1, "pending", path=path)
    with pytest.raises(crm.CrmError):
        crm.set_stage(u["a-1"], r1, "수주", **won(), contract_end=in_days(30), path=path)


def test_renew_extends_contract(env):
    path, u, a, b, r1 = env
    with pytest.raises(crm.CrmError):
        crm.renew_contract(u["a-1"], r1, in_days(400), path=path)  # 아직 계약 고객 아님
    crm.set_stage(u["a-1"], r1, "수주", **won(), contract_end=in_days(30), path=path)
    with pytest.raises(crm.CrmError):
        crm.renew_contract(u["a-1"], r1, in_days(20), path=path)  # 지금보다 앞당길 수 없음
    assert crm.renew_contract(u["a-1"], r1, in_days(395), path=path) == in_days(395)
    assert db.get_registration(r1, path=path)["expires_on"] == in_days(395)
    assert any(e["kind"] == "renew" for e in db.list_events(path=path))
    with pytest.raises(crm.CrmError):
        crm.renew_contract(u["b-boss"], r1, in_days(500), path=path)  # 남의 고객


def test_subscription_grace_then_expires(env):
    # 구독이 끝나면 90일 갱신 유예(담당 유지) → 그래도 갱신이 없으면 만료, 누구나 등록 가능
    path, u, a, b, r1 = env
    crm.set_stage(u["a-1"], r1, "수주", **won(kind="sub1"), path=path)
    assert db.get_registration(r1, path=path)["expires_on"] == in_days(365)
    db.update_registration(r1, {"expires_on": in_days(-1)}, allowed=db.ADMIN_EDITABLE, path=path)
    assert db.expire_overdue(path=path) == 1
    reg = db.get_registration(r1, path=path)
    assert reg["status"] == "lapsed" and "갱신 유예" in crm.relation(reg)
    assert "90일 갱신 유예" in db.list_events(path=path)[0]["detail"]
    db.update_registration(r1, {"expires_on": in_days(-(db.SUB_GRACE_DAYS + 1))}, allowed=db.ADMIN_EDITABLE, path=path)
    assert db.expire_overdue(path=path) == 1
    assert status(path, r1) == "expired"
    assert "유예 종료" in db.list_events(path=path)[0]["detail"]


def test_contract_customer_goes_to_review_not_block(env):
    # 계약 고객엔 보호 '기간'이 없다: 자동 차단 목록이 아니라 담당 고객 목록(총판 검토)에 있다
    path, u, a, b, r1 = env
    assert r1 in [h["id"] for h in db.blocking_registrations(path=path)]   # 가망 고객일 땐 자동 차단
    crm.set_stage(u["a-1"], r1, "수주", **won(), path=path)
    assert r1 not in [h["id"] for h in db.blocking_registrations(path=path)]
    assert r1 in [h["id"] for h in db.owned_registrations(path=path)]
    assert r1 in [h["id"] for h in db.holding_registrations(path=path)]    # 총판 조치 중복 확인엔 포함
    assert crm.relation(db.get_registration(r1, path=path)).startswith("담당 고객")


def test_sub3_term(env):
    path, u, a, b, r1 = env
    crm.set_stage(u["a-1"], r1, "수주", **won(kind="sub3"), path=path)
    assert db.get_registration(r1, path=path)["expires_on"] == in_days(365 * 3)


def test_perpetual_lapses_but_stays_with_partner(env):
    # 영구는 유지보수가 끝나도 고객이 계속 쓴다 → lapsed. 원래 파트너가 담당으로 남는다
    path, u, a, b, r1 = env
    crm.set_stage(u["a-1"], r1, "수주", **won(), path=path)
    db.update_registration(r1, {"expires_on": in_days(-1)}, allowed=db.ADMIN_EDITABLE, path=path)
    assert db.expire_overdue(path=path) == 1
    reg = db.get_registration(r1, path=path)
    assert reg["status"] == "lapsed" and reg["partner_id"] == a
    assert "유지보수 종료" in db.list_events(path=path)[0]["detail"]
    # 자동 차단(holding)에선 빠지고, 별도 목록에서 총판 검토 대상으로 잡힌다
    assert r1 not in [h["id"] for h in db.blocking_registrations(path=path)]
    assert matching.find_conflicts("샛별정밀 주식회사", "", db.owned_registrations(path=path))["exact"]
    # 원래 파트너가 계속 본다
    assert r1 in [r["id"] for r in crm.visible_registrations(u["a-1"], path=path)]


def test_perpetual_rejoin_maintenance(env):
    path, u, a, b, r1 = env
    crm.set_stage(u["a-1"], r1, "수주", **won(), path=path)
    db.update_registration(r1, {"expires_on": in_days(-400)}, allowed=db.ADMIN_EDITABLE, path=path)
    db.expire_overdue(path=path)
    reg = db.get_registration(r1, path=path)
    assert crm.renew_label(reg) == "유지보수 재가입"
    assert crm.renewal_default(reg) == in_days(365)  # 끝난 지 오래됐으니 오늘부터 1년
    crm.renew_contract(u["a-1"], r1, crm.renewal_default(reg), path=path)
    reg = db.get_registration(r1, path=path)
    assert reg["status"] == "won" and reg["expires_on"] == in_days(365)
    assert "유지보수 재가입" in db.list_events(path=path)[0]["detail"]


def test_renew_labels_and_defaults(env):
    path, u, a, b, r1 = env
    crm.set_stage(u["a-1"], r1, "수주", **won(kind="sub1"), path=path)
    reg = db.get_registration(r1, path=path)
    assert crm.renew_label(reg) == "구독 갱신" and crm.end_label(reg) == "구독 종료"
    assert crm.renewal_default(reg, 3) == (date.fromisoformat(reg["expires_on"]) + timedelta(days=365 * 3)).isoformat()


def test_ency_done_list_is_cumulative(env):
    path, u, a, b, r1 = env
    crm.set_stage(u["a-1"], r1, "수주", **won(), path=path)
    admin.mark_ency_issued(MANAGER, r1, path=path)
    db.update_registration(r1, {"expires_on": in_days(-1)}, allowed=db.ADMIN_EDITABLE, path=path)
    db.expire_overdue(path=path)  # 유지보수 만료 → 그래도 발급 완료 목록에 남는다
    waiting, done = admin.ency_queue(path=path)
    assert [r["id"] for r in done] == [r1] and admin.account_status(done[0]) == "유지보수 만료 (사용 중)"


def test_existing_account_detected_on_rewin(env):
    path, u, a, b, r1 = env
    crm.set_stage(u["a-1"], r1, "수주", **won(kind="sub1"), path=path)
    admin.mark_ency_issued(MANAGER, r1, path=path)
    admin.release(MANAGER, r1, "구독 종료 후 정리", path=path)
    # 다른 파트너가 같은 회사를 다시 수주, 같은 이메일 → 기존 계정 있음
    r2 = db.add_registration(b, "샛별정밀 주식회사", "", "", "", "", "active", owner="b-boss", path=path)
    crm.set_stage(u["b-boss"], r2, "수주", **won(kind="sub1"), path=path)
    row2 = db.get_registration(r2, path=path)
    assert admin.existing_account(row2, path=path)["id"] == r1
    assert admin.existing_account({**row2, "ency_account": "other@x.example"}, path=path) is None


def test_won_alert_window(env):
    path, u, a, b, r1 = env
    crm.set_stage(u["a-1"], r1, "수주", **won(), contract_end=in_days(45), path=path)
    assert [r["id"] for r in db.expiring_soon(path=path)] == [r1]  # 계약 고객은 60일 안이면 알림
    crm.renew_contract(u["a-1"], r1, in_days(400), path=path)
    assert db.expiring_soon(path=path) == []


def test_admin_actions_on_won(env):
    path, u, a, b, r1 = env
    crm.set_stage(u["a-1"], r1, "수주", **won(), contract_end=in_days(200), path=path)
    with pytest.raises(admin.ActionError):
        admin.extend(MANAGER, r1, 30, "연장", path=path)  # 계약 고객은 연장 대상 아님
    admin.transfer(MANAGER, r1, b, "고객 요청", path=path)
    reg = db.get_registration(r1, path=path)
    assert reg["partner_id"] == b and reg["status"] == "won" and reg["expires_on"] == in_days(200)
    admin.release(MANAGER, r1, "계약 해지", path=path)
    assert status(path, r1) == "released"


def test_end_contract_releases_won(env):
    path, u, a, b, r1 = env
    crm.set_stage(u["a-1"], r1, "수주", **won(), contract_end=in_days(200), path=path)
    db.add_account("boss", auth.hash_password("pw"), "admin", path=path)
    assert accounts.end_contract(auth.login("boss", "pw", path=path), a, "계약 종료", True, path=path) == 1
    assert status(path, r1) == "released"


# ---- ② 이관 시 이전 파트너 기록은 비공개·보존 ----------------------------------

def test_transfer_keeps_previous_notes_private_but_preserved(env):
    path, u, a, b, r1 = env
    crm.add_activity(u["a-1"], r1, "미팅", "A의 메모", path=path)
    admin.transfer(MANAGER, r1, b, "담당 변경", path=path)
    assert crm.timeline(u["b-boss"], r1, path=path) == []
    # 지워지지 않고 남아 있다: 총판이 사유를 남기고 열면 A가 쓴 기록이 보인다
    notes = [x["note"] for x in db.list_activities([r1], path=path)]
    assert "A의 메모" in notes


# ---- ③ 연장 횟수: 제한 없음, 등록별·고객사 누적 표시 ---------------------------

def test_extension_counts_per_registration_and_customer(env):
    path, u, a, b, r1 = env
    for _ in range(3):  # 제한 없이 연장된다
        admin.extend(MANAGER, r1, 30, "고객 일정 지연", path=path)
    assert admin.extension_counts(path=path)[r1] == (3, 3)
    # 만료 후 다른 파트너가 같은 회사(표기만 다름)를 다시 등록해 1회 연장 → 고객사 누적에 합산
    db.set_status(r1, "expired", path=path)
    r2 = db.add_registration(b, "샛별정밀 주식회사", "", "", "", "", "active", path=path)
    admin.extend(MANAGER, r2, 30, "예산 심의 지연", path=path)
    counts = admin.extension_counts(path=path)
    assert counts[r2] == (1, 4) and counts[r1] == (3, 4)
    other = db.add_registration(a, "누리금속", seed.fake_biz_no(2), "", "", "", "active", path=path)
    assert admin.extension_counts(path=path)[other] == (0, 0)
