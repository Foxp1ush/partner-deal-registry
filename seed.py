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

SEED_ACTOR = {"role": "admin", "partner_role": "manager", "username": "seed"}  # 시연 데이터에서 총판 조치를 흉내 낼 때


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
    # 2년 전 영구 라이선스 수주 → 무상 유지보수 1년 끝남 → '유지보수 만료(사용 중)', A파트너스가 계속 담당
    ("bora", "A파트너스", "보라테크(주)", 10010, "서보라", "010-0000-1010", 760, "a-sales2", "수주"),
    # 1년 구독 고객 (구독 종료 전)
    ("mirae", "C엔지니어링", "미래가공(주)", 10011, "정미래", "010-0000-1011", 200, "c-sales2", "수주"),
]

# 시연용 증빙 이미지: (등록 키, 종류). 가상 명함 그림을 만들어 붙인다.
EVIDENCE = [("hanbit", "card"), ("sat", "card")]

# 계약 고객: (등록 키, 수주 며칠 전, 종료일까지 남은 일수, 라이선스 종류, ENCY 계정 발급 여부)
# 남은 일수가 음수인 영구 라이선스는 seed 끝의 expire_overdue()에서 '유지보수 만료(사용 중)'가 된다.
WON = [
    ("gaon", 300, 45, "perpetual", False),   # 유지보수 종료 45일 전 → 연장 알림, 계정 발급 대기
    ("bora", 740, -375, "perpetual", True),  # 유지보수 1년 전에 끝남 → 유지보수 만료(사용 중), 재가입 영업 대상
    ("mirae", 180, 185, "sub1", True),       # 1년 구독 중
]

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
    ("gaon", 300, "b-sales1", "계약", "영구 라이선스 계약 체결 (무상 유지보수 1년)."),
    ("bora", 740, "a-sales2", "계약", "영구 라이선스 계약 체결 (무상 유지보수 1년)."),
    ("bora", 30, "a-sales2", "전화", "유지보수 재가입 문의 — 신규 버전 기능 소개 요청."),
    ("mirae", 180, "c-sales2", "계약", "1년 구독 계약 체결."),
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
     "수주한 고객은 보호 기간 없이 판 파트너의 담당 고객이 됩니다. 담당은 고객이 파트너 변경을 요청할 때(총판 이관)만 바뀌며, "
     "다른 파트너가 같은 고객을 등록하면 총판이 검토합니다. 영구 라이선스는 유지보수(시작일 + 1년 무상)가 끝나도 담당이 유지되어 "
     "재가입 영업을 하고, 구독은 끝난 뒤 90일 안에 갱신하지 않으면 담당이 풀립니다. 수주 처리 시 라이선스 종류·시작일과 고객사 ENCY 계정 이메일을 꼭 입력해 주세요"
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
    ("engineer", "admin1234", "admin", None, "총판 엔지니어", "engineer"),
]


def seed_accounts() -> None:
    """없는 시연용 계정만 만든다(예전 DB에도 새 계정이 추가됨)."""
    ids = {p["name"]: p["id"] for p in db.list_partners()}
    for username, password, role, partner, name, partner_role in DEMO_ACCOUNTS:
        if not db.has_account(username):
            db.add_account(username, auth.hash_password(password), role, ids.get(partner), name, partner_role)


# 시연용 기술 문의: (작성자, 고객사, 분류, 제목, 며칠 전, 상태, [(쪽, 계정, 몇 시간 뒤, 내용), ...])
TICKETS = [
    ("c-sales1", "다솜기공", "오류", "5축 가공 경로 생성 중 프로그램이 멈춤", 0.1, "open",
     [("partner", "c-sales1", 0, "고객사에서 5축 동시 가공 경로를 만들 때 진행률 80%에서 화면이 멈춘다고 합니다. "
                                 "작은 부품은 괜찮고 큰 금형만 그렇습니다.")]),
    ("a-sales2", "보라테크(주)", "라이선스", "유지보수 만료 후 최신 버전 설치 가능 여부", 2, "answered",
     [("partner", "a-sales2", 0, "보라테크에서 유지보수가 끝난 상태로 최신 버전을 설치해도 되는지 묻습니다."),
      ("engineer", "engineer", 3, "영구 라이선스라 지금 버전은 계속 쓰실 수 있습니다. 다만 유지보수 종료일 이후에 나온 "
                                  "버전은 설치해도 실행되지 않습니다. 최신 버전이 필요하면 유지보수 재가입 후 설치해 주세요.")]),
    ("b-sales1", "가온기계(주)", "사용법", "공구 라이브러리를 다른 PC로 옮기는 방법", 10, "closed",
     [("partner", "b-sales1", 0, "가온기계에서 PC를 바꾸는데 공구 라이브러리를 그대로 옮기고 싶다고 합니다."),
      ("engineer", "engineer", 5, "도구 > 공구 라이브러리 > 내보내기로 파일을 만든 뒤, 새 PC에서 가져오기를 하시면 됩니다."),
      ("partner", "b-sales1", 30, "옮겼습니다. 감사합니다!")]),
]


