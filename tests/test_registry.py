import pytest

import ai_extract
import db
import registry
import seed


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_extract, "available", lambda: False)
    path = tmp_path / "t.db"
    db.init(path)
    a = db.add_partner("A", path=path)
    b = db.add_partner("B", path=path)
    first = registry.submit(a, "(주)샛별정밀", seed.fake_biz_no(1), path=path)
    assert first["outcome"] == "active"
    return path, a, b


def test_new_company_is_active(env):
    path, a, b = env
    r = registry.submit(b, "하람로보틱스", seed.fake_biz_no(3), path=path)
    assert r["outcome"] == "active"
    assert db.list_events(path=path)[0]["kind"] == "register"


def test_same_biz_no_blocked(env):
    path, a, b = env
    r = registry.submit(b, "전혀다른이름", seed.fake_biz_no(1), path=path)
    assert r["outcome"] == "blocked" and "다른 파트너" in r["message"]
    assert "A" not in r["message"]  # 다른 파트너 이름은 노출하지 않는다
    assert len(db.list_registrations(path=path)) == 1


def test_corp_form_only_blocked(env):
    path, a, b = env
    assert registry.submit(b, "샛별정밀 주식회사", path=path)["outcome"] == "blocked"
    r = registry.submit(a, "샛별정밀", path=path)
    assert r["outcome"] == "blocked" and "이미 등록하신" in r["message"]


def test_similar_name_pending_without_ai(env):
    path, a, b = env
    r = registry.submit(b, "샛별정밀기계", path=path)
    assert r["outcome"] == "pending" and r["ai"] == []
    reg = db.list_registrations(path=path, statuses=("pending",))[0]
    assert "샛별정밀" in reg["review_reason"]


def test_invalid_biz_no(env):
    path, a, b = env
    assert registry.submit(b, "하람로보틱스", "123-45-67890", path=path)["outcome"] == "invalid"
    assert registry.submit(b, "  ", path=path)["outcome"] == "invalid"
    assert len(db.list_registrations(path=path)) == 1


def test_approve_and_reject(env):
    path, a, b = env
    r1 = registry.submit(b, "샛별정밀기계", path=path)
    registry.approve(r1["registration_id"], path=path)
    assert r1["registration_id"] in [r["id"] for r in db.list_registrations(path=path, statuses=("active",))]
    assert db.list_events(path=path)[0]["kind"] == "approve"

    r2 = registry.submit(b, "샛별정밀공업", path=path)
    registry.reject(r2["registration_id"], path=path)
    assert db.list_registrations(path=path, statuses=("rejected",))[0]["id"] == r2["registration_id"]
    assert db.list_events(path=path)[0]["kind"] == "reject"


def test_ai_judgment_attached(env, monkeypatch):
    path, a, b = env
    monkeypatch.setattr(ai_extract, "available", lambda: True)
    monkeypatch.setattr(ai_extract, "judge_same_company",
                        lambda *a, **k: [{"index": 0, "likely_same": True, "reason": "약칭 차이"}])
    r = registry.submit(b, "샛별정밀기계", path=path)
    assert r["outcome"] == "pending" and r["ai"][0]["likely_same"]
    assert "AI 의견" in db.list_registrations(path=path, statuses=("pending",))[0]["review_reason"]
    assert db.ai_calls_today(path=path) == 1
