"""보유·관심 종목 뉴스 수집기

GitHub Actions에서 하루 4번(06·12·18·24시 KST) 실행되어 site/data/news.json 을 만듭니다.
- 국내 종목: 네이버 증권 종목 뉴스(종목코드 기준) + 구글 뉴스(국문) + DART 공시
- 미국 종목: 구글 뉴스(영문·국문) + 야후 파이낸스 + SEC 공시
이전 실행 결과(배포된 news.json)를 이어 받아 새 기사만 덧붙이고, 비슷한 제목은 하나로 묶습니다.
"""
import datetime as dt
import hashlib
import html
import io
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlparse, parse_qs
from zoneinfo import ZoneInfo

import requests

KST = ZoneInfo("Asia/Seoul")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "site", "data", "news.json")
CACHE = os.path.join(ROOT, "cache")
CFG = json.load(open(os.path.join(ROOT, "engine", "config.json"), encoding="utf-8"))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
S = requests.Session()
S.headers["User-Agent"] = UA
NOW = dt.datetime.now(KST).replace(microsecond=0)
STATUS = {}


# ---------------------------------------------------------------- 공통
def iso(t):
    return t.astimezone(KST).replace(microsecond=0).isoformat()


def mark(src, ok, n=0, err=None):
    s = STATUS.setdefault(src, {"ok": 0, "fail": 0, "items": 0, "err": None})
    if ok:
        s["ok"] += 1
        s["items"] += n
    else:
        s["fail"] += 1
        s["err"] = err


def get(url, src, timeout=15, **kw):
    err = None
    for attempt in range(2):
        try:
            r = S.get(url, timeout=timeout, **kw)
            if r.status_code == 200:
                return r
            err = f"HTTP {r.status_code}"
            if r.status_code in (403, 404):
                break
        except Exception as e:
            err = str(e)[:150]
        time.sleep(1.5)
    mark(src, False, err=err)
    print(f"  ! {src} 실패 {err} — {url.split('?')[0][:100]}")      # 주소의 키 값은 로그에 남기지 않음
    return None


def clean(t):
    t = html.unescape(re.sub(r"<[^>]+>", "", t or ""))
    return re.sub(r"\s+", " ", t).strip()


def norm(t):
    t = re.sub(r"\[[^\]]*\]|【[^】]*】|<[^>]*>", "", t or "")
    return re.sub(r"[^0-9a-z가-힣]", "", t.lower())


def grams(s):
    return {s[i:i + 2] for i in range(len(s) - 1)} or {s}


def similar(a, b):
    if not a or not b:
        return 0.0
    ga, gb = grams(a), grams(b)
    inter = len(ga & gb)
    jac = inter / len(ga | gb)
    small = min(len(ga), len(gb))
    cont = inter / small if small >= 8 else 0      # 짧은 제목이 긴 제목에 거의 다 들어있는 경우
    return max(jac, cont * 0.85)


def cache_json(name, max_age_h, loader):
    os.makedirs(CACHE, exist_ok=True)
    p = os.path.join(CACHE, name)
    if os.path.exists(p) and time.time() - os.path.getmtime(p) < max_age_h * 3600:
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            pass
    try:
        v = loader()
        if v:
            json.dump(v, open(p, "w", encoding="utf-8"), ensure_ascii=False)
        return v
    except Exception as e:
        print(f"  ! {name} 받기 실패: {e}")
        if os.path.exists(p):
            return json.load(open(p, encoding="utf-8"))
        return None


# ---------------------------------------------------------------- 중요 태그
def _kw(words):
    return re.compile("|".join(re.escape(w) for w in words))


def _en(words):
    return re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b", re.I)


