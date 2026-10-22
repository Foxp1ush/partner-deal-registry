"""파트너 영업권 관리 v2 — Streamlit 앱.

실행: .venv\\Scripts\\python.exe -m streamlit run app.py

역할(사이드바에서 선택)
- 파트너사: 고객사 등록(이미지 → AI 자동 입력), 내 영업권, 선점 조회, 공지
- 총판 관리자: 검토 대기 승인·반려, 전체 현황, 만료 임박, 이력 로그, 공지 작성
"""

from __future__ import annotations

import base64
import os
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

import accounts
import admin
import ai_extract
import auth
import crm
import db
import evidence
import matching
import seed
import stats
import tickets

AI_DAILY_LIMIT = int(os.environ.get("AI_DAILY_LIMIT", "30"))
ADMIN = "총판 관리자"
STATUS_LABEL = {"active": "보호 중", "pending": "검토 대기", "won": "계약 고객", "lapsed": "계약 고객(만료·유예)",
                "expired": "만료", "rejected": "반려", "released": "해제"}
FORM_FIELDS = ["company_name", "biz_no", "contact_name", "contact_phone", "note"]

# page_icon: 브라우저 탭 아이콘. 이모지를 바꾸거나 줄을 지우면 Streamlit 기본 아이콘이 나온다.
# (예전에 기본 아이콘이 안 나오던 문제는 starlette 버전 문제였고, requirements.txt에서 고쳤다)
st.set_page_config(page_title="ENCY Sales(ENCY 파트너용)", page_icon="📊", layout="wide")

# Streamlit Cloud에서는 secrets로 키를 넣는다
if "ANTHROPIC_API_KEY" not in os.environ:
    try:
        os.environ["ANTHROPIC_API_KEY"] = st.secrets["ANTHROPIC_API_KEY"]
    except Exception:
        pass

seed.seed()
db.expire_overdue()
tickets.auto_close()


def days_left(expires_on: str) -> int:
    return (date.fromisoformat(expires_on) - date.today()).days


def reg_table(rows: list[dict], show_partner: bool = True,
              ext: dict[int, tuple[int, int]] | None = None) -> pd.DataFrame:
    """ext를 주면(총판 화면) 연장 횟수 열을 붙인다: 이 등록 n회 / 고객사 누적 m회."""
    kinds = db.evidence_kinds()
    data = []
    for r in rows:
        item = {
            "회사명": r["company_name"],
            "사업자번호": matching.format_biz_no(r["biz_no"]),
            "등록일": r["registered_at"][:10],
            "상태": STATUS_LABEL[r["status"]],
            # 가망 고객은 영업 보호 기간, 계약 고객은 '담당 고객 + 다음 영업 시점' (계약 고객엔 보호 기간이 없다)
            "보호 · 담당": crm.relation(r),
            "고객 담당자": r["contact_name"],
            "증빙": evidence.summary(r, kinds.get(r["id"], set())),
        }
        if show_partner:
            item = {"파트너": r["partner_name"], **item}
            item["라이선스"] = crm.LICENSE_TYPES.get(r["license_type"], "영구") if r["status"] in ("won", "lapsed") else ""
            item["계약 금액 (공급가액, VAT 별도)"] = (crm.won_text(r["contract_amount"])
                                                if r["status"] in ("won", "lapsed") else "")
            item["ENCY 계정"] = ((r["ency_account"] + ("" if r["ency_issued_at"] else " (발급 대기)"))
                                 if r["status"] in ("won", "lapsed") and r["ency_account"] else "")  # 총판이 계정 발급에 씀
        else:  # 고객사별 영업 단계는 파트너 화면에만. 총판은 집계만 본다.
            item["영업 단계"] = r["stage"]
            item["예상 구매일"] = (r["expected_close"] + (" (지남)" if crm.is_overdue(r) else "")
                                   if r["stage"] in crm.FORECAST_STAGES and r["expected_close"] else "-")
        if ext is not None:
            own, total = ext.get(r["id"], (0, 0))
            item["연장"] = f"{own}회 (고객사 누적 {total}회)"
        data.append(item)
    return pd.DataFrame(data)


def show_evidence(registration_id: int, add_as: str | None = None) -> None:
    """등록 증빙 이미지를 보여 주고 내려받게 한다. add_as(계정 이름)를 주면 이미지를 추가로 올릴 수 있다.

    파트너(자기 등록)와 총판 화면이 함께 쓴다. 다른 파트너는 이 함수를 부르는 화면에 들어올 수 없다.
    """
    items = evidence.list_for(registration_id)
    if not items:
        st.caption("첨부된 증빙 이미지가 없습니다.")
    reg = db.get_registration(registration_id)
    cols = st.columns(3)
    for i, e in enumerate(items):
        with cols[i % 3]:
            if e["file_path"].exists():
                st.image(str(e["file_path"]), width="stretch")
                st.download_button("⬇ 내려받기", e["file_path"].read_bytes(), mime="image/jpeg",
                                   file_name=evidence.download_name(reg["company_name"] if reg else "", e),
                                   key=f"dl_{registration_id}_{e['id']}")
            else:
                st.warning("이미지 파일을 찾을 수 없습니다.")
            st.caption(f"{e['kind_label']} · {e['ts'][:16].replace('T', ' ')} · {e['uploader']}")
    if add_as is None:
        return
    with st.form(f"add_evidence_{registration_id}", clear_on_submit=True):
        st.markdown("**증빙 추가** (예: 명함만 냈다면 나중에 사업자등록증을 더할 수 있습니다)")
        kind = st.radio("종류", list(evidence.KINDS), format_func=evidence.KINDS.get, horizontal=True)
        f = st.file_uploader("이미지", type=["png", "jpg", "jpeg", "webp"])
        if st.form_submit_button("추가") and f is not None:
            try:
                evidence.save(registration_id, kind, f.getvalue(), add_as)
            except evidence.EvidenceError as err:
                st.error(str(err))
            else:
                st.rerun()


def ai_budget_left() -> int:
    return max(0, AI_DAILY_LIMIT - db.ai_calls_today())


def use_ai(partner: str, what: str) -> bool:
    """AI 호출 가능 여부를 확인하고, 가능하면 호출 기록을 남긴다."""
    if not ai_extract.available():
        st.info("AI 기능이 꺼져 있습니다(API 키 없음). 직접 입력해 주세요.")
        return False
    if ai_budget_left() <= 0:
        st.warning("오늘 데모 AI 호출 한도를 다 썼습니다. 직접 입력해 주세요.")
        return False
    db.log("ai_call", partner, detail=what)
    return True


# ---- 로그인·사이드바 ----------------------------------------------------
ASSETS = Path(__file__).parent / "assets"
LOGIN_BG = "login_bg"  # assets/login_bg.png(또는 .jpg·.jpeg·.webp)
MAIN_BG = "main_bg"    # 로그인 이후 배경. 없으면 config.toml의 backgroundColor(단색)
IMAGE_TYPES = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".webp": "webp"}


def bg_css(name: str) -> str | None:
    """화면 전체에 배경 이미지를 까는 CSS. assets에 그 이름의 이미지가 없으면 None."""
    for ext, mime in IMAGE_TYPES.items():
        path = ASSETS / f"{name}{ext}"
        if path.exists():
            img = base64.b64encode(path.read_bytes()).decode()
            return (f'[data-testid="stApp"] {{ background: url("data:image/{mime};base64,{img}")'
                    ' center / cover no-repeat fixed; }')
    return None


def main_background() -> None:
    """로그인 이후 화면 배경 이미지. 글씨가 잘 보이도록 본문 영역엔 반투명 흰 판을 깐다."""
    css = bg_css(MAIN_BG)
    if css is None:
        return
    st.markdown(f"""
<style>
{css}
[data-testid="stHeader"] {{ background: transparent; }}  /* 맨 위 흰 띠 없애기 */
[data-testid="stMainBlockContainer"] {{
    background: rgba(255, 255, 255, 0.78);  /* 마지막 숫자: 흰 판 진하기 (0 투명 ~ 1 불투명) */
    backdrop-filter: blur(10px);            /* 판 뒤 사진 흐림 정도. 0px이면 선명하게 비침 */
    border-radius: 20px;
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.12);
    max-width: calc(100% - 6rem);           /* 양옆 여백(사진이 보이는 폭) */
    margin: 4rem auto 3rem;                 /* 위 · 양옆(가운데 정렬) · 아래 여백 */
}}
</style>
""", unsafe_allow_html=True)


def login_background() -> None:
    """로그인 화면 배경 이미지. 이미지 파일이 없으면 그냥 넘어간다."""
    css = bg_css(LOGIN_BG)
    if css is None:
        return
    st.markdown(f"""

<style>
{css}
[data-testid="stForm"] {{
    background: rgba(255, 255, 255, 0.92);
}}
[data-testid="stColumn"]:has([data-testid="stForm"]) {{
    background: rgba(255, 255, 255, 0.9);
    padding: 2rem;
    border-radius: 16px;
}}
</style>
""", unsafe_allow_html=True)



def login_page() -> None:
    login_background()
    _, mid, _ = st.columns([1, 1, 1])   # 화면을 3칸으로 나눔 (왼쪽 빈칸 · 가운데 · 오른쪽 빈칸)
    with mid:                           # ↓ 여기부터 전부 가운데 칸에 들어감
        st.title("파트너 계정 로그인", text_alignment="center")
        with st.form("login"):
            username = st.text_input("아이디")
            password = st.text_input("비밀번호", type="password")
            if st.form_submit_button("로그인", type="primary"):
                account = auth.login(username, password)
                if account is None:
                    st.error("아이디 또는 비밀번호가 맞지 않습니다.")
                else:
                    clear_form()
                    st.session_state["user"] = account
                    st.rerun()
        with st.expander("시연용 계정"):
            st.markdown("\n".join(
                f"- {partner or '총판'} · {name} ({ROLE_LABEL[prole] if partner else ADMIN_ROLE_LABEL[prole]}): "
                f"`{username}` / `{password}`"
                for username, password, _, partner, name, prole in seed.DEMO_ACCOUNTS
            ))



ROLE_LABEL = {"manager": "파트너 관리자", "rep": "영업 담당자"}
ADMIN_ROLE_LABEL = {"manager": "총괄 매니저", "engineer": "엔지니어"}  # 총판 계정의 partner_role


def password_form(user: dict, key: str) -> None:
    with st.form(key, clear_on_submit=True):
        current = st.text_input("현재 비밀번호", type="password")
        new = st.text_input(f"새 비밀번호 ({accounts.MIN_PASSWORD_LEN}자 이상)", type="password")
        confirm = st.text_input("새 비밀번호 확인", type="password")
        if st.form_submit_button("변경"):
            try:
                accounts.change_own_password(user, current, new, confirm)
            except accounts.AccountError as e:
                st.error(str(e))
            else:
                st.session_state["flash_pw"] = "비밀번호를 바꿨습니다."
                st.rerun()


def must_change_page(user: dict) -> None:
    st.title("비밀번호 변경")
    st.info(f"**{user['display_name'] or user['username']}**님, 임시 비밀번호로 로그인했습니다. "
            "계속하려면 새 비밀번호를 정해 주세요.")
    password_form(user, "first_pw")
    if st.button("로그아웃"):
        st.session_state.clear()
        st.rerun()


def org_name(user: dict) -> str:
    return "총판" if user["role"] == "admin" else user["partner_name"]


def role_name(user: dict) -> str:
    if user["role"] == "admin":
        return ADMIN_ROLE_LABEL.get(user["partner_role"], "총판")
    return ROLE_LABEL[user["partner_role"]]


