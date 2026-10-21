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

AI_DAILY_LIMIT = int(os.environ.get("AI_DAILY_LIMIT", "30"))
ADMIN = "총판 관리자"
STATUS_LABEL = {"active": "보호 중", "pending": "검토 대기", "won": "계약 고객", "expired": "만료", "rejected": "반려",
                "released": "해제"}
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
            "만료일": r["expires_on"],  # 계약 고객은 계약 종료일
            # 숫자·빈칸이 섞이면 표에 90.0 / None처럼 보여서 글자로 표시
            "남은 일수": f"D-{days_left(r['expires_on'])}" if r["status"] in ("active", "won") else "-",
            "상태": STATUS_LABEL[r["status"]],
            "고객 담당자": r["contact_name"],
            "증빙": evidence.summary(r, kinds.get(r["id"], set())),
        }
        if show_partner:
            item = {"파트너": r["partner_name"], **item}
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
    """등록 증빙 이미지를 보여 준다. add_as(계정 이름)를 주면 이미지를 추가로 올릴 수 있다."""
    items = evidence.list_for(registration_id)
    if not items:
        st.caption("첨부된 증빙 이미지가 없습니다.")
    cols = st.columns(3)
    for i, e in enumerate(items):
        with cols[i % 3]:
            if e["file_path"].exists():
                st.image(str(e["file_path"]), width="stretch")
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
LOGIN_BG = Path(__file__).parent / "assets" / "login_bg.png"


