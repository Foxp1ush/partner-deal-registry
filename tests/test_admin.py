from datetime import datetime, timedelta

import pytest

import admin
import db
import seed


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "t.db"
    db.init(path)
    a = db.add_partner("A", path=path)
    b = db.add_partner("B", path=path)
    sat = db.add_registration(a, "(주)샛별정밀", seed.fake_biz_no(1), "", "", "", "active", path=path)
    nuri = db.add_registration(b, "누리금속", seed.fake_biz_no(2), "", "", "", "active", path=path)
    return path, a, b, sat, nuri


def status(path, rid):
    return db.get_registration(rid, path=path)["status"]


def test_reason_required(env):
    path, a, b, sat, nuri = env
    with pytest.raises(admin.ActionError, match="사유"):
        admin.release(sat, "  ", path=path)
    assert status(path, sat) == "active"


def test_release_frees_company(env):
    path, a, b, sat, nuri = env
    admin.release(sat, "영업 중단", path=path)
    assert status(path, sat) == "released"
    assert sat not in [h["id"] for h in db.holding_registrations(path=path)]
    assert db.list_events(path=path)[0]["kind"] == "release"
    with pytest.raises(admin.ActionError):  # 해제된 건은 다시 조치 불가
        admin.extend(sat, 30, "x", path=path)


def test_extend_only_active(env):
    path, a, b, sat, nuri = env
    before = db.get_registration(sat, path=path)["expires_on"]
    new = admin.extend(sat, 30, "계약 직전", path=path)
    assert new == (datetime.fromisoformat(before) + timedelta(days=30)).date().isoformat()
    db.set_status(nuri, "expired", path=path)
    with pytest.raises(admin.ActionError):
        admin.extend(nuri, 30, "x", path=path)


def test_transfer_keeps_period_and_blocks_duplicate(env):
    path, a, b, sat, nuri = env
    exp = db.get_registration(sat, path=path)["expires_on"]
    admin.transfer(sat, b, "담당 파트너 변경", path=path)
    row = db.get_registration(sat, path=path)
    assert row["partner_id"] == b and row["expires_on"] == exp
    # B에게 같은 사업자번호 등록이 이미 있으면 이관 불가
    dup = db.add_registration(a, "누리금속 주식회사", seed.fake_biz_no(2), "", "", "", "active", path=path)
    with pytest.raises(admin.ActionError, match="받는 파트너"):
        admin.transfer(dup, b, "x", path=path)


def test_rename_blocks_exact_and_confirms_similar(env):
    path, a, b, sat, nuri = env
    with pytest.raises(admin.ActionError, match="같은 회사"):
        admin.rename(sat, "누리금속 주식회사", "오타", path=path)
    similar = admin.rename(sat, "누리금속기계", "오타", path=path)
    assert similar and db.get_registration(sat, path=path)["company_name"] == "(주)샛별정밀"
    assert admin.rename(sat, "누리금속기계", "오타", confirm_similar=True, path=path) == []
    assert db.get_registration(sat, path=path)["company_name"] == "누리금속기계"
    assert admin.rename(sat, "샛별정밀공업", "정정", path=path) == []


def test_restore_rejected(env):
    path, a, b, sat, nuri = env
    p = db.add_registration(b, "샛별정밀기계", "", "", "", "", "rejected", path=path)
    admin.restore(p, "다른 회사로 확인", path=path)
    assert status(path, p) == "active"


def test_restore_blocked_when_taken_or_expired(env):
    path, a, b, sat, nuri = env
    p = db.add_registration(b, "샛별정밀 주식회사", "", "", "", "", "rejected", path=path)
    with pytest.raises(admin.ActionError, match="그사이"):
        admin.restore(p, "x", path=path)
    old = datetime.now() - timedelta(days=db.PROTECTION_DAYS + 1)
    q = db.add_registration(b, "다른회사", "", "", "", "", "rejected", registered_at=old, path=path)
    with pytest.raises(admin.ActionError, match="기간"):
        admin.restore(q, "x", path=path)