def account_tab(user: dict) -> None:
    if msg := st.session_state.pop("flash_profile", None):
        st.success(msg)
    c = st.columns(3)
    c[0].metric("아이디", user["username"])
    c[1].metric("소속", org_name(user))
    c[2].metric("역할", role_name(user))
    st.caption("아이디·소속·역할은 관리자가 정합니다. 바꿔야 하면 관리자에게 요청해 주세요.")

    st.subheader("프로필")
    with st.form("profile"):
        c1, c2 = st.columns(2)
        display_name = c1.text_input("이름 *", value=user["display_name"])
        title = c2.text_input("직함", value=user["title"], placeholder="예: 과장")
        department = c1.text_input("부서", value=user["department"], placeholder="예: 영업 1팀")
        phone = c2.text_input("연락처", value=user["phone"], placeholder="예: 010-0000-0000")
        email = c1.text_input("이메일", value=user["email"], placeholder="예: name@company.com")
        st.caption("연락처·이메일은 업무 연락용으로 팀 관리자와 총판 담당자에게 보일 수 있습니다.")
        if st.form_submit_button("저장", type="primary"):
            try:
                accounts.update_profile(user, display_name, title, department, phone, email)
            except accounts.AccountError as e:
                st.error(str(e))
            else:
                st.session_state["flash_profile"] = "프로필을 저장했습니다."
                st.rerun()

    st.subheader("비밀번호 변경")
    password_form(user, "side_pw")


def sidebar(user: dict, account_page: st.Page) -> None:
    """메뉴(st.navigation) 아래에 붙는 내 정보·계정·AI 상태."""
    with st.sidebar:
        st.divider()
        name = " ".join(x for x in (user["display_name"] or user["username"], user["title"]) if x)
        st.markdown(f"👤 **{org_name(user)}**   \n{name} · {role_name(user)}", text_alignment="center")
        if not (user["phone"] and user["email"]):
            st.caption("프로필(연락처·이메일)을 채워 주세요.")
            st.page_link(account_page, label="내 계정으로 가기 →")
        if st.button("로그아웃", width="stretch"):
            st.session_state.clear()
            st.rerun()
        if ai_extract.available():
            st.caption(f"🤖 AI 사용 가능 · 오늘 남은 호출 {ai_budget_left()}회")
        else:
            st.caption("🤖 AI 꺼짐 · 수동 입력 모드")
        st.caption(
            "모든 회사·파트너·연락처는 **가상 데이터**입니다. "
            f"고객사를 먼저 등록한 파트너에게 {db.PROTECTION_DAYS}일간 영업권이 보호됩니다."
        )


# ---- 파트너 화면 --------------------------------------------------------

def clear_form() -> None:
    for f in FORM_FIELDS:
        st.session_state[f"f_{f}"] = ""
    for k in ("check", "uncertain", "logged_block"):
        st.session_state.pop(k, None)
    # 업로더는 key를 바꿔야 비워진다
    st.session_state["upload_gen"] = st.session_state.get("upload_gen", 0) + 1


def invalidate_check() -> None:
    st.session_state.pop("check", None)


def current_uploads() -> dict:
    """지금 등록 화면에 올라와 있는 증빙 이미지: {'card': 파일, 'cert': 파일} 중 있는 것만."""
    gen = st.session_state.get("upload_gen", 0)
    return {k: st.session_state[f"{k}_{gen}"] for k in evidence.KINDS
            if st.session_state.get(f"{k}_{gen}") is not None}


def register_tab(partner: dict) -> None:
    # 고객사 등록 화면을 가운데 칸에 넣는다 (입력칸이 화면 끝까지 늘어나지 않게). [1, 2, 1]의 가운데 숫자를 키우면 넓어진다.
    _, mid, _ = st.columns([1.5, 2.8, 1.5])
    with mid:
        register_form(partner)


def register_form(partner: dict) -> None:
    st.write("명함이나 사업자등록증 이미지를 올리면 AI가 내용을 채워 줍니다. 직접 입력해도 됩니다.")
    st.info(f"**{evidence.REQUIREMENT}** 첨부한 이미지는 등록 증빙으로 보관되며 총판이 확인할 수 있습니다.")

    if st.session_state.pop("reset_form", False):
        clear_form()
    if msg := st.session_state.pop("flash", None):
        st.success(msg)
    for f in FORM_FIELDS:
        st.session_state.setdefault(f"f_{f}", "")

    gen = st.session_state.get("upload_gen", 0)
    u1, u2 = st.columns(2)
    for col, kind in ((u1, "card"), (u2, "cert")):
        with col:
            f = st.file_uploader(f"{evidence.KINDS[kind]} 이미지", type=["png", "jpg", "jpeg", "webp"],
                                 key=f"{kind}_{gen}")
            if f is not None:
                st.image(f, width="stretch")
    uploads = current_uploads()
    # AI 자동 입력은 사업자등록증을 먼저 읽는다 (사업자번호가 확실히 적혀 있으므로)
    up = uploads.get("cert") or uploads.get("card")
    if up is not None:
        if st.button("🪄 AI로 자동 입력", disabled=not ai_extract.available()):
            if use_ai(partner["name"], "이미지 추출"):
                with st.spinner("이미지를 읽는 중…"):
                    try:
                        media = up.type if up.type in ("image/png", "image/jpeg", "image/webp") else "image/jpeg"
                        out = ai_extract.extract_from_image(up.getvalue(), media)
                    except Exception as e:  # 추출 실패해도 수동 입력은 계속 가능
                        st.error(f"자동 입력에 실패했습니다: {e}")
                        out = None
                if out:
                    for f in ("company_name", "biz_no", "contact_name", "contact_phone"):
                        st.session_state[f"f_{f}"] = out[f]
                    st.session_state["uncertain"] = out["uncertain_fields"]
                    invalidate_check()
                    st.rerun()

    if st.session_state.get("uncertain"):
        st.warning("AI가 확신하지 못한 항목: " + ", ".join(st.session_state["uncertain"]) + " — 꼭 확인해 주세요.")

    # 확인 후 회사명·사업자번호를 바꾸면 이전 확인 결과는 무효 → 다시 확인해야 등록 버튼이 나온다
    c1, c2 = st.columns(2)
    c1.text_input("회사명 *", key="f_company_name", on_change=invalidate_check)
    c2.text_input("사업자등록번호", key="f_biz_no", placeholder="000-00-00000", on_change=invalidate_check)
    c1.text_input("담당자", key="f_contact_name")
    c2.text_input("연락처", key="f_contact_phone")
    st.text_area("메모(영업 단계 등)", key="f_note", height=80)

    if st.button("선점 여부 확인", type="primary"):
        st.session_state["check"] = run_check(partner)

    check = st.session_state.get("check")
    if check and check["partner_id"] == partner["id"]:
        show_check_result(partner, check)


def run_check(partner: dict) -> dict:
    name = st.session_state["f_company_name"].strip()
    biz = st.session_state["f_biz_no"].strip()
    result = {"partner_id": partner["id"], "name": name, "biz": biz, "error": None,
              "exact": [], "similar": [], "ai": {}}
    if not name:
        result["error"] = "회사명을 입력해 주세요."
        return result
    if biz and not matching.is_valid_biz_no(biz):
        result["error"] = "사업자등록번호 형식이 올바르지 않습니다(10자리·검증번호 확인)."
        return result
    uploads = current_uploads()
    if msg := evidence.missing_requirement(biz, "card" in uploads, "cert" in uploads):
        result["error"] = msg
        return result
    found = matching.find_conflicts(name, biz, db.blocking_registrations())
    result["exact"], result["similar"] = found["exact"], found["similar"]
    # 담당 파트너가 있는 계약 고객: 자동 차단 대신 총판 검토로 보낸다 (고객이 파트너 변경을 원할 수 있어서)
    lapsed = matching.find_conflicts(name, biz, db.owned_registrations())
    result["lapsed"] = [(row, why) for row, why in lapsed["exact"]] + [(row, f"유사도 {s}") for row, s in lapsed["similar"]]
    others = [row for row, _ in result["similar"] if row["partner_id"] != partner["id"]]
    if others and not result["exact"] and ai_extract.available() and use_ai(partner["name"], "유사 회사명 판단"):
        try:
            for j in ai_extract.judge_same_company(name, biz, others):
                if 0 <= j["index"] < len(others):
                    result["ai"][others[j["index"]]["id"]] = j
        except Exception as e:
            result["ai_error"] = str(e)
    return result


def show_check_result(partner: dict, check: dict) -> None:
    if check["error"]:
        st.error(check["error"])
        return

    exact = check["exact"]
    if exact:
        row, why = exact[0]
        if row["partner_id"] == partner["id"]:
            st.info(f"이미 우리 회사가 등록한 고객사입니다 ({row['company_name']}, 만료 {row['expires_on']}).")
        else:
            # 경쟁 파트너끼리 고객 정보가 새지 않도록 어느 파트너인지는 보여 주지 않는다
            st.error(f"**등록 불가** — 다른 파트너가 이미 선점 중인 고객사입니다 ({why}, 보호 만료 {row['expires_on']}).")
            if st.session_state.get("logged_block") != (partner["id"], check["name"]):
                db.log("blocked", partner["name"], row["id"],
                       f"{check['name']} 등록 시도 → {row['partner_name']} 선점({why})")
                st.session_state["logged_block"] = (partner["id"], check["name"])
        return

    owned = check.get("lapsed", [])  # 담당 파트너가 있는 계약 고객
    own = [r for r, why in owned if r["partner_id"] == partner["id"] and "유사도" not in why]
    if own:
        st.info(f"이미 우리 회사 담당 고객입니다 ({own[0]['company_name']} · {crm.relation(own[0])}). "
                f"새로 등록하지 말고 '영업 관리'에서 **{crm.renew_label(own[0])}**로 처리해 주세요.")
        return

    # 비슷한 이름의 다른 파트너 등록: 파트너에게는 존재 여부만 알리고, 상세(회사명·파트너·AI 의견)는 관리자 검토 사유에만 남긴다
    similar = [(r, s) for r, s in check["similar"] if r["partner_id"] != partner["id"]]
    flagged, notes = [], []
    for row, why in owned:
        if row["partner_id"] != partner["id"]:
            flagged.append(row)
            notes.append(f"{row['company_name']}({row['partner_name']}의 담당 고객 · {admin.account_status(row)}, {why}) "
                         "— 고객이 파트너 변경을 원하는지 확인 후, 맞으면 이 등록을 반려하고 기존 등록을 이관")
    for row, score in similar:
        ai = check["ai"].get(row["id"])
        if ai is None or ai["likely_same"]:
            flagged.append(row)
            note = f"{row['company_name']}({row['partner_name']}, 유사도 {score})"
            if ai:
                note += f" — AI: 같은 회사 가능성 높음, {ai['reason']}"
            notes.append(note)

    if flagged:
        st.warning("이 고객사(또는 이름이 비슷한 고객사)는 다른 파트너가 담당 중이거나 담당했던 기존 고객일 수 있습니다. "
                   "**총판 관리자 검토 후** 보호가 시작됩니다. 선점 시각은 지금으로 기록됩니다.")
        label, status = "검토 요청으로 등록", "pending"
        reason = "유사 등록: " + "; ".join(notes)
    else:
        st.success("선점된 고객사가 없습니다. 등록하면 오늘부터 90일간 보호됩니다.")
        label, status, reason = "등록하기", "active", ""

    if st.button(label, type="primary"):
        # 마지막 안전장치: 확인했던 값과 지금 입력값이 다르면 저장하지 않는다
        if (st.session_state["f_company_name"].strip(), st.session_state["f_biz_no"].strip()) != (check["name"], check["biz"]):
            invalidate_check()
            st.error("확인 후 입력값이 바뀌었습니다. '선점 여부 확인'을 다시 눌러 주세요.")
            return
        uploads = current_uploads()
        if msg := evidence.missing_requirement(check["biz"], "card" in uploads, "cert" in uploads):
            st.error(msg)  # 확인한 뒤 이미지를 지운 경우
            return
        # 이미지를 먼저 줄여 본다: 읽을 수 없는 파일이면 등록 전에 멈춘다
        try:
            shrunk = {k: evidence.shrink(f.getvalue()) for k, f in uploads.items()}
        except evidence.EvidenceError as e:
            st.error(str(e))
            return
        rid = db.add_registration(
            partner["id"], check["name"], matching.format_biz_no(check["biz"]),
            st.session_state["f_contact_name"].strip(), st.session_state["f_contact_phone"].strip(),
            st.session_state["f_note"].strip(), status, reason, owner=partner["user"]["username"],
        )
        db.log("register" if status == "active" else "pending", partner["name"], rid,
               f"{check['name']} " + ("등록" if status == "active" else f"검토 요청 — {reason}"))
        for kind, data in shrunk.items():
            evidence.store(rid, kind, data, partner["user"]["username"])
        # 입력 위젯이 이미 그려졌으므로 값은 다음 실행 첫머리에서 비운다
        st.session_state["reset_form"] = True
        st.session_state["flash"] = "등록되었습니다." if status == "active" else "검토 요청이 접수되었습니다."
        st.rerun()