def login_background() -> None:
    """로그인 화면 배경 이미지. 이미지 파일이 없으면 그냥 넘어간다."""
    if not LOGIN_BG.exists():
        return
    img = base64.b64encode(LOGIN_BG.read_bytes()).decode()
    st.markdown(f"""
    
<style>
[data-testid="stApp"] {{
    background: url("data:image/png;base64,{img}") center / cover no-repeat fixed;
}}
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
                f"- {partner or ADMIN} · {name} ({ROLE_LABEL[prole] if partner else '총판'}): `{username}` / `{password}`"
                for username, password, _, partner, name, prole in seed.DEMO_ACCOUNTS
            ))



ROLE_LABEL = {"manager": "파트너 관리자", "rep": "영업 담당자"}


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


def sidebar(user: dict) -> None:
    with st.sidebar:
        st.header("영업권 관리 v2")
        if user["role"] == "admin":
            st.write(f"**{ADMIN}** ({user['username']})")
        else:
            st.write(f"**{user['partner_name']}** · {user['display_name'] or user['username']}  \n"
                     f"{ROLE_LABEL[user['partner_role']]}")
        if st.button("로그아웃"):
            st.session_state.clear()
            st.rerun()
        with st.expander("비밀번호 변경"):
            password_form(user, "side_pw")
        st.divider()
        if ai_extract.available():
            st.success(f"AI 사용 가능 · 오늘 남은 호출 {ai_budget_left()}회")
        else:
            st.info("AI 꺼짐 · 수동 입력 모드")
        st.divider()
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
    st.subheader("고객사 등록")
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
    found = matching.find_conflicts(name, biz, db.holding_registrations())
    result["exact"], result["similar"] = found["exact"], found["similar"]
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

    # 비슷한 이름의 다른 파트너 등록: 파트너에게는 존재 여부만 알리고, 상세(회사명·파트너·AI 의견)는 관리자 검토 사유에만 남긴다
    similar = [(r, s) for r, s in check["similar"] if r["partner_id"] != partner["id"]]
    flagged, notes = [], []
    for row, score in similar:
        ai = check["ai"].get(row["id"])
        if ai is None or ai["likely_same"]:
            flagged.append(row)
            note = f"{row['company_name']}({row['partner_name']}, 유사도 {score})"
            if ai:
                note += f" — AI: 같은 회사 가능성 높음, {ai['reason']}"
            notes.append(note)

    if flagged:
        st.warning("이름이 비슷한 고객사가 이미 다른 파트너에게 선점되어 있습니다. "
                   "같은 회사일 수 있어 **총판 관리자 검토 후** 보호가 시작됩니다. 선점 시각은 지금으로 기록됩니다.")
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
        st.warning(f"계약이 {db.CONTRACT_ALERT_DAYS}일 안에 끝나는 계약 고객 {len(renew)}곳 — 갱신하지 않으면 종료일 다음 날 "
                   "다른 파트너가 등록할 수 있습니다: "
                   + ", ".join(f"{r['company_name']}(D-{days_left(r['expires_on'])})" for r in renew))
    st.caption("계약 고객은 90일이 아니라 계약 종료일까지 영업권이 유지됩니다. '영업 관리'에서 단계를 '수주'로 바꾸면 계약 고객이 됩니다.")
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
    editable = {r["id"]: r for r in rows if r["status"] in ("active", "pending", "won")}
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
            note = st.text_area("메모", value=r["note"], height=80)
            if not st.form_submit_button("저장", type="primary"):
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
        new = {"biz_no": biz, "contact_name": contact.strip(), "contact_phone": phone.strip(), "note": note.strip()}
        changed = {k: v for k, v in new.items() if v != r[k]}
        if not changed:
            st.info("바뀐 내용이 없습니다.")
            return
        db.update_registration(rid, changed)
        labels = {"biz_no": "사업자번호", "contact_name": "담당자", "contact_phone": "연락처", "note": "메모"}
        db.log("edit", partner["name"], rid,
               f"{r['company_name']} 수정: " + ", ".join(
                   f"{labels[k]} '{r[k]}'→'{v}'" if k == "biz_no" else labels[k] for k, v in changed.items()))
        st.session_state["flash_my"] = "수정했습니다."
        st.rerun()


def lookup_tab(partner: dict) -> None:
    st.write("회사명이나 사업자번호로 이미 선점된 고객사인지 확인합니다. "
             "다른 파트너의 고객은 선점 여부와 보호 만료일만 보입니다.")
    q = st.text_input("검색어")
    if not q:
        return
    found = matching.find_conflicts(q, q if matching.normalize_biz_no(q) else "", db.holding_registrations())
    if not found["exact"] and not found["similar"]:
        st.success("선점된 고객사가 없습니다. 바로 등록할 수 있습니다.")
        return
    for row, why in found["exact"]:
        if row["partner_id"] == partner["id"]:
            st.info(f"우리 회사가 보호 중인 고객사입니다: {row['company_name']} ({STATUS_LABEL[row['status']]}, 만료 {row['expires_on']})")
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

    rid = st.selectbox(
        "고객사", list(rows),
        format_func=lambda i: f"{rows[i]['company_name']} · {rows[i]['stage']} · {STATUS_LABEL[rows[i]['status']]}"
                              + (f" · 담당 {owner_name(rows[i]['owner'])}" if crm.is_manager(user) else ""),
    )
    r = rows[rid]
    until = "계약 종료" if r["status"] == "won" else "만료"
    expected = f" · 예상 구매일 {r['expected_close']}" if r["stage"] in crm.FORECAST_STAGES and r["expected_close"] else ""
    st.markdown(f"**{r['company_name']}** · 고객 담당자 {r['contact_name'] or '-'} · "
                f"우리 담당 {owner_name(r['owner'])} · 영업권 {STATUS_LABEL[r['status']]}({until} {r['expires_on']})  \n"
                f"영업 단계 **{r['stage']}**{expected}")
    if crm.is_overdue(r):
        st.warning("예상 구매일이 지났습니다. 단계나 예상 구매일을 다시 확인해 주세요.")

    with st.expander("영업 단계 기준 보기"):
        st.markdown("\n".join(f"- **{s}** — {crm.STAGE_GUIDE[s]}" for s in crm.STAGES))

    c1, c2 = st.columns(2)
    with c1, st.form(f"stage_{rid}"):
        stage = st.selectbox("영업 단계", crm.STAGES, index=crm.STAGES.index(r["stage"]),
                             format_func=lambda s: f"{s} — {crm.STAGE_GUIDE[s]}")
        default_exp = (date.fromisoformat(r["expected_close"]) if r["expected_close"]
                       and r["expected_close"] >= date.today().isoformat() else date.today() + timedelta(days=30))
        expected_close = st.date_input("예상 구매일 ('구매 검토'·'구매 임박'일 때)", value=default_exp,
                                       min_value=date.today())
        contract_end = None
        if r["status"] != "won":
            contract_end = st.date_input("계약 종료일 ('수주'로 바꿀 때)", value=date.today() + timedelta(days=365),
                                         min_value=date.today() + timedelta(days=1),
                                         max_value=date.today() + timedelta(days=db.CONTRACT_MAX_DAYS))
            st.caption("수주하면 90일 보호 대신 계약 종료일까지 영업권이 유지됩니다.")
        if st.form_submit_button("단계 저장"):
            try:
                crm.set_stage(user, rid, stage, contract_end=contract_end.isoformat() if contract_end else None,
                              expected_close=expected_close.isoformat())
            except crm.CrmError as e:
                st.error(str(e))
            else:
                done = f"단계를 '{stage}'(으)로 저장했습니다."
                if stage in crm.FORECAST_STAGES:
                    done += f" 예상 구매일은 {expected_close}입니다."
                if stage == "수주" and r["status"] != "won":
                    done += f" 계약 종료일 {contract_end}까지 영업권이 유지됩니다."
                st.session_state["flash_crm"] = done
                st.rerun()
    if r["status"] == "won":
        with c1, st.form(f"renew_{rid}"):
            exp = date.fromisoformat(r["expires_on"])
            new_end = st.date_input("계약 갱신 — 새 계약 종료일", value=exp + timedelta(days=365),
                                    min_value=exp + timedelta(days=1),
                                    max_value=date.today() + timedelta(days=db.CONTRACT_MAX_DAYS))
            if st.form_submit_button("계약 갱신"):
                try:
                    end = crm.renew_contract(user, rid, new_end.isoformat())
                except crm.CrmError as e:
                    st.error(str(e))
                else:
                    st.session_state["flash_crm"] = f"계약을 갱신했습니다. 계약 종료일 {end}까지 영업권이 유지됩니다."
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
        "아이디": s["username"], "이름": s["display_name"], "역할": ROLE_LABEL[s["partner_role"]],
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

def review_tab() -> None:
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
                db.set_status(r["id"], "active")
                db.log("approve", r["partner_name"], r["id"], f"{r['company_name']} 승인(별개 회사로 판단)")
                st.rerun()
            if c2.button("반려", key=f"no{r['id']}"):
                db.set_status(r["id"], "rejected")
                db.log("reject", r["partner_name"], r["id"], f"{r['company_name']} 반려(기존 등록과 같은 회사)")
                st.rerun()


EVENT_LABEL = {"register": "등록", "pending": "검토 요청", "blocked": "등록 차단", "approve": "승인",
               "reject": "반려", "expire": "만료", "ai_call": "AI 호출", "notice": "공지", "edit": "정보 수정",
               "release": "등록 해제", "extend": "기간 연장", "transfer": "이관", "rename": "회사명 수정",
               "restore": "반려 취소", "handover": "퇴사 인계", "account": "계정 관리",
               "won": "수주(계약 고객)", "renew": "계약 갱신", "evidence": "증빙 첨부"}


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


def manage_tab() -> None:
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
    until = "계약 종료" if r["status"] == "won" else "만료"
    st.markdown(
        f"**{r['company_name']}** · {r['partner_name']} · {STATUS_LABEL[r['status']]}  \n"
        f"사업자번호 {matching.format_biz_no(r['biz_no']) or '미기재'} · 등록 {r['registered_at'][:10]} · "
        f"{until} {r['expires_on']}  \n"
        f"기간 연장: 이 등록 **{own_ext}회** · 이 고객사 누적 **{total_ext}회** (모든 파트너·재등록 포함)"
    )
    with st.expander(f"📎 등록 증빙 — {evidence.summary(r, db.evidence_kinds().get(rid, set()))}", expanded=True):
        show_evidence(rid)
    status = r["status"]
    if status in ("expired", "released"):
        st.info("만료·해제된 등록은 조치 대상이 아닙니다. 다시 보호하려면 파트너가 새로 등록해야 합니다.")
        return

    actions = {"active": ["기간 연장", "이관", "회사명 수정", "등록 해제"],
               "pending": ["회사명 수정", "등록 해제"],
               "won": ["이관", "회사명 수정", "등록 해제"],  # 계약 고객은 계약 종료일로 보호 → 연장 대상 아님
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
            st.caption("보호 기간(계약 고객은 계약 종료일)은 그대로 유지합니다. 이전 파트너가 남긴 영업 기록은 "
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
        run_action(admin.extend, f"{days}일 연장했습니다.", rid, days, reason)
    elif action == "이관":
        run_action(admin.transfer, "이관했습니다.", rid, to, reason)
    elif action == "회사명 수정":
        similar = run_action(admin.rename, "회사명을 바꿨습니다.", rid, new_name, reason, confirm_similar=confirm)
        if similar:
            st.warning("비슷한 이름의 다른 등록이 있습니다. 같은 회사가 아닌지 확인한 뒤, 체크하고 다시 눌러 주세요.\n\n"
                       + "\n".join(f"- {s}" for s in similar))
    elif action == "등록 해제":
        if not sure:
            st.error("'해제합니다'에 체크해 주세요.")
        else:
            run_action(admin.release, "등록을 해제했습니다.", rid, reason)
    elif action == "반려 되돌리기":
        run_action(admin.restore, "반려를 취소하고 보호 중으로 되돌렸습니다.", rid, reason)


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
        st.info(f"{r['company_name']} ({r['partner_name']}) 상세 기록 — 이 열람은 {r['partner_name']} 관리자 화면에 기록되었습니다.")
        show_timeline(opened[1])


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


def admin_view(user: dict) -> None:
    tabs = st.tabs(["검토 대기", "등록 관리", "전체 현황", "만료 임박", "이력 로그", "공지 작성", "영업 현황", "파트너·계정"])
    with tabs[6]:
        pipeline_tab(user)
    with tabs[7]:
        partners_tab(user)
    with tabs[0]:
        review_tab()
    with tabs[1]:
        manage_tab()
    with tabs[2]:
        rows = db.list_registrations()
        c = st.columns(6)
        for col, (label, s) in zip(c, [("보호 중", "active"), ("검토 대기", "pending"), ("계약 고객", "won"),
                                       ("만료", "expired"), ("반려", "rejected"), ("해제", "released")]):
            col.metric(label, sum(r["status"] == s for r in rows))
        st.dataframe(reg_table(rows, ext=admin.extension_counts()), hide_index=True, width="stretch")
    with tabs[3]:
        st.caption(f"보호 중은 {db.EXPIRING_SOON_DAYS}일 안, 계약 고객은 계약 종료 {db.CONTRACT_ALERT_DAYS}일 안에 끝나는 등록입니다.")
        soon = db.expiring_soon()
        if soon:
            st.dataframe(reg_table(soon, ext=admin.extension_counts()), hide_index=True, width="stretch")
        else:
            st.info("곧 끝나는 등록이 없습니다.")
    with tabs[4]:
        st.caption("분쟁이 생기면 '누가 언제 등록했는지'를 이 기록으로 확인합니다.")
        ev = db.list_events()
        st.dataframe(pd.DataFrame([{
            "시각": e["ts"].replace("T", " "), "구분": EVENT_LABEL.get(e["kind"], e["kind"]),
            "파트너": e["partner"], "내용": e["detail"],
        } for e in ev]), hide_index=True, width="stretch")
    with tabs[5]:
        with st.form("notice", clear_on_submit=True):
            title = st.text_input("제목")
            body = st.text_area("내용")
            if st.form_submit_button("공지 등록") and title and body:
                db.add_notice(title, body)
                db.log("notice", ADMIN, detail=title)
                st.toast("공지를 등록했습니다.")
        notices_tab()


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
    login_page()
elif user["must_change_pw"]:
    must_change_page(user)
else:
    sidebar(user)
    st.title("파트너 영업 관리")
    if user["role"] == "admin":
        admin_view(user)
    else:
        # 파트너 화면에서는 표의 내려받기 버튼을 숨긴다 (화면 차원의 제한 — 실제 운영에선 서버에서 막아야 함)
        st.markdown("<style>[data-testid='stElementToolbar']{display:none}</style>", unsafe_allow_html=True)
        me = {"id": user["partner_id"], "name": user["partner_name"], "user": user}
        names = ["고객사 등록", "내 영업권", "영업 관리", "선점 조회", "공지"]
        if crm.is_manager(user):
            names += ["팀 관리", "총판 열람 기록"]
        tabs = dict(zip(names, st.tabs(names)))
        with tabs["고객사 등록"]:
            register_tab(me)
        with tabs["내 영업권"]:
            my_tab(me)
        with tabs["영업 관리"]:
            crm_tab(user)
        with tabs["선점 조회"]:
            lookup_tab(me)
        with tabs["공지"]:
            notices_tab()
        if crm.is_manager(user):
            with tabs["팀 관리"]:
                team_tab(user)
            with tabs["총판 열람 기록"]:
                access_log_tab(user)