TAGS = [
    ("실적", _kw(["실적", "영업이익", "흑자전환", "적자전환", "어닝", "컨센서스", "가이던스", "매출 성장", "역대 최대"]),
     _en(["earnings", "quarterly results", "revenue", "guidance", "EPS", "beats", "misses", "outlook"])),
    ("수주·계약", _kw(["수주", "공급계약", "계약 체결", "계약체결", "단일판매", "납품", "MOU", "양해각서", "협약"]),
     _en(["contract", "awarded", "award", "agreement", "partnership", "order"])),
    ("임상·허가", _kw(["임상", "FDA", "식약처", "품목허가", "기술이전", "기술수출", "라이선스", "특허", "학회"]),
     _en(["FDA", "approval", "trial", "phase", "patent", "license", "clearance"])),
    ("자금조달", _kw(["유상증자", "전환사채", "신주인수권", "무상증자", "제3자배정", "블록딜", "오버행"]),
     _en(["offering", "convertible", "dilution", "secondary", "share sale"])),
    ("주주환원", _kw(["자사주", "배당", "소각", "밸류업"]),
     _en(["buyback", "repurchase", "dividend"])),
    ("M&A", _kw(["인수", "합병", "M&A", "지분 매각", "경영권", "최대주주 변경"]),
     _en(["acquisition", "acquire", "acquires", "merger", "takeover", "buyout"])),
    ("증권가", _kw(["목표가", "목표주가", "투자의견", "커버리지", "매수 추천", "탑픽"]),
     _en(["upgrade", "upgrades", "downgrade", "downgrades", "price target", "initiates", "overweight", "underweight"])),
    ("리스크", _kw(["소송", "리콜", "거래정지", "관리종목", "상장폐지", "횡령", "배임", "압수수색", "검찰", "세무조사", "감사의견", "불성실공시", "하한가"]),
     _en(["lawsuit", "probe", "investigation", "recall", "subpoena", "fraud", "delist", "layoffs", "sued", "plunges", "short seller"])),
]


def tags_for(title):
    out = []
    for name, ko, en in TAGS:
        if ko.search(title) or en.search(title):
            out.append(name)
    return out


# ---------------------------------------------------------------- 종목 목록
def load_stocks():
    j = json.load(open(os.path.join(ROOT, "stocks.json"), encoding="utf-8"))
    out, seen = [], set()
    for group in ("portfolio", "watch"):
        for s in j.get(group) or []:
            s = dict(s)
            s["market"] = (s.get("market") or "KR").upper()
            s["name"] = (s.get("name") or "").strip()
            s["code"] = (s.get("code") or "").strip().upper()
            s["group"] = group
            if not s["name"] and not s["code"]:
                continue
            out.append(s)
    # 국내 종목코드 채우기
    need = [s for s in out if s["market"] == "KR" and not re.fullmatch(r"[0-9A-Z]{6}", s["code"])]
    if need:
        lst = krx_listing() or {}
        for s in need:
            key = re.sub(r"\s", "", s["name"]).upper()
            s["code"] = lst.get(key, "")
            print(f"  종목코드 찾기: {s['name']} → {s['code'] or '못 찾음'}")
    # 미국 영문명 채우기
    for s in out:
        if s["market"] == "US" and s["code"] and not s.get("en"):
            s["en"] = yahoo_name(s["code"]) or ""
            if not s["name"]:
                s["name"] = s["en"] or s["code"]
    final = []
    for s in out:
        s["id"] = f"{s['market']}:{s['code'] or s['name']}"
        if s["id"] in seen:          # 보유와 관심에 모두 있으면 보유로만
            continue
        seen.add(s["id"])
        final.append(s)
    return final


def krx_listing():
    def load():
        m = {}
        try:
            import FinanceDataReader as fdr
            df = fdr.StockListing("KRX")
            for code, name in zip(df["Code"], df["Name"]):
                m[re.sub(r"\s", "", str(name)).upper()] = str(code)
        except Exception as e:
            print(f"  FinanceDataReader 실패: {e}")
        if not m:
            from pykrx import stock
            day = NOW.strftime("%Y%m%d")
            for mkt in ("KOSPI", "KOSDAQ", "KONEX"):
                for t in stock.get_market_ticker_list(day, market=mkt):
                    m[re.sub(r"\s", "", stock.get_market_ticker_name(t)).upper()] = t
        return m
    return cache_json("krx_names.json", 24 * 7, load)


def yahoo_name(ticker):
    r = get(f"https://query1.finance.yahoo.com/v1/finance/search?q={quote(ticker)}&quotesCount=3&newsCount=0",
            "yahoo_name")
    try:
        for q in r.json().get("quotes", []):
            if q.get("symbol", "").upper() == ticker.upper():
                return q.get("shortname") or q.get("longname")
    except Exception:
        pass
    return None