def my_tab(partner: dict) -> None:
    if msg := st.session_state.pop("flash_my", None):
        st.success(msg)
    user = partner["user"]
    rows = db.list_registrations(partner["id"])
    if not crm.is_manager(user):  # 영업 담당자는 자기 담당 고객만
        rows = [r for r in rows if r["owner"] == user["username"]]
    if not rows:
        st.info("아직 등록한 고객사가 없습니다.")
        return
    soon = [r for r in rows if r["status"] == "active" and days_left(r["expires_on"]) <= db.EXPIRING_SOON_DAYS]
    if soon:
        st.warning(f"보호 기간이 {db.EXPIRING_SOON_DAYS}일 안에 끝나는 고객사 {len(soon)}곳: "
                   + ", ".join(f"{r['company_name']}(D-{days_left(r['expires_on'])})" for r in soon))
    renew = [r for r in rows if r["status"] == "won" and days_left(r["expires_on"]) <= db.CONTRACT_ALERT_DAYS]
    if renew:
        st.warning(f"유지보수·구독이 {db.CONTRACT_ALERT_DAYS}일 안에 끝나는 계약 고객 {len(renew)}곳 — 연장·갱신 영업 시점입니다: "
                   + ", ".join(f"{r['company_name']}({crm.LICENSE_TYPES.get(r['license_type'], '영구')}, "
                               f"D-{days_left(r['expires_on'])})" for r in renew))
    lapsed = [r for r in rows if r["status"] == "lapsed"]
    rejoin = [r for r in lapsed if db.is_perpetual(r)]
    grace = [r for r in lapsed if not db.is_perpetual(r)]
    if rejoin:
        st.info(f"유지보수가 끝난 영구 라이선스 고객 {len(rejoin)}곳 — 계속 우리 회사 담당입니다. 유지보수 재가입 영업 대상: "
                + ", ".join(r["company_name"] for r in rejoin))
    if grace:
        st.warning(f"구독이 끝난 고객 {len(grace)}곳 — 갱신 유예 {db.SUB_GRACE_DAYS}일 안에 갱신하지 않으면 담당이 풀립니다: "
                   + ", ".join(f"{r['company_name']}(~{crm.grace_end(r)})" for r in grace))
    st.caption("기본 영업 보호일은 90일. 계약 완료된 고객의 경우 보호 기간 없이 우리 회사 담당 고객으로 계속 관리되며, "
               "유지보수·구독 종료일은 연장·갱신 영업 시점입니다.")
    st.dataframe(reg_table(rows, show_partner=False), hide_index=True, width="stretch")
    edit_section(partner, rows)
    with st.expander("📎 등록 증빙 이미지 (명함·사업자등록증)"):
        st.caption("우리 회사가 등록한 고객사의 증빙만 보입니다. 증빙은 총판도 확인할 수 있으며, 지워지지 않습니다.")
        by_id = {r["id"]: r for r in rows}
        eid = st.selectbox("고객사", list(by_id), key="evidence_pick",
                           format_func=lambda i: f"{by_id[i]['company_name']} ({STATUS_LABEL[by_id[i]['status']]}, "
                                                 f"등록 {by_id[i]['registered_at'][:10]})")
        can_add = by_id[eid]["status"] in ("active", "pending", "won")
        show_evidence(eid, add_as=partner["user"]["username"] if can_add else None)


def biz_no_conflict(registration_id: int, biz: str, partner_id: int) -> str | None:
    """사업자번호를 바꿀 때 다른 유효 등록과 겹치는지 확인. 겹치면 안내 문구를 돌려준다."""
    nb = matching.normalize_biz_no(biz)
    for h in db.holding_registrations():
        if h["id"] != registration_id and matching.normalize_biz_no(h["biz_no"]) == nb:
            if h["partner_id"] == partner_id:
                return f"이 사업자번호는 이미 '{h['company_name']}' 등록에 쓰이고 있습니다."
            return "이 사업자번호는 이미 다른 파트너가 선점 중인 고객사의 번호입니다."
    return None


def edit_section(partner: dict, rows: list[dict]) -> None:
    editable = {r["id"]: r for r in rows if r["status"] in ("active", "pending", "won", "lapsed")}
    if not editable:
        return
    with st.expander("✏️ 등록 정보 수정"):
        st.caption("회사명과 등록일은 선점 기준이라 바꿀 수 없습니다. 회사명을 잘못 입력했다면 총판 관리자에게 요청해 주세요.")
        rid = st.selectbox("수정할 고객사", list(editable),
                           format_func=lambda i: f"{editable[i]['company_name']} ({STATUS_LABEL[editable[i]['status']]})")
        r = editable[rid]
        with st.form(f"edit_{rid}"):
            biz = st.text_input("사업자등록번호", value=matching.format_biz_no(r["biz_no"]) if r["biz_no"] else "",
                                placeholder="000-00-00000")
            c1, c2 = st.columns(2)
            contact = c1.text_input("담당자", value=r["contact_name"])
            phone = c2.text_input("연락처", value=r["contact_phone"])
            email = (st.text_input("고객사 ENCY 계정 이메일", value=r["ency_account"])
                     if r["status"] in ("won", "lapsed") else r["ency_account"])
            note = st.text_area("메모", value=r["note"], height=80)
            amount = r["contract_amount"]
            if r["status"] in ("won", "lapsed"):
                amount = st.number_input(f"계약 금액 — 원, {crm.VAT_NOTE}", min_value=0, step=100_000,
                                         value=r["contract_amount"] or None, placeholder="예: 12000000")
            if not st.form_submit_button("저장", type="primary"):
                return
        if r["status"] in ("won", "lapsed"):
            try:
                email = crm.check_ency_account(email)
                if amount or r["contract_amount"]:  # 예전 계약 고객은 금액이 비어 있어도 다른 칸을 고칠 수 있게
                    amount = crm.check_amount(amount)
            except crm.CrmError as e:
                st.error(str(e))
                return
        biz = biz.strip()
        if biz and not matching.is_valid_biz_no(biz):
            st.error("사업자등록번호 형식이 올바르지 않습니다(10자리·검증번호 확인).")
            return
        has = db.evidence_kinds().get(rid, set())
        if msg := evidence.missing_requirement(biz, "card" in has, "cert" in has):
            st.error(f"사업자번호를 지울 수 없습니다. {msg}")
            return
        biz = matching.format_biz_no(biz) if biz else ""
        if biz and matching.normalize_biz_no(biz) != matching.normalize_biz_no(r["biz_no"]):
            if msg := biz_no_conflict(rid, biz, partner["id"]):
                st.error(f"저장할 수 없습니다. {msg}")
                return
        new = {"biz_no": biz, "contact_name": contact.strip(), "contact_phone": phone.strip(), "note": note.strip(),
               "ency_account": email, "contract_amount": int(amount or 0)}
        changed = {k: v for k, v in new.items() if v != r[k]}
        if not changed:
            st.info("바뀐 내용이 없습니다.")
            return
        db.update_registration(rid, changed)
        if "ency_account" in changed and r["ency_issued_at"]:
            # 이메일이 바뀌면 새 이메일로 다시 발급해야 하므로 총판의 발급 대기 목록으로 되돌린다
            db.update_registration(rid, {"ency_issued_at": ""}, allowed=db.ADMIN_EDITABLE)
            db.log("ency_issued", partner["name"], rid,
                   f"{r['company_name']} ENCY 계정 이메일 변경 → 다시 발급 대기 ({r['ency_account']}→{changed['ency_account']})")
        labels = {"biz_no": "사업자번호", "contact_name": "담당자", "contact_phone": "연락처", "note": "메모",
                  "ency_account": "ENCY 계정", "contract_amount": "계약 금액(공급가액)"}
        shown = {"contract_amount": crm.won_text}
        db.log("edit", partner["name"], rid,
               f"{r['company_name']} 수정: " + ", ".join(
                   f"{labels[k]} '{shown.get(k, str)(r[k])}'→'{shown.get(k, str)(v)}'"
                   if k in ("biz_no", "ency_account", "contract_amount") else labels[k]
                   for k, v in changed.items()))
        st.session_state["flash_my"] = "수정했습니다."
        st.rerun()


def lookup_tab(partner: dict) -> None:
    st.write("회사명이나 사업자번호로 이미 선점된 고객사인지 확인합니다. "
             "다른 파트너의 고객은 선점 여부와 보호 만료일만 보입니다.")
    q = st.text_input("검색어")
    if not q:
        return
    found = matching.find_conflicts(q, q if matching.normalize_biz_no(q) else "", db.blocking_registrations())
    owned = matching.find_conflicts(q, q if matching.normalize_biz_no(q) else "", db.owned_registrations())
    owned_rows = [r for r, _ in owned["exact"]] + [r for r, _ in owned["similar"]]
    for r in owned_rows:
        if r["partner_id"] == partner["id"]:
            st.info(f"우리 회사 담당 고객입니다: {r['company_name']} ({crm.relation(r)}).")
    if any(r["partner_id"] != partner["id"] for r in owned_rows):
        # 다른 파트너의 계약 고객은 이름·날짜를 보여 주지 않는다
        st.warning("다른 파트너가 담당하는 기존 고객일 수 있습니다. 등록하면 총판 관리자 검토를 거칩니다.")
    if not found["exact"] and not found["similar"]:
        if not owned_rows:
            st.success("선점된 고객사가 없습니다. 바로 등록할 수 있습니다.")
        return
    for row, why in found["exact"]:
        if row["partner_id"] == partner["id"]:
            st.info(f"우리 회사가 보호 중인 고객사입니다: {row['company_name']} ({crm.relation(row)})")
        else:
            st.error(f"다른 파트너가 선점 중입니다 ({why}, 보호 만료 {row['expires_on']}).")
    mine = [r for r, _ in found["similar"] if r["partner_id"] == partner["id"]]
    others = [r for r, _ in found["similar"] if r["partner_id"] != partner["id"]]
    if mine:
        st.info("이름이 비슷한 우리 회사 고객사: " + ", ".join(r["company_name"] for r in mine))
    if others:
        # 다른 파트너 고객의 이름을 보여 주면 검색어를 바꿔 가며 경쟁사 고객 목록을 알아낼 수 있어서 개수만 알린다
        st.warning(f"이름이 비슷한 고객사 {len(others)}곳이 다른 파트너에게 선점되어 있습니다. "
                   "등록하면 총판 관리자 검토를 거칩니다.")


