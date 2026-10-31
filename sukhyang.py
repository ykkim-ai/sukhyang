"""숙향 가치투자 지침(2026-10-22 확정)에 따른 계산, 분석, 리포트."""
from __future__ import annotations

import datetime as dt

from data import (REPRT_FY, REPRT_H1, REPRT_Q1, REPRT_Q3, REPRT_NAME, FS_NAME, DartError,
                  extract, parse_net_cash, parse_shares, parse_dividend, get_price)

GRADES = ["매수 검토 후보", "관찰 대상", "기준 미달", "자료 부족"]

# 지침 9장: 숙향의 기피 업종(한국표준산업분류 앞자리, 참고 표시용)
AVOID_INDUSTRY = {"41": "건설", "42": "건설", "51": "항공", "50": "해운", "641": "은행", "21": "의약품(일부 바이오)",
                  "7011": "연구개발(일부 바이오)"}
FINANCE_PREFIX = ("64", "65", "66")


# ───────────────────────── 지침 3·4장: 계산식 ─────────────────────────

def weighted_eps(eps_recent, eps_prev, eps_prev2):
    """(최근 EPS × 3 + 전년 EPS × 2 + 전전년 EPS) ÷ 6"""
    if None in (eps_recent, eps_prev, eps_prev2):
        return None
    return (eps_recent * 3 + eps_prev * 2 + eps_prev2) / 6


def intrinsic_value(bps, w_eps):
    """(최근 BPS + 가중평균 EPS × 10) ÷ 2"""
    if bps is None or w_eps is None:
        return None
    return (bps + w_eps * 10) / 2


def discount_rate(iv, price):
    """(내재가치 - 현재주가) ÷ 내재가치 × 100"""
    if iv is None or price is None or iv <= 0:
        return None
    return (iv - price) / iv * 100


def upside(iv, price):
    """(내재가치 - 현재주가) ÷ 현재주가 × 100"""
    if iv is None or price is None or price <= 0:
        return None
    return (iv - price) / price * 100


# ───────────────────────── 지침 2장: 네 가지 조건 ─────────────────────────

def judge_conditions(price, eps, bps, dps, deposit_rate, net_cash):
    rows = []
    # PER
    if price is None or eps is None:
        rows.append(("PER", None, "확인 불가", "10배 이하", "확인 불가"))
    elif eps <= 0:
        rows.append(("PER", None, "적자(EPS ≤ 0)", "10배 이하", "미달"))
    else:
        per = price / eps
        rows.append(("PER", per, f"{per:.2f}배", "10배 이하", "충족" if per <= 10 else "미달"))
    # PBR
    if price is None or bps is None:
        rows.append(("PBR", None, "확인 불가", "1배 이하", "확인 불가"))
    elif bps <= 0:
        rows.append(("PBR", None, "자본잠식(BPS ≤ 0)", "1배 이하", "미달"))
    else:
        pbr = price / bps
        rows.append(("PBR", pbr, f"{pbr:.2f}배", "1배 이하", "충족" if pbr <= 1 else "미달"))
    # 배당수익률
    if price is None or dps is None:
        rows.append(("배당수익률", None, "확인 불가", "예금금리 이상", "확인 불가"))
    else:
        dy = dps / price * 100
        if not deposit_rate:
            rows.append(("배당수익률", dy, f"{dy:.2f}%", "예금금리 미입력", "확인 불가"))
        else:
            rows.append(("배당수익률", dy, f"{dy:.2f}%", f"{deposit_rate:.2f}% 이상",
                         "충족" if dy >= deposit_rate else "미달"))
    # 순현금
    if net_cash is None:
        rows.append(("순현금", None, "확인 불가", "0보다 클 것", "확인 불가"))
    else:
        rows.append(("순현금", net_cash, f"{net_cash / 1e8:,.0f}억원", "0보다 클 것",
                     "충족" if net_cash > 0 else "미달"))
    return [dict(zip(("조건", "값", "실제 수치", "기준", "판정"), r)) for r in rows]


# ───────────────────────── 분석 실행 ─────────────────────────

def _find_latest(client, code, today):
    for y in (today.year - 1, today.year - 2):
        for fs in ("CFS", "OFS"):
            rows = client.fin_all(code, y, REPRT_FY, fs)
            if rows:
                return y, fs, rows
    return None, None, None


def _year_record(rows, fs, notes, y):
    rec = {"year": y}
    for k in ("revenue", "op", "eps_reported", "ocf", "capex", "receivables", "inventory"):
        rec[k] = extract(rows, k)
    if fs == "CFS":
        rec["ni"] = extract(rows, "ni_owner")
        rec["equity"] = extract(rows, "equity_owner")
        if rec["ni"] is None:
            rec["ni"] = extract(rows, "ni_total")
            if rec["ni"] is not None:
                notes.append(f"{y}년: 지배주주 순이익 계정을 찾지 못해 연결 당기순이익(비지배 포함)을 사용")
        if rec["equity"] is None:
            rec["equity"] = extract(rows, "equity_total")
            if rec["equity"] is not None:
                notes.append(f"{y}년: 지배주주 자본 계정을 찾지 못해 자본총계(비지배 포함)를 사용")
    else:
        rec["ni"] = extract(rows, "ni_total")
        rec["equity"] = extract(rows, "equity_total")
    for k, f in (("assets", "assets"), ("ca", "current_assets"), ("cl", "current_liab"),
                 ("ncl", "noncurrent_liab"), ("gp", "gross_profit"), ("cogs", "cogs")):
        rec[k] = extract(rows, f)
    rec["assets_begin"] = extract(rows, "assets", fields=("frmtrm_amount",))  # 기초 자산 = 같은 보고서의 전기말
    if rec["capex"] is not None:
        rec["capex"] = abs(rec["capex"])
    rec["fcf"] = rec["ocf"] - rec["capex"] if rec["ocf"] is not None and rec["capex"] is not None else None
    rec["rcept_no"] = rows[0].get("rcept_no") if rows else None
    return rec