# ---------------------------------------------------------------- 수집원
def naver(stock):
    code = stock["code"]
    items = []
    r = get(f"https://m.stock.naver.com/api/news/stock/{code}?pageSize=30&page=1", "naver",
            headers={"Referer": "https://m.stock.naver.com/"})
    if r is not None:
        try:
            def walk(o):
                if isinstance(o, dict):
                    if o.get("articleId") and o.get("officeId") and o.get("title"):
                        yield o
                    for v in o.values():
                        yield from walk(v)
                elif isinstance(o, list):
                    for v in o:
                        yield from walk(v)
            for a in walk(r.json()):
                t = clean(a.get("titleFull") or a.get("title"))
                d = str(a.get("datetime") or a.get("dt") or "")
                try:
                    when = dt.datetime.strptime(d[:12], "%Y%m%d%H%M").replace(tzinfo=KST)
                except ValueError:
                    when = None
                items.append({"title": t, "press": clean(a.get("officeName")), "time": when,
                              "url": f"https://n.news.naver.com/mnews/article/{a['officeId']}/{a['articleId']}",
                              "src": "naver"})
        except Exception as e:
            print(f"  ! naver 해석 실패 {code}: {e}")
    if not items:                                          # 모바일 API가 막히면 PC 페이지로
        r = get(f"https://finance.naver.com/item/news_news.naver?code={code}&page=1", "naver_pc",
                headers={"Referer": f"https://finance.naver.com/item/news.naver?code={code}"})
        if r is not None:
            txt = r.content.decode("euc-kr", "replace")
            for href, title, info, date in re.findall(
                    r'<td class="title">\s*<a href="([^"]+)"[^>]*>(.*?)</a>.*?<td class="info">(.*?)</td>\s*<td class="date">(.*?)</td>',
                    txt, re.S):
                q = parse_qs(urlparse(html.unescape(href)).query)
                aid, oid = (q.get("article_id") or [""])[0], (q.get("office_id") or [""])[0]
                url = (f"https://n.news.naver.com/mnews/article/{oid}/{aid}" if aid and oid
                       else "https://finance.naver.com" + html.unescape(href))
                try:
                    when = dt.datetime.strptime(clean(date), "%Y.%m.%d %H:%M").replace(tzinfo=KST)
                except ValueError:
                    when = None
                items.append({"title": clean(title), "press": clean(info), "time": when, "url": url, "src": "naver"})
            if items:
                mark("naver_pc", True, len(items))
    else:
        mark("naver", True, len(items))
    return items


def google(query, lang, src, must=None):
    if lang == "ko":
        hl, gl, ceid = "ko", "KR", "KR:ko"
    else:
        hl, gl, ceid = "en-US", "US", "US:en"
    q = f"{query} when:{CFG['search_days']}d"
    r = get(f"https://news.google.com/rss/search?q={quote(q)}&hl={hl}&gl={gl}&ceid={ceid}", src)
    if r is None:
        return []
    items = []
    try:
        root = ET.fromstring(r.content)
        for it in root.iter("item"):
            title = clean(it.findtext("title"))
            se = it.find("source")
            press = clean(se.text) if se is not None and se.text else ""
            if press.startswith("http"):                   # 매체명 대신 주소가 온 경우 도메인만
                press = urlparse(press).netloc.removeprefix("www.")
            if press and title.endswith(" - " + press):
                title = title[: -len(press) - 3].strip()
            try:
                when = parsedate_to_datetime(it.findtext("pubDate")).astimezone(KST)
            except Exception:
                when = None
            if must and CFG.get("google_title_filter") and not must.search(title):
                continue
            items.append({"title": title, "press": press, "time": when,
                          "url": (it.findtext("link") or "").strip(), "src": "google"})
        items.sort(key=lambda x: x["time"] or NOW, reverse=True)
        items = items[: CFG.get("per_query_max", 30)]
        mark(src, True, len(items))
    except Exception as e:
        mark(src, False, err=f"해석 실패 {e}")
    return items


