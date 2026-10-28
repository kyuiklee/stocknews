"""보유·관심·가격추적 종목의 시세와 재무 지표 (공개 정보만)

site/data/prices.json — 종목별 일별 종가(약 5년)와 원/달러 환율. 앱이 폰에 저장된 매매 기록과 합쳐 수익률을 계산합니다.
site/data/market.json — 업종, PER·PBR·배당수익률·ROE·시가총액, 현재가
보유 수량·금액 같은 개인 정보는 서버에 없습니다.
"""
import datetime as dt
import json
import os
import re
import threading
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "cache")
OUT_PX = os.path.join(ROOT, "site", "data", "prices.json")
OUT_MK = os.path.join(ROOT, "site", "data", "market.json")
KEEP = 1300            # 보관할 거래일 수 (약 5년)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
      "Referer": "https://finance.naver.com/"}
S = requests.Session()
S.headers.update(UA)
STATUS = {}
DEBUG = {}


def _st(k, ok, err=None):
    s = STATUS.setdefault(k, {"ok": 0, "fail": 0, "err": None})
    s["ok" if ok else "fail"] += 1
    if err:
        s["err"] = str(err)[:150]


def num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) if v == v else None
    s = re.sub(r"[^0-9.\-]", "", str(v))
    if s in ("", "-", ".", "-."):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _load(name, default):
    p = os.path.join(CACHE, name)
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            pass
    return default


def _save(name, obj):
    os.makedirs(CACHE, exist_ok=True)
    json.dump(obj, open(os.path.join(CACHE, name), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))


def merge_series(old, new):
    """{t:[yyyymmdd], c:[close]} 두 개를 날짜 기준으로 합침 (새 값 우선)"""
    m = dict(zip(old.get("t", []), old.get("c", [])))
    m.update(zip(new.get("t", []), new.get("c", [])))
    t = sorted(m)[-KEEP:]
    return {"t": t, "c": [m[x] for x in t]}


# ---------------------------------------------------------------- 국내 시세
def naver_daily(code, count):
    url = f"https://fchart.stock.naver.com/sise.nhn?symbol={code}&timeframe=day&count={count}&requestType=0"
    for attempt in range(3):
        try:
            r = S.get(url, timeout=15)
            rows = [x.split("|") for x in re.findall(r'data="([^"]+)"', r.text)]
            t, c = [], []
            for row in rows:
                cl = num(row[4])
                if cl and cl > 0:
                    t.append(int(row[0]))
                    c.append(cl)
            if t:
                return {"t": t, "c": c}
            return None
        except Exception as e:
            err = e
            time.sleep(1 + attempt)
    _st("kr_price", False, err)
    return None


def fdr_daily(code, days):
    try:
        import FinanceDataReader as fdr
        start = (dt.date.today() - dt.timedelta(days=int(days * 1.5))).isoformat()
        h = fdr.DataReader(code, start)
        if h is not None and len(h):
            return {"t": [int(d.strftime("%Y%m%d")) for d in h.index], "c": [float(x) for x in h["Close"]]}
    except Exception as e:
        _st("kr_price_fdr", False, e)
    return None