def seed_tickets() -> None:
    """기술 문의가 하나도 없을 때만 시연용 문의를 넣는다(예전 DB에도 추가됨)."""
    if db.list_tickets():
        return
    regs = db.list_registrations()
    now = datetime.now()
    for author, company, category, title, days_ago, status, msgs in TICKETS:
        acc = db.get_account(author)
        reg = max((r for r in regs if r["company_name"] == company and r["partner_id"] == acc["partner_id"]),
                  key=lambda r: r["id"], default=None)
        start = now - timedelta(days=days_ago)
        at = lambda hours: (start + timedelta(hours=hours)).isoformat(timespec="seconds")
        tid = db.add_ticket(acc["partner_id"], reg["id"] if reg else None, company if reg else "", author,
                            category, title, ts=at(0))
        for side, who, hours, body in msgs:
            db.add_ticket_message(tid, who, side, body, ts=at(hours))
        answered = [h for side, _, h, _ in msgs if side == "engineer"]
        fields = {"status": status, "updated_at": at(msgs[-1][2])}
        if status != "open":
            fields["assignee"] = "engineer"
        if answered:
            fields["answered_at"] = at(answered[-1])
        if status == "closed":
            fields.update(closed_at=at(msgs[-1][2]), closed_by=author)
        db.update_ticket(tid, fields)
        db.log("ticket", acc["partner_name"], reg["id"] if reg else None,
               f"#{tid} {title} — 문의 접수 ({author}, {company})", ts=at(0))


# 실적 통계 시연용 6개월치 가상 이력. 회사명은 실재하지 않는 조합이다.
HISTORY_NAMES = ["가람", "나래", "다온", "라온", "마루", "바다", "새솔", "아라", "자람", "차오", "하늬", "누리온", "단비",
                 "미리내", "별하", "솔빛", "온새", "윤슬", "이든", "초롱", "푸르나", "한결", "해솔", "가온누리", "늘봄",
                 "도담", "로운", "모아", "보람", "슬기", "아토", "여울", "은솔", "지음", "채움", "타래", "파랑", "하람빛",
                 "그루", "너울", "다래", "마중", "비움", "산들", "아름솔", "에움", "진솔", "큰솔", "토담", "희망찬"]
HISTORY_SUFFIX = ["정밀", "기공", "금형", "테크", "산업", "기계", "가공", "엔지니어링"]
HISTORY_REPS = {"A파트너스": ["a-sales1", "a-sales2"], "B테크": ["b-sales1", "b-sales2"], "C엔지니어링": ["c-sales1", "c-sales2"]}
HISTORY_AMOUNTS = [8_000_000, 12_000_000, 15_000_000, 18_000_000, 24_000_000, 32_000_000, 45_000_000]  # 공급가액
HISTORY_TICKETS = [("설치", "설치 중 오류 코드가 나옵니다"), ("라이선스", "라이선스 이전 방법 문의"),
                   ("사용법", "후처리기 설정 방법"), ("오류", "공구 경로 계산이 멈춥니다"), ("기타", "교육 일정 문의")]


