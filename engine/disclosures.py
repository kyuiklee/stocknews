"""DART 공시 모음 — 자사주(시장 전체)와 투자자문·운용사 지분 공시

site/data/disc.json 을 만듭니다.
- 자사주: 주요사항보고서 중 자기주식 취득·취득신탁·신탁해지·처분 결정 + 거래소 공시의 주식소각 결정
  취득·신탁·처분은 DART 상세 API로 금액·기간·목적을 붙이고 시가총액 대비 비율을 계산합니다.
- 투자자문: 주식등의대량보유상황보고서(5% 이상) 중 advisors.json 의 운용사가 제출한 것
  majorstock API로 보유비율과 증감을 붙입니다.
처음 실행할 때는 자사주 90일, 지분 공시 1년치를 한 번에 받고, 그 뒤로는 지난 실행 이후 것만 받습니다.
"""
import datetime as dt
import json
import os
import re
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "site", "data", "disc.json")
CACHE = os.path.join(ROOT, "cache")
API = "https://opendart.fss.or.kr/api"
CFG = {
    "bb_backfill_days": 90,     # 자사주 처음 받을 기간
    "bb_keep_days": 120,        # 자사주 보관 기간
    "so_backfill_days": 30,     # 소각(거래소 공시, 양이 많음) 처음 받을 기간
    "st_backfill_days": 365,    # 지분 공시 처음 받을 기간
    "st_keep_days": 730,        # 지분 공시 보관 기간
    "overlap_days": 2,          # 지난 실행과 겹쳐 받는 날 수 (늦게 올라온 공시 대비)
}
STATUS = {}
DEBUG = {}
SEEN = ""            # 이번 실행 시각 — 새로 들어온 공시 표시용


def _st(k, ok, n=0, err=None):
    s = STATUS.setdefault(k, {"ok": 0, "fail": 0, "items": 0, "err": None})
    if ok:
        s["ok"] += 1
        s["items"] += n
    else:
        s["fail"] += 1
        s["err"] = err


def num(v):
    if v is None:
        return None
    s = re.sub(r"[^0-9.\-]", "", str(v))
    if s in ("", "-", ".", "-."):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def ymd(v):
    """'2026년 10월 21일', '2026-10-21', '20261021' → '2026-10-21'"""
    if not v:
        return None
    m = re.search(r"(\d{4})\D{0,3}(\d{1,2})\D{0,3}(\d{1,2})", str(v))
    if not m:
        return None
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def norm_name(s):
    s = re.sub(r"\(주\)|㈜|주식회사|\s", "", s or "")
    return s.upper()


class Dart:
    def __init__(self, key):
        self.key = key
        self.calls = 0
        self.limit_hit = False

    def get(self, path, tag, **params):
        if self.limit_hit:
            return None
        params["crtfc_key"] = self.key
        err = None
        for attempt in range(3):
            try:
                self.calls += 1
                j = requests.get(f"{API}/{path}", params=params, timeout=40).json()
                st = j.get("status")
                if st in ("000", "013"):           # 013 = 결과 없음
                    return j
                if st == "020":                     # 요청 한도 초과
                    self.limit_hit = True
                    err = "DART 하루 요청 한도 초과"
                    break
                err = f"{st} {j.get('message')}"
                if st in ("010", "011", "012", "100", "101"):
                    break
            except Exception as e:
                err = str(e)[:120]
            time.sleep(1.5 * (attempt + 1))
        _st(tag, False, err=err)
        print(f"  ! DART {path} 실패: {err}")
        return None

    def list_all(self, tag, start, end, **params):
        """공시검색 — 기간 3개월 제한이 있어 89일씩 나눠 받음"""
        out = []
        s = start
        while s <= end:
            e = min(end, s + dt.timedelta(days=88))
            page = 1
            while True:
                j = self.get("list.json", tag, bgn_de=s.strftime("%Y%m%d"), end_de=e.strftime("%Y%m%d"),
                             page_no=page, page_count=100, sort="date", sort_mth="desc", **params)
                if j is None:
                    return out, False
                rows = j.get("list") or []
                out += rows
                tp = int(j.get("total_page") or 1)
                if page >= tp or not rows:
                    break
                page += 1
                time.sleep(0.15)
            s = e + dt.timedelta(days=1)
        _st(tag, True, len(out))
        return out, True


