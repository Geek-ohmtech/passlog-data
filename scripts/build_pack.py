#!/usr/bin/env python3
"""PASSLOG 데이터 팩 빌더 — public/packs/v2/{manifest.json, status.json, history.json, datasets/<id>.<sha8>.json} + public/v1/*

  1) 이번 실행에서 만들 데이터셋을 고른다 (cron 묶음 · --only · 큐레이션은 매번)
  2) 소스를 돌린다 (데이터셋마다 따로 성공/실패. 키가 없으면 그 데이터셋만 건너뜀)
  3) 검증 (구조 · 크기 5MB · 경보 0–4 · 환율 ±20% · 사진 id 게이트 …) → 실패하면 이전 파일 유지
  4) 이전 파일과 견줘 (실행 시각 같은 volatile 키를 빼고) 같으면 이전 파일 그대로, 다르면 정규화 JSON →
     datasets/<id>.<sha256 앞 8자>.json (변경 불가 파일)
  5) 바뀐 게 하나라도 있을 때만 manifest.json 을 다시 쓴다 (sequence+1, publishedAt) → 앱 확인은 평소 304
  6) status.json (소스별 상태 · 연속 실패 · 알림), history.json (참조가 끊긴 해시 파일은 7일 뒤 삭제)
  7) v1 호환 파일 (meta · rates · advisories · countries/{ISO2}) — 이미 설치된 앱의 SnapshotClient 용

예)
  # 로컬 시험: 네트워크 · 키 없이 shared/data 로 전체 팩 → http://localhost:8765/packs/v2/
  python3 -I info-pipeline/scripts/build_pack.py --offline --out /tmp/passlog-pack
  python3 -m http.server 8765 --directory /tmp/passlog-pack
  # GitHub Actions (배포 리포 passlog-data 루트)
  python3 -I scripts/build_pack.py --event schedule --schedule "17 */6 * * *" --out public

키(환경 변수, 값은 출력하지 않음): DATA_GO_KR_KEY(입국요건 · TourAPI · 특일), KOREAEXIM_KEY(환율, 없으면 Frankfurter 만)
종료 코드: 0 (소스 실패는 status.json 에 남기고 성공으로 끝냄) · 1 --strict 에서 실패한 데이터셋이 있음 · 2 인자 오류
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import packlib  # noqa: E402
from packlib import DATASETS, Spec  # noqa: E402

MOFA_SOURCE = {"name": "외교부 해외안전여행", "url": "https://www.0404.go.kr"}
EXIM_SOURCE = {"name": "한국수출입은행", "url": "https://www.koreaexim.go.kr"}
FRANKFURTER_SOURCE = {"name": "Frankfurter", "url": "https://frankfurter.dev"}
ALERT_AFTER = {"advisories": 2, "rates": 2, "entry": 4}
DEFAULT_ALERT_AFTER = 4
STATUS_REWRITE_HOURS = 24


class Skip(Exception):
    """이번에는 만들지 않음 (키 없음 · 오프라인 · 파일 없음) — 이전 파일 유지, 실패로 세지 않음"""


class Reject(Exception):
    """검증 실패 — 이전 파일 유지, 실패로 세고 바로 알림"""


class Produced:
    __slots__ = ("obj", "raw", "as_of", "min_app", "warnings", "sources")

    def __init__(self, obj: Any, raw: Optional[bytes] = None, as_of: Optional[str] = None,
                 min_app: Optional[str] = None, warnings: Optional[List[str]] = None,
                 sources: Optional[List[str]] = None) -> None:
        self.obj = obj
        self.raw = raw
        self.as_of = as_of
        self.min_app = min_app
        self.warnings = warnings or []
        self.sources = sources or []


# ---------------------------------------------------------------------------
# 실행 문맥
# ---------------------------------------------------------------------------
class Ctx:
    def __init__(self, args: argparse.Namespace, log: Callable[[str], None]) -> None:
        self.args = args
        self.log = log
        self.now: dt.datetime = (packlib.parse_date(args.now) if args.now else packlib.now_kst()).astimezone(packlib.KST)
        self.today = self.now.date()
        self.offline: bool = args.offline
        self.out = Path(args.out).resolve()
        self.pack_dir = self.out / "packs" / "v2"
        self.ds_dir = self.pack_dir / "datasets"
        self.v1_dir = self.out / "v1"
        self.curated = Path(args.curated).resolve() if args.curated else packlib.default_curated_dir()
        self.data_key = "" if args.offline else packlib.secret("DATA_GO_KR_KEY")
        self.exim_key = "" if args.offline else packlib.secret("KOREAEXIM_KEY")
        self.prev_manifest: Optional[Dict[str, Any]] = packlib.read_json(self.pack_dir / "manifest.json")
        if not isinstance(self.prev_manifest, dict) or not isinstance(self.prev_manifest.get("datasets"), dict):
            self.prev_manifest = None
        self.history: Dict[str, Any] = packlib.read_json(self.pack_dir / "history.json") or {"files": {}}
        self.history.setdefault("files", {})
        self._prev_cache: Dict[str, Any] = {}
        self._prev_ok: Dict[str, Optional[Dict[str, Any]]] = {}
        self._tour: Optional[Tuple[Dict[str, Any], Dict[str, Any]]] = None
        self._tour_error: Optional[str] = None
        self.alerts: List[Dict[str, str]] = []

    # 이전에 실은 파일 — 파일이 있고 **바이트 sha256 이 manifest 값과 같을 때만** (줄바꿈 정규화 등으로 바이트가 바뀌었는데
    # JSON 의미가 같아 'unchanged' 로 남으면, manifest sha 와 실제 파일이 어긋난 채 배포돼 모든 앱이 그 데이터셋을 거부한다)
    def prev_entry(self, ds: str) -> Optional[Dict[str, Any]]:
        if not self.prev_manifest:
            return None
        if ds not in self._prev_ok:
            e = self.prev_manifest["datasets"].get(ds)
            ok = None
            if isinstance(e, dict) and e.get("path"):
                f = self.ds_dir / str(e["path"])
                if f.is_file() and packlib.sha256_hex(f.read_bytes()) == str(e.get("sha256", "")).lower():
                    ok = e
                elif f.is_file():
                    self.log(f"  {ds}: 이전 파일 {f.name} 의 sha256 이 manifest 와 달라 다시 쓴다")
            self._prev_ok[ds] = ok
        return self._prev_ok[ds]

    def prev_obj(self, ds: str) -> Any:
        if ds not in self._prev_cache:
            e = self.prev_entry(ds)
            self._prev_cache[ds] = packlib.read_json(self.ds_dir / e["path"]) if e else None
        return self._prev_cache[ds]

    def curated_path(self, name: str) -> Path:
        return self.curated / name

    def curated_obj(self, name: str) -> Any:
        return packlib.read_json(self.curated_path(name))

    def need_key(self, spec: Spec, value: str) -> None:
        if self.offline:
            raise Skip("오프라인")
        if not value:
            raise Skip(f"{spec.key} 없음")


# ---------------------------------------------------------------------------
# 소스
# ---------------------------------------------------------------------------
def from_curated(ctx: Ctx, spec: Spec) -> Produced:
    path = ctx.curated_path(spec.bundled or "")
    if not spec.bundled or not path.is_file():
        raise Skip("큐레이션 파일 없음")
    raw = path.read_bytes()
    try:
        obj = json.loads(raw.decode("utf-8"))
    except ValueError as e:
        raise Reject(f"JSON 이 아님: {e}") from None
    return Produced(obj, raw=raw, as_of=packlib.data_as_of(spec.id, obj))


def produce_destinations(ctx: Ctx, spec: Spec) -> Produced:
    import make_photo_ids

    p = from_curated(ctx, spec)
    photo_doc = ctx.curated_obj("photo-ids.json")
    if not isinstance(photo_doc, dict):
        photos = packlib.MONOREPO_ROOT / "shared" / "data" / "destination-photos"
        version = make_photo_ids.app_version()
        if photos.is_dir() and version:  # 모노레포: 지금 앱 빌드에 들어가는 사진
            photo_doc = {"versions": {version: make_photo_ids.photo_ids(photos)}}
    try:
        version = make_photo_ids.gate(p.obj, photo_doc)
    except ValueError as e:
        raise Reject(f"사진 게이트: {e}") from None
    if version and _version_gt(version, packlib.DEFAULT_MIN_APP_VERSION):
        p.min_app = version
        p.warnings.append(f"번들 사진이 {version} 부터 있어 minAppVersion={version}")
    return p


def _version_gt(a: str, b: str) -> bool:
    import make_photo_ids

    return make_photo_ids.version_key(a) > make_photo_ids.version_key(b)


def produce_climate(ctx: Ctx, spec: Spec) -> Produced:
    if not ctx.args.refresh_climate or ctx.offline:
        return from_curated(ctx, spec)
    prev = ctx.prev_entry(spec.id)
    previous = (ctx.ds_dir / prev["path"]) if prev else ctx.curated_path(spec.bundled or "")
    with tempfile.TemporaryDirectory(prefix="climate-") as tmp:
        out = Path(tmp) / "destination-climate.json"
        cmd = [sys.executable, "-I", str(SCRIPT_DIR / "sources" / "climate.py"),
               "--catalog", str(ctx.curated_path("destinations.json")), "--previous", str(previous), "--out", str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        sys.stderr.write(r.stderr[-4000:])
        if r.returncode != 0:
            raise RuntimeError(f"climate.py 종료 코드 {r.returncode}")
        if not out.is_file():  # 이전 파일과 내용이 같음 → 이전 것(또는 큐레이션) 그대로
            source = previous if previous.is_file() else ctx.curated_path(spec.bundled or "")
            raw = source.read_bytes()
            obj = json.loads(raw.decode("utf-8"))
            return Produced(obj, raw=raw, as_of=packlib.data_as_of(spec.id, obj))
        raw = out.read_bytes()
        obj = json.loads(raw.decode("utf-8"))
        return Produced(obj, raw=raw, as_of=packlib.data_as_of(spec.id, obj))


def produce_advisories(ctx: Ctx, spec: Spec) -> Produced:
    if ctx.offline:
        return from_curated(ctx, spec)
    prev = ctx.prev_entry(spec.id)
    previous = (ctx.ds_dir / prev["path"]) if prev else ctx.curated_path("advisories.json")
    with tempfile.TemporaryDirectory(prefix="advisories-") as tmp:
        out = Path(tmp) / "advisories.json"
        cmd = [sys.executable, "-I", str(SCRIPT_DIR / "advisories.py"),
               "--countries", str(ctx.curated_path("countries.json")),
               "--catalog", str(ctx.curated_path("destinations.json")),
               "--out", str(out)]
        if previous.is_file():
            cmd += ["--previous", str(previous)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        sys.stderr.write(r.stderr[-6000:])
        if r.returncode != 0:
            raise RuntimeError(f"advisories.py 종료 코드 {r.returncode} (여행지 판정 오류)")
        if not out.is_file():
            raise RuntimeError("0404 여행경보를 읽지 못했거나 단계가 한꺼번에 많이 바뀌어 이전 파일 유지 (advisories.py 경고 참고)")
        obj = json.loads(out.read_text(encoding="utf-8"))
    return Produced(obj, as_of=packlib.data_as_of(spec.id, obj), sources=[MOFA_SOURCE["name"]])


def produce_entry(ctx: Ctx, spec: Spec) -> Produced:
    ctx.need_key(spec, ctx.data_key)
    import fetch

    countries = (ctx.curated_obj("countries.json") or {}).get("countries") or []
    codes = [c["code"] for c in countries if isinstance(c, dict) and c.get("code")]
    name_to_code = {c["nameKo"]: c["code"] for c in countries if isinstance(c, dict) and c.get("nameKo")}
    try:
        table = fetch.fetch_entry(ctx.data_key, codes, name_to_code)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(packlib.scrub(e, ctx.data_key)[:300]) from None
    if len(table) < 100:
        raise Reject(f"입국허가요건 나라 수가 이상함: {len(table)}")
    data = {k: table[k] for k in sorted(table)}
    as_of = packlib.iso(ctx.now)
    return Produced(packlib.envelope(spec.id, as_of, data, [MOFA_SOURCE]), as_of=as_of)


def produce_rates(ctx: Ctx, spec: Spec) -> Produced:
    from sources import fx

    if ctx.args.rates_file:
        obj = packlib.read_json(Path(ctx.args.rates_file))
        if not isinstance(obj, dict):
            raise Reject("--rates-file 을 읽지 못함")
        obj = {k: obj[k] for k in ("base", "date", "rates", "sources", "dates") if k in obj}
        return Produced(obj, as_of=packlib.normalize_as_of(obj.get("date")))
    if ctx.offline:
        raise Skip("오프라인 (--rates-file 로 시험 가능)")
    try:
        doc, info = fx.build(exim_key=ctx.exim_key, today=ctx.today, previous=ctx.prev_obj(spec.id),
                             log=ctx.log)
    except fx.RatesRejected as e:
        raise Reject(str(e)) from None
    return Produced(doc, as_of=packlib.normalize_as_of(doc["date"]), warnings=info["warnings"], sources=info["sources"])


def produce_holidays_kr(ctx: Ctx, spec: Spec) -> Produced:
    from sources import holidays as hsrc

    base = from_curated(ctx, spec)
    kasi_rows = None
    warnings: List[str] = []
    if ctx.data_key and not ctx.offline:
        try:
            kasi_rows = hsrc.fetch_kasi(ctx.data_key, [ctx.today.year, ctx.today.year + 1, ctx.today.year + 2])
        except Exception as e:  # noqa: BLE001 — 특일은 보강일 뿐
            warnings.append("특일 정보: " + packlib.scrub(e, ctx.data_key)[:200])
    elif not ctx.offline:
        warnings.append("DATA_GO_KR_KEY 없음 — 임시공휴일(특일 정보) 보강 건너뜀")
    pkg = hsrc._holidays_pkg()
    if pkg is None:
        warnings.append("python-holidays 없음 — 계산 · 교차 확인 건너뜀")
    doc, used, w = hsrc.build_kr(base.obj, today=ctx.today, pkg=pkg, kasi_rows=kasi_rows)
    warnings += w
    if doc == base.obj:
        base.warnings = warnings
        return base
    doc["updated"] = ctx.today.isoformat()
    note = " 파이프라인이 python-holidays(큐레이션에 없는 해 · 교차 확인)와 천문연 특일 정보(임시공휴일)로 보강해요."
    if note.strip() not in str(doc.get("source", "")):
        doc["source"] = str(doc.get("source", "")) + note
    return Produced(doc, as_of=packlib.data_as_of(spec.id, doc), warnings=warnings, sources=used)


def produce_holidays_world(ctx: Ctx, spec: Spec) -> Produced:
    from sources import holidays as hsrc

    pkg = hsrc._holidays_pkg()
    if pkg is None:
        raise Skip("python-holidays 없음")
    catalog = (ctx.curated_obj("destinations.json") or {}).get("destinations") or []
    codes = {d.get("countryCode") for d in catalog if isinstance(d, dict) and d.get("countryCode")}
    data, skipped = hsrc.build_world(codes, today=ctx.today, pkg=pkg)
    if len(data["countries"]) < max(10, len(codes) // 2):
        raise Reject(f"공휴일 나라 수가 적음: {len(data['countries'])}/{len(codes)}")
    as_of = ctx.today.isoformat()
    warnings = [f"python-holidays 미지원: {', '.join(skipped)}"] if skipped else []
    return Produced(packlib.envelope(spec.id, as_of, data, [hsrc.PYHOLIDAYS_SOURCE]), as_of=as_of, warnings=warnings)


def produce_local_apps_status(ctx: Ctx, spec: Spec) -> Produced:
    if ctx.offline:
        raise Skip("오프라인")
    from sources import apps

    local_apps = ctx.curated_obj("local-apps.json")
    if not isinstance(local_apps, dict):
        raise Skip("local-apps.json 없음")
    data = apps.build(local_apps, previous=ctx.prev_obj(spec.id), today=ctx.today.isoformat(), log=ctx.log)
    as_of = packlib.iso(ctx.now)
    return Produced(packlib.envelope(spec.id, as_of, data, [apps.ITUNES_SOURCE]), as_of=as_of)


def _tour(ctx: Ctx) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    if ctx._tour_error:
        raise RuntimeError(ctx._tour_error)
    if ctx._tour is None:
        from sources import tourapi

        previous = {}
        for ds, name in tourapi.FILES.items():
            previous[ds] = ctx.prev_obj(ds) or ctx.curated_obj(name)
        try:
            ctx._tour = tourapi.run(key=ctx.data_key, cache_dir=Path(ctx.args.tourapi_cache),
                                    catalog_path=ctx.curated_path("destinations.json"), curated_dir=ctx.curated,
                                    previous=previous, now=ctx.now, per_run=ctx.args.tourapi_budget,
                                    day_cap=ctx.args.tourapi_day_cap, log=ctx.log)
        except Exception as e:  # noqa: BLE001
            ctx._tour_error = packlib.scrub(f"{type(e).__name__}: {e}", ctx.data_key)[:300]
            raise RuntimeError(ctx._tour_error) from None
        info = ctx._tour[1]
        ctx.log(f"  tourapi: 호출 {info.get('calls')} · 오늘 {info.get('usedToday')}/{info.get('budget')} · "
                f"증분 {info.get('sync')} · 만료 {info.get('expired')}")
    return ctx._tour


def produce_tour(ctx: Ctx, spec: Spec) -> Produced:
    if ctx.offline:
        return from_curated(ctx, spec)
    ctx.need_key(spec, ctx.data_key)
    out, info = _tour(ctx)
    if spec.id in (info.get("errors") or {}):
        raise RuntimeError(info["errors"][spec.id])
    obj = out.get(spec.id)
    if not isinstance(obj, dict):
        raise RuntimeError("출력 없음")
    warnings = list((info.get("notes") or {}).get(spec.id, []))[:10]
    if info.get("fatal"):
        warnings.append(str(info["fatal"]))
    return Produced(obj, as_of=packlib.data_as_of(spec.id, obj), warnings=warnings)


def produce_source_status(ctx: Ctx, spec: Spec) -> Produced:
    if ctx.offline:
        raise Skip("오프라인")
    from sources import urlwatch

    sets = urlwatch.url_sets_from(ctx.curated)
    if not sets:
        raise Skip("감시할 큐레이션 파일(airline-baggage · travel-guide)이 아직 없음")
    data, newly = urlwatch.check(sets, ctx.prev_obj(spec.id), ctx.today.isoformat())
    for u in newly:
        ctx.alerts.append({"dataset": spec.id, "kind": "changed", "message": f"공식 페이지 변경 감지 · 검수 필요: {u}"})
    as_of = packlib.iso(ctx.now)
    return Produced(packlib.envelope(spec.id, as_of, data, [{"name": "공식 페이지 변경 감지"}]), as_of=as_of)


def produce_planned(ctx: Ctx, spec: Spec) -> Produced:
    raise Skip("아직 만드는 곳 없음 (3단계: 인천공항 정기운항편 활용신청 후)")


PRODUCERS: Dict[str, Callable[[Ctx, Spec], Produced]] = {
    "advisories": produce_advisories,
    "entry": produce_entry,
    "rates": produce_rates,
    "holidays-kr": produce_holidays_kr,
    "holidays-world": produce_holidays_world,
    "destinations": produce_destinations,
    "destination-climate": produce_climate,
    "local-apps-status": produce_local_apps_status,
    "kr-restaurants": produce_tour,
    "kr-attractions": produce_tour,
    "kr-festivals": produce_tour,
    "source-status": produce_source_status,
    "flights-icn": produce_planned,
}


def producer_for(spec: Spec) -> Callable[[Ctx, Spec], Produced]:
    return PRODUCERS.get(spec.id) or from_curated


# ---------------------------------------------------------------------------
# 검증
# ---------------------------------------------------------------------------
def _is_day(s: Any) -> bool:
    return isinstance(s, str) and len(s) == 10 and packlib.parse_date(s) is not None


def validate(ds: str, obj: Any, data: bytes, ctx: Optional[Ctx] = None) -> None:
    spec = DATASETS[ds]
    if len(data) > packlib.MAX_DATASET_BYTES:
        raise Reject(f"크기 {len(data)} > 5MB")
    if spec.envelope:
        if not isinstance(obj, dict) or obj.get("id") != ds or not isinstance(obj.get("schema"), int) \
                or packlib.parse_date(obj.get("dataAsOf")) is None or "data" not in obj:
            raise Reject("봉투 형식이 아님 {schema, id, dataAsOf, sources, data}")
        if obj["schema"] > packlib.DATASET_SCHEMA:
            raise Reject(f"schema {obj['schema']} > {packlib.DATASET_SCHEMA}")
    if ds == "advisories":
        countries = obj.get("countries") if isinstance(obj, dict) else None
        if not isinstance(countries, dict) or len(countries) < 150:
            raise Reject("경보 나라 수가 150 미만")
        bad = [k for k, v in countries.items() if not isinstance(v, dict) or v.get("level") not in (0, 1, 2, 3, 4)]
        if bad:
            raise Reject(f"경보 단계가 0–4 가 아님: {', '.join(bad[:8])}")
        if packlib.parse_date(obj.get("asOf")) is None:
            raise Reject("asOf 없음")
    elif ds == "rates":
        rates = obj.get("rates") if isinstance(obj, dict) else None
        if obj.get("base") != "KRW" or not _is_day(obj.get("date")) or not isinstance(rates, dict) or len(rates) < 20:
            raise Reject("환율 형식 오류 (base KRW · date · 통화 20개 이상)")
        bad = [k for k, v in rates.items() if not isinstance(v, (int, float)) or not v > 0]
        if bad:
            raise Reject(f"환율 값 오류: {', '.join(bad[:8])}")
        for need in ("USD", "JPY", "EUR"):
            if need not in rates:
                raise Reject(f"{need} 환율 없음")
    elif ds == "holidays-kr":
        hs = obj.get("holidays") if isinstance(obj, dict) else None
        if not isinstance(hs, list) or len(hs) < 15 or not all(isinstance(h, dict) and _is_day(h.get("date")) for h in hs):
            raise Reject("공휴일 형식 오류")
        if ctx and not any(str(h["date"]).startswith(str(ctx.today.year)) for h in hs):
            raise Reject(f"{ctx.today.year}년 공휴일이 없음")
    elif ds == "destinations":
        ds_ = obj.get("destinations") if isinstance(obj, dict) else None
        if not isinstance(ds_, list) or len(ds_) < 50 or not all(isinstance(d, dict) and d.get("id") and "lat" in d for d in ds_):
            raise Reject("여행지 카탈로그 형식 오류")
        ids = [d["id"] for d in ds_]
        if len(ids) != len(set(ids)):
            raise Reject("여행지 id 중복")
    elif ds == "countries":
        cs = obj.get("countries") if isinstance(obj, dict) else None
        if not isinstance(cs, list) or len(cs) < 150 or not all(isinstance(c, dict) and c.get("code") for c in cs):
            raise Reject("나라 표 형식 오류")
    elif ds in ("kr-restaurants", "kr-attractions", "kr-festivals"):
        dests = obj.get("destinations") if isinstance(obj, dict) else None
        if not isinstance(dests, dict) or not dests:
            raise Reject("destinations 없음")
        for did, block in dests.items():
            items = block.get("items") if isinstance(block, dict) else None
            if not isinstance(items, list) or not all(isinstance(x, dict) and x.get("contentId") and x.get("title") for x in items):
                raise Reject(f"{did}: 항목 형식 오류 (contentId · title)")
            for x in items:
                for f in ("image", "thumb"):
                    if x.get(f) and not str(x[f]).startswith("https://"):
                        raise Reject(f"{did}: https 가 아닌 사진 주소")
    elif ds == "local-apps-status":
        apps = (obj.get("data") or {}).get("apps")
        if not isinstance(apps, dict) or not apps:
            raise Reject("apps 없음")
    elif ds == "holidays-world":
        if not isinstance((obj.get("data") or {}).get("countries"), dict):
            raise Reject("countries 없음")
    elif ds == "entry":
        if not isinstance(obj.get("data"), dict):
            raise Reject("data 없음")


# ---------------------------------------------------------------------------
# 게시 판단
# ---------------------------------------------------------------------------
def _age_hours(as_of: Optional[str], now: dt.datetime) -> float:
    d = packlib.parse_date(as_of)
    return (now - d).total_seconds() / 3600 if d else float("inf")


def decide(ctx: Ctx, spec: Spec, p: Produced) -> Tuple[Dict[str, Any], str]:
    """(manifest entry, "updated" | "unchanged")"""
    prev = ctx.prev_entry(spec.id)
    prev_obj = ctx.prev_obj(spec.id) if prev else None
    data = p.raw if p.raw is not None else packlib.canonical_bytes(p.obj)
    validate(spec.id, p.obj, data, ctx)
    if prev and prev_obj is not None and prev.get("minAppVersion") == p.min_app:
        same = packlib.without_keys(prev_obj, spec.volatile) == packlib.without_keys(p.obj, spec.volatile)
        if same:
            due = bool(spec.heartbeat) and _age_hours(prev.get("dataAsOf"), ctx.now) > (spec.heartbeat or 0)
            if not due or packlib.sha256_hex(data) == prev.get("sha256"):
                return prev, "unchanged"
    sha = packlib.sha256_hex(data)
    name = f"{spec.id}.{sha[:8]}.json"
    path = ctx.ds_dir / name
    if not path.is_file() or packlib.sha256_hex(path.read_bytes()) != sha:
        packlib.write_bytes_atomic(path, data)
    hist = ctx.history["files"].setdefault(name, {"dataset": spec.id, "sha256": sha,
                                                   "firstPublished": packlib.iso(ctx.now)})
    hist.pop("unreferencedSince", None)
    as_of = p.as_of
    if spec.kind == "curated" or not as_of:
        as_of = packlib.later_as_of(as_of, hist.get("firstPublished")) or packlib.iso(ctx.now)
    entry: Dict[str, Any] = {"path": name, "sha256": sha, "size": len(data), "schema": packlib.DATASET_SCHEMA,
                             "dataAsOf": as_of, "ttlHours": spec.ttl}
    if spec.stale is not None:
        entry["staleAfterHours"] = spec.stale
    if p.min_app:
        entry["minAppVersion"] = p.min_app
    if len(data) >= packlib.LARGE_BYTES and not spec.safety and not spec.no_large:
        entry["large"] = True
    if prev and {k: v for k, v in prev.items()} == entry:
        return prev, "unchanged"
    return entry, "updated"


# ---------------------------------------------------------------------------
# manifest · history · status
# ---------------------------------------------------------------------------
def write_manifest(ctx: Ctx, datasets: Dict[str, Dict[str, Any]]) -> Tuple[Dict[str, Any], bool]:
    ordered = {k: datasets[k] for k in packlib.by_priority(datasets)}
    prev = ctx.prev_manifest
    if prev and prev.get("datasets") == ordered and prev.get("schemaVersion") == packlib.MANIFEST_SCHEMA_VERSION:
        return prev, False
    # history.json 에도 마지막 sequence 를 남겨 manifest 가 지워져도 번호가 뒤로 가지 않게 (앱은 역행을 거부한다)
    seq = max(int((prev or {}).get("sequence") or 0), int(ctx.history.get("lastSequence") or 0)) + 1
    ctx.history["lastSequence"] = seq
    manifest = {
        "schemaVersion": packlib.MANIFEST_SCHEMA_VERSION,
        "sequence": seq,
        "packVersion": ctx.now.strftime("%Y.%m.%d-%H%M"),
        "publishedAt": packlib.iso(ctx.now),
        "minAppVersion": packlib.DEFAULT_MIN_APP_VERSION,
        "baseURL": "datasets/",
        "datasets": ordered,
        "signature": None,
    }
    data = packlib.pretty_bytes(manifest)
    if len(data) > packlib.MAX_MANIFEST_BYTES:
        raise RuntimeError("manifest 가 256KB 를 넘음")
    packlib.write_bytes_atomic(ctx.pack_dir / "manifest.json", data)
    return manifest, True


def prune(ctx: Ctx, referenced: set) -> List[str]:
    removed: List[str] = []
    files = ctx.history["files"]
    if ctx.ds_dir.is_dir():
        for f in ctx.ds_dir.iterdir():
            if packlib.DATASET_FILE_RE.match(f.name) and f.name not in files:
                files[f.name] = {"dataset": f.name.rsplit(".", 2)[0], "sha256": packlib.sha256_hex(f.read_bytes()),
                                 "firstPublished": packlib.iso(ctx.now)}
    for name in sorted(files):
        rec = files[name]
        if name in referenced:
            rec.pop("unreferencedSince", None)
            continue
        since = packlib.parse_date(rec.get("unreferencedSince"))
        if since is None:
            rec["unreferencedSince"] = packlib.iso(ctx.now)
        elif (ctx.now - since).total_seconds() > packlib.RETAIN_DAYS * 86400:
            try:
                (ctx.ds_dir / name).unlink()
            except FileNotFoundError:
                pass
            del files[name]
            removed.append(name)
    return removed


def write_history(ctx: Ctx) -> bool:
    path = ctx.pack_dir / "history.json"
    doc = {"schemaVersion": 1, "note": "참조가 끊긴 해시 파일은 unreferencedSince 7일 뒤 지운다 (build_pack.py)",
           "lastSequence": int(ctx.history.get("lastSequence") or 0),
           "files": {k: ctx.history["files"][k] for k in sorted(ctx.history["files"])}}
    old = packlib.read_json(path)
    if old == doc:
        return False
    packlib.write_bytes_atomic(path, packlib.pretty_bytes(doc))
    return True


def _stable(status: Dict[str, Any]) -> Any:
    """status.json 을 다시 쓸지 볼 때 견주는 부분: 실패 상태 · 연속 실패 수 · 알림 · sequence (시각 · 경고 문구 · 갱신/그대로 차이는 뺀다)"""
    rows = {}
    for ds, row in (status.get("datasets") or {}).items():
        if not isinstance(row, dict):
            continue
        state = row.get("state")
        rows[ds] = ("ok" if state in ("updated", "unchanged") else state, row.get("consecutiveFailures"),
                    row.get("message") if state in ("failed", "rejected") else None, row.get("dataAsOf"))
    return {"datasets": rows, "alerts": status.get("alerts"), "sequence": status.get("sequence"),
            "offline": status.get("offline")}


def build_status(ctx: Ctx, results: Dict[str, Dict[str, Any]], manifest: Dict[str, Any], selected: List[str]
                 ) -> Tuple[Dict[str, Any], bool]:
    path = ctx.pack_dir / "status.json"
    prev = packlib.read_json(path) or {}
    prev_ds = prev.get("datasets") or {}
    ds_status: Dict[str, Any] = {}
    alerts = list(ctx.alerts)
    for ds in packlib.DATASET_IDS:
        old = dict(prev_ds.get(ds) or {})
        r = results.get(ds)
        if r is None:
            if old:
                ds_status[ds] = old
            continue
        row: Dict[str, Any] = {"state": r["state"]}
        if r.get("message"):
            row["message"] = r["message"][:300]
        if r.get("warnings"):
            row["warnings"] = [w[:200] for w in r["warnings"][:8]]
        fails = int(old.get("consecutiveFailures") or 0)
        if r["state"] in ("updated", "unchanged"):
            fails = 0
            row["lastSuccessAt"] = packlib.iso(ctx.now)
        elif r["state"] in ("failed", "rejected"):
            fails += 1
            if old.get("lastSuccessAt"):
                row["lastSuccessAt"] = old["lastSuccessAt"]
        elif old.get("lastSuccessAt"):
            row["lastSuccessAt"] = old["lastSuccessAt"]
        row["consecutiveFailures"] = fails
        entry = manifest.get("datasets", {}).get(ds)
        if entry:
            row["dataAsOf"] = entry.get("dataAsOf")
        ds_status[ds] = row
        threshold = 1 if r["state"] == "rejected" else ALERT_AFTER.get(ds, DEFAULT_ALERT_AFTER)
        if r["state"] in ("failed", "rejected") and fails >= threshold:
            alerts.append({"dataset": ds, "kind": r["state"],
                           "message": f"{ds} {fails}회 연속 {'거부' if r['state'] == 'rejected' else '실패'}: {r.get('message', '')[:200]}"})
    status = {
        "schemaVersion": 1,
        "checkedAt": packlib.iso(ctx.now),
        "event": ctx.args.event,
        "schedule": ctx.args.schedule or None,
        "offline": ctx.offline,
        "selected": selected,
        "sequence": manifest.get("sequence"),
        "publishedAt": manifest.get("publishedAt"),
        "datasets": ds_status,
        "alerts": alerts,
    }
    prev_checked = packlib.parse_date(prev.get("checkedAt"))
    fresh = prev_checked is not None and (ctx.now - prev_checked).total_seconds() < STATUS_REWRITE_HOURS * 3600
    if prev and fresh and _stable(prev) == _stable(status):
        return prev, False
    packlib.write_bytes_atomic(path, packlib.pretty_bytes(status))
    return status, True


# ---------------------------------------------------------------------------
# v1 호환 (SnapshotClient)
# ---------------------------------------------------------------------------
def _put_v1(path: Path, obj: Any, volatile: Tuple[str, ...] = ("generatedAt",)) -> bool:
    prev = packlib.read_json(path)
    if prev is not None and packlib.without_keys(prev, volatile) == packlib.without_keys(obj, volatile):
        return False
    packlib.write_bytes_atomic(path, packlib.pretty_bytes(obj))
    return True


def write_v1(ctx: Ctx, datasets: Dict[str, Dict[str, Any]]) -> int:
    import fetch

    def load(ds: str) -> Any:
        e = datasets.get(ds)
        return packlib.read_json(ctx.ds_dir / e["path"]) if e else None

    now = packlib.iso(ctx.now)
    changed = 0
    adv = load("advisories")
    entry_env = load("entry")
    entry = entry_env.get("data") if isinstance(entry_env, dict) else None
    rates = load("rates")
    if isinstance(adv, dict):
        changed += _put_v1(ctx.v1_dir / "advisories.json", adv, ())
    sources = [MOFA_SOURCE]
    if isinstance(rates, dict):
        used = set((rates.get("sources") or {}).values()) or {"koreaexim"}
        if "koreaexim" in used:
            sources.append(EXIM_SOURCE)
        if "frankfurter" in used:
            sources.append(FRANKFURTER_SOURCE)
        doc = {"base": "KRW", "date": rates["date"], "generatedAt": now, "rates": rates["rates"]}
        for k in ("sources", "dates"):
            if rates.get(k):
                doc[k] = rates[k]
        changed += _put_v1(ctx.v1_dir / "rates.json", doc)
    countries = (ctx.curated_obj("countries.json") or {}).get("countries") or []
    if isinstance(adv, dict) or isinstance(entry, dict) or (ctx.v1_dir / "countries").is_dir():
        for c in countries:
            code = c.get("code") if isinstance(c, dict) else None
            if not code:
                continue
            path = ctx.v1_dir / "countries" / f"{code}.json"
            prev = packlib.read_json(path) or {}
            e = prev.get("entry") or {"visaFree": None, "maxStayDays": None, "note": None}
            if isinstance(entry, dict):
                e = entry.get(code) or {"visaFree": None, "maxStayDays": None, "note": None}
            if code == "KR":
                e = {"visaFree": True, "maxStayDays": None, "note": "대한민국 국민은 비자가 필요하지 않습니다."}
            a = prev.get("advisory") or {"level": 0, "special": False, "banned": False, "note": None}
            if isinstance(adv, dict):
                a = fetch.advisory_from_index(adv, code) or {"level": 0, "special": False, "banned": False, "note": None}
            obj = {"code": code, "nameKo": c.get("nameKo") or code,
                   "entry": {"visaFree": e.get("visaFree"), "maxStayDays": e.get("maxStayDays"), "note": e.get("note")},
                   "advisory": a, "sources": [MOFA_SOURCE["name"]], "generatedAt": now}
            changed += _put_v1(path, obj)
    meta_path = ctx.v1_dir / "meta.json"
    if changed or not meta_path.is_file():
        packlib.write_bytes_atomic(meta_path, packlib.pretty_bytes(
            {"schemaVersion": 1, "generatedAt": now, "sources": sources, "sample": False}))
        changed += 1
    return changed


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def plan(args: argparse.Namespace, log: Callable[[str], None]) -> int:
    """--plan: 만들 데이터셋만 알려 준다 (워크플로가 TourAPI 캐시를 복원 · 저장할지 정할 때)"""
    selected, unknown = packlib.select_datasets(args.event, args.schedule or "", (args.only or "").split(","))
    if unknown:
        log(f"오류: 모르는 데이터셋 {', '.join(unknown)}")
        return 2
    tour = bool(set(selected) & {"kr-restaurants", "kr-attractions", "kr-festivals"}) and not args.offline \
        and bool(packlib.secret("DATA_GO_KR_KEY"))
    log(f"만들 것: {', '.join(selected)} · TourAPI {'예' if tour else '아니오'}")
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as fh:
            fh.write(f"tourapi={'true' if tour else 'false'}\n")
    return 0


def run(args: argparse.Namespace, log: Callable[[str], None]) -> int:
    if args.plan:
        return plan(args, log)
    ctx = Ctx(args, log)
    selected, unknown = packlib.select_datasets(args.event, args.schedule or "", (args.only or "").split(","))
    if unknown:
        log(f"오류: 모르는 데이터셋 {', '.join(unknown)} — 가능한 값: {', '.join(packlib.DATASET_IDS)}")
        return 2
    if not ctx.curated.is_dir():
        log(f"오류: 큐레이션 폴더가 없음: {ctx.curated}")
        return 2
    log(f"데이터 팩: {packlib.iso(ctx.now)} · {args.event}{(' ' + args.schedule) if args.schedule else ''}"
        f"{' · 오프라인' if ctx.offline else ''} · 큐레이션 {ctx.curated} → {ctx.out}")
    log(f"  이번에 만들 것: {', '.join(selected)}")

    datasets: Dict[str, Dict[str, Any]] = {}
    if ctx.prev_manifest:
        for ds, e in ctx.prev_manifest["datasets"].items():
            if ds in DATASETS and ctx.prev_entry(ds):
                datasets[ds] = e
    results: Dict[str, Dict[str, Any]] = {}
    for ds in selected:
        spec = DATASETS[ds]
        try:
            p = producer_for(spec)(ctx, spec)
            entry, state = decide(ctx, spec, p)
            datasets[ds] = entry
            results[ds] = {"state": state, "warnings": p.warnings}
        except Skip as e:
            results[ds] = {"state": "skipped", "message": str(e)}
        except Reject as e:
            results[ds] = {"state": "rejected", "message": str(e)}
        except Exception as e:  # noqa: BLE001 — 데이터셋 하나의 실패가 전체를 멈추지 않게
            msg = packlib.scrub(f"{type(e).__name__}: {e}", ctx.data_key, ctx.exim_key)
            results[ds] = {"state": "failed", "message": msg[:300]}
        r = results[ds]
        mark = {"updated": "↑", "unchanged": "=", "skipped": "-", "failed": "✗", "rejected": "✗"}[r["state"]]
        line = f"  {mark} {ds:20s} {r['state']}"
        if r.get("message"):
            line += f" — {r['message']}"
        log(line)
        for w in r.get("warnings") or []:
            log(f"      ! {packlib.scrub(w, ctx.data_key, ctx.exim_key)[:200]}")

    manifest, manifest_changed = write_manifest(ctx, datasets)
    referenced = {e["path"] for e in manifest.get("datasets", {}).values()}
    removed = prune(ctx, referenced)
    history_changed = write_history(ctx)
    v1_changed = 0 if args.no_v1 else write_v1(ctx, manifest.get("datasets", {}))
    status, status_changed = build_status(ctx, results, manifest, selected)

    changed = manifest_changed or bool(removed) or history_changed or v1_changed > 0 or status_changed
    updated = [d for d, r in results.items() if r["state"] == "updated"]
    failed = [d for d, r in results.items() if r["state"] in ("failed", "rejected")]
    log(f"완료: sequence {manifest.get('sequence')}{' (새 manifest)' if manifest_changed else ' (manifest 그대로)'} · "
        f"갱신 {len(updated)} · 실패 {len(failed)} · 지운 파일 {len(removed)} · v1 변경 {v1_changed} · 알림 {len(status['alerts'])}")
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as fh:
            fh.write(f"changed={'true' if changed else 'false'}\n")
            fh.write(f"sequence={manifest.get('sequence')}\n")
            fh.write(f"alerts={len(status['alerts'])}\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(f"### 데이터 팩 sequence {manifest.get('sequence')}\n\n| 데이터셋 | 상태 | 메모 |\n|---|---|---|\n")
            for ds, r in results.items():
                fh.write(f"| {ds} | {r['state']} | {str(r.get('message') or '').replace('|', '/')[:120]} |\n")
    if args.strict and failed:
        return 1
    return 0


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="출력 루트 (그 안에 v1/ · packs/v2/). 이전 팩이 있으면 그것과 견준다")
    ap.add_argument("--curated", help=f"큐레이션 폴더 (기본: {packlib.default_curated_dir()})")
    ap.add_argument("--event", default="manual", help="schedule · push · workflow_dispatch · manual (기본)")
    ap.add_argument("--schedule", default="", help="cron 문자열 (event=schedule 일 때 데이터셋 묶음 선택)")
    ap.add_argument("--only", default="", help="쉼표로 구분한 데이터셋 id (비우면 묶음 전체)")
    ap.add_argument("--offline", action="store_true", help="네트워크 · 키 없이 큐레이션 파일만으로 (로컬 시험)")
    ap.add_argument("--now", help="기준 시각 ISO8601 (시험용)")
    ap.add_argument("--rates-file", help="환율을 이 v1 형식 파일로 (오프라인 시험용)")
    ap.add_argument("--refresh-climate", action="store_true", help="NASA POWER 로 destination-climate 다시 계산 (1년 1회)")
    ap.add_argument("--tourapi-cache", default=os.environ.get("TOURAPI_CACHE") or str(packlib.PIPELINE_DIR / ".cache" / "tourapi"),
                    help="TourAPI 원응답 캐시 (키 없음 · Actions cache 로 유지)")
    ap.add_argument("--tourapi-budget", type=int, default=600, help="TourAPI 실행당 최대 호출 (기본 600)")
    ap.add_argument("--tourapi-day-cap", type=int, default=900, help="TourAPI 하루(KST) 합계 상한 (기본 900, 개발 계정 1,000)")
    ap.add_argument("--no-v1", action="store_true", help="v1 호환 파일을 쓰지 않음")
    ap.add_argument("--strict", action="store_true", help="실패 · 거부된 데이터셋이 있으면 종료 코드 1")
    ap.add_argument("--plan", action="store_true", help="만들 데이터셋만 출력 (GITHUB_OUTPUT 에 tourapi=true|false)")
    return ap.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    return run(args, lambda m: print(m, file=sys.stderr, flush=True))


if __name__ == "__main__":
    sys.exit(main())