Q_END_MONTH = {REPRT_Q1: 3, REPRT_H1: 6, REPRT_Q3: 9}


def _cum_pair(rows, key, reprt):
    """당기·전년 동기 누적값(손익계산서 계정). 같은 기간끼리 짝짓고, 1분기는 분기값 = 누적값."""
    pairs = [("thstrm_add_amount", "frmtrm_add_amount")]
    if reprt == REPRT_Q1:
        pairs.append(("thstrm_amount", "frmtrm_q_amount"))
    cur = prev = None
    for f_cur, f_prev in pairs:
        cur = extract(rows, key, fields=(f_cur,))
        prev = extract(rows, key, fields=(f_prev,))
        if cur is not None and prev is not None:
            break
    return cur, prev


def _ttm(fy, rows, key, reprt):
    """사업연도 값 + 다음 해 누적 - 그 해 같은 기간 누적 = 최근 12개월."""
    cur, prev = _cum_pair(rows, key, reprt) if rows else (None, None)
    return fy + cur - prev if None not in (fy, cur, prev) else None


def _ttm_basis(client, code, fs, recs, latest, latest_sh, quarter, q_rows, share_basis, acc_mt, notes):
    """최근 4분기(TTM) 기준 EPS 3개 구간과 최신 분기말 BPS.
    구간이 겹치지 않도록 1년씩 민다: 사업연도 순이익 + 다음 해 누적 - 그 해 같은 기간 누적."""
    reprt = quarter["reprt"]
    m = Q_END_MONTH[reprt]
    calendar = acc_mt in ("", "12")  # 12월 결산이 아니면 달력 월을 표기하지 않는다
    ni_key = "ni_owner" if fs == "CFS" else "ni_total"

    def q_shares(year):
        try:
            return (parse_shares(client.stock_total(code, year, reprt)) or {}).get("adjusted")
        except DartError:
            return None

    sh0 = q_shares(latest + 1) or latest_sh
    eps, labels = [], []
    for k in range(3):
        y = latest - k
        rows = q_rows
        if k:
            try:
                rows = client.fin_all(code, y + 1, reprt, fs)
            except DartError:
                rows = None
        ni = _ttm(recs[y].get("ni"), rows, ni_key, reprt)
        sh = sh0 if k == 0 or share_basis != "year" else (q_shares(y + 1) or recs[y].get("adj_shares") or sh0)
        eps.append(ni / sh if ni is not None and sh else None)
        labels.append(f"{y + 1}년 EPS (TTM {y}.{m + 1:02d}~{y + 1}.{m:02d})" if calendar
                      else f"{y + 1}년 EPS (TTM, {y + 1}사업연도 {REPRT_NAME[reprt]}까지 12개월)")
    if None in eps:
        notes.append("최근 4분기(TTM) 기준: 과거 같은 분기 보고서가 없어 일부 구간을 계산하지 못했습니다.")

    equity = extract(q_rows, "equity_owner") if fs == "CFS" else None
    if equity is None:
        equity = extract(q_rows, "equity_total")
    bps = equity / sh0 if equity is not None and sh0 else None
    w = weighted_eps(*eps)
    bps_when = f"{latest + 1}.{m:02d}월말 {REPRT_NAME[reprt]}" if calendar \
        else f"{latest + 1}사업연도 {REPRT_NAME[reprt]} 기준"
    return {"eps": tuple(eps), "labels": tuple(labels), "shares": sh0,
            "bps": bps, "bps_label": f"{latest + 1}년 BPS ({bps_when})", "w_eps": w,
            "iv": intrinsic_value(bps, w), "name": f"최근 4분기 TTM ({quarter['label']} 반영)"}


