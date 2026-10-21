import pytest

import accounts
import auth
import db
import seed


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "t.db"
    db.init(path)
    adm_hash = auth.hash_password("admin-pw")
    db.add_account("admin", adm_hash, "admin", path=path)
    admin = auth.login("admin", "admin-pw", path=path)
    return path, admin


def new_partner(path, admin, name="새파트너", user="new-boss"):
    pw = accounts.create_partner(admin, name, user, "대표", path=path)
    return pw, auth.login(user, pw, path=path)


def test_create_partner_issues_temp_password(env):
    path, admin = env
    pw, boss = new_partner(path, admin)
    assert boss["partner_role"] == "manager" and boss["must_change_pw"]
    with pytest.raises(accounts.AccountError):
        accounts.create_partner(admin, "새파트너", "x", "", path=path)  # 같은 이름
    with pytest.raises(accounts.AccountError):
        accounts.create_partner(boss, "다른곳", "y", "", path=path)  # 파트너는 못 함
    assert pw not in "".join(e["detail"] for e in db.list_events(path=path))  # 비밀번호는 기록하지 않음


def test_change_own_password_rules(env):
    path, admin = env
    pw, boss = new_partner(path, admin)
    with pytest.raises(accounts.AccountError, match="현재"):
        accounts.change_own_password(boss, "wrong", "abcdefgh1", "abcdefgh1", path=path)
    with pytest.raises(accounts.AccountError, match="서로"):
        accounts.change_own_password(boss, pw, "abcdefgh1", "abcdefgh2", path=path)
    with pytest.raises(accounts.AccountError, match="8자"):
        accounts.change_own_password(boss, pw, "short", "short", path=path)
    with pytest.raises(accounts.AccountError, match="아이디"):
        accounts.change_own_password(boss, pw, "new-boss123", "new-boss123", path=path)
    accounts.change_own_password(boss, pw, "good-pass-1", "good-pass-1", path=path)
    assert auth.login("new-boss", pw, path=path) is None
    assert not auth.login("new-boss", "good-pass-1", path=path)["must_change_pw"]


def test_staff_and_reset_scope(env):
    path, admin = env
    _, boss = new_partner(path, admin)
    _, other_boss = new_partner(path, admin, "다른파트너", "other-boss")
    staff_pw = accounts.add_staff(boss, "new-rep", "직원", path=path)
    rep = auth.login("new-rep", staff_pw, path=path)
    assert rep["partner_role"] == "rep" and rep["must_change_pw"]
    with pytest.raises(accounts.AccountError):
        accounts.add_staff(rep, "x", "", path=path)  # 담당자는 계정 못 만듦
    with pytest.raises(accounts.AccountError):
        accounts.reset_password(other_boss, "new-rep", path=path)  # 다른 회사 직원
    new_pw = accounts.reset_password(boss, "new-rep", path=path)
    assert auth.login("new-rep", staff_pw, path=path) is None
    assert auth.login("new-rep", new_pw, path=path)["must_change_pw"]
    with pytest.raises(accounts.AccountError):
        accounts.reset_password(boss, "admin", path=path)
    assert accounts.reset_password(admin, "new-boss", path=path)  # 총판은 파트너 관리자 재설정 가능


def test_last_manager_cannot_step_down(env):
    path, admin = env
    _, boss = new_partner(path, admin)
    accounts.add_staff(boss, "new-rep", "직원", path=path)
    with pytest.raises(accounts.AccountError, match="한 명도"):
        accounts.set_role(boss, "new-boss", "rep", path=path)
    accounts.set_role(boss, "new-rep", "manager", path=path)
    accounts.set_role(boss, "new-boss", "rep", path=path)  # 이제 다른 관리자가 있으니 가능
    assert auth.refresh(boss, path=path)["partner_role"] == "rep"  # 로그인 중에도 바로 반영


def test_end_contract_blocks_all_and_can_release(env):
    path, admin = env
    _, boss = new_partner(path, admin)
    pid = boss["partner_id"]
    db.add_registration(pid, "샛별정밀", seed.fake_biz_no(1), "", "", "", "active", path=path)
    assert accounts.end_contract(admin, pid, "계약 만료", release_registrations=True, path=path) == 1
    assert auth.refresh(boss, path=path) is None
    assert db.holding_registrations(path=path) == []
    with pytest.raises(accounts.AccountError):
        accounts.end_contract(admin, pid, "x", False, path=path)
    accounts.resume_contract(admin, pid, path=path)
    assert auth.refresh(boss, path=path) is not None
