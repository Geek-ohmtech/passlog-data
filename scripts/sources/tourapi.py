#!/usr/bin/env python3
"""한국관광공사 TourAPI 데이터셋 'kr-restaurants' · 'kr-attractions' · 'kr-festivals' (DATA_GO_KR_KEY).

수집 · 형식은 앱 리포 scripts/tourapi-snapshot.py(배포 리포는 scripts/vendor/ 사본)를 그대로 쓴다 — 이 모듈은
그 스크립트의 `run_snapshot()` 을 불러 감싸기만 한다(다시 구현하지 않음). 출력 형식은 번들 kr-*.json 과 같다.

감싸는 일
  1) 원응답 캐시(키 없음, GitHub Actions cache 로 실행 사이에 유지)를 나이로 만료한다(작업마다 시차를 두어 한꺼번에 몰리지 않게):
       searchFestival2 20시간(축제는 매일) · areaBasedList2 6.5일±12시간(목록은 주간) ·
       detailIntro2/detailCommon2 28일±7일 · locationBasedList2/searchKeyword2/lclsSystmCode2 30일±5일
  2) 증분: areaBasedSyncList2(modifiedtime=날짜)로 지난 확인 뒤 바뀐 콘텐츠를 찾아 그 상세 · 그 시·군·구 목록 캐시만 지운다
  3) 호출 한도: 실행 한 번에 600회 이하, 하루(KST) 합계 900회 이하 (개발 계정 1,000회/일). usage.json 으로 센다
  4) 축제 기간: 오늘 ~ 12개월 뒤 말일 (스크립트 기본값은 고정 날짜라 실행 때마다 바꿔 준다)
  5) 이전 파일과 합치기: 한도가 떨어져 덜 모은 여행지 · 검사에 걸린 여행지 · 이번에 못 만든 여행지는 이전 블록을 그대로 쓴다.
     카탈로그에 없는 여행지 블록은 뺀다. 축제는 끝난 것을 뺀다. 맛집 · 볼거리 항목 수가 이전보다 30% 넘게 줄면 그 데이터셋을 거부한다.
키는 환경 변수에서만 읽고(run_snapshot(key=…)), 출력 · 로그 · 파일 어디에도 남기지 않는다.
"""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import importlib.util
import inspect
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Set, Tuple

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import packlib  # noqa: E402

KINDS = {"kr-restaurants": "restaurants", "kr-attractions": "attractions", "kr-festivals": "festivals"}
FILES = {"kr-restaurants": "kr-restaurants.json", "kr-attractions": "kr-attractions.json",
         "kr-festivals": "kr-festivals.json"}
TOUR_SOURCE = {"name": "한국관광공사 TourAPI 4.0", "url": "https://api.visitkorea.or.kr"}

PER_RUN_BUDGET = 600
DAY_CAP = 900
TTL_HOURS = {
    "searchFestival2": 20.0,
    "areaBasedList2": 6.5 * 24,
    "detailIntro2": 28 * 24.0,
    "detailCommon2": 28 * 24.0,
    "locationBasedList2": 30 * 24.0,
    "searchKeyword2": 30 * 24.0,
    "lclsSystmCode2": 30 * 24.0,
    "areaBasedSyncList2": 72.0,   # 증분 응답은 실행 때마다 다시 받으므로 오래 둘 필요 없음
}
JITTER_HOURS = {"areaBasedList2": 12.0, "detailIntro2": 7 * 24.0, "detailCommon2": 7 * 24.0,
                "locationBasedList2": 5 * 24.0, "searchKeyword2": 5 * 24.0, "lclsSystmCode2": 5 * 24.0}
SYNC_OP = "areaBasedSyncList2"
SYNC_TYPES = {"39", "12", "14", "28", "38"}   # 음식점 · 관광지 · 문화시설 · 레포츠 · 쇼핑 (축제는 매일 다시 받음)
SYNC_MAX_DAYS = 7
SYNC_MAX_PAGES = 10
SYNC_STATE = "pipeline-sync.json"
MAX_SHRINK = 0.3


# ---------------------------------------------------------------------------
# 스크립트 불러오기
# ---------------------------------------------------------------------------
def load_module(path: Optional[Path] = None):
    path = path or packlib.vendor_script("tourapi-snapshot.py")
    if path is None:
        raise RuntimeError("tourapi-snapshot.py 를 찾지 못함 (scripts/vendor/ 또는 앱 리포 scripts/)")
    spec = importlib.util.spec_from_file_location("tourapi_snapshot", str(path))
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    if not hasattr(mod, "run_snapshot"):
        raise RuntimeError("tourapi-snapshot.py 에 run_snapshot() 이 없음 — 스크립트를 최신으로 복사하세요")
    return mod


