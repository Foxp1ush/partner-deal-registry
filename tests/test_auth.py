import auth
import db


def test_password_hash_roundtrip():
    h = auth.hash_password("demo1234")
    assert "demo1234" not in h
    assert auth.check_password("demo1234", h)
    assert not auth.check_password("wrong", h)
    assert h != auth.hash_password("demo1234")  # salt가 매번 다르다


def test_login(tmp_path):
    path = tmp_path / "t.db"
    db.init(path)
    pid = db.add_partner("A", path=path)
    db.add_account("partner-a", auth.hash_password("pw"), "partner", pid, path=path)
    db.add_account("admin", auth.hash_password("adm"), "admin", path=path)
    user = auth.login("partner-a", "pw", path=path)
    assert (user["role"], user["partner_id"], user["partner_name"], user["partner_role"]) == ("partner", pid, "A", "manager")
    assert auth.login("admin", "adm", path=path)["role"] == "admin"
    assert auth.login("partner-a", "adm", path=path) is None
    assert auth.login("nobody", "pw", path=path) is None
    # 퇴사 처리하면 로그인 불가, 로그인 중이던 세션도 무효
    db.set_account_active("partner-a", False, path=path)
    assert auth.login("partner-a", "pw", path=path) is None
    assert not auth.still_valid(user, path=path)


def test_old_db_gets_new_columns(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as c:  # 예전 구조의 DB
        c.execute("CREATE TABLE accounts (username TEXT PRIMARY KEY, password_hash TEXT NOT NULL, "
                  "role TEXT NOT NULL, partner_id INTEGER)")
        c.execute("INSERT INTO accounts VALUES ('old', ?, 'partner', 1)", (auth.hash_password("pw"),))
    db.init(path)
    acc = db.get_account("old", path=path)
    assert acc["partner_role"] == "manager" and acc["active"] == 1