# ---------------------------------------------------------------- 국내 지표
def naver_fund(code):
    out = {}
    try:
        j = S.get(f"https://m.stock.naver.com/api/stock/{code}/integration", timeout=15,
                  headers={"Referer": "https://m.stock.naver.com/"}).json()
        infos = {(x.get("code") or "").lower(): x.get("value") for x in j.get("totalInfos") or []}
        if infos and "naver_integration" not in DEBUG:
            DEBUG["naver_integration"] = {k: infos[k] for k in list(infos)[:30]}
        out["per"] = num(infos.get("per"))
        out["pbr"] = num(infos.get("pbr"))
        out["eps"] = num(infos.get("eps"))
        out["bps"] = num(infos.get("bps"))
        out["div"] = num(infos.get("dividendyieldratio"))
        out["fper"] = num(infos.get("cnsper"))
        mv = infos.get("marketvalue")            # 예: '1조 2,345억'
        if mv:
            jo = re.search(r"([\d,]+)\s*조", mv)
            eok = re.search(r"([\d,]+)\s*억", mv)
            v = (num(jo.group(1)) or 0) * 1e12 if jo else 0
            v += (num(eok.group(1)) or 0) * 1e8 if eok else 0
            out["mcap"] = v or None
        ind = j.get("industryCode") or (j.get("stockItemTotalInfo") or {}).get("industryCode")
        if ind:
            out["ind_code"] = str(ind)
        _st("kr_fund", True)
    except Exception as e:
        _st("kr_fund", False, e)
    try:
        j = S.get(f"https://m.stock.naver.com/api/stock/{code}/finance/annual", timeout=15,
                  headers={"Referer": "https://m.stock.naver.com/"}).json()
        fi = j.get("financeInfo") or {}
        keys = [x.get("key") for x in fi.get("trTitleList") or [] if x.get("isConsensus") != "Y"]
        for row in fi.get("rowList") or []:
            if "ROE" in (row.get("title") or ""):
                cols = row.get("columns") or {}
                for k in reversed(keys):
                    v = num((cols.get(k) or {}).get("value"))
                    if v is not None:
                        out["roe"] = v
                        out["roe_y"] = k[:4]
                        break
                break
        _st("kr_roe", True)
    except Exception as e:
        _st("kr_roe", False, e)
    if out.get("roe") is None and out.get("eps") and out.get("bps"):
        out["roe"] = round(out["eps"] / out["bps"] * 100, 1)       # 대략값: 주당순이익 / 주당순자산
        out["roe_est"] = True
    return {k: v for k, v in out.items() if v is not None}


def krx_desc():
    """KRX 상장사 설명(업종·주요제품). 90초 넘으면 포기, 7일 캐시"""
    c = _load("krx_desc.json", None)
    if c and time.time() - c.get("at", 0) < 7 * 86400:
        return c["m"]
    box = {}

    def run():
        try:
            import FinanceDataReader as fdr
            box["d"] = fdr.StockListing("KRX-DESC")
        except Exception as e:
            box["e"] = e
    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(90)
    if "d" not in box:
        print(f"  KRX 업종 목록 실패: {box.get('e') or '90초 초과'}")
        return (c or {}).get("m", {})
    d = box["d"]
    m = {}
    for _, r in d.iterrows():
        code = str(r.get("Code", "")).zfill(6)
        m[code] = {"sector": str(r.get("Sector") or "").strip() or None, "product": str(r.get("Industry") or "").strip() or None}
    _save("krx_desc.json", {"at": time.time(), "m": m})
    return m


# ---------------------------------------------------------------- 미국
def yf_history(tickers, period):
    import yfinance as yf
    out = {}
    if not tickers:
        return out
    for attempt in range(3):
        try:
            df = yf.download(tickers, period=period, interval="1d", auto_adjust=False, group_by="ticker",
                             threads=True, progress=False)
            break
        except Exception as e:
            print(f"  yfinance 재시도 {attempt + 1}: {e}")
            time.sleep(8 * (attempt + 1))
            df = None
    if df is None or df.empty:
        _st("us_price", False, "빈 응답")
        return out
    import pandas as pd
    for t in tickers:
        try:
            g = df[t] if isinstance(df.columns, pd.MultiIndex) else df
            g = g.dropna(subset=["Close"])
        except KeyError:
            continue
        if len(g):
            out[t] = {"t": [int(d.strftime("%Y%m%d")) for d in g.index], "c": [round(float(x), 4) for x in g["Close"]]}
            _st("us_price", True)
    return out


def yf_fund(t):
    try:
        import yfinance as yf
        i = yf.Ticker(t).info or {}
        dy = i.get("trailingAnnualDividendYield")
        out = {"per": i.get("trailingPE"), "fper": i.get("forwardPE"), "pbr": i.get("priceToBook"),
               "div": round(dy * 100, 2) if isinstance(dy, (int, float)) else None,
               "roe": round(i["returnOnEquity"] * 100, 1) if isinstance(i.get("returnOnEquity"), (int, float)) else None,
               "mcap": i.get("marketCap"), "sector_en": i.get("sector"), "ind": i.get("industry"),
               "eps": i.get("trailingEps")}
        _st("us_fund", True)
        return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in out.items() if v is not None}
    except Exception as e:
        _st("us_fund", False, e)
        return {}


