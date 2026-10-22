"""OpenDART 데이터 수집, 재무제표 파싱, 주가 조회."""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
import re
import time
import zipfile
import xml.etree.ElementTree as ET

import requests

BASE_URL = "https://opendart.fss.or.kr/api"
REPRT_FY, REPRT_H1, REPRT_Q1, REPRT_Q3 = "11011", "11012", "11013", "11014"
REPRT_NAME = {
    REPRT_FY: "사업보고서",
    REPRT_H1: "반기보고서",
    REPRT_Q1: "1분기보고서",
    REPRT_Q3: "3분기보고서",
}
FS_NAME = {"CFS": "연결재무제표", "OFS": "별도재무제표"}
# 실행 위치와 상관없이 프로그램 폴더 안에 캐시를 둔다
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".dart_cache")
CACHE_TTL = 90 * 86400  # 지난 연도 자료도 90일마다 다시 받아 정정공시를 반영


class DartError(RuntimeError):
    pass


# ───────────────────────── DART 클라이언트 ─────────────────────────

class DartClient:
    def __init__(self, api_key: str, cache_dir: str = CACHE_DIR):
        if not api_key or not api_key.strip():
            raise DartError("DART API 인증키가 없습니다. 사이드바에 입력하세요.")
        self.key = api_key.strip()
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)

    def _cache_path(self, endpoint, params):
        raw = endpoint + json.dumps(params, sort_keys=True)
        return os.path.join(self.cache_dir, hashlib.md5(raw.encode()).hexdigest() + ".json")

    def _get(self, endpoint, params, cacheable=False):
        """status 000이면 data, 013(자료 없음)이면 None, 그 외는 DartError."""
        path = self._cache_path(endpoint, params)
        if cacheable and os.path.exists(path) and time.time() - os.path.getmtime(path) < CACHE_TTL:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        query = dict(params, crtfc_key=self.key)
        data = None
        for attempt in range(3):
            try:
                r = requests.get(f"{BASE_URL}/{endpoint}", params=query, timeout=30)
                r.raise_for_status()
                data = r.json()
                break
            except (requests.RequestException, ValueError) as e:
                if attempt == 2:
                    raise DartError(f"DART 요청 실패({endpoint}): {e}")
                time.sleep(1.5)
        status = data.get("status")
        if status == "013":
            return None
        if status != "000":
            raise DartError(f"DART 오류 {status}: {data.get('message')}")
        if cacheable:  # 지난 연도의 확정 자료만 디스크에 보관
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        return data

    # 종목명 → 고유번호
    def corp_list(self, refresh=False):
        path = os.path.join(self.cache_dir, "corp_codes.json")
        fresh = os.path.exists(path) and (time.time() - os.path.getmtime(path) < 7 * 86400)
        if fresh and not refresh:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        for attempt in range(3):
            try:
                r = requests.get(f"{BASE_URL}/corpCode.xml", params={"crtfc_key": self.key}, timeout=60)
                r.raise_for_status()
                break
            except requests.RequestException as e:
                if attempt == 2:
                    raise DartError(f"기업코드 목록 요청 실패(인터넷 연결을 확인하세요): {e}")
                time.sleep(1.5)
        try:
            z = zipfile.ZipFile(io.BytesIO(r.content))
        except zipfile.BadZipFile:
            raise DartError(f"기업코드 목록을 받지 못했습니다: {r.text[:200]}")
        root = ET.fromstring(z.read(z.namelist()[0]))
        items = []
        for el in root.iter("list"):
            sc = (el.findtext("stock_code") or "").strip()
            if sc:  # 상장사만
                items.append({
                    "corp_code": (el.findtext("corp_code") or "").strip(),
                    "corp_name": (el.findtext("corp_name") or "").strip(),
                    "stock_code": sc,
                })
        with open(path, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False)
        return items

    def search(self, name):
        name = name.strip()
        items = self.corp_list()
        if re.fullmatch(r"\d{6}", name):
            return [c for c in items if c["stock_code"] == name]
        exact = [c for c in items if c["corp_name"] == name]
        if exact:
            return exact
        key = _clean(name).lower()
        return [c for c in items if key in _clean(c["corp_name"]).lower()][:30]

    def company(self, corp_code):
        return self._get("company.json", {"corp_code": corp_code})

    def fin_all(self, corp_code, year, reprt, fs_div):
        cacheable = year < dt.date.today().year  # 지난 연도 보고서(분기 포함)는 확정 자료
        d = self._get("fnlttSinglAcntAll.json",
                      {"corp_code": corp_code, "bsns_year": str(year),
                       "reprt_code": reprt, "fs_div": fs_div}, cacheable)
        return d["list"] if d else None

    def stock_total(self, corp_code, year, reprt=REPRT_FY):
        cacheable = year < dt.date.today().year
        d = self._get("stockTotqySttus.json",
                      {"corp_code": corp_code, "bsns_year": str(year), "reprt_code": reprt}, cacheable)
        return d["list"] if d else None

    def dividend(self, corp_code, year, reprt=REPRT_FY):
        cacheable = reprt == REPRT_FY and year < dt.date.today().year
        d = self._get("alotMatter.json",
                      {"corp_code": corp_code, "bsns_year": str(year), "reprt_code": reprt}, cacheable)
        return d["list"] if d else None


