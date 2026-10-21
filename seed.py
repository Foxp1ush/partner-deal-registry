"""데모용 가상 데이터. 실제 회사·파트너와 무관하다.

사업자등록번호는 앞 3자리를 실제로 쓰이지 않는 000으로 두고 검증번호만 맞춰 만든다.
"""

from __future__ import annotations

import io
from datetime import date, datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import admin
import auth
import db
import evidence


def fake_biz_no(serial: int) -> str:
    body = f"000{serial:06d}"  # 9자리
    digits = [int(c) for c in body]
    weights = [1, 3, 7, 1, 3, 7, 1, 3, 5]
    total = sum(x * w for x, w in zip(digits, weights)) + (digits[8] * 5) // 10
    check = (10 - total % 10) % 10
    d = body + str(check)
    return f"{d[:3]}-{d[3:5]}-{d[5:]}"


# 프로젝트에 넣어 둔 한글 글꼴 (나눔고딕, OFL 라이선스). 리눅스 서버(Streamlit Cloud)에는 윈도우 글꼴이 없어서 함께 둔다.
CARD_FONT = Path(__file__).parent / "assets" / "fonts" / "NanumGothic-Regular.ttf"


def _font(size: int):
    for f in (CARD_FONT, "malgun.ttf"):
        try:
            return ImageFont.truetype(str(f), size)
        except OSError:
            continue
    return ImageFont.load_default()  # 한글이 깨질 수 있음


def fake_card(company: str, name: str, phone: str) -> bytes:
    """시연용 가상 명함 그림(PNG)."""
    img = Image.new("RGB", (900, 500), "white")
    d = ImageDraw.Draw(img)
    big, small = _font(52), _font(30)
    d.rectangle([20, 20, 880, 480], outline="#3182F6", width=6)
    d.text((70, 80), company, font=big, fill="#191F28")
    d.text((70, 220), f"{name}  |  생산기술팀", font=small, fill="#333D4B")
    d.text((70, 280), f"M. {phone}", font=small, fill="#333D4B")
    d.text((70, 400), "가상 명함 (시연용 데이터)", font=small, fill="#8B95A1")
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


PARTNERS = ["A파트너스", "B테크", "C엔지니어링"]

# (키, 파트너, 회사명, 사업자번호 serial, 담당자, 연락처, 며칠 전 등록, 담당 직원, 영업 단계)
REGISTRATIONS = [
    ("sat", "A파트너스", "(주)샛별정밀", 10001, "김하늘", "010-0000-1001", 20, "a-sales1", "견적·데모"),
    ("nuri", "A파트너스", "누리금속 주식회사", 10002, "이도윤", "010-0000-1002", 82, "a-sales2", "구매 임박"),
    # 갱신형 구독 고객: 2년 전 첫 계약(만료된 옛 등록) → 이번 갱신 영업으로 다시 등록. 이력이 한 타임라인으로 이어진다.
    ("sky_old", "A파트너스", "푸른하늘소프트(주)", 10007, "조은비", "010-0000-1007", 760, "a-sales1", "수주"),
    ("sky", "A파트너스", "푸른하늘소프트(주)", 10007, "조은비", "010-0000-1007", 10, "a-sales1", "구매 검토"),
    ("haram", "B테크", "하람로보틱스", 10003, "박서진", "010-0000-1003", 45, "b-sales1", "구매 검토"),
    ("onsaemi", "B테크", "㈜온새미오토메이션", 10004, "최유나", "010-0000-1004", 88, "b-sales2", "첫 연락"),
    ("dasom", "C엔지니어링", "다솜기공", 10005, "정민재", "010-0000-1005", 5, "c-sales1", "명함 확보"),
    ("saeum", "C엔지니어링", "새움산업(주)", 10006, "한지우", "010-0000-1006", 95, "c-sales2", "실패"),  # 만료 예정 데이터
    ("gaon", "B테크", "가온기계(주)", 10008, "윤가온", "010-0000-1008", 320, "b-sales1", "수주"),
    # 사업자번호 없이 명함만으로 등록한 고객 (serial None)
    ("hanbit", "C엔지니어링", "한빛정밀", None, "강민호", "010-0000-1009", 3, "c-sales1", "명함 확보"),
]

# 시연용 증빙 이미지: (등록 키, 종류). 가상 명함 그림을 만들어 붙인다.
EVIDENCE = [("hanbit", "card"), ("sat", "card")]