def analyze(client, corp, deposit_rate=None, rate_date=None, share_basis="year",
            include_lease=False, manual_price=None, value_basis="annual", today=None):
    today = today or dt.date.today()
    code = corp["corp_code"]
    notes, sources = [], []

    # DART 호출을 하나씩 기다리지 않도록 필요한 공시를 동시에 미리 받아 둔다
    y0 = today.year
    client.prefetch([("company", code)] +
                    [("fin_all", code, y, REPRT_FY, f) for y in (y0 - 1, y0 - 2) for f in ("CFS", "OFS")])
    info = client.company(code) or {}
    latest, fs, latest_rows = _find_latest(client, code, today)
    if latest is not None:
        calls = []
        for y in range(latest - 9, latest + 1):
            calls += [("fin_all", code, y, REPRT_FY, fs), ("stock_total", code, y), ("dividend", code, y)]
        for r in (REPRT_Q3, REPRT_H1, REPRT_Q1):
            calls += [("fin_all", code, latest + 1, r, fs), ("stock_total", code, latest + 1, r)]
        client.prefetch(calls)
    if latest is None:
        return {"error": "최근 2개 사업연도의 사업보고서 재무제표를 DART에서 찾지 못했습니다.", "corp": corp}

    # 10개 사업연도
    years = list(range(latest - 9, latest + 1))
    recs = {}
    for y in years:
        try:
            rows = latest_rows if y == latest else client.fin_all(code, y, REPRT_FY, fs)
        except DartError as e:
            rows = None
            notes.append(f"{y}년 재무제표 조회 오류: {e}")
        if not rows:
            recs[y] = {"year": y, "missing": True}
            continue
        rec = _year_record(rows, fs, notes, y)
        nc = parse_net_cash(rows, include_lease)
        rec["net_cash"] = nc["net_cash"] if nc else None
        try:
            rec["shares"] = parse_shares(client.stock_total(code, y))
        except DartError:
            rec["shares"] = None
        try:
            div = parse_dividend(client.dividend(code, y))
        except DartError:
            div = parse_dividend(None)
        rec.update(dps=div["dps"], payout=div["payout"], div_yield=div["yield"],
                   div_total=None if div["total_mil"] is None else div["total_mil"] * 1e6)
        recs[y] = rec
        if rec.get("rcept_no"):
            sources.append((f"{y}년 사업보고서({FS_NAME[fs]})", rec["rcept_no"]))

    missing = [y for y in years if recs[y].get("missing")]
    if missing:
        notes.append(f"{FS_NAME[fs]} 기준 자료가 없는 연도: {', '.join(map(str, missing))} "
                     "(연결·별도 혼합 금지 원칙에 따라 다른 기준으로 채우지 않음)")

    # 지침 3.3: 자사주 제외 주식수로 EPS·BPS 계산
    latest_sh = (recs[latest].get("shares") or {}).get("adjusted")
    for y in years:
        r = recs[y]
        if r.get("missing"):
            continue
        own = (r.get("shares") or {}).get("adjusted")
        sh = own if share_basis == "year" else latest_sh
        if sh is None and latest_sh:
            sh = latest_sh
            notes.append(f"{y}년: 주식수 자료가 없어 {latest}년 조정주식수를 사용")
        r["adj_shares"] = sh
        r["eps"] = r["ni"] / sh if sh and r.get("ni") is not None else None
        r["bps"] = r["equity"] / sh if sh and r.get("equity") is not None else None
        r["roe"] = r["ni"] / r["equity"] * 100 if r.get("ni") is not None and r.get("equity") else None
        r["op_margin"] = r["op"] / r["revenue"] * 100 if r.get("op") is not None and r.get("revenue") else None
    if latest_sh is None:
        notes.append("자사주 제외 주식수를 확인하지 못해 EPS·BPS를 계산할 수 없습니다.")

    # 주식수 급변(분할·병합·증자·소각) 표시
    share_changes = []
    for y in years[1:]:
        a, b = recs[y - 1].get("adj_shares"), recs[y].get("adj_shares")
        if share_basis == "year" and a and b and abs(b / a - 1) >= 0.1:
            share_changes.append((y, a, b))
    if share_changes:
        notes.append("조정주식수가 크게 바뀐 해가 있습니다(" +
                     ", ".join(f"{y}년 {a:,.0f}→{b:,.0f}주" for y, a, b in share_changes) +
                     "). 주식분할·병합이라면 사이드바에서 '최근 주식수 일괄 적용'으로 비교하세요.")

    # 최신 분기 (TTM, 최신 순현금)
    quarter, q_rows = None, None
    for reprt in (REPRT_Q3, REPRT_H1, REPRT_Q1):
        try:
            q_rows = client.fin_all(code, latest + 1, reprt, fs)
        except DartError:
            q_rows = None
        if q_rows:
            L0 = recs[latest]
            ttm = _ttm(L0.get("ni"), q_rows, "ni_owner" if fs == "CFS" else "ni_total", reprt)
            quarter = {"label": f"{latest + 1}년 {REPRT_NAME[reprt]}", "reprt": reprt, "ttm_ni": ttm,
                       "ttm_eps": ttm / latest_sh if ttm is not None and latest_sh else None,
                       "ttm_revenue": _ttm(L0.get("revenue"), q_rows, "revenue", reprt),
                       "ttm_op": _ttm(L0.get("op"), q_rows, "op", reprt),
                       "net_cash": parse_net_cash(q_rows, include_lease),
                       "rcept_no": q_rows[0].get("rcept_no")}
            sources.append((quarter["label"] + f"({FS_NAME[fs]})", quarter["rcept_no"]))
            break

    # 주가
    if manual_price:
        price, price_date, price_src = float(manual_price), today.isoformat(), "직접 입력"
    else:
        price, price_date, price_src = get_price(corp["stock_code"])
        if price is None:
            notes.append(f"주가 조회 실패: {price_src}. 사이드바에서 주가를 직접 입력하세요.")

    # 내재가치: 연간(지침 원식)과 최근 4분기 TTM 두 기준을 모두 계산하고 선택한 쪽을 주 결과로
    L = recs[latest]
    annual_eps = (L.get("eps"), recs[latest - 1].get("eps"), recs[latest - 2].get("eps"))
    annual_w = weighted_eps(*annual_eps)
    annual = {"eps": annual_eps, "labels": tuple(f"{y}년 EPS" for y in (latest, latest - 1, latest - 2)),
              "bps": L.get("bps"), "bps_label": f"{latest}년 BPS", "w_eps": annual_w,
              "iv": intrinsic_value(L.get("bps"), annual_w), "name": "연간 사업보고서 (지침 원식)"}
    acc_mt = (info.get("acc_mt") or "").strip()
    ttm_b = _ttm_basis(client, code, fs, recs, latest, latest_sh, quarter, q_rows, share_basis, acc_mt, notes) \
        if quarter else None
    if value_basis == "ttm" and ttm_b and ttm_b["iv"] is not None:
        basis, alt = ttm_b, annual
    else:
        if value_basis == "ttm" and not quarter:
            # 사업보고서 직후~1분기보고서 전: 사업보고서가 가장 최신이라 TTM = 연간
            notes.append(f"최신 공시가 {latest}년 사업보고서라 연간 기준으로 계산했습니다(이 시점에는 TTM과 같음).")
        elif value_basis == "ttm":
            notes.append("최근 4분기 TTM 기준을 계산할 수 없어 연간 사업보고서 기준으로 계산했습니다.")
        basis, alt = annual, ttm_b
    (eps_l, eps_1, eps_2), bps_l, w, iv = basis["eps"], basis["bps"], basis["w_eps"], basis["iv"]

    # 순현금: 최신 분기 우선, 없으면 최근 사업연도
    if quarter and quarter["net_cash"]:
        nc_detail, nc_basis = quarter["net_cash"], quarter["label"]
    else:
        nc_detail = parse_net_cash(latest_rows, include_lease)
        nc_basis = f"{latest}년 사업보고서"

    conditions = judge_conditions(price, eps_l, bps_l, L.get("dps"), deposit_rate,
                                  nc_detail["net_cash"] if nc_detail else None)

    # 보수적 참고가치: 10년 평균 순이익 ÷ 최근 조정주식수
    nis = [recs[y]["ni"] for y in years if not recs[y].get("missing") and recs[y].get("ni") is not None]
    conservative = None
    if nis and latest_sh and bps_l is not None:
        avg_eps = sum(nis) / len(nis) / latest_sh
        conservative = {"avg_eps": avg_eps, "n_years": len(nis), "value": intrinsic_value(bps_l, avg_eps),
                        "method": f"가중평균 EPS 대신 최근 {len(nis)}개 연도 평균 순이익 ÷ {latest}년 조정주식수를 "
                                  "사용 (숙향식 기본 내재가치를 대체하지 않는 참고값)"}

    induty = (info.get("induty_code") or "").strip()
    result = {
        "corp": corp, "info": info, "today": today.isoformat(), "latest": latest, "fs": fs,
        "years": years, "recs": recs, "quarter": quarter, "ttm": ttm_b,
        "price": price, "price_date": price_date, "price_src": price_src,
        "deposit_rate": deposit_rate, "rate_date": rate_date,
        "share_basis": share_basis, "include_lease": include_lease,
        "shares": L.get("shares"), "latest_shares": latest_sh,
        "eps": (eps_l, eps_1, eps_2), "bps": bps_l, "w_eps": w, "iv": iv,
        "eps_labels": basis["labels"], "bps_label": basis["bps_label"], "basis_name": basis["name"],
        "alt": alt if alt and alt["iv"] is not None else None,
        "discount": discount_rate(iv, price), "upside": upside(iv, price),
        "conservative": conservative, "conditions": conditions, "fscore": fscore(recs, latest),
        "net_cash_detail": nc_detail, "net_cash_basis": nc_basis,
        "industry_flag": next((v for k, v in AVOID_INDUSTRY.items() if induty.startswith(k)), None),
        "is_finance": induty.startswith(FINANCE_PREFIX),
        "notes": notes, "sources": sources,
    }
    if info.get("acc_mt") and info["acc_mt"] != "12":
        notes.append(f"결산월이 {info['acc_mt']}월입니다. 사업연도 표기가 달력 연도와 다를 수 있습니다.")
    if result["is_finance"]:
        notes.append("금융업은 재무제표 구조가 달라 PER·PBR·순현금과 내재가치 계산이 적절하지 않을 수 있습니다.")
    result["risks"] = risk_signals(result)
    return result


