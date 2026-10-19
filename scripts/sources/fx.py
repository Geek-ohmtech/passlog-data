#!/usr/bin/env python3
"""환율 데이터셋 'rates' — 한국수출입은행 AP01(키 있을 때) 우선, 없는 통화는 Frankfurter v2.

형식은 기존 v1 rates.json 그대로(봉투 없음) + 선택 필드 sources · dates (앱 RatesSnapshot):
  {"base": "KRW", "date": "2026-10-19", "rates": {"JPY": 8.4306, …},
   "sources": {"JPY": "koreaexim", "MNT": "frankfurter"}, "dates": {"JPY": "2026-10-19", …}}
  - rates 값 = 통화 1단위당 원화. Frankfurter 는 base=USD 로 받아 KRW/X 로 환산(base=KRW 는 정밀도가 나쁨)
  - date = 수출입은행 고시일(없으면 Frankfurter USD→KRW 기준일)

검증: 이전 파일(14일 안)과 견줘 ±20% 넘게 움직인 통화가 많으면(>max(3, 10%)) 데이터셋 전체를 거부,
몇 개뿐이면 그 통화만 이전 값 · 출처 · 기준일로 되돌리고 경고.

  python3 -I scripts/sources/fx.py --out /tmp/rates.json            # Frankfurter (+ KOREAEXIM_KEY 있으면 수출입은행)
  python3 -I scripts/sources/fx.py --frankfurter-file f.json --out x  # 네트워크 없이
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import packlib  # noqa: E402
from sources import net  # noqa: E402

FRANKFURTER_URL = "https://api.frankfurter.dev/v2/rates?base=USD"
FRANKFURTER_SOURCE = {"name": "Frankfurter (시장 중간값)", "url": "https://frankfurter.dev"}
EXIM_SOURCE = {"name": "한국수출입은행", "url": "https://www.koreaexim.go.kr"}
MAX_JUMP = 0.20
COMPARE_WITHIN_DAYS = 14


class RatesRejected(Exception):
    pass


def sig(v: float, digits: int = 6) -> float:
    return float(f"{v:.{digits}g}")


def parse_frankfurter(raw: bytes) -> Dict[str, Tuple[float, str]]:
    """v2 /rates?base=USD 응답(행 배열 {date, base, quote, rate}) → {통화: (1단위당 원화, 기준일)}"""
    doc = json.loads(raw.decode("utf-8"))
    rows: List[Dict[str, Any]] = []
    if isinstance(doc, list):
        rows = [r for r in doc if isinstance(r, dict)]
    elif isinstance(doc, dict) and isinstance(doc.get("rates"), dict):  # v1 모양도 받아 준다
        rows = [{"date": doc.get("date"), "base": doc.get("base"), "quote": q, "rate": r}
                for q, r in doc["rates"].items()]
    usd: Dict[str, Tuple[float, str]] = {}
    for r in rows:
        if str(r.get("base") or "USD").upper() != "USD":
            continue
        q = str(r.get("quote") or "").upper()
        try:
            rate = float(r.get("rate"))
        except (TypeError, ValueError):
            continue
        if len(q) == 3 and q.isalpha() and rate > 0:
            usd[q] = (rate, str(r.get("date") or ""))
    if "KRW" not in usd:
        raise ValueError("Frankfurter 응답에 KRW 가 없음")
    krw_rate, krw_date = usd["KRW"]
    out: Dict[str, Tuple[float, str]] = {"USD": (sig(krw_rate), krw_date)}
    for q, (rate, date) in usd.items():
        if q in ("KRW", "USD"):
            continue
        out[q] = (sig(krw_rate / rate), min(date, krw_date) if date and krw_date else (date or krw_date))
    return out


def fetch_frankfurter(getter: Callable[..., Tuple[int, Dict[str, str], bytes]] = net.get) -> Dict[str, Tuple[float, str]]:
    status, _headers, body = getter(FRANKFURTER_URL, timeout=30)
    if status != 200:
        raise net.FetchError(f"HTTP {status}")
    return parse_frankfurter(body)


def fetch_koreaexim(key: str, today: dt.date) -> Tuple[str, Dict[str, float]]:
    """fetch.py 의 수출입은행 AP01 조회(오늘부터 최대 7일 전까지)를 그대로 쓴다"""
    import fetch  # scripts/fetch.py

    return fetch.fetch_rates(key, today)


def merge(exim: Optional[Tuple[str, Dict[str, float]]],
          frank: Optional[Dict[str, Tuple[float, str]]]) -> Dict[str, Any]:
    rates: Dict[str, float] = {}
    sources: Dict[str, str] = {}
    dates: Dict[str, str] = {}
    date = ""
    if exim:
        date, table = exim
        for cur, v in table.items():
            if v and v > 0 and cur != "KRW":
                rates[cur] = sig(float(v))
                sources[cur] = "koreaexim"
                dates[cur] = date
    if frank:
        for cur, (v, d) in frank.items():
            if cur in rates or cur == "KRW":
                continue
            rates[cur] = v
            sources[cur] = "frankfurter"
            dates[cur] = d
        if not date:
            date = frank.get("USD", (0, ""))[1] or max((d for _, d in frank.values() if d), default="")
    if not rates or not date:
        raise ValueError("환율: 쓸 수 있는 값이 없음")
    order = sorted(rates)
    return {"base": "KRW", "date": date, "rates": {k: rates[k] for k in order},
            "sources": {k: sources[k] for k in order}, "dates": {k: dates[k] for k in order}}


def check_against_previous(doc: Dict[str, Any], previous: Optional[Dict[str, Any]],
                           max_jump: float = MAX_JUMP) -> List[str]:
    """이전 파일과 비교. 많이 튀면 RatesRejected, 몇 개면 그 통화만 이전 값으로 되돌리고 경고 목록을 돌려준다"""
    warnings: List[str] = []
    if not isinstance(previous, dict) or not isinstance(previous.get("rates"), dict):
        return warnings
    pd_ = packlib.parse_date(previous.get("date"))
    nd_ = packlib.parse_date(doc.get("date"))
    if pd_ is None or nd_ is None or abs((nd_ - pd_).days) > COMPARE_WITHIN_DAYS:
        return warnings
    prev_rates = previous["rates"]
    bad = []
    for cur, v in doc["rates"].items():
        p = prev_rates.get(cur)
        if isinstance(p, (int, float)) and p > 0 and abs(v / p - 1) > max_jump:
            bad.append(cur)
    common = sum(1 for c in doc["rates"] if c in prev_rates)
    if len(bad) > max(3, int(common * 0.1)):
        raise RatesRejected(f"전날 대비 ±{int(max_jump * 100)}% 넘게 바뀐 통화 {len(bad)}개: {', '.join(bad[:10])}")
    for cur in bad:
        warnings.append(f"{cur} {prev_rates[cur]} → {doc['rates'][cur]} (±{int(max_jump * 100)}% 초과) — 이전 값 유지")
        doc["rates"][cur] = prev_rates[cur]
        psrc = (previous.get("sources") or {}).get(cur)
        pdate = (previous.get("dates") or {}).get(cur) or previous.get("date")
        if psrc:
            doc["sources"][cur] = psrc
        if pdate:
            doc["dates"][cur] = pdate
    return warnings


def build(*, exim_key: str, today: dt.date, previous: Optional[Dict[str, Any]],
          getter: Callable[..., Tuple[int, Dict[str, str], bytes]] = net.get,
          exim_fetcher: Optional[Callable[[str, dt.date], Tuple[str, Dict[str, float]]]] = None,
          log: Callable[[str], None] = print) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """(rates 문서, 정보{sources, warnings}) — 둘 다 실패하면 예외"""
    exim = frank = None
    errors: List[str] = []
    if exim_key:
        try:
            exim = (exim_fetcher or fetch_koreaexim)(exim_key, today)
            log(f"  rates: 수출입은행 {len(exim[1])}개 ({exim[0]})")
        except Exception as e:  # noqa: BLE001 — 소스별 실패는 다른 소스로 채운다
            errors.append("koreaexim: " + packlib.scrub(e, exim_key)[:160])
    try:
        frank = fetch_frankfurter(getter)
        log(f"  rates: Frankfurter {len(frank)}개")
    except Exception as e:  # noqa: BLE001
        errors.append("frankfurter: " + str(e)[:160])
    if exim is None and frank is None:
        raise RuntimeError("; ".join(errors) or "환율 소스 없음")
    doc = merge(exim, frank)
    warnings = check_against_previous(doc, previous)
    used = sorted(set(doc["sources"].values()))
    return doc, {"sources": used, "warnings": warnings + errors}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="환율 데이터셋(rates) 만들기")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--previous", type=Path)
    ap.add_argument("--frankfurter-file", type=Path, help="네트워크 대신 이 Frankfurter v2 응답 파일")
    args = ap.parse_args(argv)
    getter = net.get
    if args.frankfurter_file:
        body = args.frankfurter_file.read_bytes()
        getter = lambda url, **kw: (200, {}, body)  # noqa: E731
    key = packlib.secret("KOREAEXIM_KEY")
    previous = packlib.read_json(args.previous) if args.previous else None
    doc, info = build(exim_key="" if args.frankfurter_file else key, today=packlib.now_kst().date(),
                      previous=previous, getter=getter, log=lambda m: print(m, file=sys.stderr))
    packlib.write_bytes_atomic(args.out, packlib.pretty_bytes(doc))
    for w in info["warnings"]:
        print(f"::warning::{w}", file=sys.stderr)
    print(f"완료: {args.out} 통화 {len(doc['rates'])}개 · 출처 {', '.join(info['sources'])}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
