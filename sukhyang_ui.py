"""숙향식 가치분석 화면 — 숙향 앱(app.py)과 주식 스크리너가 함께 씁니다.

화면을 고치면 두 앱에 똑같이 반영됩니다. k 는 위젯·세션 키 앞에 붙는 구분자로,
숙향 앱은 "" (기존과 같은 키), 스크리너는 "sk_" 처럼 다른 화면과 겹치지 않게 넘깁니다.
"""
import datetime as dt
import shutil

import pandas as pd
import streamlit as st

from data import CACHE_DIR, DartClient, DartError, FS_NAME
from sukhyang import (FSCORE_NOTE, GRADES, analyze, build_report, dividend_history, fscore_label,
                      ttm_summary)

PRICE_REFRESH = 300  # 초. 이 간격이 지나 다시 분석하면 주가·당해 연도 공시를 새로 받는다


def _key(k, name):
    return f"{k}{name}" if k else None  # 숙향 앱은 기존처럼 키 없이


def settings(default_rate=0.0, k=""):
    """분석 설정 위젯. 호출한 곳(사이드바 등) 안에 그린다. 설정값 튜플을 돌려준다."""
    st.subheader("예금금리")
    deposit_rate = st.number_input("1년 정기예금 세전 금리(%)", min_value=0.0, max_value=20.0,
                                   value=default_rate, step=0.05, format="%.2f", key=_key(k, "rate"),
                                   help="0이면 배당수익률 조건은 '확인 불가'로 표시됩니다. "
                                        "Secrets에 DEPOSIT_RATE를 넣어두면 그 값이 기본으로 들어갑니다.")
    rate_date = st.date_input("금리 조회일", value=dt.date.today(), key=_key(k, "rate_date"))
    st.subheader("계산 옵션")
    value_basis = st.radio("내재가치 EPS·BPS 기준", ["최근 4분기 TTM (최신 보고서 반영)", "연간 사업보고서 (지침 원식)"],
                           key=_key(k, "value_basis"),
                           help="TTM: 최신 분기보고서까지 반영한 최근 12개월 실적을 1년씩 밀어 3개 구간으로 가중평균하고, "
                                "BPS는 최신 분기말 기준. 다른 기준의 내재가치도 참고로 함께 표시됩니다.")
    share_basis = st.radio("EPS·BPS 주식수 기준", ["연도별 자사주 제외 주식수", "최근 주식수 일괄 적용"],
                           key=_key(k, "share_basis"),
                           help="주식분할·병합이 있었다면 '일괄 적용'으로 비교해 보세요.")
    include_lease = st.checkbox("순현금 계산에 리스부채 포함", value=False, key=_key(k, "lease"))
    manual_price = st.number_input("주가 직접 입력(원, 0이면 자동 조회)", min_value=0, value=0, step=100,
                                   key=_key(k, "manual_price"))
    if st.button("캐시 비우고 새로 받기", key=_key(k, "clear"), help="정정공시를 반영하려면 누른 뒤 다시 분석하세요."):
        run_analysis.clear()
        shutil.rmtree(CACHE_DIR, ignore_errors=True)  # 디스크에 저장된 DART 자료까지 삭제
        st.session_state.pop(k + "result", None)
        st.session_state.pop(k + "params", None)
        st.success("캐시를 비웠습니다. 다시 분석하면 DART에서 새로 받습니다.")
    # 사이드바 설정값. 분석 후 바뀌면 자동으로 다시 계산한다
    return (deposit_rate, rate_date.isoformat(), "year" if share_basis.startswith("연도별") else "latest",
            include_lease, manual_price, "ttm" if value_basis.startswith("최근") else "annual")


@st.cache_data(show_spinner=False, ttl=3600)
def run_analysis(key, corp, rate, rdate, basis, lease, mprice, vbasis, bucket=0):
    # bucket은 캐시 구분용(5분 단위 시각, 밑줄로 시작하면 캐시 키에서 빠지므로 주의).
    # 지난 연도 공시는 디스크 캐시라 다시 받아도 빠르다
    return analyze(DartClient(key), corp, deposit_rate=rate or None, rate_date=rdate,
                   share_basis=basis, include_lease=lease, manual_price=mprice or None, value_basis=vbasis)


