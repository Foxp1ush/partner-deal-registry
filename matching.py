"""영업권 중복 판정 규칙 (AI 없이 동작하는 결정적 로직).

판정 순서
1. 사업자등록번호가 같으면 → 같은 회사 (확정)
2. 정규화한 회사명이 같으면 → 같은 회사 (확정)
3. 정규화한 회사명이 비슷하면 → '검토 필요' 후보 (AI·관리자가 판단)
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

# 회사명에서 법인 형태 표기를 걷어낸다. "(주)한국정밀" = "한국정밀 주식회사" = "㈜ 한국정밀"
_CORP_TOKENS = [
    "주식회사", "유한회사", "유한책임회사", "합자회사", "합명회사", "농업회사법인", "사단법인", "재단법인",
    "(주)", "㈜", "(유)", "(합)", "(사)", "(재)",
    "co.,ltd.", "co.,ltd", "co.ltd", "co.", "ltd.", "ltd", "inc.", "inc", "corp.", "corp", "corporation",
]

SIMILAR_THRESHOLD = 0.6  # 이 이상이면 '비슷한 이름' 후보로 올린다


def normalize_name(name: str) -> str:
    s = (name or "").lower().strip()
    for token in _CORP_TOKENS:
        s = s.replace(token, "")
    # 공백·문장부호 제거
    return re.sub(r"[\s\.\,\-\_\(\)\[\]&·/]", "", s)


def normalize_biz_no(biz_no: str) -> str:
    return re.sub(r"\D", "", biz_no or "")


def format_biz_no(biz_no: str) -> str:
    d = normalize_biz_no(biz_no)
    return f"{d[:3]}-{d[3:5]}-{d[5:]}" if len(d) == 10 else (biz_no or "")


def is_valid_biz_no(biz_no: str) -> bool:
    """사업자등록번호 10자리 검증번호(마지막 자리) 확인."""
    d = normalize_biz_no(biz_no)
    if len(d) != 10:
        return False
    digits = [int(c) for c in d]
    weights = [1, 3, 7, 1, 3, 7, 1, 3, 5]
    total = sum(x * w for x, w in zip(digits[:9], weights))
    total += (digits[8] * 5) // 10
    return (10 - total % 10) % 10 == digits[9]


def name_similarity(a: str, b: str) -> float:
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    if na in nb or nb in na:  # "한국정밀" ⊂ "한국정밀기계" 같은 포함 관계는 높게 본다
        return max(0.8, SequenceMatcher(None, na, nb).ratio())
    return SequenceMatcher(None, na, nb).ratio()


def find_conflicts(company_name: str, biz_no: str, existing: list[dict]) -> dict:
    """신규 등록 후보를 기존 유효 등록(active·pending)과 비교한다.

    existing: id, company_name, biz_no 키를 가진 dict 목록
    반환: {"exact": [(row, 사유)], "similar": [(row, 유사도)]}
    """
    nb = normalize_biz_no(biz_no)
    nn = normalize_name(company_name)
    exact, similar = [], []
    for row in existing:
        if nb and nb == normalize_biz_no(row["biz_no"]):
            exact.append((row, "사업자등록번호 일치"))
        elif nn and nn == normalize_name(row["company_name"]):
            exact.append((row, "회사명 일치(법인 표기 제외)"))
        else:
            score = name_similarity(company_name, row["company_name"])
            if score >= SIMILAR_THRESHOLD:
                similar.append((row, round(score, 2)))
    similar.sort(key=lambda x: -x[1])
    return {"exact": exact, "similar": similar}
