"""등록 증빙: 명함·사업자등록증 이미지 저장과 등록 요건.

정책 (2026-10-21)
- 등록하려면 사업자등록번호 · 명함 이미지 · 사업자등록증 이미지 중 하나는 꼭 있어야 한다.
  사업자번호가 가장 확실한 키지만 영업 초기에 받기 어려울 수 있어, 명함(실제 접촉의 증거)으로 대신할 수 있게 했다.
- 이미지는 회사 서버의 data/evidence 폴더에 저장하고 지우지 않는다(분쟁 근거).
- 총판은 언제든 사유 없이 볼 수 있다. 등록한 파트너는 자기 등록의 이미지만 본다. 다른 파트너는 볼 수 없다.
- 저장할 때 긴 변 1600px JPG로 줄인다(휴대폰 사진 약 3MB → 약 0.3MB). 글자를 읽기에는 충분하다.
"""

from __future__ import annotations

import io
import re
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps

import db
from db import DB_PATH

KINDS = {"card": "명함", "cert": "사업자등록증"}
MAX_SIDE = 1600
JPEG_QUALITY = 82
REQUIREMENT = "사업자등록번호, 명함 이미지, 사업자등록증 이미지 중 하나는 꼭 있어야 등록할 수 있습니다."


class EvidenceError(Exception):
    """저장할 수 없는 이미지. 메시지를 그대로 화면에 보여 준다."""


def missing_requirement(biz_no: str, has_card: bool, has_cert: bool) -> str | None:
    """등록 요건(셋 중 하나)을 못 채우면 안내 문구, 채우면 None."""
    return None if (biz_no or "").strip() or has_card or has_cert else REQUIREMENT


def evidence_dir(path: Path = DB_PATH) -> Path:
    return path.parent / "evidence"


def shrink(image_bytes: bytes) -> bytes:
    """긴 변 MAX_SIDE px 이하 JPG로 줄인다. 휴대폰 사진의 회전 정보도 반영한다."""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = ImageOps.exif_transpose(img)
    except Exception as e:
        raise EvidenceError("이미지 파일을 읽을 수 없습니다. PNG·JPG 사진인지 확인해 주세요.") from e
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return out.getvalue()


def save(registration_id: int, kind: str, image_bytes: bytes, uploader: str, path: Path = DB_PATH) -> int:
    """원본 이미지를 줄여 저장하고 증빙 id를 돌려준다."""
    return store(registration_id, kind, shrink(image_bytes), uploader, path=path)


def store(registration_id: int, kind: str, data: bytes, uploader: str, path: Path = DB_PATH) -> int:
    """이미 shrink()로 줄인 JPG를 저장한다 (등록 전에 미리 줄여 본 경우 두 번 압축하지 않으려고)."""
    if kind not in KINDS:
        raise EvidenceError("알 수 없는 증빙 종류입니다.")
    folder = evidence_dir(path)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    file = folder / f"{registration_id}_{kind}_{stamp}.jpg"
    file.write_bytes(data)
    eid = db.add_evidence(registration_id, kind, file.name, len(data), uploader, path=path)
    reg = db.get_registration(registration_id, path=path)
    db.log("evidence", reg["partner_name"] if reg else "", registration_id,
           f"{reg['company_name'] if reg else ''} {KINDS[kind]} 이미지 첨부 ({uploader})", path=path)
    return eid


def list_for(registration_id: int, path: Path = DB_PATH) -> list[dict]:
    """등록의 증빙 목록 (오래된 것부터). 각 항목에 file_path(실제 파일 위치)를 붙인다."""
    folder = evidence_dir(path)
    return [{**e, "file_path": folder / e["file"], "kind_label": KINDS[e["kind"]]}
            for e in db.list_evidence(registration_id, path=path)]


def download_name(company_name: str, item: dict) -> str:
    """내려받을 파일 이름. 예: '한빛정밀_명함_20261021-1717.jpg'. 파일 이름에 못 쓰는 글자는 뺀다."""
    safe = re.sub(r'[\\/:*?"<>|]+', "", company_name).strip() or "고객사"
    stamp = item["ts"][:16].replace("-", "").replace("T", "-").replace(":", "")
    return f"{safe}_{item['kind_label']}_{stamp}.jpg"


def summary(row: dict, kinds: set[str]) -> str:
    """증빙 수준 한 줄 요약. 예: '사업자번호 · 명함'. 사업자번호가 없으면 중복 판정이 회사명에만 기대므로 표시해 둔다."""
    parts = (["사업자번호"] if (row.get("biz_no") or "").strip() else []) + [KINDS[k] for k in KINDS if k in kinds]
    return " · ".join(parts) if parts else "없음"