# ───────────────────────── 지침 5.3: 장기 수익성 검증 신호 ─────────────────────────

def risk_signals(res):
    recs = [res["recs"][y] for y in res["years"] if not res["recs"][y].get("missing")]
    out = []
    if not recs:
        return out
    n = len(recs)
    loss = [r["year"] for r in recs if (r.get("ni") or 0) < 0]
    op_loss = [r["year"] for r in recs if (r.get("op") or 0) < 0]
    out.append(f"순이익 적자 연도: {len(loss)}/{n}년" + (f" ({', '.join(map(str, loss))})" if loss else ""))
    out.append(f"영업이익 적자 연도: {len(op_loss)}/{n}년" + (f" ({', '.join(map(str, op_loss))})" if op_loss else ""))

    nis = [r["ni"] for r in recs if r.get("ni") is not None]
    if len(nis) >= 4:
        recent, longrun = sum(nis[-3:]) / 3, sum(nis) / len(nis)
        if longrun > 0:
            out.append(f"최근 3년 평균 순이익은 {len(nis)}년 평균의 {recent / longrun:.2f}배입니다. "
                       "1보다 크게 높으면 호황·일회성 이익, 크게 낮으면 일시적 불황 여부를 확인하세요.")
        else:
            out.append(f"{len(nis)}년 평균 순이익이 0 이하입니다. 장기 수익력에 주의하세요.")

    nonop = [r["year"] for r in recs[-3:] if r.get("ni") is not None and r.get("op") is not None
             and r["op"] > 0 and r["ni"] > r["op"]]
    if nonop:
        out.append(f"순이익이 영업이익보다 큰 해(최근 3년): {', '.join(map(str, nonop))} — "
                   "자산처분이익 등 영업외·특별이익을 확인하세요.")

    pairs = [(r["ni"], r["ocf"]) for r in recs if r.get("ni") is not None and r.get("ocf") is not None]
    if pairs:
        sni, socf = sum(p[0] for p in pairs), sum(p[1] for p in pairs)
        verdict = "영업현금흐름이 순이익보다 적습니다 — 이익이 현금으로 이어지는지 확인하세요." if socf < sni \
            else "영업현금흐름이 순이익 이상으로 이익의 현금 전환은 양호합니다."
        out.append(f"{len(pairs)}년 누적 순이익 {sni / 1e8:,.0f}억원, 누적 영업현금흐름 {socf / 1e8:,.0f}억원. {verdict}")

    def growth(key):
        vals = [(r["year"], r[key]) for r in recs if r.get(key)]
        if len(vals) >= 2 and vals[0][1] > 0:
            return vals[0][0], vals[-1][0], vals[-1][1] / vals[0][1] - 1
        return None
    g_rev = growth("revenue")
    for key, label in (("receivables", "매출채권"), ("inventory", "재고자산")):
        g = growth(key)
        if g and g_rev:
            msg = f"{g[0]}→{g[1]}년 {label} 증가율 {g[2] * 100:,.0f}%, 매출 증가율 {g_rev[2] * 100:,.0f}%"
            if g[2] > g_rev[2]:
                msg += f" — {label}가 매출보다 빠르게 늘었습니다."
            out.append(msg)

    cuts = [r["year"] for i, r in enumerate(recs[1:], 1)
            if r.get("dps") is not None and recs[i - 1].get("dps") is not None and r["dps"] < recs[i - 1]["dps"]]
    out.append("주당배당금이 줄어든 해: " + (", ".join(map(str, cuts)) if cuts else "없음") +
               " (특별배당 여부는 사업보고서에서 확인)")

    sh = [(r["year"], r.get("adj_shares")) for r in recs if r.get("adj_shares")]
    if res["share_basis"] == "year" and len(sh) >= 2:
        out.append(f"조정주식수 {sh[0][0]}년 {sh[0][1]:,.0f}주 → {sh[-1][0]}년 {sh[-1][1]:,.0f}주 "
                   f"({(sh[-1][1] / sh[0][1] - 1) * 100:+.1f}%)")

    if res.get("industry_flag"):
        out.append(f"업종 참고: {res['industry_flag']} — 숙향이 개인적으로 기피한다고 밝힌 업종군입니다(조건 판정과 별개).")
    return out