# ---------------------------------------------------------------------------
# 캐시 (tourapi-snapshot.py 형식: <cache>/<op>/<sha1>.json.gz = {"op","params","fetchedAt","body"})
# ---------------------------------------------------------------------------
def cache_name(op: str, params: Dict[str, Any]) -> str:
    canon = json.dumps({"op": op, "params": {k: str(v) for k, v in sorted(params.items())}},
                       ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(canon.encode("utf-8")).hexdigest()


def iter_cache(cache_dir: Path) -> Iterator[Tuple[Path, str, Dict[str, str], Optional[dt.datetime]]]:
    cache_dir = Path(cache_dir)
    if not cache_dir.is_dir():
        return
    for op_dir in sorted(p for p in cache_dir.iterdir() if p.is_dir()):
        for f in sorted(op_dir.iterdir()):
            if not (f.name.endswith(".json.gz") or f.name.endswith(".json")):
                continue
            try:
                if f.name.endswith(".gz"):
                    with gzip.open(f, "rt", encoding="utf-8") as fh:
                        rec = json.load(fh)
                else:
                    rec = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError, EOFError):
                yield f, op_dir.name, {}, None
                continue
            params = rec.get("params") if isinstance(rec, dict) else None
            yield (f, str(rec.get("op") or op_dir.name), {k: str(v) for k, v in (params or {}).items()},
                   packlib.parse_date(rec.get("fetchedAt")))