# 계약 고객: (등록 키, 수주 며칠 전, 계약 종료까지 남은 일수). 갱신 알림(60일) 안에 들도록 둔다.
WON = [("gaon", 300, 45)]

# 예상 구매일('구매 검토'·'구매 임박'): (등록 키, 오늘부터 며칠 뒤). 음수 = 예상일이 지난 건(시연용)
EXPECTED = [("nuri", 20), ("sky", 60), ("haram", -10)]

# 총판 기간 연장 이력: (등록 키, 연장 일수, 사유)
EXTENSIONS = [("onsaemi", 30, "고객사 예산 심의가 다음 달로 미뤄짐")]

# (등록 키, 며칠 전, 기록한 직원, 유형, 내용)
ACTIVITIES = [
    ("sat", 20, "a-sales1", "미팅", "생산관리팀과 첫 미팅. 설비 데이터 수집 자동화에 관심."),
    ("sat", 12, "a-sales1", "데모", "현장 3개 라인 대상 데모 진행. 반응 긍정적."),
    ("sat", 3, "a-sales1", "메일", "제안서 송부. 다음 주 내부 검토 예정."),
    ("nuri", 80, "a-sales2", "전화", "구매팀 담당자와 통화, 예산 일정 확인."),
    ("nuri", 30, "a-sales2", "견적 제출", "표준 패키지 견적 제출."),
    ("nuri", 6, "a-sales2", "미팅", "계약 조건 협의. 도입 시점 조율 중."),
    ("sky_old", 750, "a-sales1", "계약", "1년 구독 첫 계약 체결."),
    ("sky_old", 385, "a-sales1", "갱신", "1차 갱신 완료. 사용자 수 증가."),
    ("sky_old", 120, "a-sales1", "미팅", "사용 현황 점검. 신규 부서 확대 검토 의견."),
    ("sky", 10, "a-sales1", "전화", "2차 갱신 일정 협의 시작."),
    ("gaon", 315, "b-sales1", "미팅", "가공 라인 2개 대상 도입 검토."),
    ("gaon", 300, "b-sales1", "계약", "1년 라이선스 계약 체결."),
    ("haram", 40, "b-sales1", "미팅", "자동화 라인 증설 계획 청취."),
    ("haram", 15, "b-sales1", "견적 제출", "1차 견적 제출."),
    ("onsaemi", 85, "b-sales2", "전화", "첫 통화. 담당자 소개받음."),
    ("dasom", 4, "c-sales1", "메일", "소개 메일 발송."),
    ("saeum", 60, "c-sales2", "미팅", "타사 솔루션 도입 결정으로 보류."),
]

NOTICES = [
    ("영업권 등록 정책 안내",
     "고객사 명함 또는 사업자등록증을 등록한 시점부터 90일간 해당 고객사의 영업권이 보호됩니다.\n"
     "같은 회사로 보이는 등록이 이미 있으면 총판 관리자가 확인 후 승인합니다.\n"
     "수주한 고객은 유지보수 종료일(라이선스 시작일 + 1년 무상 유지보수)까지 영업권이 유지되며, "
     "유지보수를 연장하면 종료일이 늘어납니다. 수주 처리 시 라이선스 시작일과 고객사 ENCY 계정 이메일을 꼭 입력해 주세요"
     "(총판이 그 이메일로 ENCY 계정을 발급합니다).\n"
     "고객이 다른 파트너로 이관되어도 이전 파트너의 영업 기록은 새 파트너에게 공개되지 않습니다.\n"
     "등록하려면 사업자등록번호·명함 이미지·사업자등록증 이미지 중 하나가 꼭 필요합니다. "
     "첨부한 이미지는 등록 증빙으로 보관되며(삭제되지 않음) 총판이 언제든 확인할 수 있습니다."),
    ("영업 관리(CRM) 이용 안내",
     "영업 기록은 우리 회사 직원만 봅니다. 총판은 단계별 건수만 보며, 분쟁 등으로 상세를 열람하면 "
     "'총판 열람 기록'에 사유와 함께 공개됩니다. 단가·할인율 등 민감한 내용은 적지 마세요."),
    ("데모 안내", "이 사이트의 모든 회사·파트너·연락처는 가상 데이터입니다."),
]