def crm_tab(user: dict) -> None:
    st.caption("영업 기록은 우리 회사 직원만 봅니다. 총판은 단계별 건수만 보며, 상세를 열람하면 사유가 '총판 열람 기록'에 공개됩니다.")
    if msg := st.session_state.pop("flash_crm", None):
        st.success(msg)
    rows = {r["id"]: r for r in crm.visible_registrations(user)}
    if not rows:
        st.info("담당 중인 고객사가 없습니다.")
        return

    counts = {s: sum(r["stage"] == s for r in rows.values()) for s in crm.STAGES}
    cols = st.columns(len(crm.STAGES))
    for col, s in zip(cols, crm.STAGES):
        col.metric(s, counts[s])

    staff = {s["username"]: s for s in db.list_staff(user["partner_id"])}
    def owner_name(u: str) -> str:
        return staff[u]["display_name"] or u if u in staff else "미지정"

    # 고객사가 많아져도 찾기 쉽게: 검색어 + 단계로 좁힌 뒤 고른다
    s1, s2 = st.columns([2, 1])
    query = s1.text_input("🔍 고객사 검색", key="crm_query",
                          placeholder="회사명, 사업자번호, 고객 담당자 이름·연락처로 검색")
    stages = s2.multiselect("영업 단계", crm.STAGES, key="crm_stages", placeholder="전체 단계")
    found = crm.search(list(rows.values()), query, stages)
    if not found:
        st.info("조건에 맞는 고객사가 없습니다. 검색어나 단계를 바꿔 보세요.")
        return
    if query or stages:
        st.caption(f"{len(rows)}곳 중 {len(found)}곳")
    rid = st.selectbox(
        "고객사", [x["id"] for x in found], key="crm_pick",
        format_func=lambda i: f"{rows[i]['company_name']} · {rows[i]['stage']} · {STATUS_LABEL[rows[i]['status']]}"
                              + (f" · 담당 {owner_name(rows[i]['owner'])}" if crm.is_manager(user) else ""),
    )
    r = rows[rid]
    contracted = r["status"] in ("won", "lapsed")
    expected = r["expected_close"] if r["stage"] in crm.FORECAST_STAGES and r["expected_close"] else ""
    # 고객사 요약 카드: 회사명은 크게, 항목마다 작은 이름표(caption) + 값
    with st.container(border=True):
        st.markdown(f"#### {r['company_name']}")
        k1, k2, k3, k4 = st.columns(4)
        k1.caption("**고객 담당자**")
        k1.markdown(r["contact_name"] or "-")
        k2.caption("**담당 영업**")
        k2.markdown(owner_name(r["owner"]))
        k3.caption("**상태**")
        k3.markdown(f"{STATUS_LABEL[r['status']]}  \n{crm.relation(r)}"
                    + (f"  \n{crm.LICENSE_TYPES.get(r['license_type'], '영구')} 라이선스" if contracted else "")
                    + (f"  \n계약 금액 {crm.won_text(r['contract_amount'])} (공급가액, VAT 별도)" if contracted else "")
                    + (f"  \nENCY 계정 {r['ency_account']} · "
                       + (f":green-badge[발급 완료 {r['ency_issued_at'][:10]}]" if r["ency_issued_at"]
                          else ":orange-badge[발급 대기]")
                       if contracted and r["ency_account"] else ""))
        k4.caption("**영업 단계**")
        k4.markdown(f":blue-badge[{r['stage']}]" + (f"  \n예상 구매 {expected}" if expected else ""))
    if crm.is_overdue(r):
        st.warning("예상 구매일이 지났습니다. 단계나 예상 구매일을 다시 확인해 주세요.")

    with st.expander("영업 단계 기준 보기"):
        st.markdown("\n".join(f"- **{s}** — {crm.STAGE_GUIDE[s]}" for s in crm.STAGES))

    c1, c2 = st.columns(2)
    with c1, st.form(f"stage_{rid}"):
        stage = st.selectbox("영업 단계", crm.STAGES, index=crm.STAGES.index(r["stage"]),
                             format_func=lambda s: f"{s} — {crm.STAGE_GUIDE[s]}")
        # 날짜 칸은 빈 채로 시작한다 (미리 채운 날짜를 실수로 저장하지 않게). 이미 저장된 예상 구매일만 채워 둔다.
        saved_exp = (date.fromisoformat(r["expected_close"]) if r["expected_close"]
                     and r["expected_close"] >= date.today().isoformat() else None)
        expected_close = st.date_input("예상 구매일 ('구매 임박'은 필수입력)", value=saved_exp,
                                       min_value=date.today(), format="YYYY-MM-DD")
        license_start, ency_account, license_type, contract_amount = None, None, None, None
        if not contracted:
            license_type = st.radio("라이선스 종류 (수주 시 필수 선택)", list(crm.LICENSE_TYPES), index=None,
                                    format_func=crm.LICENSE_TYPES.get, horizontal=True)
            license_start = st.date_input("라이선스 시작일 (수주 시 필수 입력)", value=None,
                                          format="YYYY-MM-DD")
            ency_account = st.text_input("고객사 ENCY 계정 이메일 (수주 시 필수 입력)",
                                         placeholder="cam@company.co.kr · 총판이 이 이메일로 ENCY 계정을 발급합니다")
            contract_amount = st.number_input(f"계약 금액 (수주 시 필수 입력) — 원, {crm.VAT_NOTE}", min_value=0,
                                              step=100_000, value=None, placeholder="예: 12000000")
            st.caption("⚠️ 부가세를 뺀 **공급가액**을 넣어 주세요. "
                       "예: 공급가액 10,000,000원 → 부가세 포함 11,000,000원이면 10000000을 입력")
        if st.form_submit_button("단계 저장"):
            try:
                crm.set_stage(user, rid, stage,
                              expected_close=expected_close.isoformat() if expected_close else None,
                              license_start=license_start.isoformat() if license_start else None,
                              ency_account=ency_account, license_type=license_type,
                              contract_amount=contract_amount)
            except crm.CrmError as e:
                st.error(str(e))
            else:
                done = f"단계를 '{stage}'(으)로 저장했습니다."
                if stage in crm.FORECAST_STAGES:
                    done += f" 예상 구매일은 {expected_close}입니다." if expected_close else " 예상 구매일은 비워 두었습니다."
                if stage == "수주" and not contracted:
                    saved = db.get_registration(rid)
                    amt = saved["contract_amount"]
                    done += (f" 계약 금액 {amt:,}원 {crm.VAT_NOTE} — 부가세 포함 시 {round(amt * 1.1):,}원.")
                    done += (f" 이제 우리 회사 담당 고객입니다(보호 기간 없음). {crm.LICENSE_TYPES[saved['license_type']]} · "
                             f"라이선스 시작 {saved['license_start']}, {crm.end_label(saved)}일 {saved['expires_on']}. "
                             f"ENCY 계정({saved['ency_account']}) 발급이 총판에 요청되었습니다.")
                st.session_state["flash_crm"] = done
                st.rerun()
    if contracted:
        # 영구: 유지보수 연장(유지보수 중) / 유지보수 재가입(만료 후, 오늘부터 1년). 구독: 1년·3년 갱신.
        action = crm.renew_label(r)
        with c1, st.form(f"renew_{rid}"):
            st.markdown(f"**{action}**")
            years = 1
            if not db.is_perpetual(r):
                years = st.radio("갱신 기간", [1, 3], format_func=lambda y: f"{y}년", horizontal=True)
            st.caption(f"새 {crm.end_label(r)}일: 1년이면 {crm.renewal_default(r, 1)}"
                       + (f", 3년이면 {crm.renewal_default(r, 3)}" if not db.is_perpetual(r) else "")
                       + (" (유지보수가 끝난 뒤라 오늘부터 계산)" if r["status"] == "lapsed" else ""))
            if st.form_submit_button(action):
                try:
                    end = crm.renew_contract(user, rid, crm.renewal_default(r, years))
                except crm.CrmError as e:
                    st.error(str(e))
                else:
                    st.session_state["flash_crm"] = f"{action} 처리했습니다. 새 {crm.end_label(r)}일은 {end}입니다."
                    st.rerun()
    if crm.is_manager(user):
        active_staff = [u for u, s in staff.items() if s["active"]]
        with c2, st.form(f"owner_{rid}"):
            new_owner = st.selectbox("우리 담당 직원", active_staff, format_func=owner_name,
                                     index=active_staff.index(r["owner"]) if r["owner"] in active_staff else 0)
            if st.form_submit_button("담당자 변경"):
                try:
                    crm.reassign(user, rid, new_owner)
                except crm.CrmError as e:
                    st.error(str(e))
                else:
                    st.session_state["flash_crm"] = f"담당자를 {owner_name(new_owner)}(으)로 바꿨습니다."
                    st.rerun()

    with st.form(f"act_{rid}", clear_on_submit=True):
        st.markdown("**활동 기록 추가**")
        a1, a2 = st.columns([1, 1])
        on_date = a1.date_input("날짜", value=date.today(), max_value=date.today())
        kind = a2.selectbox("유형", crm.ACTIVITY_KINDS)
        note = st.text_area("내용", max_chars=crm.NOTE_MAX, height=80, placeholder=crm.NOTE_GUIDE)
        st.caption("기록은 수정·삭제할 수 없습니다. 잘못 적었다면 '정정' 유형으로 새 기록을 남겨 주세요.")
        if st.form_submit_button("기록 저장", type="primary"):
            try:
                crm.add_activity(user, rid, kind, note, on_date.isoformat())
            except crm.CrmError as e:
                st.error(str(e))
            else:
                st.session_state["flash_crm"] = "기록을 저장했습니다."
                st.rerun()

    st.markdown("**타임라인** (같은 고객사의 지난 등록 이력까지 이어서 보여 줍니다)")
    show_timeline(crm.timeline(user, rid))


def show_timeline(items: list[dict]) -> None:
    if not items:
        st.caption("아직 기록이 없습니다.")
        return
    year = None
    for a in items:
        if a["on_date"][:4] != year:
            year = a["on_date"][:4]
            st.markdown(f"##### {year}년")
        st.markdown(f"- `{a['on_date']}` **{a['kind']}** — {a['note']}  \n"
                    f"  <small>{a['author_name']} · 기록 {a['ts'][:16].replace('T', ' ')}</small>",
                    unsafe_allow_html=True)