# ───────────────────────── 참고: 피오트로스키 F-Score (숙향 판정과 별개) ─────────────────────────

def _ratio(a, b):
    return a / b if a is not None and b else None


def _split_like(before, after):
    """발행주식수가 0.5배 단위 정수배 가까이 늘면 액면분할·무상증자로 본다(돈이 들어오지 않는 주식수 증가)."""
    r = after / before
    return r >= 1.5 and abs(r * 2 - round(r * 2)) < 0.01


def fscore(recs, latest):
    """최근 사업연도와 전년 사업보고서를 비교한 9개 항목. 전년 자료가 없으면 None."""
    cur, prev = recs.get(latest, {}), recs.get(latest - 1, {})
    if not prev or cur.get("missing") or prev.get("missing"):
        return None
    y0, y1 = latest, latest - 1

    def roa(r):
        return _ratio(r.get("ni"), r.get("assets_begin") or r.get("assets"))

    def leverage(r):
        return _ratio(r.get("ncl"), r.get("assets"))

    def liquidity(r):
        return _ratio(r.get("ca"), r.get("cl"))

    def margin(r):
        gp = r.get("gp")
        if gp is None and r.get("revenue") is not None and r.get("cogs") is not None:
            gp = r["revenue"] - abs(r["cogs"])
        return _ratio(gp, r.get("revenue"))

    def turnover(r):
        return _ratio(r.get("revenue"), r.get("assets_begin") or r.get("assets"))

    def p(x, d=1):
        return "-" if x is None else f"{x * 100:.{d}f}%"

    def both(a, b, f=p):
        return f"{y0}년 {f(a)} / {y1}년 {f(b)}"

    def up(a, b):
        return None if a is None or b is None else a > b

    items = []

    def add(group, name, rule, ok, shown):
        items.append({"구분": group, "항목": name, "기준": rule, "실제 수치": shown,
                      "판정": "확인 불가" if ok is None else ("통과" if ok else "미통과")})

    r0, r1 = roa(cur), roa(prev)
    add("수익성", "순이익 흑자(ROA)", "ROA > 0", None if r0 is None else r0 > 0, f"{y0}년 {p(r0)}")
    ocf, ni = cur.get("ocf"), cur.get("ni")
    add("수익성", "영업현금흐름 흑자", "영업CF > 0", None if ocf is None else ocf > 0, f"{y0}년 {eok(ocf)}억원")
    add("수익성", "ROA 개선", "전년보다 높음", up(r0, r1), both(r0, r1))
    add("수익성", "이익의 질", "영업CF > 순이익", None if None in (ocf, ni) else ocf > ni,
        f"영업CF {eok(ocf)}억 / 순이익 {eok(ni)}억")

    l0, l1 = leverage(cur), leverage(prev)
    add("재무 건전성", "장기부채 비율 감소", "비유동부채 ÷ 자산총계가 늘지 않음",
        None if None in (l0, l1) else l0 <= l1, both(l0, l1))
    q0, q1 = liquidity(cur), liquidity(prev)
    add("재무 건전성", "유동비율 개선", "전년보다 높음", up(q0, q1), both(q0, q1, lambda x: p(x, 0)))
    s0, s1 = cur.get("shares") or {}, prev.get("shares") or {}
    a, b = s1.get("issued"), s0.get("issued")
    if not a or not b or s0.get("basis") != s1.get("basis"):
        add("재무 건전성", "신주 발행 없음", "발행주식수가 늘지 않음", None, "주식수 자료 확인 불가")
    else:
        split = b > a and _split_like(a, b)
        add("재무 건전성", "신주 발행 없음", "발행주식수가 늘지 않음", b <= a or split,
            f"{y1}년 {a:,.0f}주 → {y0}년 {b:,.0f}주" + (" (액면분할·무상증자로 추정)" if split else ""))

    m0, m1 = margin(cur), margin(prev)
    add("효율성", "매출총이익률 개선", "전년보다 높음", up(m0, m1), both(m0, m1))
    t0, t1 = turnover(cur), turnover(prev)
    add("효율성", "자산회전율 개선", "전년보다 높음", up(t0, t1),
        both(t0, t1, lambda x: "-" if x is None else f"{x:.3f}회"))

    return {"year": y0, "prev": y1, "items": items,
            "score": sum(i["판정"] == "통과" for i in items),
            "count": sum(i["판정"] != "확인 불가" for i in items)}