def seed_history(days: int = 180) -> None:
    """단계 이동 기록이 하나도 없을 때만, 지난 days일치 등록·단계 이동·수주·문의를 만든다(예전 DB에도 추가됨)."""
    import random
    if db.list_stage_changes():
        return
    rnd = random.Random(7)  # 매번 같은 시연 데이터
    ids = {p["name"]: p["id"] for p in db.list_partners()}
    existing = {r["company_name"] for r in db.list_registrations()}
    now = datetime.now().replace(microsecond=0)
    stages = ["명함 확보", "첫 연락", "견적·데모", "구매 검토", "구매 임박", "수주"]
    for i, stem in enumerate(HISTORY_NAMES):
        name = f"{stem}{HISTORY_SUFFIX[i % len(HISTORY_SUFFIX)]}"
        if name in existing:
            continue
        partner = list(HISTORY_REPS)[i % 3]
        owner = rnd.choice(HISTORY_REPS[partner])
        reg_at = now - timedelta(days=rnd.randint(1, days), hours=rnd.randint(0, 8))
        rid = db.add_registration(ids[partner], name, fake_biz_no(20001 + i), "", "", "", "active",
                                  registered_at=reg_at, owner=owner)
        db.log("register", partner, rid, f"{name} 등록", ts=reg_at.isoformat())
        # 가장 멀리 갈 단계: 앞 단계일수록 많이 남는다
        reach = rnd.choices(range(6), weights=[14, 18, 18, 14, 10, 26])[0]
        t, final = reg_at, stages[0]
        for k in range(1, reach + 1):
            step = t + timedelta(days=rnd.randint(3, 20))
            if step > now:  # 아직 그 단계까지 못 간 고객 (최근 등록)
                break
            t, final = step, stages[k]
            db.add_stage_change(rid, ids[partner], stages[k - 1], stages[k], owner, ts=t.isoformat())
        reach = stages.index(final)
        fields = {"stage": final}
        if final == "수주":
            kind = rnd.choice(["perpetual", "perpetual", "sub1", "sub3"])
            start = t.date()
            fields |= {"status": "won", "won_at": t.isoformat(), "contract_amount": rnd.choice(HISTORY_AMOUNTS),
                       "license_type": kind, "license_start": start.isoformat(),
                       "expires_on": (start + timedelta(days=db.LICENSE_TYPES[kind][1])).isoformat(),
                       "ency_account": f"cam@h{i}-demo.example", "ency_issued_at": t.isoformat()}
            db.log("won", partner, rid, f"{name} 수주 — 계약 금액 {fields['contract_amount']:,}원 (공급가액, VAT 10% 별도)",
                   ts=t.isoformat())
        elif reach <= 2 and rnd.random() < 0.4:  # 초반에 멈춘 고객 일부는 보류·실패
            fields["stage"] = rnd.choice(["보류", "실패"])
            db.add_stage_change(rid, ids[partner], final, fields["stage"], owner, ts=t.isoformat())
        db.update_registration(rid, fields, allowed=db.ADMIN_EDITABLE)
    # 기술 문의: 접수 → 엔지니어 답변(2~30시간 뒤) → 대부분 종료
    accounts = {a: db.get_account(a) for reps in HISTORY_REPS.values() for a in reps}
    for i in range(24):
        author = list(accounts)[i % len(accounts)]
        category, title = HISTORY_TICKETS[rnd.randrange(len(HISTORY_TICKETS))]
        made = now - timedelta(days=rnd.randint(2, days), hours=rnd.randint(0, 8))
        reply = made + timedelta(hours=rnd.randint(2, 30))
        tid = db.add_ticket(accounts[author]["partner_id"], None, "", author, category, title, ts=made.isoformat())
        db.add_ticket_message(tid, author, "partner", f"{title}. 고객사에서 확인 요청이 왔습니다.", ts=made.isoformat())
        db.add_ticket_message(tid, "engineer", "engineer", "확인했습니다. 안내드린 순서대로 진행해 주세요.",
                              ts=reply.isoformat())
        db.update_ticket(tid, {"status": "closed", "assignee": "engineer", "answered_at": reply.isoformat(),
                               "closed_at": reply.isoformat(), "closed_by": author, "updated_at": reply.isoformat()})


def seed() -> None:
    db.init()
    if not db.is_empty():
        seed_accounts()
        seed_tickets()
        seed_history()
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
    for key, won_days_ago, days_left, license_type, issued in WON:
        rid, _ = keys[key]
        reg = db.get_registration(rid)
        end = (now + timedelta(days=days_left)).date().isoformat()
        start = (date.fromisoformat(end) - timedelta(days=db.LICENSE_TYPES[license_type][1])).isoformat()
        won_at = (now - timedelta(days=won_days_ago)).isoformat(timespec="seconds")
        db.update_registration(rid, {"status": "won", "expires_on": end, "license_start": start,
                                     "license_type": license_type,
                                     "ency_account": f"cam@{key}-demo.example",  # 가상 이메일 (.example은 실재하지 않는 도메인)
                                     "ency_issued_at": won_at if issued else "", "won_at": won_at,
                                     "contract_amount": 15_000_000},  # 공급가액(VAT 별도)
                               allowed=db.ADMIN_EDITABLE)
        db.log("won", reg["partner_name"], rid,
               f"{reg['company_name']} 수주 — {db.LICENSE_TYPES[license_type][0]}, 종료일 {end}까지 보호", ts=won_at)
    for key, days_ahead in EXPECTED:
        db.update_registration(keys[key][0], {"expected_close": (now + timedelta(days=days_ahead)).date().isoformat()},
                               allowed=db.ADMIN_EDITABLE)
    for key, days, reason in EXTENSIONS:
        admin.extend(SEED_ACTOR, keys[key][0], days, reason)
    by_key = {r[0]: r for r in REGISTRATIONS}
    for key, kind in EVIDENCE:
        _, _, company, _, contact, phone, _, owner, _ = by_key[key]
        evidence.save(keys[key][0], kind, fake_card(company, contact, phone), owner)
    for i, (title, body) in enumerate(NOTICES):
        db.add_notice(title, body, ts=(now - timedelta(days=30 - i)).isoformat(timespec="seconds"))
    db.expire_overdue()
    seed_tickets()
    seed_history()


if __name__ == "__main__":
    seed()
    print(f"seeded {db.DB_PATH}")