def yahoo(ticker, must=None):
    r = get(f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={quote(ticker)}&region=US&lang=en-US", "yahoo")
    if r is None:
        return []
    items = []
    try:
        root = ET.fromstring(r.content)
        for it in root.iter("item"):
            title = clean(it.findtext("title"))
            try:
                when = parsedate_to_datetime(it.findtext("pubDate")).astimezone(KST)
            except Exception:
                when = None
            if when and when < NOW - dt.timedelta(days=CFG["search_days"] + 1):
                continue
            if must and not must.search(title):
                continue
            items.append({"title": title, "press": "Yahoo Finance", "time": when,
                          "url": (it.findtext("link") or "").strip(), "src": "yahoo"})
        mark("yahoo", True, len(items))
    except Exception as e:
        mark("yahoo", False, err=f"해석 실패 {e}")
    return items


DART = "https://opendart.fss.or.kr/api"


def dart_corps(key):
    def load():
        r = S.get(f"{DART}/corpCode.xml", params={"crtfc_key": key}, timeout=90)
        z = zipfile.ZipFile(io.BytesIO(r.content))
        root = ET.fromstring(z.read(z.namelist()[0]))
        m = {}
        for el in root.iter("list"):
            sc = (el.findtext("stock_code") or "").strip()
            if sc:
                m[sc] = el.findtext("corp_code").strip()
        return m
    return cache_json("dart_corps.json", 24 * 7, load) or {}


def dart(stock, key, corps):
    cc = corps.get(stock["code"])
    if not cc:
        return []
    bgn = (NOW - dt.timedelta(days=CFG["search_days"] + 1)).strftime("%Y%m%d")
    r = get(f"{DART}/list.json?crtfc_key={key}&corp_code={cc}&bgn_de={bgn}&end_de={NOW:%Y%m%d}&page_count=50",
            "dart")
    if r is None:
        return []
    j = r.json()
    if j.get("status") not in ("000", "013"):          # 013 = 조회 결과 없음
        mark("dart", False, err=f"{j.get('status')} {j.get('message')}")
        return []
    items = []
    for d in j.get("list") or []:
        day = dt.datetime.strptime(d["rcept_dt"], "%Y%m%d").replace(tzinfo=KST)
        items.append({"title": clean(d.get("report_nm")), "press": clean(d.get("flr_nm")) or "DART",
                      "time": day, "date_only": True, "kind": "disc", "src": "dart",
                      "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={d['rcept_no']}"})
    mark("dart", True, len(items))
    return items


SEC_8K = {"1.01": "중요 계약", "1.02": "계약 해지", "1.03": "파산", "2.01": "자산 인수·처분", "2.02": "실적 발표",
          "2.03": "채무 발생", "2.05": "구조조정", "2.06": "손상차손", "3.01": "상장 관련", "3.02": "주식 발행",
          "4.01": "감사인 변경", "4.02": "재무제표 정정", "5.01": "지배권 변경", "5.02": "임원 변동",
          "5.03": "정관 변경", "5.07": "주총 결과", "7.01": "IR 자료", "8.01": "기타 주요사항", "9.01": "첨부 서류"}
SEC_FORM = {"8-K": "수시공시", "10-Q": "분기보고서", "10-K": "연차보고서", "6-K": "수시공시(해외)", "S-1": "증권신고서",
            "S-3": "증권신고서(간이)", "424B2": "투자설명서", "424B3": "투자설명서", "424B5": "투자설명서",
            "SC 13D": "5% 이상 지분 보고", "SC 13G": "5% 이상 지분 보고", "SC 13D/A": "지분 보고 정정",
            "SC 13G/A": "지분 보고 정정", "DEF 14A": "주총 위임장"}


def sec_ciks():
    def load():
        r = requests.get("https://www.sec.gov/files/company_tickers.json",
                         headers={"User-Agent": CFG["sec_user_agent"]}, timeout=30)
        r.raise_for_status()
        return {v["ticker"].upper(): int(v["cik_str"]) for v in r.json().values()}
    m = cache_json("sec_tickers.json", 24 * 7, load) or {}
    if not m:
        mark("sec", False, err="SEC 종목코드 표 받기 실패")
    return m


def sec(stock, ciks):
    cik = ciks.get(stock["code"])
    if not cik:
        return []
    r = get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", "sec",
            headers={"User-Agent": CFG["sec_user_agent"]})
    if r is None:
        return []
    items = []
    try:
        rec = r.json()["filings"]["recent"]
        since = NOW - dt.timedelta(days=CFG["search_days"] + 1)
        for i, form in enumerate(rec["form"]):
            if form not in CFG["sec_forms"]:
                continue
            acc = rec.get("acceptanceDateTime", [""] * (i + 1))[i]
            try:
                when = dt.datetime.fromisoformat(acc.replace("Z", "+00:00")).astimezone(KST)
                only = False
            except Exception:
                when = dt.datetime.strptime(rec["filingDate"][i], "%Y-%m-%d").replace(tzinfo=KST)
                only = True
            if when < since:
                break
            what = SEC_FORM.get(form, "공시")
            if form == "8-K":
                codes = [c.strip() for c in (rec.get("items", [""] * (i + 1))[i] or "").split(",") if c.strip()]
                names = [SEC_8K.get(c, c) for c in codes if c != "9.01"]
                if names:
                    what = " · ".join(names)
            accn = rec["accessionNumber"][i]
            items.append({"title": f"[{form}] {what}", "press": "SEC EDGAR", "time": when, "date_only": only,
                          "kind": "disc", "src": "sec",
                          "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn.replace('-', '')}/{rec['primaryDocument'][i]}"})
        mark("sec", True, len(items))
    except Exception as e:
        mark("sec", False, err=f"해석 실패 {e}")
    return items