def analyze_now(api_key, corp, params, k=""):
    try:
        bucket = int(dt.datetime.now().timestamp() // PRICE_REFRESH)
        st.session_state[k + "result"] = run_analysis(api_key, corp, *params, bucket)
        st.session_state[k + "params"] = params
    except DartError as e:
        st.session_state.pop(k + "result", None)
        st.session_state.pop(k + "params", None)
        st.error(str(e))


def _current(api_key, params, k):
    """저장된 분석 결과. 설정이 바뀌었으면 다시 계산한다. 보여줄 결과가 없으면 None"""
    res = st.session_state.get(k + "result")
    if res and st.session_state.get(k + "params") != params:
        with st.spinner("설정이 바뀌어 다시 계산하는 중입니다."):
            analyze_now(api_key, res["corp"], params, k)
        res = st.session_state.get(k + "result")
    if not res:
        return None
    if res.get("error"):
        st.error(res["error"])
        return None
    return res


def page(api_key, params, k="", title=True):
    """종목 검색 → 분석 → 결과 (숙향 앱 화면 전체)"""
    if title:
        st.title("숙향식 가치분석")
    st.caption("종목명이나 6자리 종목코드를 입력하면 최근 10년 공시로 네 가지 조건과 내재가치를 계산합니다. "
               "최종 판단은 직접 내립니다.")

    # 폼으로 묶어 종목명 입력 후 엔터만 눌러도 검색. 검색 결과가 하나면 바로 분석
    auto_run = False
    with st.form(k + "search_form", border=False):
        col1, col2 = st.columns([4, 1])
        query = col1.text_input("종목명", placeholder="예: 삼성전자 또는 005930", label_visibility="collapsed",
                                key=_key(k, "query"))
        submitted = col2.form_submit_button("검색", width="stretch", type="primary")
    # 한글 입력은 마지막 글자가 조합 중이라 첫 엔터를 Streamlit이 무시한다(두 번 눌러야 검색됨).
    # 조합 중에 눌린 엔터를 감지해 키를 뗄 때 검색 버튼을 대신 눌러 준다.
    st.html("""<script>
const doc = document;
if (!doc.__enterSearch) {
  doc.__enterSearch = true;
  let pending = false;
  const searchInput = t => t && t.tagName === "INPUT" && (t.placeholder || "").startsWith("예: 삼성전자");
  doc.addEventListener("keydown", e => {
    if (searchInput(e.target) && (e.key === "Enter" || e.code === "Enter")
        && (e.isComposing || e.keyCode === 229)) pending = true;
  }, true);
  doc.addEventListener("keyup", e => {
    if (!pending || !searchInput(e.target) || e.code !== "Enter") return;
    pending = false;
    const form = e.target.closest('[data-testid="stForm"]');
    const btn = form && form.querySelector("button");
    if (!btn || !e.target.value.trim()) return;
    e.target.blur();                        // 입력값 확정(Streamlit은 포커스가 빠질 때 값을 반영)
    setTimeout(() => btn.click(), 150);
  }, true);
}
</script>""", unsafe_allow_javascript=True)
    if submitted and query:
        try:
            st.session_state[k + "matches"] = DartClient(api_key).search(query)
            st.session_state.pop(k + "result", None)
            st.session_state.pop(k + "params", None)
            found = st.session_state[k + "matches"]
            auto_run = len(found) == 1 and found[0].get("exact")  # 비슷한 이름만 찾았으면 직접 고르게
        except DartError as e:
            st.error(str(e))

    matches = st.session_state.get(k + "matches")
    if matches is not None:
        if not matches:
            st.warning("일치하는 상장사가 없습니다. 정확한 회사명이나 종목코드로 다시 검색하세요.")
        else:
            labels = [f"{m['corp_name']} ({m['stock_code']})"
                      + (f" · {m['short']}" if m.get("short") and m["short"] != m["corp_name"] else "")
                      for m in matches]
            pick = st.selectbox(f"분석할 회사 ({len(matches)}개 찾음)", labels, key=_key(k, "pick")) \
                if len(matches) > 1 else labels[0]
            corp = matches[labels.index(pick)]
            if st.button(f"{corp['corp_name']} 분석하기", type="primary", key=_key(k, "run")) or auto_run:
                with st.spinner("DART에서 10년치 공시를 모으는 중입니다. 처음엔 1분 정도 걸립니다."):
                    analyze_now(api_key, corp, params, k)

    res = _current(api_key, params, k)
    if res:
        st.divider()
        render_result(res, k)


def corp_page(api_key, corp, params, k):
    """종목이 정해진 화면(스크리너 종목 탭). 버튼을 눌러야 공시를 받는다."""
    res = st.session_state.get(k + "result")
    if not res or res["corp"]["stock_code"] != corp["stock_code"]:
        st.write("숙향 앱과 같은 계산으로 최근 10년 공시의 네 가지 조건, 내재가치, 10년 재무를 보여 줍니다. "
                 "처음엔 1분 정도 걸리고, 지난 연도 공시는 저장돼 다음부터 빠릅니다.")
        if st.button("숙향 정밀분석 실행", type="primary", key=k + "run"):
            with st.spinner("DART에서 10년치 공시를 모으는 중입니다."):
                analyze_now(api_key, corp, params, k)
            st.rerun()
        return
    res = _current(api_key, params, k)
    if res:
        render_result(res, k)


def render_result(res, k=""):
    c, L = res["corp"], res["latest"]
    st.header(f"{c['corp_name']} ({c['stock_code']})")
    st.caption(f"{FS_NAME[res['fs']]} · {L - 9}~{L}년 사업보고서"
               + (f" · {res['quarter']['label']}" if res["quarter"] else "")
               + f" · 주가 기준일 {res['price_date'] or '확인 불가'}")

    # 핵심 수치
    m = st.columns(4)
    m[0].metric("현재 주가", f"{res['price']:,.0f}원" if res["price"] else "확인 불가")
    m[1].metric("숙향식 내재가치", f"{res['iv']:,.0f}원" if res["iv"] is not None else "계산 불가")
    m[2].metric("내재가치 대비 할인율", f"{res['discount']:.1f}%" if res["discount"] is not None else "-")
    m[3].metric("내재가치까지 상승여력", f"{res['upside']:.1f}%" if res["upside"] is not None else "-")

    # 네 가지 조건
    st.subheader("네 가지 투자조건")
    cond = pd.DataFrame(res["conditions"])[["조건", "실제 수치", "기준", "판정"]]
    met = (cond["판정"] == "충족").sum()
    st.write(f"네 조건 중 **{met}개 충족**")
    st.dataframe(cond, hide_index=True, width="stretch")
    nc = res["net_cash_detail"]
    if nc:
        with st.expander(f"순현금 계산 내역 ({res['net_cash_basis']})"):
            st.write("자산(+)", pd.DataFrame(nc["cash_items"], columns=["계정", "금액(원)"]))
            st.write("이자부 금융부채(−)", pd.DataFrame(nc["debt_items"], columns=["계정", "금액(원)"]))
            st.write(f"리스부채({'포함' if nc['include_lease'] else '제외'})",
                     pd.DataFrame(nc["lease_items"], columns=["계정", "금액(원)"]))
            st.caption("계정명 기준 자동 분류입니다. 사용 제한 예금 등은 주석에서 확인하세요.")

    # 내재가치 표
    st.subheader("내재가치 평가")
    st.caption(f"기준: {res['basis_name']}")
    e0, e1, e2 = res["eps"]
    l0, l1, l2 = res["eps_labels"]
    fmt = lambda x: "확인 불가" if x is None else f"{x:,.0f}원"
    iv_rows = [
        ("현재 주가", fmt(res["price"])), (l0, fmt(e0)), (l1, fmt(e1)), (l2, fmt(e2)),
        ("가중평균 EPS", fmt(res["w_eps"])), (res["bps_label"], fmt(res["bps"])),
        ("숙향식 내재가치", fmt(res["iv"])),
        ("보수적 참고가치", fmt(res["conservative"]["value"]) if res["conservative"] else "계산 불가"),
    ]
    if res["alt"]:
        iv_rows.append((f"참고: {res['alt']['name']} 내재가치", fmt(res["alt"]["iv"])))
    st.dataframe(pd.DataFrame(iv_rows, columns=["항목", "결과"]), hide_index=True, width="stretch")
    if res["conservative"]:
        st.caption("보수적 참고가치: " + res["conservative"]["method"])

    # 10년 재무
    st.subheader("최근 10년 재무")
    st.caption("최근 실적부터 왼쪽에 나옵니다. 아래 차트는 과거 → 최근 순서입니다.")
    rows = []
    for y in res["years"]:
        r = res["recs"][y]
        if r.get("missing"):
            rows.append({"연도": str(y)})
            continue
        e = lambda k_: None if r.get(k_) is None else round(r[k_] / 1e8)
        rows.append({"연도": str(y), "매출액(억)": e("revenue"), "영업이익(억)": e("op"),
                     "영업이익률(%)": None if r.get("op_margin") is None else round(r["op_margin"], 1),
                     "순이익(억)": e("ni"), "EPS(원)": None if r.get("eps") is None else round(r["eps"]),
                     "공시 EPS(원)": r.get("eps_reported"), "BPS(원)": None if r.get("bps") is None else round(r["bps"]),
                     "ROE(%)": None if r.get("roe") is None else round(r["roe"], 1),
                     "영업CF(억)": e("ocf"), "FCF(억)": e("fcf"), "주당배당(원)": r.get("dps"),
                     "배당성향(%)": r.get("payout"), "순현금(억)": e("net_cash"),
                     "매출채권(억)": e("receivables"), "재고자산(억)": e("inventory"),
                     "조정주식수": r.get("adj_shares")})
    t = ttm_summary(res)
    if t:  # 최신 분기까지 반영한 최근 12개월 칸
        e = lambda k_: None if t[k_] is None else round(t[k_] / 1e8)
        rows.append({"연도": t["label"], "매출액(억)": e("revenue"), "영업이익(억)": e("op"),
                     "영업이익률(%)": None if t["op_margin"] is None else round(t["op_margin"], 1),
                     "순이익(억)": e("ni"), "EPS(원)": None if t["eps"] is None else round(t["eps"]),
                     "BPS(원)": None if t["bps"] is None else round(t["bps"]),
                     "ROE(%)": None if t["roe"] is None else round(t["roe"], 1), "순현금(억)": e("net_cash")})
    df = pd.DataFrame(rows).set_index("연도")  # 과거 → 최근 (차트용)
    st.dataframe(df.iloc[::-1].T, width="stretch")  # 표는 최근 → 과거
    if t:
        st.caption(f"{t['label']}: {res['quarter']['label']}까지 반영한 최근 12개월 실적, BPS·순현금은 최신 분기말 기준")

    t1, t2, t3 = st.tabs(["이익", "주당 가치", "현금·배당"])
    with t1:
        st.bar_chart(df[["매출액(억)", "영업이익(억)", "순이익(억)"]].dropna(how="all"), stack=False)
    with t2:
        st.line_chart(df[["EPS(원)", "BPS(원)"]].dropna(how="all"))
    with t3:
        st.bar_chart(df[["영업CF(억)", "FCF(억)", "순현금(억)"]].dropna(how="all"), stack=False)
        st.line_chart(df[["주당배당(원)"]].dropna(how="all"))

    # 배당 내역
    st.subheader("배당 내역")
    drows, dsum = dividend_history(res)
    st.write(dsum)
    div_df = pd.DataFrame([{
        "연도": str(x["year"]), "주당배당금(원)": x["dps"],
        "전년 대비(%)": None if x["change"] is None else round(x["change"], 1),
        "배당성향(%)": x["payout"], "시가배당률(%)": x["yield"],
        "현재가 기준 수익률(%)": None if x["yield_now"] is None else round(x["yield_now"], 2),
        "배당금 총액(억)": None if x["total"] is None else round(x["total"] / 1e8)} for x in drows])
    num_cols = div_df.columns.drop("연도")
    div_df[num_cols] = div_df[num_cols].apply(pd.to_numeric, errors="coerce")  # 없는 값은 'None' 대신 빈칸
    st.dataframe(div_df, hide_index=True, width="stretch",
        column_config={"주당배당금(원)": st.column_config.NumberColumn(format="%,.0f"),
                       "배당금 총액(억)": st.column_config.NumberColumn(format="%,.0f")})
    st.caption("시가배당률: 회사가 사업보고서에 공시한 값(배당기준일 무렵 주가 기준, 보통주). "
               "현재가 기준 수익률: 그 해 주당배당금 ÷ 현재 주가. 빈칸은 배당이 없거나 공시에서 확인되지 않은 해입니다.")

    # 장기 검증
    st.subheader("장기 실적 검증")
    for s in res["risks"]:
        st.write("• " + s)
    if res["notes"]:
        with st.expander("자료 관련 참고사항", expanded=True):
            for n in res["notes"]:
                st.write("• " + n)

    # 참고 점수 (숙향 판정과 별개)
    st.subheader("F-Score (퀄리티 참고)")
    fs_ = res.get("fscore")
    if fs_:
        st.write(f"**{fscore_label(fs_)}** · {fs_['year']}년과 {fs_['prev']}년 사업보고서 비교")
        st.dataframe(pd.DataFrame(fs_["items"]), hide_index=True, width="stretch")
        st.caption(FSCORE_NOTE)
    else:
        st.write("전년 사업보고서 자료가 없어 계산하지 못했습니다.")

    # 종합 판단 (사용자)
    st.subheader("종합 투자 판단")
    st.caption("지침에 고정 할인율이 없으므로 등급은 위 수치를 보고 직접 고릅니다.")
    grade = st.radio("최종 등급", GRADES, index=None, horizontal=True, key=_key(k, "grade"))
    memo = st.text_area("판단 근거", placeholder="예: 네 조건 충족, 10년 연속 흑자, 순현금 유지. 분할매수 1차 검토.",
                        key=_key(k, "memo"))

    report = build_report(res, grade, memo)
    st.download_button("리포트 내려받기(.md)", report.encode("utf-8"),
                       file_name=f"{c['corp_name']}_숙향분석_{res['today']}.md", mime="text/markdown",
                       type="primary", key=_key(k, "dl"))
    with st.expander("리포트 미리보기"):
        st.markdown(report)