def fscore_label(fs):
    n = len(fs["items"])
    return f"{fs['score']}/{fs['count']}점" + (f" ({n - fs['count']}개 항목 계산 불가)" if fs["count"] < n else "")


FSCORE_NOTE = ("피오트로스키는 8~9점을 재무 체력이 좋아지는 회사, 0~2점을 약해지는 회사로 봤습니다. "
               "숙향 판정에는 반영하지 않는 참고 점수입니다. ROA·자산회전율은 기초(전년말) 자산총계 기준, "
               "순이익은 지배주주 순이익, 장기부채는 비유동부채 전체를 썼습니다.")


# ───────────────────────── 리포트(Markdown) ─────────────────────────

def won(x, d=0):
    return "확인 불가" if x is None else f"{x:,.{d}f}원"


def eok(x):
    return "-" if x is None else f"{x / 1e8:,.0f}"


def pct(x):
    return "확인 불가" if x is None else f"{x:,.1f}%"


def ttm_summary(res):
    """10년 재무 표 끝에 붙일 최근 12개월(TTM) 칸. 최신 분기보고서가 없으면 None."""
    q = res.get("quarter")
    if not q or q["ttm_ni"] is None:
        return None
    t = res.get("ttm") or {}
    eps = t["eps"][0] if t.get("eps") else q["ttm_eps"]
    bps = t.get("bps")
    rev, op = q.get("ttm_revenue"), q.get("ttm_op")
    return {"label": f"{res['latest'] + 1} TTM", "revenue": rev, "op": op, "ni": q["ttm_ni"],
            "eps": eps, "bps": bps,
            "roe": eps / bps * 100 if eps is not None and bps else None,
            "op_margin": op / rev * 100 if op is not None and rev else None,
            "net_cash": q["net_cash"]["net_cash"] if q.get("net_cash") else None}


def dividend_history(res):
    """연도별 배당 내역(최근 연도부터)과 요약 문장. 사업보고서 배당 공시만 사용(추가 조회 없음)."""
    price = res.get("price")
    years = [y for y in res["years"] if not res["recs"][y].get("missing")]
    rows = []
    for y in reversed(years):
        r = res["recs"][y]
        dps = r.get("dps")
        prev = res["recs"].get(y - 1, {}).get("dps")
        chg = (dps / prev - 1) * 100 if dps is not None and prev else None
        rows.append({"year": y, "dps": dps, "change": chg, "payout": r.get("payout"),
                     "yield": r.get("div_yield"),
                     "yield_now": dps / price * 100 if dps is not None and price else None,
                     "total": r.get("div_total")})
    def cut(x, older):
        """감액: 주당배당금이 줄고, 배당금 총액도 줄었을 때 (액면분할로 주당배당금만 준 경우 제외)"""
        if x["dps"] is None or not older["dps"] or x["dps"] >= older["dps"]:
            return False
        return not (x["total"] and older["total"] and x["total"] >= older["total"])

    paid = [x for x in rows if x["dps"]]
    streak = 0
    for i, x in enumerate(rows):  # 최근 연도부터 배당이 있고 감액이 없던 해 수
        if not x["dps"]:
            break
        streak += 1
        if i + 1 >= len(rows) or not rows[i + 1]["dps"] or cut(x, rows[i + 1]):
            break
    cuts = [x["year"] for i, x in enumerate(rows) if i + 1 < len(rows) and cut(x, rows[i + 1])]
    splits = [x["year"] for i, x in enumerate(rows) if i + 1 < len(rows) and x["dps"] is not None
              and rows[i + 1]["dps"] and x["dps"] < rows[i + 1]["dps"] and not cut(x, rows[i + 1])]
    summary = (f"최근 {len(rows)}년 중 {len(paid)}년 배당"
               + (f", 최근 {streak}년 연속 배당(감액 없음)" if streak else ", 최근 연도 배당 없음")
               + (f", 감액 {len(cuts)}회({', '.join(map(str, sorted(cuts)))}년)" if cuts else ", 감액 없음")
               + (f" · {', '.join(map(str, sorted(splits)))}년은 주당배당금이 줄었지만 배당금 총액은 늘어 "
                  "액면분할·주식수 변화로 보고 감액에서 뺐습니다" if splits else ""))
    return rows, summary


