#!/usr/bin/env python3
"""여행지 기후 예년값 → destination-climate.json (데이터 팩 데이터셋 'destination-climate', 1년에 한 번, 키 없음).

PASSLOG/scripts/climate_normals.py + climate_correct.py 와 같은 로직이다(hub/PLAN.md §4-6). 다른 점:
  - 지점 고도는 카탈로그 destinations.json 의 elev 를 쓴다(DEM API 를 해마다 부르지 않는다).
    elev 가 없는 새 여행지는 건너뛰고 경고한다 — PASSLOG/scripts/climate_correct.py --write-elev 로 먼저 넣는다.
  - 표준 라이브러리 urllib 로 받는다(GitHub Actions Ubuntu 는 루트 인증서가 있다). 실패하면 curl 로 한 번 더.

소스: NASA POWER Climatology API (MERRA-2 월평균 클라이머톨로지, 격자 ~0.5°×0.625°)
  GET https://power.larc.nasa.gov/api/temporal/climatology/point?parameters=T2M,PRECTOTCORR&community=RE&longitude=..&latitude=..&format=JSON
  - 응답 geometry.coordinates[2] = 격자 고도 → t = T2M + 0.0065 × (격자 고도 − 지점 고도)
  - PRECTOTCORR 는 mm/day → 그달 날 수를 곱해 월 강수량 mm
  - 한 번에 한 곳씩(직렬), 요청 사이 1.1초 이상.

출력 형식 (ios/Packages/PasslogKit/Sources/PasslogCore/Climate.swift `DestinationClimate` 가 읽는다):
  {"version": 1, "updated": "YYYY-MM", "source": …, "units": …, "correction": …, "pointOverrides": {id: {lat, lon, note}},
   "destinations": {id: {"t": [12], "pr": [12], "elev": m}}}

예)
  # 네트워크 없이: 원자료 캐시(PASSLOG/scripts/climate-raw.json)로 다시 만들기 — 앱 번들과 바이트가 같아야 한다
  python3 -I info-pipeline/scripts/sources/climate.py --raw scripts/climate-raw.json --out /tmp/destination-climate.json
  # 파이프라인 (1년에 한 번)
  python3 -I scripts/sources/climate.py --catalog data/destinations.json --previous data/destination-climate.json --out public/packs/…

안전장치: 받기 실패한 곳은 이전 파일의 값을 그대로 쓴다. 20%가 넘게 실패하면 아무것도 쓰지 않고 경고(종료 코드 0).
이전 파일과 내용(destinations)이 같으면 쓰지 않는다(같은 입력 → 같은 파일).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

ENDPOINT = "https://power.larc.nasa.gov/api/temporal/climatology/point"
PARAMS = "T2M,PRECTOTCORR"
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
DAYS = [31, 28.25, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
LAPSE = 0.0065
MIN_INTERVAL = 1.1
MAX_FAIL_RATIO = 0.2
USER_AGENT = "PASSLOG-info-pipeline/1.0 (destination climate normals, yearly, serial) python-urllib/3"

SCRIPT_DIR = Path(__file__).resolve().parent
PIPELINE_DIR = SCRIPT_DIR.parent.parent


def first_existing(*paths: Path) -> Path:
    return next((p for p in paths if p.exists()), paths[0])


# 모노레포: ../shared/data/*.json, 별도 리포: data/*.json (복사본)
DEFAULT_CATALOG = first_existing(PIPELINE_DIR.parent / "shared" / "data" / "destinations.json",
                                 PIPELINE_DIR / "data" / "destinations.json")
DEFAULT_PREVIOUS = first_existing(PIPELINE_DIR.parent / "shared" / "data" / "destination-climate.json",
                                  PIPELINE_DIR / "data" / "destination-climate.json")

# 기후 · 고도를 카탈로그 좌표가 아닌 곳에서 재는 여행지 — PASSLOG/scripts/climate_normals.py 의 CLIMATE_POINTS 와 같아야 한다
CLIMATE_POINTS: dict[str, tuple[float, float, str]] = {
    "hadong-gurye": (35.0674, 127.7514,
                     "하동읍 중심(하동군청) 기준 — 카탈로그 좌표(35.13, 127.6)는 하동·구례 두 읍의 가운데로 "
                     "지리산 자락(DEM 약 692m)에 찍혀 있어 기후가 실제 마을보다 춥게 나온다. 좌표는 그대로 두고 기후·고도만 하동읍에서 잰다."),
    "yakushima": (30.4256, 130.5694,
                  "미야노우라(宮之浦) 항구 마을 기준 — 카탈로그 좌표는 섬 한가운데 산지(DEM 약 1,351m)라 "
                  "고도 보정을 하면 기온이 8°C 넘게 낮아진다(7월 19°C)."),
    "mauritius": (-20.1609, 57.5012,
                  "포트루이스(해안) 기준 — 카탈로그 좌표는 섬 중앙 고원(DEM 약 567m)이라 해안 휴양지보다 3°C 안팎 낮게 나온다."),
    "saipan": (15.2069, 145.7197,
               "가라판(해안) 기준 — 카탈로그 좌표는 섬 가운데 언덕(DEM 약 373m)이라 해안보다 2°C 남짓 낮게 나온다."),
}


def warn(msg: str) -> None:
    print(f"::warning::climate: {msg}", file=sys.stderr)


def climate_point(d: dict[str, Any]) -> tuple[float, float]:
    if d["id"] in CLIMATE_POINTS:
        lat, lon, _ = CLIMATE_POINTS[d["id"]]
        return lat, lon
    return d["lat"], d["lon"]


def _get(url: str) -> bytes:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=90) as r:  # noqa: S310 (고정 https 엔드포인트)
            return r.read()
    except Exception:  # noqa: BLE001 — python.org 빌드 등 인증서 문제 → curl
        return subprocess.run(["curl", "-sS", "-f", "-m", "90", "-A", USER_AGENT, url],
                              capture_output=True, check=True).stdout


def fetch_power(lat: float, lon: float) -> tuple[float, list[float], list[float]]:
    """한 지점 → (격자 고도 m, 월평균 기온 12, 강수 mm/day 12). 실패하면 예외"""
    url = f"{ENDPOINT}?parameters={PARAMS}&community=RE&longitude={lon}&latitude={lat}&format=JSON"
    j = json.loads(_get(url))
    p = j["properties"]["parameter"]
    t = [float(p["T2M"][m]) for m in MONTHS]
    pr = [float(p["PRECTOTCORR"][m]) for m in MONTHS]
    if any(v < -900 for v in t + pr):
        raise ValueError("missing values (-999)")
    return float(j["geometry"]["coordinates"][2]), [round(v, 2) for v in t], [round(v, 3) for v in pr]


def entry(grid_elev: float, t_raw: list[float], pr_day: list[float], elev: float) -> dict[str, Any]:
    corr = LAPSE * (grid_elev - elev)
    return {"t": [round(v + corr, 1) + 0.0 for v in t_raw],
            "pr": [int(round(v * n)) for v, n in zip(pr_day, DAYS)],
            "elev": int(round(elev))}


def build(catalog: list[dict[str, Any]], *, raw: dict[str, Any] | None = None,
          previous: dict[str, Any] | None = None,
          fetch: Callable[[float, float], tuple[float, list[float], list[float]]] = fetch_power,
          sleep: Callable[[float], None] = time.sleep, updated: str | None = None) -> dict[str, Any] | None:
    """카탈로그 → 번들 dict. raw(원자료 캐시)가 있으면 네트워크 없이 만든다. 실패가 많으면 None"""
    prev_rows = (previous or {}).get("destinations", {})
    out: dict[str, Any] = {}
    failed: list[str] = []
    last = 0.0
    for d in catalog:
        did = d["id"]
        if raw is not None:
            p = raw["points"].get(did)
            e = (raw.get("elevations", {}).get(did) or {}).get("elev", d.get("elev"))
            if p is None or e is None:
                failed.append(did)
                continue
            out[did] = entry(p["gridElev"], p["tRaw"], p["prDay"], e)
            continue
        if d.get("elev") is None:
            warn(f"{did}: destinations.json 에 elev 가 없어 건너뜀")
            failed.append(did)
            continue
        lat, lon = climate_point(d)
        ok = False
        for attempt in range(3):
            wait = MIN_INTERVAL - (time.monotonic() - last)
            if wait > 0:
                sleep(wait)
            last = time.monotonic()
            try:
                grid, t_raw, pr_day = fetch(lat, lon)
                out[did] = entry(round(grid), t_raw, pr_day, float(d["elev"]))
                ok = True
                break
            except Exception as ex:  # noqa: BLE001
                warn(f"{did} 실패({attempt + 1}): {ex}")
                sleep(3 * (attempt + 1))
        if not ok:
            failed.append(did)
    for did in failed:
        if did in prev_rows:
            out[did] = prev_rows[did]  # 이전 값 유지
    if catalog and len(failed) / len(catalog) > MAX_FAIL_RATIO:
        warn(f"{len(failed)}/{len(catalog)}곳 실패 — 이전 파일을 그대로 둠")
        return None
    order = [d["id"] for d in catalog if d["id"] in out]
    return {
        "version": 1,
        "updated": updated or time.strftime("%Y-%m"),
        "source": "NASA POWER Climatology API v2.10.1 (MERRA-2, 2001–2025 월평균) + OpenTopoData mapzen 지점 고도",
        "units": {"t": "°C 월평균 기온(고도 보정)", "pr": "mm 월 강수량", "elev": "m 지점 고도"},
        "correction": "t = tRaw + 0.0065 × (gridElev − elev). 일본 3곳 대조 평균 절대 오차 약 1.2°C — 분류에만 쓰고 표시는 반올림",
        "pointOverrides": {k: {"lat": v[0], "lon": v[1], "note": v[2]} for k, v in CLIMATE_POINTS.items()},
        "destinations": {k: out[k] for k in order},
    }


def serialize(obj: dict[str, Any]) -> str:
    """한 여행지 한 줄 — PASSLOG/scripts/climate_correct.py serialize 와 같은 형식"""
    def inline(v: Any) -> str:
        return json.dumps(v, ensure_ascii=False, separators=(", ", ": "))
    lines = ["{"]
    for k in [k for k in obj if k != "destinations"]:
        if k == "pointOverrides":
            lines.append(f'  "{k}": {{')
            items = list(obj[k].items())
            for n, (kk, vv) in enumerate(items):
                lines.append(f'    "{kk}": {inline(vv)}' + ("," if n < len(items) - 1 else ""))
            lines.append("  },")
        else:
            lines.append(f'  "{k}": {inline(obj[k])},')
    lines.append('  "destinations": {')
    items = list(obj["destinations"].items())
    for n, (k, v) in enumerate(items):
        lines.append(f'    "{k}": {inline(v)}' + ("," if n < len(items) - 1 else ""))
    lines.append("  }")
    lines.append("}")
    return "\n".join(lines) + "\n"


def validate(obj: dict[str, Any], catalog: list[dict[str, Any]]) -> list[str]:
    """앱이 읽을 수 있는지: 모든 행이 t · pr 12개, 기온 −60~45°C, 강수 0 이상"""
    errors = []
    for k, v in obj.get("destinations", {}).items():
        t, pr = v.get("t", []), v.get("pr", [])
        if len(t) != 12 or len(pr) != 12:
            errors.append(f"{k}: 12개가 아님")
        elif not all(-60 <= x <= 45 for x in t) or not all(x >= 0 for x in pr):
            errors.append(f"{k}: 범위 밖 값")
    missing = [d["id"] for d in catalog if d["id"] not in obj.get("destinations", {})]
    if missing:
        errors.append(f"빠진 여행지 {len(missing)}곳: {', '.join(missing[:10])}")
    return errors


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    ap.add_argument("--previous", default=str(DEFAULT_PREVIOUS), help="이전 destination-climate.json (실패 시 대체 · 같으면 안 씀)")
    ap.add_argument("--raw", help="원자료 캐시(PASSLOG/scripts/climate-raw.json) — 있으면 네트워크 없이")
    ap.add_argument("--out", required=True)
    ap.add_argument("--updated", help="updated 값 고정 (YYYY-MM)")
    args = ap.parse_args(argv)

    catalog = json.loads(Path(args.catalog).read_text(encoding="utf-8"))["destinations"]
    prev_path = Path(args.previous)
    previous = json.loads(prev_path.read_text(encoding="utf-8")) if prev_path.exists() else None
    raw = json.loads(Path(args.raw).read_text(encoding="utf-8")) if args.raw else None

    updated = args.updated or (previous or {}).get("updated") if raw is not None else args.updated
    obj = build(catalog, raw=raw, previous=previous, updated=updated)
    if obj is None:
        return 0
    errors = validate(obj, catalog)
    if errors:
        for e in errors:
            warn(e)
        return 0
    # 내용이 같으면 updated 도 이전 값 그대로 (같은 입력 → 같은 바이트)
    if previous and previous.get("destinations") == obj["destinations"]:
        obj["updated"] = previous.get("updated", obj["updated"])
        out = Path(args.out)
        if out.exists() and out.read_text(encoding="utf-8") == serialize(obj):
            print("climate: 바뀐 것 없음", file=sys.stderr)
            return 0
    Path(args.out).write_text(serialize(obj), encoding="utf-8")
    print(f"climate: {len(obj['destinations'])}곳 → {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
