"""Claude API 연동: ① 명함·사업자등록증 이미지에서 정보 추출 ② 비슷한 회사명이 같은 회사인지 판단.

AI는 '판단을 돕는' 역할만 한다. 등록 차단·승인은 matching.py 규칙과 관리자가 결정한다.
API 키가 없으면 available()이 False가 되고, 앱은 수동 입력만으로 동작한다.
"""

from __future__ import annotations

import base64
import json
import os

MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
FALLBACK_BETA = "server-side-fallback-2026-07-01"

_EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "doc_type": {"type": "string", "enum": ["명함", "사업자등록증", "기타"]},
        "company_name": {"type": "string"},
        "biz_no": {"type": "string"},
        "contact_name": {"type": "string"},
        "contact_phone": {"type": "string"},
        "uncertain_fields": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["doc_type", "company_name", "biz_no", "contact_name", "contact_phone", "uncertain_fields"],
    "additionalProperties": False,
}

_EXTRACT_PROMPT = """이 이미지는 한국 기업의 명함 또는 사업자등록증입니다. 아래 정보를 이미지에 적힌 그대로 추출하세요.
- company_name: 상호(법인명). 이미지에 적힌 표기 그대로.
- biz_no: 사업자등록번호 10자리 (000-00-00000 형식). 없으면 빈 문자열.
- contact_name: 담당자 이름(명함 주인 또는 대표자). 없으면 빈 문자열.
- contact_phone: 휴대폰 우선, 없으면 대표번호. 없으면 빈 문자열.
- uncertain_fields: 흐리거나 잘려서 확신할 수 없는 필드 이름 목록.
이미지에 없는 정보는 추측하지 말고 빈 문자열로 두세요."""

_MATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "judgments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "likely_same": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["index", "likely_same", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["judgments"],
    "additionalProperties": False,
}


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _client():
    import anthropic  # API 키가 없을 때 import 비용·오류를 피하려고 지연 import
    return anthropic.Anthropic()


def _call(content: list[dict] | str, schema: dict, max_tokens: int = 2000) -> dict:
    resp = _client().beta.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        betas=[FALLBACK_BETA],
        fallbacks="default",
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": content}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("AI가 이 요청을 처리하지 않았습니다. 직접 입력해 주세요.")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("AI 응답이 잘렸습니다. 다시 시도하거나 직접 입력해 주세요.")
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)


def extract_from_image(image_bytes: bytes, media_type: str) -> dict:
    data = base64.standard_b64encode(image_bytes).decode("utf-8")
    return _call(
        [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}},
            {"type": "text", "text": _EXTRACT_PROMPT},
        ],
        _EXTRACT_SCHEMA,
    )


def judge_same_company(new_name: str, new_biz_no: str, candidates: list[dict]) -> list[dict]:
    """candidates: company_name, biz_no를 가진 dict 목록. 후보별 판단 목록을 돌려준다."""
    lines = "\n".join(
        f"{i}. {c['company_name']} (사업자번호: {c['biz_no'] or '미기재'})" for i, c in enumerate(candidates)
    )
    prompt = f"""B2B 영업에서 파트너사가 고객사를 '선점 등록'하려 합니다. 이미 다른 파트너가 등록한 회사와 같은 회사인지 판단을 돕고 싶습니다.

신규 등록: {new_name} (사업자번호: {new_biz_no or '미기재'})

기존 등록 후보:
{lines}

각 후보에 대해 신규 등록과 같은 회사일 가능성이 높은지(likely_same) 판단하고, 이유를 한 문장으로 쓰세요.
법인 표기 차이, 영문/한글 표기, 약칭, 지점·계열사 여부를 고려하세요. 사업자번호가 둘 다 있고 다르면 별개 법인일 가능성이 높지만, 같은 그룹의 계열사라면 이유에 적으세요."""
    return _call(prompt, _MATCH_SCHEMA)["judgments"]