def team_tab(user: dict) -> None:
    if msg := st.session_state.pop("flash_team", None):
        st.success(msg)
    show_temp_password()
    staff = db.list_staff(user["partner_id"])
    by_name = {s["username"]: s for s in staff}
    regs = db.list_registrations(user["partner_id"])
    st.dataframe(pd.DataFrame([{
        "아이디": s["username"], "이름": s["display_name"], "직함": s["title"], "부서": s["department"],
        "역할": ROLE_LABEL[s["partner_role"]], "연락처": s["phone"], "이메일": s["email"],
        "상태": ("임시 비밀번호" if s["must_change_pw"] else "사용 중") if s["active"] else "퇴사 처리",
        "담당 고객 수": sum(r["owner"] == s["username"] for r in regs),
    } for s in staff]), hide_index=True, width="stretch")

    active = [s for s in staff if s["active"]]
    names = {s["username"]: f"{s['display_name'] or s['username']} ({s['username']})" for s in staff}
    leavers = [s["username"] for s in active if s["username"] != user["username"]]
    st.markdown("**퇴사 처리**")
    st.caption("로그인이 즉시 막히고, 담당 고객과 이력은 지정한 직원에게 그대로 인계됩니다.")
    if leavers:
        with st.form("deactivate"):
            leaving = st.selectbox("퇴사자", leavers, format_func=names.get)
            to = st.selectbox("인계받을 직원", [s["username"] for s in active], format_func=names.get)
            sure = st.checkbox("퇴사 처리합니다")
            if st.form_submit_button("퇴사 처리"):
                if not sure:
                    st.error("'퇴사 처리합니다'에 체크해 주세요.")
                else:
                    try:
                        n = crm.deactivate(user, leaving, to)
                    except crm.CrmError as e:
                        st.error(str(e))
                    else:
                        st.session_state["flash_team"] = f"{names[leaving]} 퇴사 처리, 고객 {n}곳을 {names[to]}에게 인계했습니다."
                        st.rerun()

    st.markdown("**직원 추가**")
    with st.form("add_staff", clear_on_submit=True):
        c1, c2 = st.columns(2)
        username = c1.text_input("아이디")
        display = c2.text_input("이름")
        if st.form_submit_button("계정 만들기"):
            account_action(accounts.add_staff, f"{username} 계정을 만들었습니다(영업 담당자).",
                           user, username, display, temp_for=username)

    others = [s["username"] for s in active if s["username"] != user["username"]]
    if others:
        c1, c2 = st.columns(2)
        with c1, st.form("role"):
            st.markdown("**관리자 지정·해제**")
            who = st.selectbox("직원", others, key="role_who",
                               format_func=lambda u: f"{names[u]} · {ROLE_LABEL[by_name[u]['partner_role']]}")
            if st.form_submit_button("역할 바꾸기"):
                new_role = "rep" if by_name[who]["partner_role"] == "manager" else "manager"
                account_action(accounts.set_role, f"{names[who]}: {ROLE_LABEL[new_role]}(으)로 바꿨습니다.",
                               user, who, new_role)
        with c2, st.form("reset_pw"):
            st.markdown("**비밀번호 재설정**")
            who = st.selectbox("직원", others, format_func=names.get, key="reset_who")
            if st.form_submit_button("임시 비밀번호 발급"):
                account_action(accounts.reset_password, f"{names[who]} 비밀번호를 재설정했습니다.",
                               user, who, temp_for=who)


def account_action(fn, done: str, *args, temp_for: str | None = None, flash_key: str = "flash_team"):
    """계정 관련 조치 실행. 임시 비밀번호가 나오면 다음 화면에서 한 번만 보여 준다."""
    try:
        result = fn(*args)
    except accounts.AccountError as e:
        st.error(str(e))
        return
    st.session_state[flash_key] = done
    if temp_for and isinstance(result, str):
        st.session_state["temp_pw"] = (temp_for, result)
    st.rerun()


def show_temp_password() -> None:
    if shown := st.session_state.pop("temp_pw", None):
        username, pw = shown
        st.warning(f"**`{username}`의 임시 비밀번호: `{pw}`**  \n"
                   "이 화면을 벗어나면 다시 볼 수 없습니다. 본인에게 안전하게 전달하세요. "
                   "첫 로그인 때 새 비밀번호로 바꾸게 됩니다.")


def access_log_tab(user: dict) -> None:
    st.caption("총판이 우리 회사의 상세 영업 기록을 언제, 왜 열람했는지 보여 줍니다.")
    logs = db.list_access_log(user["partner_id"])
    if not logs:
        st.success("총판이 상세 기록을 열람한 적이 없습니다.")
        return
    st.dataframe(pd.DataFrame([{
        "시각": l["ts"].replace("T", " "), "열람자": l["viewer"], "고객사": l["company_name"], "사유": l["reason"],
    } for l in logs]), hide_index=True, width="stretch")


def notices_tab() -> None:
    for n in db.list_notices():
        with st.expander(f"{n['ts'][:10]} · {n['title']}"):
            st.write(n["body"])


# ---- 관리자 화면 --------------------------------------------------------

def review_tab(user: dict) -> None:
    if msg := st.session_state.pop("flash_admin", None):
        st.success(msg)
    pending = db.list_registrations(statuses=("pending",))
    if not pending:
        st.success("검토 대기 중인 등록이 없습니다.")
        return
    for r in pending:
        with st.container(border=True):
            st.markdown(f"**{r['company_name']}** · {r['partner_name']} · {r['registered_at'][:16].replace('T', ' ')} 요청")
            st.caption(r["review_reason"])
            with st.expander(f"증빙 보기 — {evidence.summary(r, db.evidence_kinds().get(r['id'], set()))}"):
                show_evidence(r["id"])
            c1, c2, _ = st.columns([1, 1, 4])
            if c1.button("승인", key=f"ok{r['id']}"):
                run_action(admin.approve, f"{r['company_name']}: 승인했습니다.", user, r["id"])
            if c2.button("반려", key=f"no{r['id']}"):
                run_action(admin.reject, f"{r['company_name']}: 반려했습니다.", user, r["id"])


EVENT_LABEL = {"register": "등록", "pending": "검토 요청", "blocked": "등록 차단", "approve": "승인",
               "reject": "반려", "expire": "만료", "ai_call": "AI 호출", "notice": "공지", "edit": "정보 수정",
               "release": "등록 해제", "extend": "기간 연장", "transfer": "이관", "rename": "회사명 수정",
               "restore": "반려 취소", "handover": "퇴사 인계", "account": "계정 관리",
               "won": "수주(계약 고객)", "renew": "유지보수 연장", "evidence": "증빙 첨부",
               "ency_issued": "ENCY 계정 발급", "ticket": "기술 문의"}


def run_action(fn, done: str, *args, **kwargs):
    """관리자 조치를 실행하고, 막히면 이유를 보여 준다. 성공하면 결과 문구를 띄우고 새로 그린다."""
    try:
        result = fn(*args, **kwargs)
    except admin.ActionError as e:
        st.error(str(e))
        return None
    if result == []:  # rename: 확인할 비슷한 등록 없이 변경 완료
        result = None
    if result is None or isinstance(result, str):
        st.session_state["flash_admin"] = done
        st.rerun()
    return result


def manage_tab(user: dict) -> None:
    st.caption("보호 중인 등록의 해제·연장·이관·회사명 수정, 반려한 건 되돌리기. 모든 조치는 사유와 함께 이력에 남습니다.")
    if msg := st.session_state.pop("flash_admin", None):
        st.success(msg)
    rows = {r["id"]: r for r in db.list_registrations()}
    rid = st.selectbox(
        "대상 등록 (입력해서 검색)", list(rows),
        format_func=lambda i: f"{rows[i]['company_name']} · {rows[i]['partner_name']} · {STATUS_LABEL[rows[i]['status']]}",
    )
    r = rows[rid]
    own_ext, total_ext = admin.extension_counts().get(rid, (0, 0))
    contracted = r["status"] in ("won", "lapsed")
    st.markdown(
        f"**{r['company_name']}** · {r['partner_name']} · {STATUS_LABEL[r['status']]}  \n"
        f"사업자번호 {matching.format_biz_no(r['biz_no']) or '미기재'} · 등록 {r['registered_at'][:10]} · "
        f"{crm.relation(r)}  \n"
        + (f"{crm.LICENSE_TYPES.get(r['license_type'], '영구')} 라이선스 · 시작 {r['license_start'] or '-'} · "
           f"**ENCY 계정 {r['ency_account'] or '미입력'}**  \n" if contracted else "")
        + f"기간 연장: 이 등록 **{own_ext}회** · 이 고객사 누적 **{total_ext}회** (모든 파트너·재등록 포함)"
    )
    with st.expander(f"📎 등록 증빙 — {evidence.summary(r, db.evidence_kinds().get(rid, set()))}", expanded=True):
        show_evidence(rid)
    status = r["status"]
    if status in ("expired", "released"):
        st.info("만료·해제된 등록은 조치 대상이 아닙니다. 다시 보호하려면 파트너가 새로 등록해야 합니다.")
        return

    actions = {"active": ["기간 연장", "이관", "회사명 수정", "등록 해제"],
               "pending": ["회사명 수정", "등록 해제"],
               "won": ["이관", "회사명 수정", "등록 해제"],  # 계약 고객은 유지보수·구독 종료일로 보호 → 연장 대상 아님
               # 계약 고객(만료·유예 포함)엔 보호 기간이 없다. 담당 변경은 고객 요청 시 이관, 관계 정리는 해제로
               "lapsed": ["이관", "회사명 수정", "등록 해제"],
               "rejected": ["반려 되돌리기"]}[status]
    action = st.radio("조치", actions, horizontal=True, key=f"act_{rid}")

    with st.form(f"form_{rid}_{action}"):
        if action == "기간 연장":
            days = st.selectbox("연장 기간", [30, 60, 90], format_func=lambda d: f"{d}일")
            st.caption("연장 횟수 제한은 없습니다. 위의 누적 연장 횟수를 보고 판단해 주세요.")
        elif action == "이관":
            others = [p for p in db.list_partners() if p["id"] != r["partner_id"]]
            to = st.selectbox("받을 파트너", [p["id"] for p in others],
                              format_func=lambda i: next(p["name"] for p in others if p["id"] == i))
            st.caption("가망 고객의 보호 기간, 계약 고객의 유지보수·구독 종료일은 그대로 유지합니다. "
                       "계약 고객은 고객이 파트너 변경을 요청했을 때 이관합니다. 이전 파트너가 남긴 영업 기록은 "
                       "새 파트너에게 보이지 않으며, 지우지 않고 분쟁 근거로 보존합니다.")
        elif action == "회사명 수정":
            new_name = st.text_input("새 회사명", value=r["company_name"])
            confirm = st.checkbox("비슷한 이름의 다른 등록이 있어도 변경합니다")
        elif action == "등록 해제":
            st.warning("해제하면 이 고객사는 비게 되어 다른 파트너가 바로 등록할 수 있습니다.")
            sure = st.checkbox("해제합니다")
        reason = st.text_input("사유 *", placeholder="예: 파트너 요청으로 영업 중단")
        if not st.form_submit_button(action, type="primary"):
            return

    if action == "기간 연장":
        run_action(admin.extend, f"{days}일 연장했습니다.", user, rid, days, reason)
    elif action == "이관":
        run_action(admin.transfer, "이관했습니다.", user, rid, to, reason)
    elif action == "회사명 수정":
        similar = run_action(admin.rename, "회사명을 바꿨습니다.", user, rid, new_name, reason, confirm_similar=confirm)
        if similar:
            st.warning("비슷한 이름의 다른 등록이 있습니다. 같은 회사가 아닌지 확인한 뒤, 체크하고 다시 눌러 주세요.\n\n"
                       + "\n".join(f"- {s}" for s in similar))
    elif action == "등록 해제":
        if not sure:
            st.error("'해제합니다'에 체크해 주세요.")
        else:
            run_action(admin.release, "등록을 해제했습니다.", user, rid, reason)
    elif action == "반려 되돌리기":
        run_action(admin.restore, "반려를 취소하고 보호 중으로 되돌렸습니다.", user, rid, reason)