# ---------------------------------------------------------------- 종목별 수집
def collect(stock, dart_key, corps, ciks):
    name, code = stock["name"], stock["code"]
    alias = [name] + list(stock.get("alias") or [])
    if stock["market"] == "KR":
        must = re.compile("|".join(re.escape(a) for a in alias if a), re.I)
        out = []
        if CFG["use_naver"] and code:
            out += naver(stock)
        if CFG["use_google_ko"]:
            q = stock.get("q_ko") or " OR ".join(f'"{a}"' for a in alias if a)
            out += google(q, "ko", "google_ko", must)
        if CFG["use_dart"] and dart_key and code:
            out += dart(stock, dart_key, corps)
        return out
    en = stock.get("en") or code
    names_en = [x for x in [en, code] + list(stock.get("alias_en") or []) if x]
    must_en = re.compile(r"\b(" + "|".join(re.escape(x) for x in names_en) + r")\b", re.I)
    must_ko = re.compile("|".join(re.escape(a) for a in alias + [code] if a), re.I)
    out = []
    if CFG["use_google_en"]:
        q = stock.get("q_en") or (f'"{en}" stock' if en != code else f'"{code}" stock')
        out += google(q, "en", "google_en", must_en)
    if CFG["use_google_ko"]:
        q = stock.get("q_ko") or " OR ".join(f'"{a}"' for a in alias if a)
        out += google(q, "ko", "google_ko", must_ko)
    if CFG["use_yahoo"]:
        out += yahoo(code, must_en)
    if CFG["use_sec"] and ciks:
        out += sec(stock, ciks)
    return out


# ---------------------------------------------------------------- 합치기
def load_prev():
    """배포된 이전 결과 → 없으면 Actions 캐시에 남은 파일"""
    cands = []
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        url = f"https://{owner.lower()}.github.io/{name}/data/news.json?t={int(time.time())}"
        try:
            r = requests.get(url, timeout=20, headers={"Cache-Control": "no-cache"})
            if r.status_code == 200:
                cands.append(r.json())
                print(f"이전 결과(배포본): {len(cands[-1].get('items', []))}건")
        except Exception as e:
            print(f"이전 결과 받기 실패: {e}")
    p = os.path.join(CACHE, "news.json")
    if os.path.exists(p):
        try:
            cands.append(json.load(open(p, encoding="utf-8")))
            print(f"이전 결과(캐시): {len(cands[-1].get('items', []))}건")
        except Exception:
            pass
    if not cands:
        return {"items": [], "runs": []}
    return max(cands, key=lambda d: d.get("updated", ""))


def item_id(sid, title):
    return hashlib.sha1(f"{sid}|{norm(title)}".encode()).hexdigest()[:12]