def build_report(res, grade=None, memo=""):
    c, L = res["corp"], res["latest"]
    e0, e1, e2 = res["eps"]
    fs = FS_NAME[res["fs"]]
    lines = [f"# {c['corp_name']}({c['stock_code']}) 숙향식 가치분석 리포트", "",
             "## 분석 기준", "",
             f"- 분석 기준일: {res['today']}",
             f"- 주가: {won(res['price'])} (기준일 {res['price_date'] or '확인 불가'}, 출처 {res['price_src']})",
             f"- 재무제표: {L - 9}~{L}년 사업보고서, {fs}"
             + (f" / 최신: {res['quarter']['label']}" if res["quarter"] else ""),
             f"- 배당금 기준연도: {L}년 사업보고서(확정 현금배당, 보통주)",
             f"- 예금금리: {str(res['deposit_rate']) + '%' if res['deposit_rate'] else '미입력'}"
             + (f" (조회일 {res['rate_date']})" if res.get("rate_date") else ""),
             f"- 주식수 기준: {'연도별 자사주 제외 주식수' if res['share_basis'] == 'year' else f'{L}년 자사주 제외 주식수 일괄 적용'}",
             f"- 순현금 리스부채: {'포함' if res['include_lease'] else '제외'}",
             f"- 내재가치 기준: {res['basis_name']}", ""]

    info = res["info"]
    lines += ["## 1. 기업 개요", "",
              f"- 대표자: {info.get('ceo_nm', '확인 불가')} / 설립일: {info.get('est_dt', '확인 불가')} / "
              f"결산월: {info.get('acc_mt', '확인 불가')}월 / 업종코드: {info.get('induty_code', '확인 불가')}",
              "- 사업 구조는 사업보고서 'II. 사업의 내용'에서 확인하세요.", ""]

    lines += ["## 2. 최근 10년 재무 요약 (금액: 억원, 주당: 원, 최근 연도부터)", "",
              "| 연도 | 매출액 | 영업이익 | 순이익 | EPS | BPS | ROE | 영업CF | FCF | 주당배당 | 순현금 |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    t = ttm_summary(res)
    if t:
        lines.append("| {} | {} | {} | {} | {} | {} | {} | - | - | - | {} |".format(
            f"**{t['label']}**", eok(t["revenue"]), eok(t["op"]), eok(t["ni"]),
            "-" if t["eps"] is None else f"{t['eps']:,.0f}", "-" if t["bps"] is None else f"{t['bps']:,.0f}",
            "-" if t["roe"] is None else f"{t['roe']:.1f}%", eok(t["net_cash"])))
    for y in reversed(res["years"]):
        r = res["recs"][y]
        if r.get("missing"):
            lines.append(f"| {y} | 자료 없음 |||||||||| ")
            continue
        lines.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
            y, eok(r.get("revenue")), eok(r.get("op")), eok(r.get("ni")),
            "-" if r.get("eps") is None else f"{r['eps']:,.0f}",
            "-" if r.get("bps") is None else f"{r['bps']:,.0f}",
            "-" if r.get("roe") is None else f"{r['roe']:.1f}%",
            eok(r.get("ocf")), eok(r.get("fcf")),
            "-" if r.get("dps") is None else f"{r['dps']:,.0f}", eok(r.get("net_cash"))))
    if t:
        lines += ["", f"{t['label']}: {res['quarter']['label']}까지 반영한 최근 12개월 실적, "
                      "BPS·순현금은 최신 분기말 기준"]

    drows, dsum = dividend_history(res)
    num = lambda v, f: "-" if v is None else format(v, f)  # noqa: E731
    lines += ["", "### 배당 내역 (보통주, 최근 연도부터)", "", dsum, "",
              "| 연도 | 주당배당금 | 전년 대비 | 배당성향 | 시가배당률(공시) | 현재가 기준 수익률 | 배당금 총액(억원) |",
              "|---|---|---|---|---|---|---|"]
    for x in drows:
        lines.append(f"| {x['year']} | {num(x['dps'], ',.0f')} | {num(x['change'], '+.1f')}{'%' if x['change'] is not None else ''} | "
                     f"{num(x['payout'], '.1f')} | {num(x['yield'], '.2f')} | {num(x['yield_now'], '.2f')} | {eok(x['total'])} |")
    lines += ["", "- 시가배당률은 회사가 공시한 값(배당기준일 무렵 주가 기준), 현재가 기준 수익률은 그 해 주당배당금 ÷ 현재 주가입니다.",
              "- 배당성향·시가배당률 단위는 %입니다. 특별배당 여부는 사업보고서에서 확인하세요."]

    lines += ["", "## 3~6. 숙향의 네 가지 투자조건", "", "| 조건 | 실제 수치 | 기준 | 판정 |", "|---|---|---|---|"]
    for row in res["conditions"]:
        lines.append(f"| {row['조건']} | {row['실제 수치']} | {row['기준']} | {row['판정']} |")
    nc = res["net_cash_detail"]
    if nc:
        lines += ["", f"순현금 계산({res['net_cash_basis']}): 현금성·금융자산 {eok(nc['cash'])}억원 − "
                      f"이자부 금융부채 {eok(nc['debt'])}억원"
                      + (f" − 리스부채 {eok(nc['lease'])}억원" if nc["include_lease"] else
                         f" (리스부채 {eok(nc['lease'])}억원 제외)")
                      + f" = {eok(nc['net_cash'])}억원",
                  "- 포함한 자산: " + (", ".join(f"{n} {eok(v)}억" for n, v in nc["cash_items"]) or "없음"),
                  "- 포함한 부채: " + (", ".join(f"{n} {eok(v)}억" for n, v in nc["debt_items"]) or "없음"),
                  "- 자동 분류 결과입니다. 금융자산의 사용 제한 여부는 주석에서 확인하세요."]

    sh = res["shares"] or {}
    l0, l1, l2 = res["eps_labels"]
    lines += ["", f"## 7~11. 숙향식 내재가치 ({res['basis_name']})", "",
              "| 항목 | 결과 |", "|---|---|",
              f"| 현재 주가 | {won(res['price'])} |",
              f"| {l0} | {won(e0)} |", f"| {l1} | {won(e1)} |", f"| {l2} | {won(e2)} |",
              f"| 가중평균 EPS | {won(res['w_eps'])} |", f"| {res['bps_label']} | {won(res['bps'])} |",
              f"| 숙향식 내재가치 | {won(res['iv'])} |",
              f"| 내재가치 대비 할인율 | {pct(res['discount'])} |",
              f"| 내재가치까지 상승여력 | {pct(res['upside'])} |"]
    cons = res["conservative"]
    lines.append(f"| 보수적 참고가치 | {won(cons['value']) if cons else '계산 불가'} |")
    alt = res.get("alt")
    if alt:
        lines.append(f"| 참고: {alt['name']} 내재가치 | {won(alt['iv'])} |")
    lines += ["", "계산 과정", ""]
    if res["basis_name"].startswith("최근 4분기"):
        lines.append("- 구간별 EPS = (사업연도 순이익 + 다음 해 같은 분기까지 누적 순이익 − 그 해 같은 분기까지 누적 순이익)"
                     " ÷ 분기말 자사주 제외 주식수. 구간이 겹치지 않게 1년씩 밀어 3개 구간을 구함")
    if sh:
        lines.append(f"- 조정주식수 = 발행주식수 {sh['issued']:,.0f} − 자기주식 {sh['treasury']:,.0f} "
                     f"= {sh['adjusted']:,.0f}주 ({sh['basis']}, {L}년말)")
    if res["w_eps"] is not None:
        lines.append(f"- 가중평균 EPS = ({e0:,.0f}×3 + {e1:,.0f}×2 + {e2:,.0f}) ÷ 6 = {res['w_eps']:,.0f}원")
    if res["iv"] is not None:
        lines.append(f"- 내재가치 = ({res['bps']:,.0f} + {res['w_eps']:,.0f}×10) ÷ 2 = {res['iv']:,.0f}원")
    if res["discount"] is not None:
        lines.append(f"- 할인율 = ({res['iv']:,.0f} − {res['price']:,.0f}) ÷ {res['iv']:,.0f} × 100 = {res['discount']:.1f}%")
    if res["upside"] is not None:
        lines.append(f"- 상승여력 = ({res['iv']:,.0f} − {res['price']:,.0f}) ÷ {res['price']:,.0f} × 100 = {res['upside']:.1f}%")
    if cons:
        lines.append(f"- 보수적 참고가치: {cons['method']}. 평균 EPS {cons['avg_eps']:,.0f}원 → {cons['value']:,.0f}원")

    lines += ["", "## 12. 장기 실적 검증 (검토 신호)", ""] + [f"- {s}" for s in res["risks"]]
    if res["notes"]:
        lines += ["", "자료 관련 참고사항", ""] + [f"- {n}" for n in res["notes"]]

    fs_ = res.get("fscore")
    lines += ["", "## 참고. 피오트로스키 F-Score (숙향 판정과 별개)", ""]
    if fs_:
        lines += [f"- 점수: **{fscore_label(fs_)}** ({fs_['year']}년과 {fs_['prev']}년 사업보고서 비교)", "",
                  "| 구분 | 항목 | 기준 | 실제 수치 | 판정 |", "|---|---|---|---|---|"]
        lines += [f"| {i['구분']} | {i['항목']} | {i['기준']} | {i['실제 수치']} | {i['판정']} |" for i in fs_["items"]]
        lines += ["", f"- {FSCORE_NOTE}"]
    else:
        lines.append("- 전년 사업보고서 자료가 없어 계산하지 못했습니다.")

    lines += ["", "## 13. 종합 투자 판단", "",
              f"- 최종 등급(사용자 판단): **{grade or '미정'}**"]
    if memo.strip():
        lines += ["- 판단 근거:", "", memo.strip()]
    lines += ["", "매매 원칙(지침 6~8장): 분할매수·분할매도를 기본으로 하며, 숙향이 제시하지 않은 고정 매수가격·"
                  "매도가격·손절률·분할 횟수는 설정하지 않는다. 주가 변동만으로 자동 매도·추가매수하지 않고 "
                  "기업가치를 재검토한 뒤 대응한다.", "",
              "## 자료 출처", ""]
    for name, rno in res["sources"]:
        lines.append(f"- {name}: https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rno}")
    lines += ["- 재무·주식수·배당 수치: 금융감독원 OpenDART API", f"- 주가: {res['price_src']}", "",
              "_이 리포트는 공시 자료를 자동 집계한 개인 분석용 자료이며, 수치는 원문 공시로 확인해야 합니다._"]
    return "\n".join(lines)
