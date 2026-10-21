"""영업 단계 (2026-10-21): 총판 기준 Stage 0~4 + 수주·보류·실패, 예상 구매일, 예전 단계 자동 변환."""

import sqlite3
from datetime import date, timedelta

import pytest

import auth
import crm
import db
import seed


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "t.db"
    db.init(path)
    a = db.add_partner("A", path=path)
    db.add_account("a-1", auth.hash_password("pw"), "partner", a, "a-1", "rep", path=path)
    user = auth.login("a-1", "pw", path=path)
    r1 = db.add_registration(a, "샛별정밀", seed.fake_biz_no(1), "", "", "", "active", owner="a-1", path=path)
    return path, user, r1


def in_days(n: int) -> str:
    return (date.today() + timedelta(days=n)).isoformat()


def test_new_registration_starts_at_stage_0(env):
    path, user, r1 = env
    reg = db.get_registration(r1, path=path)
    assert reg["stage"] == "명함 확보" and crm.stage_label(reg["stage"]) == "Stage 0 명함 확보"
    assert crm.stage_label("수주") == "수주"
    assert set(crm.STAGE_GUIDE) == set(crm.STAGES)


def test_forecast_stages_need_expected_close(env):
    path, user, r1 = env
    with pytest.raises(crm.CrmError):
        crm.set_stage(user, r1, "구매 검토", path=path)  # 예상 구매일 없음
    with pytest.raises(crm.CrmError):
        crm.set_stage(user, r1, "구매 임박", expected_close=in_days(60), path=path)  # 1개월 넘음
    crm.set_stage(user, r1, "구매 검토", expected_close=in_days(60), path=path)
    assert db.get_registration(r1, path=path)["expected_close"] == in_days(60)
    crm.set_stage(user, r1, "구매 임박", expected_close=in_days(20), path=path)
    # 같은 단계에서 날짜만 바꾸기
    crm.set_stage(user, r1, "구매 임박", expected_close=in_days(10), path=path)
    assert "예상 구매일" in crm.timeline(user, r1, path=path)[0]["note"]
    # 예측 단계를 벗어나면 예상 구매일을 지운다
    crm.set_stage(user, r1, "보류", path=path)
    assert db.get_registration(r1, path=path)["expected_close"] == ""


def test_forecast_and_overdue(env):
    path, user, r1 = env
    crm.set_stage(user, r1, "구매 임박", expected_close=in_days(10), path=path)
    assert crm.forecast(path=path)["A"]["1개월 안 (Stage 4)"] == 1
    db.update_registration(r1, {"expected_close": in_days(-3)}, allowed=db.ADMIN_EDITABLE, path=path)
    reg = db.get_registration(r1, path=path)
    assert crm.is_overdue(reg)
    assert crm.forecast(path=path)["A"] == {"1개월 안 (Stage 4)": 0, "3개월 안 (Stage 3)": 0, "예상일 지남": 1}


def test_old_stage_names_are_migrated(tmp_path):
    path = tmp_path / "old.db"
    db.init(path)
    a = db.add_partner("A", path=path)
    rid = db.add_registration(a, "X", "", "", "", "", "active", path=path)
    with sqlite3.connect(path) as c:  # 예전 DB에 남아 있던 단계 이름
        c.execute("UPDATE registrations SET stage='갱신 협상' WHERE id=?", (rid,))
    db.init(path)
    assert db.get_registration(rid, path=path)["stage"] == "구매 검토"
