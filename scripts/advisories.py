#!/usr/bin/env python3
"""외교부 여행경보 → advisories.json (앱 번들 · 정적 스냅샷 공용, Python 3.11+, 표준 라이브러리만).

소스 A(키 없음): 0404 해외안전여행 '여행경보 발령 현황' 페이지가 쓰는 공개 JSON
  POST https://www.0404.go.kr/travelAlert/apntStatus/getAlertList  (본문 {}, data.alertList[] 약 198행)
  - 문서화되지 않은 내부 엔드포인트다. 6시간 주기 파이프라인에서만 부르고, 앱은 부르지 않는다.

입력 데이터 (info-pipeline/data/)
  mofa-country-codes.json   0404 나라 이름 → ISO2 별칭 (countries.json 과 표기가 다른 것)
  advisory-geo.json         'ISO2|지역 문구' → 대략 좌표/범위(match). 처음 보는 문구는 match null + 경고
  advisory-overrides.json   사람이 확인한 보정(지역 분리·발령일·근거·추정 단계). 0404 원문이 그대로일 때만 적용
  destination-areas.json    카탈로그 여행지의 행정구역 이름 · 국경 거리 수동 판정 · 근거 문구

출력: advisories.json (schemaVersion 1 — ios/Packages/PasslogKit/Sources/PasslogCore/Advisories.swift 가 읽는다)

예)
  # 앱 번들 다시 만들기 (네트워크 없음)
  python3 -I info-pipeline/scripts/advisories.py --offline rec2/0404-alertList-raw-2026-10-19.json \\
      --catalog shared/data/destinations.json --out shared/data/advisories.json
  # 파이프라인 (6시간마다)
  python3 -I scripts/advisories.py --catalog data/destinations.json --out public/v1/advisories.json

안전장치: 소스 실패 · 파싱 실패 · 한 번에 30개국 넘게 단계가 바뀜 → 이전 파일을 그대로 두고 경고(종료 코드 0).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import math
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

KST = dt.timezone(dt.timedelta(hours=9), "KST")
SCHEMA_VERSION = 1
ALERT_URL = "https://www.0404.go.kr/travelAlert/apntStatus/getAlertList"
USER_AGENT = "PASSLOG-info-pipeline/1.0 (static travel-advisory snapshot, 4 requests/day) python-urllib/3"

SCRIPT_DIR = Path(__file__).resolve().parent
PIPELINE_DIR = SCRIPT_DIR.parent
DATA_DIR = PIPELINE_DIR / "data"


def first_existing(*paths: Path) -> Path:
    return next((p for p in paths if p.exists()), paths[0])


# 모노레포: ../shared/data/*.json, 별도 리포: data/*.json (복사본)
DEFAULT_COUNTRIES = first_existing(PIPELINE_DIR.parent / "shared" / "data" / "countries.json",
                                   DATA_DIR / "countries.json")
DEFAULT_CATALOG = first_existing(PIPELINE_DIR.parent / "shared" / "data" / "destinations.json",
                                 DATA_DIR / "destinations.json")

U_STATUS = "https://www.0404.go.kr/travelAlert/apntStatus/stepTravelAlert"
U_SPECIAL = "https://www.0404.go.kr/travelAlert/apntStatus/specialTravelCutn"
U_JSON = "https://www.0404.go.kr/travelAlert/apntStatus/getAlertList (POST, JSON)"

SOURCE_TEXT = ("외교부 해외안전여행(0404.go.kr) 여행경보 발령 현황 — 페이지가 쓰는 공개 JSON"
               "(POST /travelAlert/apntStatus/getAlertList, 로그인 불필요)")

# 0404 필드 접두어 → 단계
LEVELS = [(1, "trvlCutn"), (2, "trvlRfran"), (3, "dptcnyAdvs"), (4, "trvlPrhb")]
SPECIAL_KEY = "spclTrvlCutn"
LABEL = {0: "경보 없음", 1: "1단계 여행유의", 2: "2단계 여행자제", 3: "3단계 출국권고", 4: "4단계 여행금지"}
LEVEL_LABELS = {"0": "경보 없음", "1": "1단계 여행유의(남색)", "2": "2단계 여행자제(황색)", "2.5": "특별여행주의보",
                "3": "3단계 출국권고(적색)", "4": "4단계 여행금지(흑색)"}
# '…제외한 (전) 지역 / 외 전 지역 / 이외 지역' = 나머지 전역
REST_RE = re.compile(r"(제외한\s*(전\s*)?(지역|전역)|제외\s*전\s*지역|외\s*전\s*지역|외\s*전역|이외\s*지역)")
# 나라 기록에서 다시 계산하는 값 (보정에 들어 있으면 보정값을 쓴다)
DERIVED = ("baselineRank", "maxLevel", "summaryKo", "banned", "special", "specialScope")

warnings: list[str] = []


def warn(msg: str) -> None:
    warnings.append(msg)
    print(f"::warning::{msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# 공용
# ---------------------------------------------------------------------------
def read_json(path: Path | None) -> Any | None:
    if not path:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)


def clean(s: str | None) -> str:
    s = html.unescape(s or "")
    s = s.replace("․", "·").replace("&bull;", "•")
    return re.sub(r"\s+", " ", s).strip(" ,")


def norm_text(s: str) -> str:
    """지역 문구 키: 공백 · 가운뎃점 변형 제거"""
    return re.sub(r"[\s·․ㆍ]+", "", clean(s))


def fold(s: str) -> str:
    """행정구역 이름 비교용: 소문자 · 악센트 제거 · 공백/하이픈 제거"""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    s = s.replace("ı", "i")
    return re.sub(r"[\s\-'’.]+", "", s)


def rank(level: int | None, special: bool) -> float:
    lv = level or 0
    return float(max(lv, 2.5)) if special and lv < 3 else float(lv)


def split_regions(info: str) -> list[str]:
    """'A, B (c, d), E' → ['A', 'B (c, d)', 'E'] (괄호 안 쉼표는 자르지 않는다)"""
    out, depth, cur = [], 0, ""
    for ch in info:
        if ch in "([（":
            depth += 1
        elif ch in ")]）":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            if cur.strip():
                out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def in_bbox(bbox: list | None, lat: float, lon: float) -> bool:
    """bbox = [minLat, minLon, maxLat, maxLon] 하나 또는 그 목록 (ㄱ자 국경 · 여러 주를 상자 여러 개로) — 하나라도 안이면 True"""
    if not bbox:
        return False
    if isinstance(bbox[0], (list, tuple)):
        return any(in_bbox(b, lat, lon) for b in bbox)
    return bbox[0] <= lat <= bbox[2] and bbox[1] <= lon <= bbox[3]


# ---------------------------------------------------------------------------
# 0404 원문 → 나라 기록
# ---------------------------------------------------------------------------
def raw_fingerprint(row: dict) -> dict[str, list[str]]:
    """보정이 만들어진 원문과 지금 원문이 같은지 볼 때 쓰는 값 (비어 있지 않은 단계만)"""
    fp = {}
    for _, key in LEVELS + [(0, SPECIAL_KEY)]:
        a, s, i = row.get(key + "AllYn") or "N", row.get(key + "SomeYn") or "N", clean(row.get(key + "Info"))
        if a == "Y" or s == "Y" or i:
            fp[key] = [a, s, i]
    return fp


def parse_row(row: dict, geo: dict[str, Any]) -> dict:
    flags = {}
    for lvl, key in LEVELS:
        all_, some = row.get(key + "AllYn") == "Y", row.get(key + "SomeYn") == "Y"
        if all_ or some:  # Info 만 남고 플래그가 N 인 행(안도라 '전 지역' 등)은 경보 없음
            flags[lvl] = {"all": all_, "some": some, "info": clean(row.get(key + "Info"))}
    sp_all, sp_some = row.get(SPECIAL_KEY + "AllYn") == "Y", row.get(SPECIAL_KEY + "SomeYn") == "Y"
    sp_info = clean(row.get(SPECIAL_KEY + "Info"))

    baseline, baseline_special = 0, False
    rest = [lv for lv, f in flags.items() if f["all"] or REST_RE.search(f["info"]) or f["info"] in ("", "전 지역", "전지역")]
    if sp_all or (sp_some and REST_RE.search(sp_info)):
        baseline_special = True
    if rest:
        alls = [lv for lv in rest if flags[lv]["all"]]
        baseline = max(alls) if alls else min(rest)

    code = row["__code"]
    regions, unknown = [], []

    def add(names: str, level: int | None, special: bool) -> None:
        # 문구 전체가 좌표표에 있으면 그대로, 없으면 쉼표로 나눠 아는 조각이 있을 때만 조각별로
        pieces = [names]
        if f"{code}|{norm_text(names)}" not in geo:
            parts = split_regions(names)
            if len(parts) > 1 and any(f"{code}|{norm_text(x)}" in geo for x in parts):
                pieces = parts
        for name in pieces:
            key = f"{code}|{norm_text(name)}"
            if key not in geo:
                unknown.append(name)
            r = {"nameKo": name, "level": level, "special": special, "since": None, "nameEn": None, "note": None,
                 "rank": rank(level, special)}
            if geo.get(key):
                r["match"] = geo[key]
            regions.append(r)

    for lvl, f in sorted(flags.items()):
        if lvl == baseline and (f["all"] or REST_RE.search(f["info"]) or f["info"] in ("", "전 지역", "전지역")):
            extra = re.split(r"제외한\s*지역\s*,?", f["info"], maxsplit=1)
            if len(extra) == 2 and extra[1].strip(" ,"):
                add(extra[1].strip(" ,"), lvl, False)
            continue
        add(f["info"] or "(지역 미기재)", lvl, False)
    if (sp_some or sp_all) and not baseline_special:
        add(sp_info or "(지역 미기재)", None, True)
    inferred = False
    if baseline_special and baseline == 0:
        # 특별여행주의보만 전역에 걸린 나라: 외교부 정의상 2~3단계에 준함 → 2 (추정)
        baseline, inferred = 2, True
    return {
        "level": baseline,
        "levelInferred": inferred or None,
        "baselineSpecial": baseline_special,
        "maxLevel": max(flags) if flags else 0,
        "special": sp_all or sp_some,
        "specialScope": "all" if sp_all else ("partial" if sp_some else None),
        "regions": regions,
        "mofaRaw": {str(lv): f["info"] for lv, f in flags.items()} | ({"special": sp_info} if (sp_all or sp_some) else {}),
        "unknownRegions": unknown,
    }


def summary(level: int, bspecial: bool, regions: list[dict]) -> str:
    """'대부분 지역 경보 없음 · 일부 3단계 출국권고'처럼 범위를 밝혀 쓴다 ('경보 없음 (일부 3단계)'처럼 앞뒤가 안 맞지 않게)"""
    higher = sorted({r["level"] for r in regions if r.get("level") and r["level"] > level}, reverse=True)
    extras = [f"일부 {LABEL[lv]}" for lv in higher]
    if any(r.get("special") for r in regions) and not bspecial:
        extras.append("일부 특별여행주의보")
    lower = sorted({r["level"] for r in regions if r.get("level") is not None and r["level"] < level}, reverse=True)
    extras += [f"일부 {LABEL[lv]}" for lv in lower]
    if bspecial:
        head = "전역 특별여행주의보"
    else:
        head = f"대부분 지역 {LABEL[level]}" if extras else LABEL[level]
    return " · ".join([head] + extras)


def derive(rec: dict, keep: set[str]) -> None:
    """보정 뒤 파생 값 다시 계산 (보정이 직접 준 값은 그대로)"""
    level, bspecial, regions = rec["level"], rec.get("baselineSpecial", False), rec.get("regions", [])
    for r in regions:
        r.setdefault("special", False)
        r.setdefault("since", None)
        r["rank"] = rank(r.get("level"), r["special"])
    vals = {
        "baselineRank": rank(level, bspecial),
        "maxLevel": max([rec.get("maxLevel", 0), level] + [r.get("level") or 0 for r in regions]),
        "summaryKo": summary(level, bspecial, regions),
        "banned": "partial" if any((r.get("level") or 0) == 4 for r in regions) and level < 4 else (level == 4),
        "special": bool(rec.get("special")) or bspecial or any(r["special"] for r in regions),
        "specialScope": rec.get("specialScope") or ("partial" if any(r["special"] for r in regions) else None),
    }
    for k, v in vals.items():
        if k not in keep:
            rec[k] = v


def build_countries(rows: list[dict], countries: list[dict], codes: dict, geo: dict, overrides: dict,
                    continents: dict[str, str]) -> dict[str, dict]:
    by_name = {c["nameKo"]: c for c in countries}
    by_code = {c["code"]: c for c in countries}
    aliases = codes.get("aliases", {})
    en_fallback = codes.get("enFallback", {})
    ov = overrides.get("countries", {})
    out: dict[str, dict] = {}
    for row in rows:
        name = clean(row.get("ntnNm"))
        code = aliases.get(name) or (by_name.get(name) or {}).get("code")
        if not code:
            warn(f"ISO2 를 모르는 0404 나라 이름: {name} (mofa-country-codes.json 에 추가)")
            continue
        row = dict(row, __code=code)
        p = parse_row(row, geo)
        reg = row.get("regDt") or []
        rec = {
            "nameKo": name,
            "nameEn": (by_code.get(code) or {}).get("nameEn") or en_fallback.get(code),
            "mofaNtnCd": str(row.get("ntnCd") or ""),
            "mofaContinent": continents.get(row.get("cntntCd"), row.get("cntntCdNm")),
            "level": p["level"],
            "levelInferred": p["levelInferred"],
            "baselineSpecial": p["baselineSpecial"],
            "maxLevel": p["maxLevel"],
            "special": p["special"],
            "specialScope": p["specialScope"],
            "regions": p["regions"],
            "mofaRaw": p["mofaRaw"],
            "mofaRecordUpdated": "%04d-%02d-%02d" % tuple(reg[:3]) if len(reg) >= 3 else None,
            "mofaHistSeq": row.get("histSeq"),
            "sourceUrls": [U_STATUS] + ([U_SPECIAL] if p["special"] else []),
            "confidence": "high",
        }
        keep: set[str] = set()
        o = ov.get(code)
        if o and o.get("raw") == raw_fingerprint(row):
            rec.update(json.loads(json.dumps(o.get("set", {}))))
            keep = set(o.get("set", {}))
        else:
            if o:
                warn(f"{code} 0404 원문이 보정 때와 달라 자동 해석만 씀 — advisory-overrides.json 확인 필요")
                for k in o.get("keepWhenStale", []):
                    if k in o.get("set", {}):
                        rec[k] = o["set"][k]
            for name in p["unknownRegions"]:
                warn(f"{code} 처음 보는 지역 문구 — match 없음(advisory-geo.json 에 추가): {name}")
        derive(rec, keep)
        out[code] = {k: v for k, v in rec.items() if v is not None}
    # 0404 에 따로 없는 곳 (괌 · 사이판 = 미국 페이지)
    for code, o in ov.items():
        if o.get("synthetic") and code not in out:
            out[code] = json.loads(json.dumps(o["set"]))
    return dict(sorted(out.items()))


# ---------------------------------------------------------------------------
# 여행지 판정
# ---------------------------------------------------------------------------
def covers(region: dict, lat: float, lon: float, area: dict, cc: str) -> bool | None:
    """True = 덮음, False = 안 덮음, None = 판단 못 함"""
    manual = (area.get("regions") or {}).get(region["nameKo"])
    if manual is not None:
        return bool(manual)
    m = region.get("match")
    if not m:
        return None
    t = m.get("type")
    hit: bool | None
    if t == "point":
        hit = haversine_km(lat, lon, m["lat"], m["lon"]) <= m["radiusKm"]
    elif t == "points":
        hit = any(haversine_km(lat, lon, p["lat"], p["lon"]) <= p["radiusKm"] for p in m.get("points", []))
    elif t == "bbox":
        hit = in_bbox(m.get("bbox"), lat, lon)
    elif t in ("admin1", "admin2"):
        names = {fold(n) for n in m.get("names", [])}
        mine = area.get(t)
        if mine:
            mine_list = mine if isinstance(mine, list) else [mine]
            hit = any(fold(x) in names for x in mine_list)
        elif m.get("bbox"):
            hit = in_bbox(m["bbox"], lat, lon)
        else:
            hit = None
    elif t == "borderBuffer":
        hit = border_hit(m, lat, lon, area)
    else:
        hit = None
    extra = m.get("borderBuffer")
    if extra and hit is not True:
        b = border_hit(extra, lat, lon, area)
        if b is True:
            return True
        if b is None:
            return None if hit is not True else True
    return hit


def border_hit(m: dict, lat: float, lon: float, area: dict) -> bool | None:
    key = f"{m.get('with')}{int(m.get('km', 0))}"
    decided = (area.get("border") or {}).get(key)
    if decided is not None:
        return bool(decided)
    if m.get("bbox") and not in_bbox(m["bbox"], lat, lon):
        return False
    return None


def judge(d: dict, country: dict, area: dict) -> dict:
    lat, lon, cc = float(d["lat"]), float(d["lon"]), d["countryCode"]
    regions = country.get("regions", [])
    hits = [(r, covers(r, lat, lon, area, cc)) for r in regions]
    leveled = [r for r, h in hits if h is True and r.get("level") is not None]
    level = max(r["level"] for r in leveled) if leveled else int(country.get("level", 0))
    special = (bool(country.get("baselineSpecial")) and not leveled) or any(r.get("special") for r, h in hits if h is True)
    cur = rank(level, special)
    # 확인 필요는 판단 못 한 지역이 특별여행주의보 · 3단계 이상일 때만 (앱 Advisory.reviewRank 와 같은 규칙)
    refs = [r["nameKo"] for r, h in hits
            if h is None and rank(r.get("level"), r.get("special", False)) > cur and rank(r.get("level"), r.get("special", False)) >= 2.5]
    deciding = sorted([r for r, h in hits if h is True], key=lambda r: -rank(r.get("level"), r.get("special", False)))
    since = next((r.get("since") for r in deciding if r.get("since")), None) or area.get("since") \
        or (country.get("baselineSince") if not leveled else None)
    if area.get("basis"):
        basis = area["basis"]
    elif deciding:
        basis = f"{deciding[0]['nameKo']} 범위에 들어요"
    elif country.get("baselineSpecial"):
        basis = f"{country.get('nameKo', cc)} 전역 특별여행주의보" + (f"({since}~)" if since else "")
    elif level == 0:
        basis = f"{country.get('nameKo', cc)} · 이 지역에 내려진 여행경보 없음"
    else:
        basis = f"{country.get('nameKo', cc)} 기본 단계 · 이 지역에 따로 내려진 상향 경보 없음"
    conf = area.get("confidence") or next((r.get("confidence") for r in deciding if r.get("confidence")), None) or "high"
    return {
        "nameKo": d["nameKo"], "countryCode": cc, "lat": d["lat"], "lon": d["lon"],
        "level": level, "special": special, "rank": cur, "basis": basis, "confidence": conf,
        "since": since, "regionRefs": refs, "needsReview": bool(refs),
    }


def build_destinations(catalog: list[dict], countries: dict, parents: dict, areas: dict, strict: bool) -> tuple[dict, int]:
    out, errors = {}, 0
    for d in catalog:
        cc = d.get("countryCode")
        if not cc or cc == "KR":
            continue
        c = countries.get(cc) or countries.get(parents.get(cc, ""))
        if not c:
            warn(f"{d['id']}: 나라 {cc} 가 경보 색인에 없음 — 판정 생략")
            continue
        area = areas.get(d["id"], {})
        # 행정구역형(bbox 없는) 지역이 있는 나라의 여행지는 행정구역 이름이 있어야 한다
        for r in c.get("regions", []):
            m = r.get("match") or {}
            t = m.get("type")
            if t in ("admin1", "admin2") and not m.get("bbox") and not area.get(t) \
                    and r["nameKo"] not in (area.get("regions") or {}):
                print(f"오류: {d['id']} — {cc} '{r['nameKo']}'({t}) 판정에 destination-areas.json 의 {t} 가 필요",
                      file=sys.stderr)
                errors += 1
                break
        v = judge(d, c, area)
        if v["needsReview"]:
            msg = f"{d['id']}: 덮는지 모르는 지역 {v['regionRefs']} — destination-areas.json 에 판정 추가"
            (print(f"오류: {msg}", file=sys.stderr) if strict else warn(msg))
            errors += 1 if strict else 0
        out[d["id"]] = v
    return out, errors


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------
def fetch_alert_list(timeout: float = 30.0) -> bytes:
    req = urllib.request.Request(ALERT_URL, data=b"{}", method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def load_catalog(paths: list[Path]) -> list[dict]:
    seen, out = set(), []
    for p in paths:
        doc = read_json(p)
        items = (doc.get("destinations") or doc.get("entries") or []) if isinstance(doc, dict) else (doc or [])
        if isinstance(items, dict):
            items = list(items.values())
        for d in items:
            if isinstance(d, dict) and d.get("id") and d["id"] not in seen and "lat" in d:
                seen.add(d["id"])
                out.append(d)
    return out


def changed_levels(prev: dict | None, new: dict) -> int:
    if not prev:
        return 0
    pc = prev.get("countries", {})
    return sum(1 for k, v in new.items() if k in pc and
               (pc[k].get("level"), pc[k].get("baselineSpecial", False)) != (v.get("level"), v.get("baselineSpecial", False)))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="외교부 여행경보(0404) → advisories.json")
    ap.add_argument("--offline", type=Path, help="네트워크 대신 이 getAlertList 응답 파일을 쓴다")
    ap.add_argument("--as-of", help="원본 수집 시각 ISO8601 (기본: 온라인=지금, 오프라인=보정 파일 meta.asOf 또는 파일 시각)")
    ap.add_argument("--catalog", type=Path, action="append", help=f"destinations.json (여러 번 가능, 기본 {DEFAULT_CATALOG})")
    ap.add_argument("--countries", type=Path, default=DEFAULT_COUNTRIES)
    ap.add_argument("--data", type=Path, default=DATA_DIR, help="mofa-country-codes · advisory-geo · overrides · areas 폴더")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--previous", type=Path, help="비교할 이전 파일 (기본: --out)")
    ap.add_argument("--max-changes", type=int, default=30, help="이보다 많은 나라의 단계가 바뀌면 파서 고장으로 보고 이전 파일 유지")
    ap.add_argument("--strict", action="store_true", help="여행지 판정에 확인 필요가 하나라도 있으면 실패")
    args = ap.parse_args(argv)

    data = args.data
    codes = read_json(data / "mofa-country-codes.json") or {}
    geo = read_json(data / "advisory-geo.json") or {}
    geo = {k: v for k, v in geo.items() if not k.startswith("_")}
    overrides = read_json(data / "advisory-overrides.json") or {}
    areas = {k: v for k, v in (read_json(data / "destination-areas.json") or {}).items() if not k.startswith("_")}
    meta = overrides.get("meta", {})
    countries_doc = read_json(args.countries)
    if not countries_doc:
        print(f"오류: countries.json 을 읽을 수 없음: {args.countries}", file=sys.stderr)
        return 2
    previous = read_json(args.previous or args.out)

    try:
        raw = args.offline.read_bytes() if args.offline else fetch_alert_list()
        doc = json.loads(raw.decode("utf-8"))
        rows = doc["data"]["alertList"]
        if not isinstance(rows, list) or len(rows) < 100:
            raise ValueError(f"행 수가 이상함: {len(rows) if isinstance(rows, list) else type(rows)}")
    except (OSError, urllib.error.URLError, ValueError, KeyError, TypeError, json.JSONDecodeError) as e:
        warn(f"0404 여행경보를 읽지 못해 이전 파일을 유지: {e}")
        return 0
    sha = hashlib.sha256(raw).hexdigest()
    continents = {c.get("comnCd"): c.get("comnCdNm") for c in doc["data"].get("cntntCdList", []) if isinstance(c, dict)}

    if args.as_of:
        as_of = args.as_of
    elif args.offline:
        as_of = meta.get("asOf") if meta.get("forSha256") == sha else None
        as_of = as_of or dt.datetime.fromtimestamp(args.offline.stat().st_mtime, KST).replace(microsecond=0).isoformat()
    else:
        as_of = dt.datetime.now(KST).replace(microsecond=0).isoformat()

    parents = codes.get("parents", {})
    countries = build_countries(rows, countries_doc["countries"], codes, geo, overrides, continents)
    if len(countries) < 150:
        warn(f"나라가 {len(countries)}개뿐이라 파서 고장으로 보고 이전 파일을 유지")
        return 0
    n = changed_levels(previous, countries)
    if n > args.max_changes:
        warn(f"한 번에 {n}개국 단계가 바뀜(> {args.max_changes}) — 파서 고장 의심, 이전 파일 유지")
        return 0

    catalog = load_catalog(args.catalog or [DEFAULT_CATALOG])
    dests, errors = build_destinations(catalog, countries, parents, areas, args.strict)
    if errors:
        print(f"오류 {errors}건 — 파일을 쓰지 않음", file=sys.stderr)
        return 1

    same_snapshot = meta.get("forSha256") == sha
    out = {
        "schemaVersion": SCHEMA_VERSION,
        "asOf": as_of,
        "verifiedAt": meta.get("verifiedAt") if same_snapshot else None,
        "source": meta.get("source") if same_snapshot and meta.get("source") else SOURCE_TEXT,
        "sourceUrls": [U_STATUS, U_SPECIAL, U_JSON, "https://www.0404.go.kr/bbs/travelAlertAjmt/list",
                       "https://www.mofa.go.kr/www/brd/m_4080/list.do"],
        "rawSnapshotSha256": sha,
        "levelLabels": LEVEL_LABELS,
        "travelBanUntil": meta.get("travelBanUntil"),
        "parents": parents,
        "countries": countries,
        "destinations": dict(sorted(dests.items())),
        "schema": meta.get("schema"),
        "verification": meta.get("verification") if same_snapshot else None,
    }
    write_json(args.out, {k: v for k, v in out.items() if v is not None})
    print(f"완료: {args.out} 나라 {len(countries)} · 여행지 {len(dests)} · 경고 {len(warnings)} · 단계 변경 {n}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
