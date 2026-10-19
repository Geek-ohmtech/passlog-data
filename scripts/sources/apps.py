#!/usr/bin/env python3
"""현지 앱 가용성 오버레이 'local-apps-status' (봉투, 원격 전용, 주 1회, 키 없음).

큐레이션 local-apps.json 은 사람이 고친다(이 모듈은 바꾸지 않는다). 여기서는 iTunes Lookup 으로 앱이 아직
스토어에 있는지만 확인해 오버레이를 만든다. 조회 함수는 앱 리포 scripts/local-apps-check.py 의 것을 그대로 쓴다
(배포 리포에서는 scripts/vendor/local-apps-check.py 사본).

  data = {"apps": {"<appStoreId>": {"available": true, "store": "kr", "inKR": true, "missingCount": 0,
                                    "missingSince": "2026-10-19"?, "hidden": false,
                                    "version": "5.2.1"?, "releaseDate": "2026-09-30"?}}}
  - 한국 스토어(kr)에서 50개씩 묶어 조회 → 없는 것만 현지 스토어(storeCountry · 나라 · us · jp)에서 묶어 다시 조회
  - missingCount = 연속으로 못 찾은 주간 확인 수. 2 이상이면 hidden=true (앱은 hidden 인 앱을 숨긴다 — PLAN §1-2 #11)
  - 한 번에 30% 넘게 사라지면 조회 고장으로 보고 거부(이전 오버레이 유지)
"""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import packlib  # noqa: E402

ITUNES_SOURCE = {"name": "Apple iTunes Search API (lookup)", "url": "https://performance-partners.apple.com/search-api"}
HIDE_AFTER = 2
MAX_MISSING_RATIO = 0.3
BATCH = 50
DELAY = 3.5  # PLAN: 3.5초 간격 (약 20회/분 한도보다 넉넉히)

Lookup = Callable[[List[int], str], Optional[List[Dict[str, Any]]]]


def load_checker(path: Optional[Path] = None):
    path = path or packlib.vendor_script("local-apps-check.py")
    if path is None:
        raise RuntimeError("local-apps-check.py 를 찾지 못함 (scripts/vendor/ 또는 앱 리포 scripts/)")
    spec = importlib.util.spec_from_file_location("local_apps_check", str(path))
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def app_targets(data: Dict[str, Any], entries: Callable[[Dict[str, Any]], Iterable[Tuple[str, str, Dict[str, Any]]]]
                ) -> Dict[int, List[str]]:
    """{appStoreId: 현지 스토어 후보} — 웹 서비스 · id 0 은 뺀다"""
    out: Dict[int, List[str]] = {}
    for _where, cc, a in entries(data):
        sid = a.get("appStoreId") or 0
        if a.get("web") or not isinstance(sid, int) or sid <= 0:
            continue
        stores = out.setdefault(sid, [])
        for st in (a.get("storeCountry"), cc if len(cc or "") == 2 else None):
            if st and st.lower() not in stores:
                stores.append(st.lower())
    for stores in out.values():
        for st in ("us", "jp"):
            if st not in stores:
                stores.append(st)
    return out


def check(targets: Dict[int, List[str]], lookup: Lookup, delay: float = DELAY,
          sleep: Callable[[float], None] = time.sleep) -> Tuple[Dict[int, Tuple[str, Dict[str, Any]]], int]:
    """{id: (스토어, 결과)} 와 실패한 묶음 수"""
    found: Dict[int, Tuple[str, Dict[str, Any]]] = {}
    failures = 0
    ids = sorted(targets)
    for i in range(0, len(ids), BATCH):
        res = lookup(ids[i:i + BATCH], "kr")
        sleep(delay)
        if res is None:
            failures += 1
            continue
        for x in res:
            if x.get("trackId") in targets:
                found[x["trackId"]] = ("kr", x)
    # 현지 스토어: 후보 순서대로, 같은 스토어끼리 묶어서
    for round_ in range(4):
        by_store: Dict[str, List[int]] = {}
        for sid in ids:
            if sid in found:
                continue
            stores = targets[sid]
            if round_ < len(stores):
                by_store.setdefault(stores[round_], []).append(sid)
        if not by_store:
            break
        for store, sids in sorted(by_store.items()):
            for i in range(0, len(sids), BATCH):
                res = lookup(sids[i:i + BATCH], store)
                sleep(delay)
                if res is None:
                    failures += 1
                    continue
                for x in res:
                    if x.get("trackId") in targets and x["trackId"] not in found:
                        found[x["trackId"]] = (store, x)
    return found, failures


def build_status(targets: Dict[int, List[str]], found: Dict[int, Tuple[str, Dict[str, Any]]],
                 previous: Optional[Dict[str, Any]], today: str) -> Dict[str, Any]:
    prev_apps = ((previous or {}).get("data") or {}).get("apps") or {}
    apps: Dict[str, Dict[str, Any]] = {}
    for sid in sorted(targets):
        key = str(sid)
        prev = prev_apps.get(key) or {}
        if sid in found:
            store, info = found[sid]
            row: Dict[str, Any] = {"available": True, "store": store, "inKR": store == "kr", "missingCount": 0,
                                   "hidden": False}
            if info.get("version"):
                row["version"] = str(info["version"])
            rel = str(info.get("currentVersionReleaseDate") or "")[:10]
            if packlib.parse_date(rel):
                row["releaseDate"] = rel
        else:
            count = int(prev.get("missingCount") or 0) + 1
            row = {"available": False, "missingCount": count, "missingSince": prev.get("missingSince") or today,
                   "hidden": count >= HIDE_AFTER}
            if prev.get("version"):
                row["version"] = prev["version"]
        apps[key] = row
    return {"apps": apps}


def build(local_apps: Dict[str, Any], *, previous: Optional[Dict[str, Any]], today: str,
          lookup: Optional[Lookup] = None, entries: Optional[Callable[..., Any]] = None, delay: float = DELAY,
          sleep: Callable[[float], None] = time.sleep, log: Callable[[str], None] = print) -> Dict[str, Any]:
    """오버레이 data 를 돌려준다. 조회 고장으로 보이면 예외"""
    if lookup is None or entries is None:
        mod = load_checker()
        lookup = lookup or mod.lookup
        entries = entries or mod.entries
    targets = app_targets(local_apps, entries)
    if not targets:
        raise RuntimeError("확인할 앱이 없음")
    found, failures = check(targets, lookup, delay=delay, sleep=sleep)
    missing = [sid for sid in targets if sid not in found]
    log(f"  local-apps-status: {len(targets)}개 중 {len(found)}개 확인 · 못 찾음 {len(missing)} · 실패 묶음 {failures}")
    if failures and len(found) == 0:
        raise RuntimeError("iTunes 조회가 모두 실패")
    if len(missing) > len(targets) * MAX_MISSING_RATIO:
        raise RuntimeError(f"앱 {len(missing)}/{len(targets)}개가 한꺼번에 사라짐 — 조회 고장으로 보고 이전 오버레이 유지")
    if failures:
        raise RuntimeError(f"iTunes 묶음 {failures}개 실패 — 이번 주는 건너뜀(잘못된 '사라짐'을 막기 위해)")
    return build_status(targets, found, previous, today)