def merge(prev, stocks, fresh):
    keep_from = NOW - dt.timedelta(days=CFG["keep_days"])
    sids = {s["id"] for s in stocks}
    items = [it for it in prev.get("items", []) if it["sid"] in sids and it["time"] >= iso(keep_from)]
    by_id = {}
    for it in items:
        for k in [it["id"]] + it.get("ids", []):
            by_id[k] = it
    by_sid = {}
    for it in items:
        by_sid.setdefault(it["sid"], []).append(it)
    excl = CFG.get("exclude_words") or []
    added = 0
    for sid, cands in fresh.items():
        # 시간 오래된 것부터 넣어서 처음 보도한 기사가 대표가 되도록
        cands.sort(key=lambda c: c["time"] or NOW)
        for c in cands:
            title = c["title"]
            if not title or any(w in title for w in excl):
                continue
            when = c["time"] or NOW
            if when > NOW + dt.timedelta(hours=1):
                when = NOW
            if when < keep_from:
                continue
            iid = item_id(sid, title)
            if iid in by_id:
                ex = by_id[iid]
                if c["press"] and c["press"] not in ex["press_all"] and len(ex["press_all"]) < 10:
                    ex["press_all"].append(c["press"])
                continue
            n = norm(title)
            kind = c.get("kind", "news")
            hit = None
            if kind == "news":
                for ex in by_sid.get(sid, []):
                    if ex["kind"] != "news":
                        continue
                    if abs(dt.datetime.fromisoformat(ex["time"]) - when) > dt.timedelta(days=2):
                        continue
                    if similar(n, ex["_n"] if "_n" in ex else norm(ex["title"])) >= CFG["similar_title"]:
                        hit = ex
                        break
            if hit:
                if c["press"] and c["press"] not in hit["press_all"] and len(hit["press_all"]) < 10:
                    hit["press_all"].append(c["press"])
                hit.setdefault("ids", [])
                if len(hit["ids"]) < 30:
                    hit["ids"].append(iid)               # 다음 실행 때 같은 기사를 또 세지 않도록
                by_id[iid] = hit
                continue
            new = {"id": iid, "sid": sid, "kind": kind, "title": title, "url": c["url"], "press": c["press"],
                   "press_all": [c["press"]] if c["press"] else [], "src": c["src"], "time": iso(when),
                   "first_seen": iso(NOW), "tags": tags_for(title), "_n": n}
            if c.get("date_only"):
                new["date_only"] = True
                if when.date() == NOW.date():           # 오늘 공시는 발견 시각으로 정렬
                    new["time"] = iso(NOW)
            if kind == "disc" and "공시" not in new["tags"]:
                new["tags"] = ["공시"] + new["tags"]
            items.append(new)
            by_id[iid] = new
            by_sid.setdefault(sid, []).append(new)
            added += 1
    # 종목별 개수 제한
    out = []
    for sid in sids:
        lst = sorted([it for it in items if it["sid"] == sid], key=lambda x: x["time"], reverse=True)
        out += lst[: CFG["max_per_stock"]]
    for it in out:
        it.pop("_n", None)
    out.sort(key=lambda x: x["time"], reverse=True)
    return out, added


def main():
    t0 = time.time()
    stocks = load_stocks()
    print(f"종목 {len(stocks)}개 (보유 {sum(s['group'] == 'portfolio' for s in stocks)}, "
          f"관심 {sum(s['group'] == 'watch' for s in stocks)})")
    dart_key = os.environ.get("DART_API_KEY", "").strip()
    corps = dart_corps(dart_key) if (CFG["use_dart"] and dart_key and any(s["market"] == "KR" for s in stocks)) else {}
    if CFG["use_dart"] and not dart_key:
        print("DART_API_KEY 없음 — 국내 공시는 건너뜁니다")
    ciks = sec_ciks() if (CFG["use_sec"] and any(s["market"] == "US" for s in stocks)) else {}
    fresh = {}
    for s in stocks:
        got = collect(s, dart_key, corps, ciks)
        fresh[s["id"]] = got
        print(f"  {s['name']} ({s['code']}): {len(got)}건")
        time.sleep(0.3)
    prev = load_prev()
    items, added = merge(prev, stocks, fresh)
    runs = (prev.get("runs") or [])[-27:] + [{"at": iso(NOW), "new": added}]
    data = {
        "updated": iso(NOW),
        "stocks": [{k: s.get(k) for k in ("id", "group", "market", "name", "code", "en")} for s in stocks],
        "items": items,
        "runs": runs,
        "status": STATUS,
        "dart": bool(dart_key),
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    js = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    open(OUT, "w", encoding="utf-8").write(js)
    os.makedirs(CACHE, exist_ok=True)
    open(os.path.join(CACHE, "news.json"), "w", encoding="utf-8").write(js)
    print(f"새 기사 {added}건, 전체 {len(items)}건, {time.time() - t0:.0f}초")
    for k, v in STATUS.items():
        print(f"  {k}: 성공 {v['ok']} 실패 {v['fail']} 기사 {v['items']}" + (f" ({v['err']})" if v["err"] else ""))
    total_ok = sum(v["ok"] for k, v in STATUS.items() if k not in ("yahoo_name",))
    if stocks and total_ok == 0:
        print("모든 수집원이 실패했습니다")
        sys.exit(1)


if __name__ == "__main__":
    main()