def pipeline_tab(user: dict) -> None:
    st.caption("파트너별 영업 단계 건수만 보여 줍니다. 상세 기록은 아래에서 사유를 입력해야 열리며, 해당 파트너에게 공개됩니다.")
    data = crm.pipeline()
    st.dataframe(pd.DataFrame(data).T.rename(columns=crm.stage_label).rename_axis("파트너"), width="stretch")
    st.caption("파트너 화면에는 Stage 숫자 없이 단계 이름과 기준만 보입니다. 기준은 '있었던 일'로 적어 파트너마다 해석이 갈리지 않게 했습니다.")

    st.markdown("**구매 예상** (Stage 3·4, 파트너가 입력한 예상 구매일 기준)")
    st.dataframe(pd.DataFrame(crm.forecast()).T.rename_axis("파트너"), width="stretch")
    st.caption("'예상일 지남'은 예상 구매일이 지났는데 단계가 그대로인 고객 수입니다. 많으면 예측이 부풀려져 있다는 신호입니다.")

    st.markdown("**상세 기록 열람** (분쟁 확인 등 꼭 필요할 때만)")
    rows = {r["id"]: r for r in db.list_registrations() if r["status"] not in ("rejected", "released")}
    with st.form("open_detail"):
        rid = st.selectbox("고객사", list(rows),
                           format_func=lambda i: f"{rows[i]['company_name']} · {rows[i]['partner_name']}")
        reason = st.text_input("열람 사유 * (파트너에게 공개됩니다)", placeholder="예: B테크와 선점 분쟁 확인")
        if st.form_submit_button("열람"):
            try:
                st.session_state["opened"] = (rid, crm.admin_open_detail(user["username"], rid, reason))
            except crm.CrmError as e:
                st.error(str(e))
    opened = st.session_state.get("opened")
    if opened and opened[0] in rows:
        r = rows[opened[0]]
        writers = list(dict.fromkeys(a["partner_name"] for a in opened[1]))
        notified = list(dict.fromkeys([r["partner_name"]] + writers))
        st.info(f"{r['company_name']} 상세 기록 — 이 열람은 {', '.join(notified)} 관리자 화면에 기록되었습니다.")
        if not opened[1]:
            show_timeline([])
        # 기록을 쓴 파트너별로 나눠 보여 준다. 지금 담당이 아닌 파트너의 기록 = 이관 전(또는 예전 등록) 기록
        for name in [r["partner_name"]] + [w for w in writers if w != r["partner_name"]]:
            items = [a for a in opened[1] if a["partner_name"] == name]
            if items:
                st.markdown(f"**{name} 기록** ({'현재 담당' if name == r['partner_name'] else '이전 파트너 · 새 파트너에게는 비공개'})")
                show_timeline(items)


