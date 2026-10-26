"""숙향식 가치분석 — 종목명을 넣으면 10년 재무, 네 가지 조건, 내재가치를 계산합니다."""
import os

import streamlit as st

import sukhyang_ui as ui  # 화면 코드 (주식 스크리너와 함께 씀)

st.set_page_config(page_title="숙향식 가치분석", page_icon="📒", layout="wide")


def secret(name, default=""):
    """secrets.toml(또는 Streamlit Cloud Secrets) → 환경변수 순으로 찾는다."""
    try:
        v = st.secrets.get(name)
    except Exception:  # secrets.toml 없음
        v = None
    return v if v not in (None, "") else os.environ.get(name, default)


def saved_rate():
    try:
        return min(max(float(secret("DEPOSIT_RATE", 0)), 0.0), 20.0)
    except ValueError:
        return 0.0


# ───────── 사이드바: 분석 설정 ─────────
with st.sidebar:
    st.header("분석 설정")
    # 저장된 키는 화면(브라우저)으로 보내지 않고 서버에서만 사용
    saved_key = secret("DART_API_KEY")
    if saved_key:
        api_key = saved_key
        st.caption("🔑 저장된 DART 인증키 사용 중")
    else:
        api_key = st.text_input("DART 인증키", type="password", help="opendart.fss.or.kr에서 무료 발급")
    params = ui.settings(saved_rate())

ui.page(api_key, params)
