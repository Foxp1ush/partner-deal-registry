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
        crm.set_stage(user, r1, "구매 임박", path=path)  # '구매 임박'은 예상 구매일 필수
    crm.set_stage(user, r1, "구매 검토", path=path)        # '구매 검토'는 비워도 된다
    assert db.get_registration(r1, path=path)["stage"] == "구매 검토"
    assert db.get_registration(r1, path=path)["expected_close"] == ""
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
    assert crm.forecast(path=path)["A"] == {"1개월 안 (Stage 4)": 0, "3개월 안 (Stage 3)": 0,
                                            "시기 미정 (Stage 3)": 0, "예상일 지남": 1}


def test_forecast_undated_review(env):
    path, user, r1 = env
    crm.set_stage(user, r1, "구매 검토", path=path)
    assert crm.forecast(path=path)["A"]["시기 미정 (Stage 3)"] == 1
    crm.set_stage(user, r1, "구매 검토", expected_close=in_days(40), path=path)  # 나중에 날짜를 채움
    assert crm.forecast(path=path)["A"]["3개월 안 (Stage 3)"] == 1


def test_search_customers():
    rows = [
        {"company_name": "(주)샛별정밀", "biz_no": "000-01-00012", "contact_name": "김하늘",
         "contact_phone": "010-0000-1001", "stage": "견적·데모"},
        {"company_name": "한빛정밀", "biz_no": "", "contact_name": "강민호",
         "contact_phone": "010-0000-1009", "stage": "명함 확보"},
        {"company_name": "누리금속 주식회사", "biz_no": "000-01-00026", "contact_name": "이도윤",
         "contact_phone": "010-0000-1002", "stage": "구매 임박"},
    ]
    names = lambda q="", s=None: [r["company_name"] for r in crm.search(rows, q, s)]
    assert len(names()) == 3
    assert names("샛별 정밀") == ["(주)샛별정밀"]          # 띄어쓰기·법인 표기 무시
    assert names("정밀") == ["(주)샛별정밀", "한빛정밀"]
    assert names("00026") == ["누리금속 주식회사"]           # 사업자번호 일부
    assert names("1009") == ["한빛정밀"]                    # 연락처 일부
    assert names("강민호") == ["한빛정밀"]                  # 고객 담당자
    assert names("정밀", ["명함 확보"]) == ["한빛정밀"]      # 검색어 + 단계
    assert names("", ["구매 임박"]) == ["누리금속 주식회사"]
    assert names("없는회사") == []


def test_old_stage_names_are_migrated(tmp_path):
    path = tmp_path / "old.db"
    db.init(path)
    a = db.add_partner("A", path=path)
    rid = db.add_registration(a, "X", "", "", "", "", "active", path=path)
    with sqlite3.connect(path) as c:  # 예전 DB에 남아 있던 단계 이름
        c.execute("UPDATE registrations SET stage='갱신 협상' WHERE id=?", (rid,))
    db.init(path)
    assert db.get_registration(rid, path=path)["stage"] == "구매 검토"
