#!/usr/bin/env python3
"""shared/data/local-apps.json 재검증 (rec2/PLAN.md §5 '갱신 정책').

출시 전과 분기마다 돌린다. 키 없는 iTunes Lookup API 로 App Store 앱을 다시 찾아
사라진 앱 · 이름 · 판매자 · 번들 id 변경 · 한국 스토어 여부(kr) 변화 · 오래 업데이트 없는 앱을 보고한다.
고치는 것은 사람이 한다(이 스크립트는 JSON 을 바꾸지 않는다).

  python3 scripts/local-apps-check.py                 # 구조 검사 + iTunes 재조회
  python3 scripts/local-apps-check.py --offline       # 구조 검사만 (네트워크 없이, CI · 파이프라인용)
  python3 scripts/local-apps-check.py --stale-years 2 --json report.json

종료 코드: 0 문제 없음 · 1 확인할 것 있음 · 2 파일을 읽지 못함
요청은 50개씩 묶어 1.5초 간격으로 보낸다(Apple 공개 API 예절). 연락처는 User-Agent 에 넣지 않는다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_DATA = ROOT / "shared" / "data" / "local-apps.json"
DESTINATIONS = ROOT / "shared" / "data" / "destinations.json"
COUNTRIES = ROOT / "shared" / "data" / "countries.json"
CATEGORIES = {"transit", "pay", "taxi", "map", "food", "entry", "stay", "tour", "translate", "other"}
USER_AGENT = "PASSLOG-catalog/1.0 (local-apps re-validation for the PASSLOG iOS travel journal app) python-urllib/3"
LOOKUP = "https://itunes.apple.com/lookup"


def load(path: pathlib.Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def entries(data: dict):
    """(어디, 앱) — 나라 apps · moreApps, 도시 apps, global apps"""
    for cc, c in (data.get("countries") or {}).items():
        for key in ("apps", "moreApps"):
            for a in c.get(key) or []:
                yield f"countries.{cc}.{key}", cc, a
    for cid, c in (data.get("cities") or {}).items():
        for a in c.get("apps") or []:
            yield f"cities.{cid}.apps", c.get("country") or "", a
    for a in (data.get("global") or {}).get("apps") or []:
        yield "global.apps", "", a


# MARK: 구조 검사 (네트워크 없음)

def check_structure(data: dict) -> list[str]:
    problems: list[str] = []
    known_countries: set[str] = set()
    known_destinations: set[str] = set()
    try:
        known_countries = {c["code"].upper() for c in load(COUNTRIES)["countries"]}
    except Exception:  # noqa: BLE001 — 없는 저장소에서도 돌 수 있게
        pass
    try:
        known_destinations = {d["id"] for d in load(DESTINATIONS)["destinations"]}
    except Exception:  # noqa: BLE001
        pass

    if not data.get("asOf"):
        problems.append("asOf 없음 (화면 각주 기준일)")
    for cc in (data.get("countries") or {}):
        if len(cc) != 2 or cc.upper() != cc:
            problems.append(f"나라 코드 형식: {cc}")
        elif known_countries and cc not in known_countries:
            problems.append(f"countries.json 에 없는 나라: {cc}")
    for cid, c in (data.get("cities") or {}).items():
        if known_destinations and cid not in known_destinations:
            problems.append(f"destinations.json 에 없는 도시 id: {cid}")
        if (c.get("country") or "") not in (data.get("countries") or {}):
            problems.append(f"도시 {cid} 의 나라 {c.get('country')} 가 countries 에 없음")

    by_store_id: dict[int, tuple[str, str]] = {}
    for where, _cc, a in entries(data):
        aid = a.get("id") or ""
        label = f"{where}:{aid or '?'}"
        if not aid or not a.get("name"):
            problems.append(f"{label} id · name 없음")
        if a.get("category") not in CATEGORIES:
            problems.append(f"{label} 모르는 category {a.get('category')!r}")
        if not a.get("why"):
            problems.append(f"{label} why 비어 있음")
        store_id = a.get("appStoreId") or 0
        if a.get("web"):
            if not str(a.get("url", "")).startswith(("https://", "http://")):
                problems.append(f"{label} 웹 서비스인데 url 없음")
            continue
        if not isinstance(store_id, int) or store_id <= 0:
            if not str(a.get("url", "")).startswith(("https://", "http://")):
                problems.append(f"{label} appStoreId · url 모두 없음")
            continue
        if not a.get("kr") and not a.get("appStoreUrl") and not a.get("storeCountry"):
            problems.append(f"{label} 한국 스토어에 없는데 현지 스토어 정보(appStoreUrl · storeCountry) 없음")
        if store_id in by_store_id and by_store_id[store_id][1] != (a.get("bundleId") or ""):
            problems.append(f"{label} 같은 appStoreId {store_id} 인데 bundleId 가 다름 ({by_store_id[store_id][0]})")
        by_store_id.setdefault(store_id, (label, a.get("bundleId") or ""))
    # 한 목록 안 중복 id
    for cc, c in (data.get("countries") or {}).items():
        ids = [a.get("id") for a in (c.get("apps") or []) + (c.get("moreApps") or [])]
        dup = {i for i in ids if ids.count(i) > 1}
        if dup:
            problems.append(f"countries.{cc} 중복 id {sorted(dup)}")
    return problems


# MARK: iTunes 재조회

def fetch_json(url: str) -> dict:
    """urllib 로 받고, 파이썬에 인증서 묶음이 없는 맥(python.org 설치본)이면 시스템 curl 로 받는다"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.URLError as e:
        if not isinstance(getattr(e, "reason", None), ssl.SSLError):
            raise
    out = subprocess.run(["curl", "-s", "-f", "-m", "60", "-A", USER_AGENT, url],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def lookup(ids: list[int], country: str, retries: int = 3) -> list[dict] | None:
    query = urllib.parse.urlencode({"id": ",".join(map(str, ids)), "country": country})
    for attempt in range(retries):
        try:
            return fetch_json(f"{LOOKUP}?{query}").get("results", [])
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, subprocess.CalledProcessError, OSError):
            time.sleep(3 * (attempt + 1))
    return None