# 시연용 계정 (아이디, 비밀번호, 역할, 파트너명, 이름, 파트너 내 역할). 실제 운영 전에 반드시 바꾼다.
DEMO_ACCOUNTS = [
    ("partner-a", "demo1234", "partner", "A파트너스", "강다은 팀장", "manager"),
    ("a-sales1", "demo1234", "partner", "A파트너스", "윤서준", "rep"),
    ("a-sales2", "demo1234", "partner", "A파트너스", "임하린", "rep"),
    ("partner-b", "demo1234", "partner", "B테크", "오지훈 팀장", "manager"),
    ("b-sales1", "demo1234", "partner", "B테크", "서예린", "rep"),
    ("b-sales2", "demo1234", "partner", "B테크", "남도현", "rep"),
    ("partner-c", "demo1234", "partner", "C엔지니어링", "문채원 팀장", "manager"),
    ("c-sales1", "demo1234", "partner", "C엔지니어링", "백승우", "rep"),
    ("c-sales2", "demo1234", "partner", "C엔지니어링", "신유진", "rep"),
    ("admin", "admin1234", "admin", None, "총판 관리자", "manager"),
]


def seed_accounts() -> None:
    """없는 시연용 계정만 만든다(예전 DB에도 새 계정이 추가됨)."""
    ids = {p["name"]: p["id"] for p in db.list_partners()}
    for username, password, role, partner, name, partner_role in DEMO_ACCOUNTS:
        if not db.has_account(username):
            db.add_account(username, auth.hash_password(password), role, ids.get(partner), name, partner_role)


def seed() -> None:
    db.init()
    if not db.is_empty():
        seed_accounts()
        return
    ids = {name: db.add_partner(name) for name in PARTNERS}
    seed_accounts()
    now = datetime.now()
    keys = {}
    for key, partner, company, serial, contact, phone, days_ago, owner, stage in REGISTRATIONS:
        reg_at = (now - timedelta(days=days_ago)).replace(microsecond=0)
        rid = db.add_registration(ids[partner], company, fake_biz_no(serial) if serial else "", contact, phone,
                                  "", "active", registered_at=reg_at, owner=owner)
        db.update_registration(rid, {"stage": stage}, allowed=db.ADMIN_EDITABLE)
        db.log("register", partner, rid, f"{company} 등록", ts=reg_at.isoformat())
        keys[key] = (rid, ids[partner])
    for key, days_ago, author, kind, note in ACTIVITIES:
        rid, pid = keys[key]
        day = now - timedelta(days=days_ago)
        db.add_activity(rid, pid, author, kind, note, on_date=day.date().isoformat(),
                        ts=day.isoformat(timespec="seconds"))
    for key, won_days_ago, days_left in WON:
        rid, _ = keys[key]
        reg = db.get_registration(rid)
        end = (now + timedelta(days=days_left)).date().isoformat()
        start = (date.fromisoformat(end) - timedelta(days=365)).isoformat()  # 유지보수 종료일 = 라이선스 시작일 + 1년
        db.update_registration(rid, {"status": "won", "expires_on": end, "license_start": start,
                                     "ency_account": f"cam@{key}-demo.example"},  # 가상 이메일 (.example은 실재하지 않는 도메인)
                               allowed=db.ADMIN_EDITABLE)
        db.log("won", reg["partner_name"], rid, f"{reg['company_name']} 수주 — 계약 종료일 {end}까지 보호",
               ts=(now - timedelta(days=won_days_ago)).isoformat(timespec="seconds"))
    for key, days_ahead in EXPECTED:
        db.update_registration(keys[key][0], {"expected_close": (now + timedelta(days=days_ahead)).date().isoformat()},
                               allowed=db.ADMIN_EDITABLE)
    for key, days, reason in EXTENSIONS:
        admin.extend(keys[key][0], days, reason)
    by_key = {r[0]: r for r in REGISTRATIONS}
    for key, kind in EVIDENCE:
        _, _, company, _, contact, phone, _, owner, _ = by_key[key]
        evidence.save(keys[key][0], kind, fake_card(company, contact, phone), owner)
    for i, (title, body) in enumerate(NOTICES):
        db.add_notice(title, body, ts=(now - timedelta(days=30 - i)).isoformat(timespec="seconds"))
    db.expire_overdue()


if __name__ == "__main__":
    seed()
    print(f"seeded {db.DB_PATH}")