def partners_tab(user: dict) -> None:
    if msg := st.session_state.pop("flash_partners", None):
        st.success(msg)
    show_temp_password()
    partners = db.list_partners()
    rows = []
    for p in partners:
        staff = db.list_staff(p["id"])
        rows.append({
            "파트너사": p["name"], "상태": "계약 중" if p["active"] else "계약 종료",
            "관리자": ", ".join(s["username"] for s in staff if s["partner_role"] == "manager" and s["active"]) or "-",
            "활성 계정 수": sum(s["active"] for s in staff),
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    st.caption("파트너 직원 계정은 각 파트너 관리자가 만들고 관리합니다. 총판은 파트너사와 그 관리자 계정만 발급합니다.")

    st.markdown("**새 파트너사 등록**")
    with st.form("new_partner", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        name = c1.text_input("파트너사 이름")
        username = c2.text_input("관리자 아이디")
        display = c3.text_input("관리자 이름")
        if st.form_submit_button("등록하고 관리자 계정 발급", type="primary"):
            account_action(accounts.create_partner, f"{name} 등록, 관리자 계정 {username}을 발급했습니다.",
                           user, name, username, display, temp_for=username, flash_key="flash_partners")

    by_id = {p["id"]: p for p in partners}
    pid = st.selectbox("파트너사 선택", list(by_id),
                       format_func=lambda i: f"{by_id[i]['name']} ({'계약 중' if by_id[i]['active'] else '계약 종료'})")
    p = by_id[pid]
    managers = [s["username"] for s in db.list_staff(pid) if s["partner_role"] == "manager" and s["active"]]
    c1, c2 = st.columns(2)
    with c1, st.form("add_manager", clear_on_submit=True):
        st.markdown("**관리자 계정 추가 발급**")
        username = st.text_input("아이디", key="am_user")
        display = st.text_input("이름", key="am_name")
        if st.form_submit_button("발급"):
            account_action(accounts.add_manager, f"{p['name']} 관리자 계정 {username}을 발급했습니다.",
                           user, pid, username, display, temp_for=username, flash_key="flash_partners")
    with c2, st.form("reset_manager"):
        st.markdown("**관리자 비밀번호 재설정**")
        who = st.selectbox("관리자", managers or ["(없음)"])
        if st.form_submit_button("임시 비밀번호 발급", disabled=not managers):
            account_action(accounts.reset_password, f"{who} 비밀번호를 재설정했습니다.",
                           user, who, temp_for=who, flash_key="flash_partners")

    st.markdown("**계약 종료 / 재개**")
    if p["active"]:
        with st.form("end_contract"):
            st.warning(f"{p['name']}의 모든 계정이 즉시 로그인할 수 없게 됩니다. 영업 기록은 지워지지 않습니다.")
            reason = st.text_input("사유 *")
            release = st.checkbox("보호 중·검토 대기·계약 고객 영업권도 모두 해제 (다른 파트너가 등록할 수 있게 됨)")
            sure = st.checkbox("계약 종료합니다")
            if st.form_submit_button("계약 종료"):
                if not sure:
                    st.error("'계약 종료합니다'에 체크해 주세요.")
                else:
                    account_action(accounts.end_contract, f"{p['name']} 계약을 종료했습니다.",
                                   user, pid, reason, release, flash_key="flash_partners")
    elif st.button(f"{p['name']} 계약 재개 (계정 로그인 다시 허용)"):
        account_action(accounts.resume_contract, f"{p['name']} 계약을 재개했습니다.", user, pid,
                       flash_key="flash_partners")


def ency_tab(user: dict) -> None:
    """ENCY 계정 발급 체크리스트: 수주 때 파트너가 적은 고객사 이메일로 계정을 만든 뒤 '발급 완료'를 누른다."""
    if msg := st.session_state.pop("flash_ency", None):
        st.success(msg)
    waiting, done = admin.ency_queue()
    st.caption("파트너가 수주 처리하며 입력한 고객사 이메일로 ENCY 계정을 만든 뒤 '발급 완료'를 눌러 주세요. "
               "파트너 화면에도 발급 여부가 보입니다. 파트너가 이메일을 바꾸면 다시 '발급 대기'로 돌아옵니다.")
    st.markdown(f"**발급 대기 {len(waiting)}건**")
    if not waiting:
        st.success("발급할 계정이 없습니다.")
    for r in waiting:
        with st.container(border=True):
            c1, c2 = st.columns([4, 1])
            c1.markdown(f"**{r['company_name']}** · {r['partner_name']} · "
                        f"{crm.LICENSE_TYPES.get(r['license_type'], '영구')}  \n"
                        f"ENCY 계정 **`{r['ency_account']}`** · 라이선스 시작 {r['license_start'] or '-'} · "
                        f"{crm.end_label(r)} {r['expires_on']}")
            if prev := admin.existing_account(r):
                # 다시 수주된 고객: 같은 이메일 계정이 이미 있으면 새로 만들 필요 없이 확인만 하고 완료 처리
                c1.info(f"**기존 계정 있음** — {prev['ency_issued_at'][:10]}에 같은 이메일로 발급한 계정이 있습니다 "
                        f"({prev['partner_name']} 등록, 지금 상태: {admin.account_status(prev)}). "
                        "새로 만들지 말고 계정이 살아 있는지 확인한 뒤 '발급 완료'를 눌러 주세요.")
            if c2.button("발급 완료", key=f"ency_ok_{r['id']}", type="primary"):
                try:
                    admin.mark_ency_issued(user, r["id"])
                except admin.ActionError as e:
                    st.error(str(e))
                else:
                    st.session_state["flash_ency"] = f"{r['company_name']} ENCY 계정을 발급 완료로 표시했습니다."
                    st.rerun()
    with st.expander(f"발급 완료 {len(done)}건 (누적)"):
        st.caption("지금까지 발급한 모든 계정입니다. 영구 라이선스 계정은 유지보수가 끝나도 고객이 계속 쓰므로 목록에 남습니다.")
        if done:
            st.dataframe(pd.DataFrame([{
                "회사명": r["company_name"], "파트너": r["partner_name"], "ENCY 계정": r["ency_account"],
                "라이선스": crm.LICENSE_TYPES.get(r["license_type"], "영구"), "상태": admin.account_status(r),
                "발급일": r["ency_issued_at"][:10], "종료일": r["expires_on"],
            } for r in done]), hide_index=True, width="stretch")
            with st.form("ency_undo"):
                st.markdown("**발급 표시 취소** (잘못 눌렀을 때)")
                by_id = {r["id"]: r for r in done}
                rid = st.selectbox("고객사", list(by_id),
                                   format_func=lambda i: f"{by_id[i]['company_name']} · {by_id[i]['ency_account']}")
                reason = st.text_input("사유 *")
                if st.form_submit_button("취소"):
                    try:
                        admin.undo_ency_issued(user, rid, reason)
                    except admin.ActionError as e:
                        st.error(str(e))
                    else:
                        st.session_state["flash_ency"] = "발급 표시를 취소했습니다."
                        st.rerun()


def overview_tab() -> None:
    rows = db.list_registrations()
    c = st.columns(7)
    for col, (label, s) in zip(c, [("보호 중", "active"), ("검토 대기", "pending"), ("계약 고객", "won"),
                                   ("계약 만료·유예", "lapsed"), ("만료", "expired"), ("반려", "rejected"),
                                   ("해제", "released")]):
        col.metric(label, sum(r["status"] == s for r in rows))
    st.dataframe(reg_table(rows, ext=admin.extension_counts()), hide_index=True, width="stretch")


def expiring_tab() -> None:
    st.caption(f"보호 중은 {db.EXPIRING_SOON_DAYS}일 안, 계약 고객은 유지보수 종료 {db.CONTRACT_ALERT_DAYS}일 안에 끝나는 등록입니다.")
    soon = db.expiring_soon()
    if soon:
        st.dataframe(reg_table(soon, ext=admin.extension_counts()), hide_index=True, width="stretch")
    else:
        st.info("곧 끝나는 등록이 없습니다.")


def events_tab() -> None:
    st.caption("분쟁이 생기면 '누가 언제 등록했는지'를 이 기록으로 확인합니다.")
    ev = db.list_events()
    st.dataframe(pd.DataFrame([{
        "시각": e["ts"].replace("T", " "), "구분": EVENT_LABEL.get(e["kind"], e["kind"]),
        "파트너": e["partner"], "내용": e["detail"],
    } for e in ev]), hide_index=True, width="stretch")


def notice_write_tab() -> None:
    with st.form("notice", clear_on_submit=True):
        title = st.text_input("제목")
        body = st.text_area("내용")
        if st.form_submit_button("공지 등록") and title and body:
            db.add_notice(title, body)
            db.log("notice", ADMIN, detail=title)
            st.toast("공지를 등록했습니다.")
    notices_tab()


# ---- 실적 통계 ----------------------------------------------------------

SERIES = ["#2a78d6", "#eb6834"]  # 그래프 색: 1번 파랑(주 계열), 2번 주황 (dataviz 기본 팔레트 순서)
VAT_LABEL = "공급가액, VAT 10% 별도"


def _bar(df: pd.DataFrame, x: str, y: str, x_title: str, y_title: str, tooltip: list, horizontal: bool = False,
         sort=None):
    """한 색 막대 그래프. horizontal이면 가로 막대(이름이 긴 비교용)."""
    import altair as alt
    enc_x = alt.X(x, title=x_title, sort=sort, axis=alt.Axis(labelAngle=0))
    enc_y = alt.Y(y, title=y_title)
    if horizontal:
        # 한글 이름표는 그래프 도구가 실제보다 좁게 재서 막대와 겹친다 → 가장 긴 이름에 맞춰 왼쪽 자리를 미리 잡는다.
        # 세로 축 제목은 빼고(위 소제목이 대신함), 글자당 15px로 넉넉하게 잡는다.
        width = max(len(str(v)) for v in df[x.split(":")[0]]) * 15 + 16
        enc_x = alt.X(y, title=y_title)
        # labelOverlap=False: 이름표가 겹친다고 계산되면 하나씩 건너뛰어 숨기는 기본 동작을 끈다(이름이 빠지면 안 됨)
        enc_y = alt.Y(x, title=None, sort=sort,
                      axis=alt.Axis(minExtent=width, maxExtent=width, labelLimit=width, labelPadding=8,
                                    labelOverlap=False))
    mark = {"size": 18} if horizontal else {}
    return (alt.Chart(df).mark_bar(color=SERIES[0], cornerRadiusEnd=4, **mark)
            .encode(x=enc_x, y=enc_y, tooltip=tooltip))


def stats_period() -> tuple[date, date, str]:
    """기간 고르기 + 단위. (시작일, 종료일, 단위)"""
    today = date.today()
    c1, c2 = st.columns([3, 2])
    # '지난 3개월' = 이번 달을 포함한 3개월 (예: 10월 22일이면 8월 1일 ~ 10월 22일). 월 단위 추이의 마지막 3칸과 같다.
    presets = {"이번 달": today.replace(day=1), "지난 3개월": stats.periods("month", 3, today)[0],
               "올해": today.replace(month=1, day=1), "직접 선택": None}
    pick = c1.segmented_control("기간", list(presets), default="지난 3개월", key="stats_range") or "지난 3개월"
    start, end = (presets[pick] or today - timedelta(days=30)), today
    if pick == "직접 선택":
        picked = c1.date_input("시작일 ~ 종료일", value=(today - timedelta(days=30), today), max_value=today,
                               format="YYYY-MM-DD", key="stats_dates")
        if isinstance(picked, (tuple, list)) and len(picked) == 2:
            start, end = picked
    unit = c2.segmented_control("추이 단위", list(stats.UNITS), format_func=lambda u: stats.UNITS[u][0],
                                default="month", key="stats_unit") or "month"
    return start, end, unit


def stats_tab(user: dict) -> None:
    import altair as alt
    who = "파트너" if user["role"] == "admin" else "담당자"
    st.caption(("모든 파트너의 실적입니다." if user["role"] == "admin" else "우리 회사 실적입니다(다른 파트너는 보이지 않습니다).")
               + f" 금액은 모두 **{VAT_LABEL}**입니다.")
    start, end, unit = stats_period()
    st.caption(f"요약·비교·퍼널·문의 통계: **{start} ~ {end}** · 추이: 최근 {stats.UNITS[unit][1]}개 {stats.UNITS[unit][0]} 단위")

    s = stats.summary(user, start, end)
    m = st.columns(4)
    m[0].metric("신규 등록", f"{s['registered']}건")
    m[1].metric("수주", f"{s['won']}건")
    m[2].metric("수주 금액", stats.money(s["amount"]), help=VAT_LABEL)
    m[2].caption(f"{VAT_LABEL}" + (f" · 금액 미입력 {s['no_amount']}건 제외" if s["no_amount"] else ""))
    m[3].metric("전환율 (수주 ÷ 등록)", f"{s['rate']:.0%}" if s["rate"] is not None else "-")

    # 1) 추이 — 금액과 건수는 단위가 달라 그래프를 나눈다(축 두 개를 겹치지 않음)
    st.subheader("기간별 추이")
    t = pd.DataFrame(stats.trend(user, unit))
    t["금액(만 원)"] = t["amount"] / 10_000
    t["금액"] = t["amount"].map(stats.money)
    order = list(t["period"])
    st.altair_chart(_bar(t, "period:N", "금액(만 원):Q", stats.UNITS[unit][0], f"수주 금액 (만 원, {VAT_LABEL})",
                         [alt.Tooltip("period:N", title="기간"), alt.Tooltip("금액:N", title=f"수주 금액 ({VAT_LABEL})"),
                          alt.Tooltip("won:Q", title="수주 건수")], sort=order).properties(height=260),
                    width="stretch")
    counts = t.melt(id_vars=["period"], value_vars=["registered", "won"], var_name="series", value_name="건수")
    counts["구분"] = counts["series"].map({"registered": "신규 등록", "won": "수주"})
    line = (alt.Chart(counts).mark_line(strokeWidth=2, point=alt.OverlayMarkDef(size=64))
            .encode(x=alt.X("period:N", title=stats.UNITS[unit][0], sort=order, axis=alt.Axis(labelAngle=0)),
                    y=alt.Y("건수:Q", title="건수", axis=alt.Axis(tickMinStep=1)),
                    color=alt.Color("구분:N", scale=alt.Scale(domain=["신규 등록", "수주"], range=SERIES),
                                    legend=alt.Legend(orient="top", title=None)),
                    tooltip=[alt.Tooltip("period:N", title="기간"), "구분:N", "건수:Q"]))
    st.altair_chart(line.properties(height=220), width="stretch")
    with st.expander("표로 보기"):
        st.dataframe(t.rename(columns={"period": "기간", "registered": "신규 등록", "won": "수주 건수"})
                     [["기간", "신규 등록", "수주 건수", "금액"]].rename(columns={"금액": f"수주 금액 ({VAT_LABEL})"}),
                     hide_index=True, width="stretch")

    # 2) 비교
    st.subheader(f"{who}별 비교")
    c = pd.DataFrame(stats.compare(user, start, end))
    if c.empty:
        st.info("이 기간에 실적이 없습니다.")
    else:
        c["금액(만 원)"] = c["amount"] / 10_000
        c["금액"] = c["amount"].map(stats.money)
        c["전환율"] = c["rate"].map(lambda r: f"{r:.0%}" if r == r and r is not None else "-")
        st.altair_chart(_bar(c, "name:N", "금액(만 원):Q", who, f"수주 금액 (만 원, {VAT_LABEL})",
                             [alt.Tooltip("name:N", title=who), alt.Tooltip("금액:N", title="수주 금액"),
                              alt.Tooltip("won:Q", title="수주 건수"), alt.Tooltip("registered:Q", title="신규 등록"),
                              alt.Tooltip("전환율:N")], horizontal=True, sort=list(c["name"]))
                        .properties(height=48 * len(c) + 40), width="stretch")
        st.dataframe(c.rename(columns={"name": who, "registered": "신규 등록", "won": "수주 건수",
                                       "금액": f"수주 금액 ({VAT_LABEL})"})
                     [[who, "신규 등록", "수주 건수", f"수주 금액 ({VAT_LABEL})", "전환율"]],
                     hide_index=True, width="stretch")

    # 3) 퍼널
    st.subheader("단계별 퍼널")
    f = stats.funnel(user, start, end)
    fd = pd.DataFrame(f["stages"])
    first = fd["count"].iloc[0] if len(fd) else 0
    if not first:
        st.info("이 기간에 등록된 고객이 없습니다.")
    else:
        n = list(fd["count"])
        fd["다음 단계로"] = [f"{n[i + 1] / n[i]:.0%}" if i + 1 < len(n) and n[i] else "-" for i in range(len(n))]
        fd["처음 대비"] = (fd["count"] / first).map(lambda r: f"{r:.0%}")
        bars = _bar(fd, "stage:N", "count:Q", "단계", "도달한 고객 수",
                    [alt.Tooltip("stage:N", title="단계"), alt.Tooltip("count:Q", title="도달 고객"),
                     alt.Tooltip("처음 대비:N"), alt.Tooltip("다음 단계로:N")],
                    horizontal=True, sort=list(fd["stage"]))
        labels = (alt.Chart(fd).mark_text(align="left", dx=6, color="gray")
                  .encode(x="count:Q", y=alt.Y("stage:N", sort=list(fd["stage"])),
                          text=alt.Text("처음 대비:N")))
        st.altair_chart((bars + labels).properties(height=48 * len(fd) + 40), width="stretch")
        st.caption(f"{start} ~ {end}에 등록된 고객 {first}곳이 가장 멀리 간 단계 기준입니다. "
                   f"막대 옆 숫자는 처음 대비 남은 비율입니다. 보류·실패로 멈춘 고객: {f['dropped']}곳")

    # 4) 기술 문의
    st.subheader("기술 문의")
    ts_ = stats.ticket_stats(user, start, end)
    k = st.columns(3)
    k[0].metric("접수된 문의", f"{ts_['total']}건")
    avg = ts_["avg_first_reply_hours"]
    k[1].metric("평균 첫 답변 시간", "-" if avg is None else (f"{avg:.1f}시간" if avg < 48 else f"{avg / 24:.1f}일"))
    k[2].metric("지금 열린 문의", f"{ts_['open']}건", help="기간과 상관없이 종료되지 않은 문의")
    if ts_["total"]:
        td = pd.DataFrame(ts_["by_category"])
        st.altair_chart(_bar(td, "category:N", "count:Q", "분류", "문의 수",
                             [alt.Tooltip("category:N", title="분류"), alt.Tooltip("count:Q", title="문의 수")],
                             sort=tickets.CATEGORIES).properties(height=220), width="stretch")


# ---- 기술 문의(티켓) ----------------------------------------------------

IMAGE_UPLOAD_TYPES = ["png", "jpg", "jpeg", "webp"]


def person(username: str) -> str:
    """계정 이름 + 직함 + 연락처 한 줄 (연락처는 프로필에 채운 것만)."""
    if not username:
        return "미배정"
    acc = db.get_account(username)
    if acc is None:
        return username
    name = " ".join(x for x in (acc["display_name"] or username, acc["title"]) if x)
    contact = " · ".join(x for x in (acc["phone"], acc["email"]) if x)
    return f"{name} ({contact})" if contact else name


def run_ticket(fn, done: str, *args) -> None:
    try:
        fn(*args)
    except tickets.TicketError as e:
        st.error(str(e))
    else:
        st.session_state["flash_ticket"] = done
        st.rerun()


def new_ticket_form(user: dict) -> None:
    regs = crm.visible_registrations(user)
    options = [None] + [r["id"] for r in regs]
    names = {r["id"]: r["company_name"] for r in regs}
    with st.form("new_ticket", clear_on_submit=True):
        c1, c2 = st.columns([2, 1])
        rid = c1.selectbox("고객사", options, format_func=lambda i: names[i] if i else "고객사 미지정",
                           help="내 고객 목록에 있는 고객사만 고를 수 있습니다.")
        category = c2.selectbox("분류", tickets.CATEGORIES)
        title = st.text_input("제목", max_chars=tickets.TITLE_MAX)
        body = st.text_area("내용", height=150, max_chars=tickets.BODY_MAX,
                            placeholder="어떤 작업 중에 무엇이 안 되는지, 오류 문구, 사용 중인 버전을 적어 주세요.")
        files = st.file_uploader(f"화면 캡처 (최대 {tickets.MAX_IMAGES}장)", type=IMAGE_UPLOAD_TYPES,
                                 accept_multiple_files=True)
        if st.form_submit_button("문의 올리기", type="primary"):
            run_ticket(tickets.create, "문의를 올렸습니다. 엔지니어가 맡으면 '처리 중'으로 바뀝니다.",
                       user, rid, category, title, body, [f.getvalue() for f in files or []])


def message_form(key: str, label: str, button: str, on_submit) -> None:
    """답장·추가 질문·다시 문의 공통 입력칸. on_submit(본문, 이미지 목록)."""
    with st.form(key, clear_on_submit=True):
        body = st.text_area(label, height=120, max_chars=tickets.BODY_MAX)
        files = st.file_uploader("이미지 첨부", type=IMAGE_UPLOAD_TYPES, accept_multiple_files=True,
                                 key=f"{key}_files")
        if st.form_submit_button(button, type="primary"):
            on_submit(body, [f.getvalue() for f in files or []])


def ticket_detail(user: dict, tid: int) -> None:
    t = tickets.get(user, tid)
    with st.container(border=True):
        st.subheader(f"#{t['id']} {t['title']}")
        st.markdown(f"**{tickets.STATUS_LABEL[t['status']]}** · {t['category']} · "
                    f"고객사 {t['customer_name'] or '미지정'} · {t['partner_name']}")
        c1, c2 = st.columns(2)
        c1.caption(f"문의한 사람: {person(t['author'])}")
        c2.caption(f"담당 엔지니어: {person(t['assignee'])}")
        if user["role"] == "admin" and t["registration_id"]:
            reg = db.get_registration(t["registration_id"])
            if reg:  # 엔지니어에겐 라이선스 상태만. 영업 기록은 보여 주지 않는다
                kind = crm.LICENSE_TYPES.get(reg["license_type"], "") if reg["status"] in ("won", "lapsed") else ""
                st.caption(f"고객 라이선스: {kind or '미구매(영업 중)'} · {crm.relation(reg)}")

    for m in tickets.messages(user, tid):
        engineer = m["side"] == "engineer"
        with st.chat_message("assistant" if engineer else "user", avatar="🛠️" if engineer else "🏢"):
            st.caption(f"{person(m['author']) if engineer else m['author']} · {m['ts'][:16].replace('T', ' ')}")
            st.write(m["body"])
            for f in m["files"]:
                if f["file_path"].exists():
                    st.image(str(f["file_path"]), width=360)

    closed = t["status"] == "closed"
    if tickets.is_engineer(user):
        if not t["assignee"] and not closed:
            if st.button("내가 맡기", type="primary", key=f"claim_{tid}"):
                run_ticket(tickets.claim, "문의를 맡았습니다. 답변을 남겨 주세요.", user, tid)
        elif t["assignee"] == user["username"] and not closed:
            message_form(f"reply_{tid}", "답변", "답변 보내기",
                         lambda body, imgs: run_ticket(tickets.reply, "답변을 보냈습니다.", user, tid, body, imgs))
        elif closed:
            st.caption("종료된 문의입니다. 파트너가 다시 문의하면 열립니다.")
    elif tickets.is_admin_manager(user):
        engineers = db.list_engineers()
        if not closed and engineers:
            c1, c2 = st.columns([3, 1], vertical_alignment="bottom")
            who = c1.selectbox("담당 엔지니어 지정", [e["username"] for e in engineers],
                               format_func=person, key=f"assign_{tid}")
            if c2.button("지정", key=f"assign_btn_{tid}"):
                run_ticket(tickets.assign, f"담당을 {person(who)}(으)로 정했습니다.", user, tid, who)
    elif closed:
        message_form(f"reopen_{tid}", "해결이 안 됐나요? 무엇이 안 되는지 적어 주세요.", "다시 문의",
                     lambda body, imgs: run_ticket(tickets.reopen, "문의를 다시 열었습니다.", user, tid, body, imgs))
    else:
        if t["status"] == "answered" and st.button("✅ 해결됨", type="primary", key=f"resolve_{tid}"):
            run_ticket(tickets.resolve, "문의를 종료했습니다.", user, tid)
        if t["status"] == "answered":
            st.caption(f"해결됐으면 [해결됨]을 눌러 주세요. 답변 뒤 {tickets.AUTO_CLOSE_DAYS}일 동안 반응이 없으면 자동으로 종료됩니다.")
        message_form(f"reply_{tid}", "추가 질문", "보내기",
                     lambda body, imgs: run_ticket(tickets.reply, "추가 질문을 보냈습니다.", user, tid, body, imgs))


def tickets_tab(user: dict) -> None:
    if msg := st.session_state.pop("flash_ticket", None):
        st.success(msg)
    if user["role"] == "partner":
        st.caption("고객사의 기술 문의를 올리면 총판 엔지니어가 답합니다."
                   + (" 우리 회사 문의를 모두 볼 수 있습니다." if crm.is_manager(user) else " 내가 올린 문의만 보입니다."))
        with st.expander("➕ 새 문의 올리기"):
            new_ticket_form(user)
    query = st.text_input("🔍 문의 검색", key="ticket_query",
                          placeholder="제목·대화 내용·고객사·번호로 찾기 (예: 라이선스 설치, #12)")
    if query.strip():
        # 검색할 때는 보기·종료 여부와 상관없이, 내가 볼 수 있는 문의 전체(종료 포함)에서 찾는다
        rows = tickets.search(tickets.visible(user, "all"), query)
        st.caption(f"종료된 문의를 포함해 {len(rows)}건을 찾았습니다.")
    else:
        if user["role"] == "partner":
            rows = tickets.visible(user)
        else:
            scopes = {"unassigned": "미배정", "all": "전체"}
            if tickets.is_engineer(user):
                scopes = {"unassigned": "미배정", "mine": "내 담당", "all": "전체"}
            st.session_state.setdefault("ticket_scope", "unassigned")
            scope = st.segmented_control("보기", list(scopes), format_func=scopes.get,
                                         key="ticket_scope") or "unassigned"
            rows = tickets.visible(user, scope)
        if not st.toggle("종료된 문의도 보기", key="ticket_show_closed"):
            rows = [t for t in rows if t["status"] != "closed"]
    if not rows:
        st.info("해당하는 문의가 없습니다.")
        return
    st.dataframe(pd.DataFrame([{
        "번호": t["id"], "상태": tickets.STATUS_LABEL[t["status"]], "분류": t["category"],
        "고객사": t["customer_name"] or "미지정", "제목": t["title"], "파트너": t["partner_name"],
        "담당": t["assignee"] or "-", "최근 갱신": t["updated_at"][:16].replace("T", " "),
    } for t in rows]), hide_index=True, width="stretch")
    # 표 순서는 갱신 때마다 바뀌므로, 여는 문의는 번호로 고른다 (답장 후에도 같은 문의가 열려 있게)
    by_id = {t["id"]: t for t in rows}
    tid = st.selectbox("열어 볼 문의", [None] + list(by_id), key="ticket_open", placeholder="문의를 고르세요",
                       format_func=lambda i: "문의를 고르세요" if i is None else
                       f"#{i} · {tickets.STATUS_LABEL[by_id[i]['status']]} · {by_id[i]['title']}")
    if tid is not None:
        ticket_detail(user, tid)


# ---- 왼쪽 메뉴 ----------------------------------------------------------

def page(fn, title: str, url: str, icon: str, *args, count: int = 0) -> st.Page:
    """메뉴 한 줄. count가 있으면 메뉴 이름 뒤에 (n)을 붙인다. 주소(url)는 건수와 상관없이 고정."""
    def run() -> None:
        st.title(title)
        fn(*args)
    return st.Page(run, title=f"{title} ({count})" if count else title, url_path=url)


def build_pages(user: dict) -> dict[str, list[st.Page]]:
    """역할별 메뉴. {묶음 이름: [페이지, ...]} — 위에서부터 순서대로 왼쪽 창에 나온다."""
    if user["role"] == "admin":
        if user["partner_role"] == "engineer":
            return {
                "지원": [page(tickets_tab, "기술 문의", "tickets", "💬", user, count=tickets.counts(user)),
                         page(notices_tab, "공지", "notices", "📢")],
                "계정": [page(account_tab, "내 계정", "account", "👤", user)],
            }
        return {
            "할 일": [page(review_tab, "검토 대기", "review", "📝", user,
                         count=len(db.list_registrations(statuses=("pending",)))),
                      page(ency_tab, "ENCY 계정 발급", "ency", "🔑", user, count=len(admin.ency_queue()[0])),
                      page(expiring_tab, "만료 임박", "expiring", "⏰", count=len(db.expiring_soon()))],
            "영업": [page(pipeline_tab, "영업 현황", "pipeline", "📈", user),
                     page(stats_tab, "실적 통계", "stats", "📊", user),
                     page(overview_tab, "전체 현황", "overview", "🗂️"),
                     page(manage_tab, "등록 관리", "manage", "🛠️", user)],
            "지원": [page(tickets_tab, "기술 문의", "tickets", "💬", user, count=tickets.counts(user)),
                     page(notice_write_tab, "공지 작성", "notices", "📢")],
            "관리": [page(partners_tab, "파트너·계정", "partners", "🏢", user),
                     page(events_tab, "이력 로그", "events", "📜")],
            "계정": [page(account_tab, "내 계정", "account", "👤", user)],
        }
    me = {"id": user["partner_id"], "name": user["partner_name"], "user": user}
    pages = {
        "홈": [page(notices_tab, "공지", "notices", "📢")],
        "영업": [page(register_tab, "고객사 등록", "register", "➕", me),
                 page(my_tab, "내 영업권", "mine", "🛡️", me),
                 page(crm_tab, "영업 관리", "crm", "📇", user),
                 page(lookup_tab, "선점 조회", "lookup", "🔍", me)],
        "지원": [page(tickets_tab, "기술 문의", "tickets", "💬", user, count=tickets.counts(user))],
    }
    if crm.is_manager(user):
        pages["팀"] = [page(team_tab, "팀 관리", "team", "👥", user),
                       page(access_log_tab, "총판 열람 기록", "access-log", "👁️", user),
                       page(stats_tab, "실적 통계", "stats", "📊", user)]
    pages["계정"] = [page(account_tab, "내 계정", "account", "👤", user)]
    return pages


user = st.session_state.get("user")
if user is not None:
    user = auth.refresh(user)  # 로그인 중 퇴사·계약 종료·역할 변경을 바로 반영
    if user is None:
        st.session_state.clear()
        st.warning("사용할 수 없는 계정입니다. 관리자에게 문의해 주세요.")
    else:
        st.session_state["user"] = user
if msg := st.session_state.pop("flash_pw", None):
    st.success(msg)
if user is None:
    st.navigation([st.Page(login_page, title="로그인", url_path="login")], position="hidden").run()
elif user["must_change_pw"]:
    st.navigation([st.Page(lambda: must_change_page(user), title="비밀번호 변경", url_path="password")],
                  position="hidden").run()
else:
    # 메뉴를 옮기면 화면에 없는 입력칸 값은 Streamlit이 지운다. 고객사 등록 칸은 다시 넣어 유지한다.
    # 기술 문의의 보기·종료 표시 선택도 검색하는 동안 사라지지 않게 같이 유지한다.
    for k in [f"f_{f}" for f in FORM_FIELDS] + ["ticket_scope", "ticket_show_closed"]:
        if k in st.session_state:
            st.session_state[k] = st.session_state[k]
    main_background()
    pages = build_pages(user)
    nav = st.navigation(pages)
    sidebar(user, pages["계정"][0])
    if user["role"] != "admin":
        # 파트너 화면에서는 표의 내려받기 버튼을 숨긴다 (화면 차원의 제한 — 실제 운영에선 서버에서 막아야 함)
        st.markdown("<style>[data-testid='stElementToolbar']{display:none}</style>", unsafe_allow_html=True)
    nav.run()