def check_store(data: dict, stale_years: float, delay: float, limit: int | None) -> tuple[list[str], dict]:
    apps = [(where, cc, a) for where, cc, a in entries(data) if not a.get("web") and (a.get("appStoreId") or 0) > 0]
    ids = sorted({a["appStoreId"] for _, _, a in apps})
    if limit:
        ids = ids[:limit]
    found_kr: dict[int, dict] = {}
    for i in range(0, len(ids), 50):
        batch = ids[i:i + 50]
        res = lookup(batch, "kr")
        if res is None:
            return [f"iTunes 조회 실패 (kr, {i}번째 묶음) — 네트워크 확인 후 다시"], {}
        for x in res:
            if x.get("trackId"):
                found_kr[x["trackId"]] = x
        time.sleep(delay)

    found_local: dict[int, tuple[str, dict]] = {}
    for store_id in [i for i in ids if i not in found_kr]:
        stores: list[str] = []
        for _, cc, a in apps:
            if a.get("appStoreId") == store_id:
                if a.get("storeCountry"):
                    stores.append(a["storeCountry"].lower())
                if len(cc) == 2:
                    stores.append(cc.lower())
        stores += ["us", "jp"]
        for st in dict.fromkeys(stores):
            res = lookup([store_id], st)
            time.sleep(delay)
            if res:
                found_local[store_id] = (st, res[0])
                break

    problems: list[str] = []
    today = dt.date.today()
    seen: set[tuple[int, str]] = set()
    for where, _cc, a in apps:
        store_id = a["appStoreId"]
        if store_id not in ids:
            continue
        label = f"{where}:{a.get('id')}({store_id})"
        info = found_kr.get(store_id) or (found_local.get(store_id) or (None, None))[1]
        if info is None:
            problems.append(f"사라짐: {label} — 한국 · 현지 스토어 모두 없음")
            continue
        in_kr = store_id in found_kr
        if bool(a.get("kr")) != in_kr:
            problems.append(f"kr 플래그 불일치: {label} 데이터 {a.get('kr')} · 스토어 {in_kr}")
        if (store_id, "meta") in seen:
            continue
        seen.add((store_id, "meta"))
        for key, field in (("trackName", "trackName"), ("sellerName", "sellerName"), ("bundleId", "bundleId")):
            want = a.get(key)
            got = info.get(field)
            if want and got and want != got:
                problems.append(f"{key} 바뀜: {label} {want!r} → {got!r}")
        released = info.get("currentVersionReleaseDate") or ""
        try:
            last = dt.date.fromisoformat(released[:10])
            if (today - last).days > stale_years * 365:
                problems.append(f"오래 업데이트 없음: {label} 마지막 {last.isoformat()}")
        except ValueError:
            pass
    summary = {"checked": len(ids), "kr": len(found_kr), "local": len(found_local),
               "missing": len([i for i in ids if i not in found_kr and i not in found_local])}
    return problems, summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=pathlib.Path, default=DEFAULT_DATA)
    ap.add_argument("--offline", action="store_true", help="구조 검사만 (iTunes 조회 안 함)")
    ap.add_argument("--stale-years", type=float, default=2.0, help="이보다 오래 업데이트 없으면 보고 (기본 2년)")
    ap.add_argument("--delay", type=float, default=1.5, help="요청 사이 쉬는 초 (기본 1.5)")
    ap.add_argument("--limit", type=int, default=None, help="앞에서 N개 앱만 조회 (시험용)")
    ap.add_argument("--json", type=pathlib.Path, default=None, help="보고서를 JSON 으로 저장")
    args = ap.parse_args()

    try:
        data = load(args.data)
    except (OSError, json.JSONDecodeError) as e:
        print(f"읽지 못함: {args.data}: {e}", file=sys.stderr)
        return 2

    report: dict = {"file": str(args.data), "asOf": data.get("asOf"), "checkedAt": dt.datetime.now().isoformat(timespec="seconds")}
    structure = check_structure(data)
    report["structure"] = structure
    print(f"구조 검사: 문제 {len(structure)}건")
    for p in structure:
        print("  -", p)

    store: list[str] = []
    if not args.offline:
        store, summary = check_store(data, args.stale_years, args.delay, args.limit)
        report["store"] = store
        report["storeSummary"] = summary
        print(f"App Store 재조회: {summary or '실패'} · 확인할 것 {len(store)}건")
        for p in store:
            print("  -", p)

    if args.json:
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return 1 if structure or store else 0


if __name__ == "__main__":
    sys.exit(main())
