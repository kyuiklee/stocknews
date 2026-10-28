"""시세만 빠르게 다시 받기 (앱의 '지금 시세 받기' 버튼, 장중 자동 갱신)

뉴스·공시는 새로 모으지 않고, 지금 배포된 파일을 그대로 다시 올립니다.
"""
import json
import os
import time

import requests

import collect
import market

ROOT = collect.ROOT
DATA = os.path.join(ROOT, "site", "data")


def keep_deployed():
    """배포된 news·disc·symbols 파일을 받아 두어 이번 배포에서 사라지지 않게 함"""
    os.makedirs(DATA, exist_ok=True)
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    base = ""
    if "/" in repo:
        owner, name = repo.split("/", 1)
        base = f"https://{owner.lower()}.github.io/{name}/data/"
    for f in ("news.json", "disc.json", "symbols.json", "market.json", "prices.json"):
        ok = False
        if base:
            try:
                r = requests.get(base + f + f"?t={int(time.time())}", timeout=30, headers={"Cache-Control": "no-cache"})
                if r.status_code == 200 and r.content[:1] == b"{":
                    open(os.path.join(DATA, f), "wb").write(r.content)
                    ok = True
            except Exception as e:
                print(f"  {f} 받기 실패: {e}")
        if not ok:
            c = os.path.join(collect.CACHE, f)
            if os.path.exists(c):
                open(os.path.join(DATA, f), "wb").write(open(c, "rb").read())
                ok = True
        print(f"  기존 {f}: {'유지' if ok else '없음'}")


def main():
    t0 = time.time()
    keep_deployed()
    stocks = collect.load_stocks() + collect.load_price_only()
    market.run(stocks, collect.NOW, collect.CFG["sec_user_agent"])
    print(f"시세만 갱신 완료 {time.time() - t0:.0f}초")


if __name__ == "__main__":
    main()