def _jitter(path: Path, hours: float) -> float:
    if not hours:
        return 0.0
    h = int(hashlib.sha1(path.name.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return (h * 2 - 1) * hours


def expire_cache(cache_dir: Path, now: dt.datetime, ttl: Optional[Dict[str, float]] = None,
                 jitter: Optional[Dict[str, float]] = None) -> Dict[str, int]:
    ttl = TTL_HOURS if ttl is None else ttl
    jitter = JITTER_HOURS if jitter is None else jitter
    removed: Dict[str, int] = {}
    for f, op, _params, fetched in iter_cache(cache_dir):
        if op not in ttl:
            continue
        limit = ttl[op] + _jitter(f, jitter.get(op, 0.0))
        if fetched is None or (now - fetched).total_seconds() > limit * 3600:
            try:
                f.unlink()
            except OSError:
                continue
            removed[op] = removed.get(op, 0) + 1
    return removed


def invalidate(cache_dir: Path, content_ids: Set[str], areas: Set[Tuple[str, str]]) -> Dict[str, int]:
    """바뀐 콘텐츠의 상세 · 그 시·군·구 목록 캐시를 지운다 (area = (lDongRegnCd, lDongSignguCd))"""
    removed: Dict[str, int] = {}
    regions = {r for r, _ in areas}
    for f, op, params, _fetched in iter_cache(cache_dir):
        hit = False
        if op in ("detailIntro2", "detailCommon2"):
            hit = params.get("contentId") in content_ids
        elif op == "areaBasedList2":
            regn, sign = params.get("lDongRegnCd", ""), params.get("lDongSignguCd", "")
            hit = (regn, sign) in areas if sign else regn in regions
        if hit:
            try:
                f.unlink()
            except OSError:
                continue
            removed[op] = removed.get(op, 0) + 1
    return removed


def used_today(cache_dir: Path, today: str) -> int:
    try:
        return int(json.loads((Path(cache_dir) / "usage.json").read_text()).get(today, 0))
    except (OSError, ValueError, AttributeError):
        return 0


def run_budget(cache_dir: Path, today: str, per_run: int = PER_RUN_BUDGET, day_cap: int = DAY_CAP) -> int:
    """run_snapshot 의 budget(그날 합계 상한) — 이번 실행이 per_run 을 넘지 않게"""
    return min(used_today(cache_dir, today) + per_run, day_cap)


# ---------------------------------------------------------------------------
# 증분 (areaBasedSyncList2)
# ---------------------------------------------------------------------------
def _items(body: Any) -> List[Dict[str, Any]]:
    b = ((body or {}).get("response") or {}).get("body") or {}
    it = b.get("items")
    if not isinstance(it, dict):
        return []
    x = it.get("item") or []
    return [x] if isinstance(x, dict) else [r for r in x if isinstance(r, dict)]


def _total(body: Any) -> int:
    try:
        return int((((body or {}).get("response") or {}).get("body") or {}).get("totalCount") or 0)
    except (TypeError, ValueError):
        return 0


def sync_changes(api: Any, days: Iterable[dt.date]) -> Tuple[Set[str], Set[Tuple[str, str]], bool]:
    """(바뀐 contentId, 그 시·군·구, 모두 성공했나). 같은 날을 다시 물을 때 캐시를 쓰지 않게 먼저 지운다.
    하루치가 SYNC_MAX_PAGES 쪽을 넘어 다 읽지 못하면 실패(False) — lastSync 를 옮기지 않아 다음 실행에서 다시 묻는다
    (그대로 성공으로 두면 그날 변경분 일부를 영영 놓친다. 7일이 넘도록 못 읽으면 sync_days 가 나이 만료에 맡긴다)"""
    ids: Set[str] = set()
    areas: Set[Tuple[str, str]] = set()
    for day in days:
        page = 1
        seen = 0
        complete = False
        while page <= SYNC_MAX_PAGES:
            params = {"modifiedtime": day.strftime("%Y%m%d"), "arrange": "C", "numOfRows": 1000, "pageNo": page}
            try:
                api.cache_path(SYNC_OP, {k: str(v) for k, v in params.items()}).unlink()
            except (OSError, AttributeError):
                pass
            body = api.get(SYNC_OP, params)
            if body is None:
                return ids, areas, False
            rows = _items(body)
            for r in rows:
                if str(r.get("contenttypeid") or "") not in SYNC_TYPES:
                    continue
                cid = str(r.get("contentid") or "")
                if cid:
                    ids.add(cid)
                regn, sign = str(r.get("lDongRegnCd") or ""), str(r.get("lDongSignguCd") or "")
                if regn:
                    areas.add((regn, sign))
            seen += len(rows)
            if not rows or seen >= _total(body):
                complete = True
                break
            page += 1
        if not complete:
            return ids, areas, False
    return ids, areas, True


def sync_days(cache_dir: Path, today: dt.date) -> Tuple[List[dt.date], bool]:
    """(물어볼 날들, 상태 파일이 있었나). 처음이거나 7일 넘게 비었으면 증분 없이 나이 만료에만 맡긴다"""
    state = packlib.read_json(Path(cache_dir) / SYNC_STATE) or {}
    last = packlib.parse_date(state.get("lastSync"))
    if last is None:
        return [], False
    start = last.date()
    if (today - start).days > SYNC_MAX_DAYS or start > today:
        return [], True
    return [start + dt.timedelta(days=i) for i in range((today - start).days + 1)], True


def save_sync_state(cache_dir: Path, today: dt.date) -> None:
    packlib.write_bytes_atomic(Path(cache_dir) / SYNC_STATE,
                               packlib.pretty_bytes({"lastSync": today.isoformat()}))


# ---------------------------------------------------------------------------
# 합치기
# ---------------------------------------------------------------------------
def problem_ids(problems: Iterable[str]) -> Dict[str, Set[str]]:
    """'restaurants/busan/…: …' → {"restaurants": {"busan"}}"""
    out: Dict[str, Set[str]] = {}
    for p in problems or []:
        parts = str(p).split("/")
        if len(parts) >= 2 and parts[0] in ("restaurants", "attractions", "festivals"):
            out.setdefault(parts[0], set()).add(parts[1].split(":")[0])
    return out


def _drop_ended(block: Dict[str, Any], today: str) -> Dict[str, Any]:
    items = [x for x in block.get("items") or [] if not (isinstance(x, dict) and x.get("end") and str(x["end"]) < today)]
    out = dict(block)
    out["items"] = items
    return out


def merge(kind: str, new: Dict[str, Any], previous: Optional[Dict[str, Any]], keep_previous: Set[str],
          catalog_ids: Set[str], today: str) -> Tuple[Dict[str, Any], List[str]]:
    notes: List[str] = []
    prev_d = (previous or {}).get("destinations") or {}
    new_d = new.get("destinations") or {}
    dests: Dict[str, Any] = {}
    for did, block in new_d.items():
        if did not in catalog_ids:
            continue
        if did in keep_previous and did in prev_d:
            dests[did] = prev_d[did]
            notes.append(f"{did}: 이전 블록 유지")
        else:
            dests[did] = block
    for did, block in prev_d.items():
        if did in catalog_ids and did not in dests:
            dests[did] = block
            notes.append(f"{did}: 이번에 못 만들어 이전 블록 유지")
    if kind == "festivals":
        dests = {k: _drop_ended(v, today) for k, v in dests.items()}
    out = {k: v for k, v in new.items() if k not in ("destinations", "provisionalIds")}
    out["destinations"] = dests
    if kind != "festivals":  # 축제는 철이 지나면 크게 줄 수 있다
        before = sum(len((prev_d[d] or {}).get("items") or []) for d in prev_d if d in catalog_ids)
        after = sum(len((b or {}).get("items") or []) for b in dests.values())
        if before and after < before * (1 - MAX_SHRINK):
            raise RuntimeError(f"{kind}: 항목 {before} → {after} ({int(MAX_SHRINK * 100)}% 넘게 줄어 거부 — 소스 고장 의심)")
    return out, notes


def festival_window(today: dt.date) -> Tuple[str, str]:
    y, m = today.year + 1, today.month
    nxt = dt.date(y + (m // 12), m % 12 + 1, 1)
    end = nxt - dt.timedelta(days=1)
    return today.strftime("%Y%m%d"), end.strftime("%Y%m%d")


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------
def run(*, key: str, cache_dir: Path, catalog_path: Path, curated_dir: Path, previous: Dict[str, Optional[Dict[str, Any]]],
        now: dt.datetime, per_run: int = PER_RUN_BUDGET, day_cap: int = DAY_CAP, module: Any = None,
        log: Callable[[str], None] = print, verify_images: bool = True) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """({데이터셋 id: 새 문서}, 정보). 키가 없거나 스크립트가 없으면 예외"""
    if not key:
        raise RuntimeError("DATA_GO_KR_KEY 없음")
    mod = module or load_module()
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    today = now.astimezone(packlib.KST).date()
    info: Dict[str, Any] = {}

    info["expired"] = expire_cache(cache_dir, now)
    days, had_state = sync_days(cache_dir, today)
    budget = run_budget(cache_dir, today.isoformat(), per_run, day_cap)
    info["budget"] = budget
    if days:
        api = mod.TourAPI(cache_dir, budget, 0.3, offline=False, key=key)
        ids, areas, ok = sync_changes(api, days)
        info["sync"] = {"days": [d.isoformat() for d in days], "changed": len(ids), "areas": len(areas), "ok": ok}
        if ok:
            info["invalidated"] = invalidate(cache_dir, ids, areas)
            save_sync_state(cache_dir, today)
    else:
        info["sync"] = {"days": [], "note": "처음 실행 또는 7일 넘게 비어 나이 만료만" if not had_state else "건너뜀"}
        save_sync_state(cache_dir, today)
    budget = run_budget(cache_dir, today.isoformat(), per_run, day_cap)
    if budget <= used_today(cache_dir, today.isoformat()):
        log("  tourapi: 오늘 호출 한도에 닿아 캐시만으로 만든다")

    mod.FEST_FROM, mod.FEST_TO = festival_window(today)
    tmp = Path(tempfile.mkdtemp(prefix="tourapi-out-"))
    try:
        codes = Path(curated_dir) / "kr-tour-codes.json"
        if codes.is_file():
            shutil.copyfile(codes, tmp / "kr-tour-codes.json")
        kwargs = dict(budget=budget, ids=None, out_dir=tmp, cache_dir=cache_dir, catalog=catalog_path,
                      extras=None, use_extras=False, offline=False, spacing=0.3, write=True, check_images=0,
                      verify_images=verify_images, key=key, log=log)
        accepted = inspect.signature(mod.run_snapshot).parameters
        summary = mod.run_snapshot(**{k: v for k, v in kwargs.items() if k in accepted})
        info["calls"] = summary.get("callsThisRun")
        info["usedToday"] = summary.get("usedToday")
        info["fatal"] = summary.get("fatal")
        pending = {k: set(v or []) for k, v in (summary.get("pending") or {}).items()}
        probs = problem_ids(summary.get("problems") or [])
        catalog = packlib.read_json(catalog_path) or {}
        catalog_ids = {d.get("id") for d in catalog.get("destinations") or [] if isinstance(d, dict)}
        out: Dict[str, Dict[str, Any]] = {}
        notes: Dict[str, List[str]] = {}
        errors: Dict[str, str] = {}
        for ds, kind in KINDS.items():
            new = packlib.read_json(tmp / FILES[ds])
            if not isinstance(new, dict):
                errors[ds] = "출력 파일 없음"
                continue
            keep = pending.get(kind, set()) | probs.get(kind, set())
            try:
                out[ds], notes[ds] = merge(kind, new, previous.get(ds), keep, catalog_ids, today.isoformat())
            except RuntimeError as e:
                errors[ds] = str(e)
        info["notes"] = {k: v[:20] for k, v in notes.items() if v}
        info["errors"] = errors
        return out, info
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