# ---------------------------------------------------------------- 실행
def run(stocks, now):
    """stocks: [{market, code, name, group}] — 보유·관심·가격추적 모두"""
    t0 = time.time()
    px = _load("px.json", {})
    seen = set()
    todo = []
    for s in stocks:
        if not s.get("code"):
            continue
        key = f"{s['market']}:{s['code']}"
        if key in seen:
            continue
        seen.add(key)
        todo.append((key, s))
    # 국내 시세
    for key, s in todo:
        if s["market"] != "KR":
            continue
        have = px.get(key)
        cnt = 40 if have and len(have.get("t", [])) > 200 else KEEP
        new = naver_daily(s["code"], cnt) or fdr_daily(s["code"], cnt)
        if new:
            px[key] = merge_series(have or {}, new)
            _st("kr_price", True)
        time.sleep(0.15)
    # 미국 시세 + 환율
    us = [s["code"] for k, s in todo if s["market"] == "US"]
    full = [t for t in us if len((px.get(f"US:{t}") or {}).get("t", [])) <= 200]
    inc = [t for t in us if t not in full]
    for group, period in ((full, "6y"), (inc, "3mo")):
        for t, ser in yf_history(group, period).items():
            px[f"US:{t}"] = merge_series(px.get(f"US:{t}") or {}, ser)
    fx_old = px.get("FX:USDKRW") or {}
    fx = yf_history(["KRW=X"], "3mo" if len(fx_old.get("t", [])) > 200 else "6y").get("KRW=X")
    if fx:
        px["FX:USDKRW"] = merge_series(fx_old, fx)
    _save("px.json", px)
    # 지표 (하루 한 번이면 충분: 6시간 넘었을 때만 새로)
    mk_old = _load("mk.json", {"at": 0, "info": {}})
    fresh = time.time() - mk_old.get("at", 0) < 5 * 3600 and mk_old.get("v") == 2
    info = {k: {x: y for x, y in v.items() if x not in ("sector", "product")} for k, v in mk_old.get("info", {}).items()}
    desc = krx_desc() if any(s["market"] == "KR" for _, s in todo) else {}
    for key, s in todo:
        if fresh and key in info:
            continue
        if s["market"] == "KR":
            f = naver_fund(s["code"])
            d = desc.get(s["code"]) or {}
            if d.get("product") and d["product"] != "nan":
                f["ind"] = d["product"]          # KRX-DESC 의 Industry = 한국표준산업분류 업종
        else:
            f = yf_fund(s["code"])
        f = {k: v for k, v in f.items() if v is not None}
        if f:
            info[key] = f
        time.sleep(0.2)
    if not fresh:
        _save("mk.json", {"at": time.time(), "info": info, "v": 2})
    keys = {k for k, _ in todo}
    out_px = {"updated": now.isoformat(), "fx": px.get("FX:USDKRW"),
              "px": {k: {**v, "cur": "USD" if k.startswith("US:") else "KRW"} for k, v in px.items() if k in keys}}
    names = {k: s.get("name") for k, s in todo}
    for k, v in out_px["px"].items():
        if v.get("c"):
            info.setdefault(k, {})["price"] = v["c"][-1]
            info[k]["price_date"] = v["t"][-1]
    out_mk = {"updated": now.isoformat(), "info": {k: {**info.get(k, {}), "name": names[k]} for k in keys},
              "status": STATUS, "debug": DEBUG}
    os.makedirs(os.path.dirname(OUT_PX), exist_ok=True)
    json.dump(out_px, open(OUT_PX, "w", encoding="utf-8"), separators=(",", ":"))
    json.dump(out_mk, open(OUT_MK, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print(f"시세·지표: {len(out_px['px'])}종목, 환율 {'있음' if out_px['fx'] else '없음'}, {time.time() - t0:.0f}초")
    for k, v in STATUS.items():
        print(f"  {k}: 성공 {v['ok']} 실패 {v['fail']}" + (f" ({v['err']})" if v["err"] else ""))
