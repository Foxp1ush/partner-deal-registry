from datetime import datetime, timedelta

import db
import matching
import seed


def test_normalize_corp_forms():
    assert matching.normalize_name("(주)한국정밀") == matching.normalize_name("한국정밀 주식회사")
    assert matching.normalize_name("㈜ 한국정밀") == "한국정밀"


def test_biz_no_checksum():
    assert matching.is_valid_biz_no(seed.fake_biz_no(10001))
    d = matching.normalize_biz_no(seed.fake_biz_no(10001))
    wrong = d[:9] + str((int(d[9]) + 1) % 10)
    assert not matching.is_valid_biz_no(wrong)
    assert not matching.is_valid_biz_no("123")


def test_conflict_order():
    existing = [
        {"id": 1, "company_name": "(주)샛별정밀", "biz_no": seed.fake_biz_no(1)},
        {"id": 2, "company_name": "누리금속", "biz_no": seed.fake_biz_no(2)},
    ]
    # 사업자번호 일치 → 이름이 달라도 확정
    r = matching.find_conflicts("전혀다른이름", seed.fake_biz_no(2), existing)
    assert [row["id"] for row, _ in r["exact"]] == [2]
    # 법인 표기만 다름 → 확정
    r = matching.find_conflicts("샛별정밀 주식회사", "", existing)
    assert [row["id"] for row, _ in r["exact"]] == [1]
    # 비슷한 이름 → 검토 후보
    r = matching.find_conflicts("샛별정밀기계", "", existing)
    assert not r["exact"] and [row["id"] for row, _ in r["similar"]] == [1]
    # 무관한 이름 → 통과
    r = matching.find_conflicts("하람로보틱스", "", existing)
    assert not r["exact"] and not r["similar"]


def test_expiry(tmp_path):
    path = tmp_path / "t.db"
    db.init(path)
    pid = db.add_partner("P", path=path)
    old = datetime.now() - timedelta(days=db.PROTECTION_DAYS + 1)
    rid = db.add_registration(pid, "X", "", "", "", "", "active", registered_at=old, path=path)
    assert db.expire_overdue(path=path) == 1
    assert db.list_registrations(path=path)[0]["status"] == "expired"
    assert db.holding_registrations(path=path) == []
    assert db.list_events(path=path)[0]["registration_id"] == rid