# ---------------------------------------------------------------- 자사주
BB_KINDS = [  # (보고서명에 들어있는 낱말, 구분, 상세 API)
    ("자기주식취득신탁계약해지결정", "신탁해지", "tsstkAqTrctrCcDecsn.json"),
    ("자기주식취득신탁계약체결결정", "신탁", "tsstkAqTrctrCnsDecsn.json"),
    ("자기주식취득결정", "취득", "tsstkAqDecsn.json"),
    ("자기주식처분결정", "처분", "tsstkDpDecsn.json"),
]


def bb_kind(report_nm):
    t = re.sub(r"\s", "", report_nm)
    for kw, kind, api in BB_KINDS:
        if kw in t:
            return kind, api
    if "소각" in t and ("주식소각결정" in t or "자기주식소각" in t):
        return "소각", None
    return None, None


def pick(d, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", "-"):
            return re.sub(r"\s+", " ", str(v)).strip()        # 줄바꿈 섞인 값 정리
    return None


def bb_detail(kind, d):
    """상세 API 한 건 → 금액·주식수·기간·목적"""
    if kind == "취득":
        amt = (num(d.get("aqpln_prc_ostk")) or 0) + (num(d.get("aqpln_prc_estk")) or 0)
        qty = (num(d.get("aqpln_stk_ostk")) or 0) + (num(d.get("aqpln_stk_estk")) or 0)
        return {"amount": amt or None, "shares": qty or None,
                "from": ymd(d.get("aqexpd_bgd")), "to": ymd(d.get("aqexpd_edd")),
                "purpose": pick(d, "aq_pp"), "method": pick(d, "aq_mth"), "agent": pick(d, "cs_iv_bk"),
                "board": ymd(d.get("aq_dd") or d.get("bddd"))}
    if kind in ("신탁", "신탁해지"):
        amt = num(pick(d, "ctr_prc", "ctr_prc_bfcc", "ctr_prc_atcc"))
        return {"amount": amt, "from": ymd(pick(d, "ctr_pd_bgd", "ctr_pd_bfcc_bgd")),
                "to": ymd(pick(d, "ctr_pd_edd", "ctr_pd_bfcc_edd")),
                "purpose": pick(d, "ctr_pp", "cc_pp"), "agent": pick(d, "ctr_cns_int", "cc_int"),
                "board": ymd(d.get("bddd"))}
    if kind == "처분":
        amt = (num(d.get("dppln_prc_ostk")) or 0) + (num(d.get("dppln_prc_estk")) or 0)
        qty = (num(d.get("dppln_stk_ostk")) or 0) + (num(d.get("dppln_stk_estk")) or 0)
        how = [lbl for k, lbl in (("dp_m_mkt", "장내"), ("dp_m_ovtm", "시간외"), ("dp_m_otc", "장외"), ("dp_m_etc", "기타"))
               if num(d.get(k))]
        return {"amount": amt or None, "shares": qty or None,
                "from": ymd(d.get("dpprpd_bgd")), "to": ymd(d.get("dpprpd_edd")),
                "purpose": pick(d, "dp_pp"), "method": "·".join(how) or None, "board": ymd(d.get("bddd"))}
    return {}


def marcaps():
    p = os.path.join(CACHE, "krx_marcap.json")
    if os.path.exists(p) and time.time() - os.path.getmtime(p) < 20 * 3600:
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            pass
    m = {}
    try:
        import FinanceDataReader as fdr
        df = fdr.StockListing("KRX")
        for code, cap in zip(df["Code"], df["Marcap"]):
            if cap == cap and cap:
                m[str(code)] = float(cap)
        os.makedirs(CACHE, exist_ok=True)
        json.dump(m, open(p, "w"))
    except Exception as e:
        print(f"  시가총액 받기 실패: {e}")
        if os.path.exists(p):
            m = json.load(open(p, encoding="utf-8"))
    return m


def buybacks(dart, prev, today):
    known = {b["id"]: b for b in prev.get("buyback", [])}
    st = prev.get("state", {})
    last = st.get("bb_last")
    start = (dt.date.fromisoformat(last) - dt.timedelta(days=CFG["overlap_days"]) if last
             else today - dt.timedelta(days=CFG["bb_backfill_days"]))
    rows, ok = dart.list_all("dart_bb", start, today, pblntf_detail_ty="B001")
    so_last = st.get("so_last")
    so_start = (dt.date.fromisoformat(so_last) - dt.timedelta(days=CFG["overlap_days"]) if so_last
                else today - dt.timedelta(days=CFG["so_backfill_days"]))
    rows2, ok2 = dart.list_all("dart_so", so_start, today, pblntf_ty="I")
    rows += [r for r in rows2 if "소각" in r.get("report_nm", "")]
    caps = marcaps()
    new = []
    for r in rows:
        kind, api = bb_kind(r.get("report_nm", ""))
        if not kind or r.get("corp_cls") not in ("Y", "K", "N"):
            continue
        rid = r["rcept_no"]
        if rid in known:
            continue
        b = {"id": rid, "code": (r.get("stock_code") or "").strip(), "name": r.get("corp_name"),
             "cls": r.get("corp_cls"), "corp": r.get("corp_code"), "kind": kind,
             "title": re.sub(r"\s+", " ", r.get("report_nm", "")).strip(),
             "fix": "정정" in r.get("report_nm", ""), "date": ymd(r["rcept_dt"]),
             "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rid}", "_api": api, "seen": SEEN}
        new.append(b)
        known[rid] = b
    # 상세: 회사·API별로 한 번씩
    groups = {}
    for b in new:
        if b["_api"]:
            groups.setdefault((b["corp"], b["_api"]), []).append(b)
    for (corp, api), lst in groups.items():
        d0 = min(b["date"] for b in lst).replace("-", "")
        d1 = max(b["date"] for b in lst).replace("-", "")
        j = dart.get(api, "dart_bb_detail", corp_code=corp, bgn_de=d0, end_de=d1)
        if j is None:
            continue
        _st("dart_bb_detail", True, len(j.get("list") or []))
        recs = {x.get("rcept_no"): x for x in j.get("list") or []}
        if recs and api not in DEBUG:
            DEBUG[api] = sorted(next(iter(recs.values())).keys())
        for b in lst:
            d = recs.get(b["id"])
            if d:
                b.update({k: v for k, v in bb_detail(b["kind"], d).items() if v is not None})
        time.sleep(0.1)
    for b in new:
        b.pop("_api", None)
        cap = caps.get(b["code"])
        if cap:
            b["marcap"] = cap
            if b.get("amount"):
                b["pct"] = round(b["amount"] / cap * 100, 2)
    keep = (today - dt.timedelta(days=CFG["bb_keep_days"])).isoformat()
    out = sorted([b for b in known.values() if b["date"] >= keep], key=lambda x: (x["date"], x["id"]), reverse=True)
    if ok:
        st["bb_last"] = today.isoformat()
    if ok2:
        st["so_last"] = today.isoformat()
    print(f"자사주 공시: 새로 {len(new)}건, 보관 {len(out)}건")
    return out


# ---------------------------------------------------------------- 운용사 지분
def load_advisors():
    try:
        j = json.load(open(os.path.join(ROOT, "advisors.json"), encoding="utf-8"))
        return [a for a in j.get("advisors", []) if a.get("name")]
    except Exception as e:
        print(f"advisors.json 읽기 실패: {e}")
        return []


# 추천 후보에서 뺄 곳: 대형·지수형 운용사, 부동산 운용사, 벤처캐피탈
BIG_HOUSES = (r"미래에셋|삼성자산|삼성액티브|KB자산|케이비자산|한화자산|신한자산|NH-Amundi|키움투자|한국투자신탁|"
              r"교보악사|하나자산|우리자산|DB자산|이지스|코람코|마스턴|인베스트먼트|캐피탈|벤처|국민연금|블랙록|뱅가드")


def adv_of(flr, advisors):
    n = norm_name(flr)
    for a in advisors:
        for m in (a.get("match") or [a["name"]]):
            if norm_name(m) and norm_name(m) in n:
                return a["name"]
    return None


def stakes(dart, prev, today, advisors):
    known = {s["id"]: s for s in prev.get("stake", [])}
    st = prev.get("state", {})
    last = st.get("st_last")
    # 운용사 목록이 바뀌면(새로 추가) 그 운용사는 과거 1년을 다시 훑어야 하므로 처음부터 받음
    sig = sorted(a["name"] for a in advisors)
    added_adv = [a for a in sig if a not in (st.get("st_advisors") or [])]
    if last and not added_adv:
        start = dt.date.fromisoformat(last) - dt.timedelta(days=CFG["overlap_days"])
    else:
        start = today - dt.timedelta(days=CFG["st_backfill_days"])
        if last:
            print(f"  운용사 추가됨 ({', '.join(added_adv)}) — 지분 공시 {CFG['st_backfill_days']}일 다시 받기")
    rows, ok = dart.list_all("dart_stake", start, today, pblntf_detail_ty="D001")
    others = {}
    seen_other = set(st.get("other_recent") or []) if (last and not added_adv) else set()
    recent_from = (today - dt.timedelta(days=CFG["overlap_days"] + 1)).isoformat()
    recent_ids = []
    new = []
    for r in rows:
        flr = r.get("flr_nm") or ""
        adv = adv_of(flr, advisors)
        if not adv:
            if re.search(r"자산운용|투자자문|파트너스|투자일임", flr) and not re.search(BIG_HOUSES, flr):
                rid = r["rcept_no"]
                if ymd(r["rcept_dt"]) >= recent_from:
                    recent_ids.append(rid)
                if rid not in seen_other:               # 겹쳐 받은 기간의 같은 공시는 한 번만 셈
                    k = re.sub(r"\(주\)|㈜|주식회사", "", flr).strip()
                    others[k] = others.get(k, 0) + 1
                    seen_other.add(rid)
            continue
        rid = r["rcept_no"]
        if rid in known:
            known[rid]["adv"] = adv
            continue
        s = {"id": rid, "adv": adv, "flr": flr, "code": (r.get("stock_code") or "").strip(),
             "name": r.get("corp_name"), "cls": r.get("corp_cls"), "corp": r.get("corp_code"),
             "title": re.sub(r"\s+", " ", r.get("report_nm", "")).strip(), "date": ymd(r["rcept_dt"]),
             "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rid}", "seen": SEEN}
        s["simple"] = "약식" in s["title"]
        new.append(s)
        known[rid] = s
    # 상세: 대상 회사별 대량보유 보고 목록에서 같은 접수번호 찾기
    by_corp = {}
    for s in new:
        by_corp.setdefault(s["corp"], []).append(s)
    for corp, lst in by_corp.items():
        j = dart.get("majorstock.json", "dart_stake_detail", corp_code=corp)
        if j is None:
            continue
        recs = j.get("list") or []
        _st("dart_stake_detail", True, len(recs))
        if recs and "majorstock" not in DEBUG:
            DEBUG["majorstock"] = sorted(recs[0].keys())
        by_id = {x.get("rcept_no"): x for x in recs}
        for s in lst:
            d = by_id.get(s["id"])
            if not d:
                continue
            s["rt"] = num(d.get("stkrt"))
            s["rt_chg"] = num(d.get("stkrt_irds"))
            s["qty"] = num(d.get("stkqy"))
            s["qty_chg"] = num(d.get("stkqy_irds"))
            s["reason"] = re.sub(r"\s+", " ", (d.get("report_resn") or "")).strip()[:160] or None
            s["repror"] = d.get("repror")
        time.sleep(0.1)
    for s in new:
        s["dir"] = direction(s)
    keep = (today - dt.timedelta(days=CFG["st_keep_days"])).isoformat()
    names = {a["name"] for a in advisors}
    out = sorted([s for s in known.values() if s["date"] >= keep and s.get("adv") in names],
                 key=lambda x: (x["date"], x["id"]), reverse=True)
    if ok:
        st["st_last"] = today.isoformat()
        st["st_advisors"] = sig
        st["other_recent"] = sorted(set(recent_ids))
    # 다른 운용사 후보: 이번에 훑은 기간 기준. 겹쳐 받는 짧은 기간이면 이전 집계와 합침
    prev_others = {o["name"]: o["n"] for o in prev.get("other_filers", []) if not re.search(BIG_HOUSES, o["name"])}
    if last and not added_adv:
        for k, v in others.items():
            prev_others[k] = prev_others.get(k, 0) + v
        others = prev_others
    other_list = sorted(({"name": k, "n": v} for k, v in others.items()), key=lambda x: -x["n"])[:60]
    print(f"운용사 지분 공시: 새로 {len(new)}건, 보관 {len(out)}건")
    return out, other_list


def direction(s):
    rt, ch = s.get("rt"), s.get("rt_chg")
    reason = s.get("reason") or ""
    if "신규" in reason or (rt is not None and ch is not None and abs(rt - ch) < 0.005 and rt > 0):
        return "신규"
    if rt is not None and rt < 5:
        return "5%미만"
    if ch is not None:
        if ch > 0.004:
            return "증가"
        if ch < -0.004:
            return "감소"
    return "변동"


# ---------------------------------------------------------------- 실행
def load_prev():
    cands = []
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        try:
            r = requests.get(f"https://{owner.lower()}.github.io/{name}/data/disc.json?t={int(time.time())}",
                             timeout=30, headers={"Cache-Control": "no-cache"})
            if r.status_code == 200:
                cands.append(r.json())
        except Exception as e:
            print(f"이전 공시 결과 받기 실패: {e}")
    p = os.path.join(CACHE, "disc.json")
    if os.path.exists(p):
        try:
            cands.append(json.load(open(p, encoding="utf-8")))
        except Exception:
            pass
    if not cands:
        return {}
    return max(cands, key=lambda d: d.get("updated", ""))


def run(key, now):
    if not key:
        print("DART_API_KEY 없음 — 자사주·투자자문 공시는 건너뜁니다")
        return
    global SEEN
    t0 = time.time()
    today = now.date()
    SEEN = now.isoformat()
    prev = load_prev()
    prev.setdefault("state", {})
    dart = Dart(key)
    advisors = load_advisors()
    bb = buybacks(dart, prev, today)
    stk, others = stakes(dart, prev, today, advisors)
    data = {
        "updated": now.isoformat(),
        "buyback": bb,
        "stake": stk,
        "advisors": advisors,
        "other_filers": others,
        "state": prev["state"],
        "status": STATUS,
        "calls": dart.calls,
        "debug": DEBUG,
    }
    js = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    open(OUT, "w", encoding="utf-8").write(js)
    os.makedirs(CACHE, exist_ok=True)
    open(os.path.join(CACHE, "disc.json"), "w", encoding="utf-8").write(js)
    print(f"공시 모음 완료: DART 요청 {dart.calls}회, {time.time() - t0:.0f}초")
    for k, v in STATUS.items():
        print(f"  {k}: 성공 {v['ok']} 실패 {v['fail']} 건수 {v['items']}" + (f" ({v['err']})" if v["err"] else ""))