# ───────────────────────── 파싱 도우미 ─────────────────────────

def to_num(v):
    if v is None:
        return None
    s = str(v).replace(",", "").replace(" ", "").strip()
    if s in ("", "-", "N/A"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    try:
        x = float(s)
    except ValueError:
        return None
    return -x if neg else x


def _clean(s):
    return re.sub(r"[\s·]", "", s or "")


def _ord(r):
    try:
        return int(r.get("ord") or 0)
    except ValueError:
        return 0


def find_value(rows, sj_divs, ids=(), exact=(), prefix=(), exclude=(), fields=("thstrm_amount",)):
    """표준계정 ID → 계정명 일치 → 계정명 시작 순서로 찾는다. (값, 행) 반환."""
    def val(r):
        for f in fields:
            v = to_num(r.get(f))
            if v is not None:
                return v
        return None

    for sj in sj_divs:
        cand = sorted([r for r in rows if r.get("sj_div") == sj], key=_ord)
        for i in ids:
            for r in cand:
                if r.get("account_id") == i and val(r) is not None:
                    return val(r), r
        ex = [_clean(e) for e in exact]
        for r in cand:
            nm = _clean(r.get("account_nm"))
            if nm in ex and not any(x in nm for x in exclude) and val(r) is not None:
                return val(r), r
        for r in cand:
            nm = _clean(r.get("account_nm"))
            if any(nm.startswith(_clean(p)) for p in prefix) and not any(x in nm for x in exclude) \
                    and val(r) is not None:
                return val(r), r
    return None, None


ACCOUNTS = {
    "revenue": dict(sj_divs=("IS", "CIS"), ids=("ifrs-full_Revenue",),
                    exact=("매출액", "수익(매출액)", "영업수익", "매출", "수익"),
                    prefix=("매출액", "영업수익"), exclude=("원가", "총이익")),
    "op": dict(sj_divs=("IS", "CIS"), ids=("dart_OperatingIncomeLoss",),
               exact=("영업이익", "영업이익(손실)", "영업손익"), prefix=("영업이익", "영업손익")),
    "ni_owner": dict(sj_divs=("IS", "CIS"), ids=("ifrs-full_ProfitLossAttributableToOwnersOfParent",),
                     exact=("지배기업의소유주에게귀속되는당기순이익", "지배기업의소유주에게귀속되는당기순이익(손실)",
                            "지배기업소유주지분", "지배기업의소유주지분"),
                     prefix=("지배기업의소유주에게귀속되는당기순",), exclude=("포괄",)),
    "ni_total": dict(sj_divs=("IS", "CIS"), ids=("ifrs-full_ProfitLoss",),
                     exact=("당기순이익", "당기순이익(손실)", "당기순손익"),
                     prefix=("당기순이익",), exclude=("포괄", "귀속", "주당")),
    "equity_owner": dict(sj_divs=("BS",), ids=("ifrs-full_EquityAttributableToOwnersOfParent",),
                         exact=("지배기업소유주지분", "지배기업의소유주에게귀속되는자본", "지배기업의소유주지분"),
                         prefix=("지배기업의소유주에게귀속되는자본",)),
    "equity_total": dict(sj_divs=("BS",), ids=("ifrs-full_Equity",), exact=("자본총계",), prefix=("자본총계",)),
    "eps_reported": dict(sj_divs=("IS", "CIS"), ids=("ifrs-full_BasicEarningsLossPerShare",),
                         exact=("기본주당이익", "기본주당이익(손실)", "기본주당순이익", "기본주당순이익(손실)"),
                         prefix=("기본주당",), exclude=("희석",)),
    "ocf": dict(sj_divs=("CF",), ids=("ifrs-full_CashFlowsFromUsedInOperatingActivities",),
                exact=("영업활동현금흐름", "영업활동으로인한현금흐름"), prefix=("영업활동",)),
    "capex": dict(sj_divs=("CF",), ids=("ifrs-full_PurchaseOfPropertyPlantAndEquipment",),
                  exact=("유형자산의취득", "유형자산취득"), prefix=("유형자산의취득",)),
    "receivables": dict(sj_divs=("BS",),
                        ids=("ifrs-full_CurrentTradeReceivables", "dart_ShortTermTradeReceivable",
                             "ifrs-full_TradeAndOtherCurrentReceivables"),
                        exact=("매출채권", "매출채권및기타채권", "매출채권및기타유동채권"),
                        prefix=("매출채권",), exclude=("장기", "비유동")),
    "inventory": dict(sj_divs=("BS",), ids=("ifrs-full_Inventories",), exact=("재고자산",), prefix=("재고자산",)),
}


def extract(rows, key, fields=("thstrm_amount",)):
    return find_value(rows, fields=fields, **ACCOUNTS[key])[0]


# 순현금 = 현금성자산 + 현금화 가능한 금융자산 - 이자부 금융부채
SECTION_IDS = {
    "ifrs-full_CurrentAssets": "CA", "ifrs-full_NoncurrentAssets": "NCA", "ifrs-full_Assets": None,
    "ifrs-full_CurrentLiabilities": "CL", "ifrs-full_NoncurrentLiabilities": "NCL",
    "ifrs-full_Liabilities": None, "ifrs-full_Equity": None,
}
SECTION_NAMES = {"유동자산": "CA", "비유동자산": "NCA", "자산총계": None, "유동부채": "CL",
                 "비유동부채": "NCL", "부채총계": None, "자본총계": None}
CASH_KW = ("현금및현금성자산", "단기금융상품", "단기투자자산", "단기매매금융자산", "단기금융자산",
           "당기손익-공정가치측정금융자산", "당기손익공정가치측정금융자산")
DEBT_KW = ("차입금", "사채", "유동성장기부채")
LEASE_KW = ("리스부채",)
DEBT_EXCLUDE = ("할인", "할증", "전환권", "이자", "보증")


def parse_net_cash(rows, include_lease=False):
    bs = sorted([r for r in rows if r.get("sj_div") == "BS"], key=_ord)
    section, seen_header = None, False
    cash, debt, lease, seen = [], [], [], set()
    for r in bs:
        nm = _clean(r.get("account_nm"))
        aid = r.get("account_id")
        if aid in SECTION_IDS or nm in SECTION_NAMES:
            section = SECTION_IDS.get(aid, SECTION_NAMES.get(nm))
            seen_header = True
            continue
        v = to_num(r.get("thstrm_amount"))
        if v is None or (nm, v) in seen:
            continue
        in_ca = section == "CA" or not seen_header
        in_liab = section in ("CL", "NCL") or not seen_header
        if in_ca and any(k in nm for k in CASH_KW):
            cash.append((r.get("account_nm"), v)); seen.add((nm, v))
        elif in_liab and any(k in nm for k in LEASE_KW):
            lease.append((r.get("account_nm"), v)); seen.add((nm, v))
        elif in_liab and any(k in nm for k in DEBT_KW) and not any(x in nm for x in DEBT_EXCLUDE):
            debt.append((r.get("account_nm"), v)); seen.add((nm, v))
    if not cash and not debt:
        return None
    c, d, l = sum(v for _, v in cash), sum(v for _, v in debt), sum(v for _, v in lease)
    return {
        "cash_items": cash, "debt_items": debt, "lease_items": lease,
        "cash": c, "debt": d, "lease": l, "include_lease": include_lease,
        "net_cash": c - d - (l if include_lease else 0),
    }


def parse_shares(rows):
    if not rows:
        return None
    by_se = {_clean(r.get("se")): r for r in rows}
    for label, basis in (("합계", "보통주+우선주 합계"), ("보통주", "보통주")):
        r = by_se.get(label)
        if r:
            issued = to_num(r.get("istc_totqy"))
            if issued:
                treasury = to_num(r.get("tesstk_co")) or 0
                return {"issued": issued, "treasury": treasury,
                        "adjusted": issued - treasury, "basis": basis}
    return None


def parse_dividend(rows):
    out = {"dps": None, "payout": None, "total_mil": None}
    for r in rows or []:
        se, kind = _clean(r.get("se")), _clean(r.get("stock_knd"))
        v = to_num(r.get("thstrm"))
        if "주당현금배당금" in se and kind in ("", "-", "보통주") and out["dps"] is None:
            out["dps"] = v
        elif "현금배당성향" in se and out["payout"] is None:
            out["payout"] = v
        elif "현금배당금총액" in se and out["total_mil"] is None:
            out["total_mil"] = v
    return out


# ───────────────────────── 주가 ─────────────────────────

def get_price(stock_code):
    """(종가, 기준일, 출처) — 실패하면 (None, None, 오류내용)."""
    errors = []
    start = dt.date.today() - dt.timedelta(days=20)
    try:
        import FinanceDataReader as fdr
        df = fdr.DataReader(stock_code, start.isoformat()).dropna(subset=["Close"])
        if len(df):
            return float(df["Close"].iloc[-1]), df.index[-1].date().isoformat(), "FinanceDataReader"
    except Exception as e:  # noqa: BLE001
        errors.append(f"FinanceDataReader: {e}")
    try:
        from pykrx import stock
        df = stock.get_market_ohlcv(start.strftime("%Y%m%d"), dt.date.today().strftime("%Y%m%d"), stock_code)
        df = df[df["종가"] > 0]
        if len(df):
            return float(df["종가"].iloc[-1]), df.index[-1].date().isoformat(), "pykrx(한국거래소)"
    except Exception as e:  # noqa: BLE001
        errors.append(f"pykrx: {e}")
    return None, None, "; ".join(errors) or "주가 데이터 없음"
