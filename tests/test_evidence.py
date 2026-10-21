"""등록 증빙 (2026-10-21): 사업자번호·명함·사업자등록증 중 하나 필수, 이미지 축소 저장, 지우지 않음."""

import io

import pytest
from PIL import Image

import db
import evidence
import seed


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "t.db"
    db.init(path)
    a = db.add_partner("A", path=path)
    rid = db.add_registration(a, "한빛정밀", "", "", "", "", "active", path=path)
    return path, rid


def photo(w: int, h: int, fmt: str = "PNG") -> bytes:
    out = io.BytesIO()
    Image.new("RGBA" if fmt == "PNG" else "RGB", (w, h), (200, 30, 30, 255) if fmt == "PNG" else (200, 30, 30)).save(out, fmt)
    return out.getvalue()


def test_requirement_one_of_three():
    assert evidence.missing_requirement("", False, False) == evidence.REQUIREMENT
    assert evidence.missing_requirement("  ", False, False) == evidence.REQUIREMENT
    assert evidence.missing_requirement("000-01-00001", False, False) is None
    assert evidence.missing_requirement("", True, False) is None
    assert evidence.missing_requirement("", False, True) is None


def test_save_shrinks_and_lists(env):
    path, rid = env
    evidence.save(rid, "card", photo(4000, 3000), "a-1", path=path)
    items = evidence.list_for(rid, path=path)
    assert len(items) == 1 and items[0]["kind_label"] == "명함"
    f = items[0]["file_path"]
    assert f.exists() and f.parent == path.parent / "evidence"
    with Image.open(f) as img:
        assert img.format == "JPEG" and max(img.size) == evidence.MAX_SIDE
    assert any(e["kind"] == "evidence" for e in db.list_events(path=path))


def test_small_image_is_not_enlarged(env):
    path, rid = env
    evidence.save(rid, "cert", photo(800, 600, "JPEG"), "a-1", path=path)
    with Image.open(evidence.list_for(rid, path=path)[0]["file_path"]) as img:
        assert img.size == (800, 600)


def test_bad_file_and_kind_rejected(env):
    path, rid = env
    with pytest.raises(evidence.EvidenceError):
        evidence.save(rid, "card", b"not an image", "a-1", path=path)
    with pytest.raises(evidence.EvidenceError):
        evidence.save(rid, "selfie", photo(10, 10), "a-1", path=path)
    assert evidence.list_for(rid, path=path) == []


def test_evidence_kept_after_release(env):
    path, rid = env
    evidence.save(rid, "card", photo(100, 100), "a-1", path=path)
    db.set_status(rid, "released", path=path)
    assert len(evidence.list_for(rid, path=path)) == 1  # 해제·만료돼도 지우지 않는다


def test_summary_and_kinds(env):
    path, rid = env
    row = db.get_registration(rid, path=path)
    assert evidence.summary(row, set()) == "없음"
    evidence.save(rid, "card", photo(100, 100), "a-1", path=path)
    kinds = db.evidence_kinds(path=path)[rid]
    assert evidence.summary(row, kinds) == "명함"
    assert evidence.summary({**row, "biz_no": seed.fake_biz_no(1)}, kinds | {"cert"}) == "사업자번호 · 명함 · 사업자등록증"


def test_download_name(env):
    path, rid = env
    evidence.save(rid, "card", photo(100, 100), "a-1", path=path)
    item = evidence.list_for(rid, path=path)[0]
    name = evidence.download_name('(주)한빛/정밀:*', item)
    assert name.startswith("(주)한빛정밀_명함_") and name.endswith(".jpg")
    assert not any(c in name for c in '\\/:*?"<>|')
    assert evidence.download_name("", item).startswith("고객사_명함_")


def test_card_font_is_bundled():
    # 리눅스 서버에서도 한글 명함이 깨지지 않도록 글꼴 파일이 저장소에 있어야 한다
    assert seed.CARD_FONT.exists()
    assert seed._font(30).getname()[0] == "NanumGothic"


def test_fake_card_is_valid_image():
    with Image.open(io.BytesIO(seed.fake_card("한빛정밀", "강민호", "010-0000-0000"))) as img:
        assert img.size == (900, 500)
