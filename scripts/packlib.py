#!/usr/bin/env python3
"""데이터 팩 공용 도구 (build_pack.py · sources/* · 테스트 · 앱 쪽 build-bundled-manifest 가 같이 쓴다).

- 데이터셋 등록표 `DATASETS` — 이름(id)은 앱 `PasslogDataPack/DatasetID.swift` 의 rawValue 와 같아야 한다
  (tests/test_packlib.py 가 Swift 파일과 대조한다).
- 정규화 JSON(키 정렬 · 공백 없음) · sha256 · 날짜 해석(`PackDate.parse` 와 같은 규칙) · dataAsOf 규칙.

Python 3.9+ 표준 라이브러리만 (macOS /usr/bin/python3 3.9, GitHub 러너 3.13).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

KST = dt.timezone(dt.timedelta(hours=9), "KST")

MANIFEST_SCHEMA_VERSION = 2          # PackManifest.supportedSchemaVersion
DATASET_SCHEMA = 1                   # DatasetID.supportedSchema
MAX_DATASET_BYTES = 5 * 1024 * 1024  # DataPackLimits.maxDatasetBytes
MAX_MANIFEST_BYTES = 256 * 1024      # DataPackLimits.maxManifestBytes
LARGE_BYTES = 256 * 1024             # 이보다 크면 manifest large=true (안전 데이터셋 · no_large 제외)
RETAIN_DAYS = 7                      # 참조가 끊긴 해시 파일 보존 기간
DEFAULT_MIN_APP_VERSION = "1.0.0"    # 팩 전체 최소 앱 버전 (데이터 팩을 처음 읽는 버전)

SCRIPT_DIR = Path(__file__).resolve().parent
PIPELINE_DIR = SCRIPT_DIR.parent
MONOREPO_ROOT = PIPELINE_DIR.parent


# ---------------------------------------------------------------------------
# 데이터셋 등록표
# ---------------------------------------------------------------------------
class Spec:
    """데이터셋 하나의 규칙.

    kind      curated   = 큐레이션 파일 그대로(바이트 그대로 싣는다)
              generated = 파이프라인이 만든다. 번들 파일과 같은 형식(봉투 없음)
              remote    = 원격 전용(번들 없음). envelope=True 면 {schema,id,dataAsOf,sources,data}
              planned   = id 만 있다(아직 만드는 곳 없음) → 늘 건너뜀
    groups    어느 실행에서 만드나: advisories(6시간) · rates(평일 2회) · weekly · daily · curated(모든 실행)
    volatile  이전 파일과 견줄 때 빼는 최상위 키(실행 시각 등) — 나머지가 같으면 이전 파일을 그대로 쓴다
    heartbeat 내용이 같아도 dataAsOf 가 이 시간보다 오래되면 새로 싣는다(앱의 '오래됨' 판정이 멈춘 파이프라인만 가리키게)
    no_large  크기가 LARGE_BYTES 를 넘어도 large 로 표시하지 않는다 — 앱이 바로 반영하는(appliesLive) 자주 바뀌는 자료라
              셀룰러 사용자도 받아야 하는 것 (국내 맛집 · 관광지 · 축제, 수백 KB)
    """

    __slots__ = ("id", "kind", "bundled", "envelope", "ttl", "stale", "heartbeat", "volatile", "groups", "key",
                 "safety", "no_large")

    def __init__(self, id: str, kind: str, bundled: Optional[str] = None, envelope: bool = False,
                 ttl: float = 24, stale: Optional[float] = 180 * 24, heartbeat: Optional[float] = None,
                 volatile: Tuple[str, ...] = (), groups: Tuple[str, ...] = ("curated",), key: Optional[str] = None,
                 safety: bool = False, no_large: bool = False) -> None:
        self.id = id
        self.kind = kind
        self.bundled = bundled
        self.envelope = envelope
        self.ttl = ttl
        self.stale = stale
        self.heartbeat = heartbeat
        self.volatile = volatile
        self.groups = groups
        self.key = key
        self.safety = safety
        self.no_large = no_large

    def __repr__(self) -> str:  # pragma: no cover - 디버그용
        return f"Spec({self.id!r}, {self.kind!r})"


_H = 24.0
_SPECS = [
    # 안전 · 시세 — 즉시 반영
    Spec("advisories", "generated", "advisories.json", ttl=3, stale=48, heartbeat=12,
         volatile=("asOf", "verifiedAt", "rawSnapshotSha256"), groups=("advisories",), safety=True),
    Spec("entry", "remote", envelope=True, ttl=6, stale=7 * _H, heartbeat=72, volatile=("dataAsOf",),
         groups=("advisories",), key="DATA_GO_KR_KEY", safety=True),
    Spec("rates", "remote", ttl=12, stale=4 * _H, volatile=("generatedAt",), groups=("rates",), safety=True),
    # 공휴일
    Spec("holidays-kr", "generated", "kr-holidays.json", ttl=7 * _H, stale=None, volatile=("updated",),
         groups=("weekly",), safety=True),
    Spec("holidays-world", "remote", envelope=True, ttl=7 * _H, stale=None, volatile=("dataAsOf",),
         groups=("weekly",)),
    # 큐레이션 카탈로그 (다음 실행 때 반영)
    Spec("destinations", "curated", "destinations.json"),
    Spec("countries", "curated", "countries.json"),
    Spec("checklist-rules", "curated", "checklist-rules.json"),
    Spec("checklist-template", "curated", "checklist-template.json"),
    Spec("kr-places", "curated", "kr-places.json"),
    Spec("travel-phrases", "curated", "travel-phrases.json"),
    Spec("local-apps", "curated", "local-apps.json"),
    Spec("local-apps-status", "remote", envelope=True, ttl=7 * _H, stale=21 * _H, heartbeat=6 * _H,
         volatile=("dataAsOf",), groups=("weekly",)),
    Spec("dining-links", "curated", "dining-links.json"),
    Spec("airports", "curated", "airports.json"),
    Spec("dish-wiki", "curated", "dish-wiki.json"),
    Spec("taste-survey", "curated", "taste-survey.json"),
    Spec("destination-climate", "curated", "destination-climate.json"),
    # 한국관광공사 TourAPI (DATA_GO_KR_KEY) — 매일 증분(areaBasedSyncList2) + 주간 목록 갱신, 축제는 매일
    Spec("kr-restaurants", "generated", "kr-restaurants.json", ttl=24, stale=30 * _H, heartbeat=7 * _H,
         volatile=("generatedAt",), groups=("daily", "weekly"), key="DATA_GO_KR_KEY", no_large=True),
    Spec("kr-attractions", "generated", "kr-attractions.json", ttl=24, stale=30 * _H, heartbeat=7 * _H,
         volatile=("generatedAt",), groups=("daily", "weekly"), key="DATA_GO_KR_KEY", no_large=True),
    Spec("kr-festivals", "generated", "kr-festivals.json", ttl=24, stale=7 * _H, heartbeat=48,
         volatile=("generatedAt",), groups=("daily", "weekly"), key="DATA_GO_KR_KEY", no_large=True),
    # 여행 정보 허브 (뒤 워크플로: 큐레이션 파일이 생기면 그대로 싣는다)
    Spec("airline-baggage", "curated", "airline-baggage.json"),
    Spec("travel-guide", "curated", "travel-guide.json"),
    Spec("source-status", "remote", envelope=True, ttl=24, stale=7 * _H, heartbeat=72, volatile=("dataAsOf",),
         groups=("daily",)),
    Spec("flights-icn", "planned", envelope=True, ttl=7 * _H, stale=None, groups=("weekly",), key="DATA_GO_KR_KEY"),
]

DATASETS: Dict[str, Spec] = {s.id: s for s in _SPECS}
DATASET_IDS: List[str] = [s.id for s in _SPECS]

# DatasetID.priority (클수록 먼저) — 실행 순서와 manifest 정렬에 쓴다
PRIORITY = {
    "advisories": 100, "entry": 90, "rates": 80, "travel-guide": 70, "airline-baggage": 65, "holidays-kr": 60,
    "holidays-world": 55, "kr-festivals": 50, "source-status": 45, "local-apps-status": 44, "flights-icn": 43,
    "kr-restaurants": 40, "kr-attractions": 39, "countries": 20, "checklist-rules": 20, "checklist-template": 20,
    "kr-places": 10, "travel-phrases": 10, "local-apps": 10, "dining-links": 10, "dish-wiki": 10,
    "taste-survey": 10, "destination-climate": 10, "destinations": 5, "airports": 5,
}


def by_priority(ids: Iterable[str]) -> List[str]:
    return sorted(ids, key=lambda i: (-PRIORITY.get(i, 0), i))


# 워크플로 cron(UTC) → 실행 묶음. 정각을 피한다.
SCHEDULES = {
    "17 */6 * * *": "advisories",      # KST 02:17 · 08:17 · 14:17 · 20:17
    "25 2,5 * * 1-5": "rates",         # 평일 KST 11:25 · 14:25
    "40 18 * * 0": "weekly",           # 월 KST 03:40
    "50 21 * * *": "daily",            # 매일 KST 06:50
}
ALL_GROUPS = ("advisories", "rates", "weekly", "daily", "curated")


def select_datasets(event: str, schedule: str = "", only: Iterable[str] = ()) -> Tuple[List[str], List[str]]:
    """(이번 실행에서 만들 id, 모르는 id) — only 가 있으면 그것만, schedule 이면 그 묶음 + 큐레이션,
    push 는 큐레이션만, workflow_dispatch · manual 은 전부"""
    only = [x.strip() for x in only if x and x.strip()]
    if only:
        unknown = [x for x in only if x not in DATASETS]
        return by_priority(x for x in dict.fromkeys(only) if x in DATASETS), unknown
    if event == "schedule":
        group = SCHEDULES.get(" ".join(schedule.split()))
        groups = {group, "curated"} if group else set(ALL_GROUPS)
    elif event == "push":
        groups = {"curated"}  # 큐레이션 재배포만 (0404 · TourAPI 등 외부 호출은 예약 실행에서만)
    else:
        groups = set(ALL_GROUPS)
    return by_priority(s.id for s in _SPECS if groups & set(s.groups)), []


# ---------------------------------------------------------------------------
# JSON · 해시
# ---------------------------------------------------------------------------
def canonical_bytes(obj: Any) -> bytes:
    """정규화 JSON: 키 정렬 · 공백 없음 · UTF-8 그대로 → 같은 내용은 같은 바이트(같은 sha256)"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def pretty_bytes(obj: Any) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=False) + "\n").encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def dataset_file_name(dataset_id: str, data: bytes) -> str:
    return f"{dataset_id}.{sha256_hex(data)[:8]}.json"


DATASET_FILE_RE = re.compile(r"^([a-z0-9-]+)\.([0-9a-f]{8})\.json$")


def read_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_bytes_atomic(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def without_keys(obj: Any, keys: Iterable[str]) -> Any:
    if isinstance(obj, dict):
        drop = set(keys)
        return {k: v for k, v in obj.items() if k not in drop}
    return obj


# ---------------------------------------------------------------------------
# 날짜 (PasslogDataPack PackDate 와 같은 규칙)
# ---------------------------------------------------------------------------
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
_COMPACT_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})(\d{2})?(\d{2})?(\d{2})?$")


def now_kst() -> dt.datetime:
    return dt.datetime.now(KST).replace(microsecond=0)


def iso(t: dt.datetime) -> str:
    """"2026-10-19T03:17:42+09:00" (KST, 초 단위) — PackDate.format 과 같은 모양"""
    return t.astimezone(KST).replace(microsecond=0).isoformat()


def parse_date(value: Any) -> Optional[dt.datetime]:
    """ISO8601(오프셋 · Z · 소수 초) · "yyyy-MM-dd"(그날 00:00 KST) · "yyyy-MM"(1일) · "yyyyMMdd[HHmmss]"(KST)"""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    m = _DATE_RE.match(s)
    if m:
        try:
            return dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=KST)
        except ValueError:
            return None
    m = _MONTH_RE.match(s)
    if m:
        try:
            return dt.datetime(int(m.group(1)), int(m.group(2)), 1, tzinfo=KST)
        except ValueError:
            return None
    m = _COMPACT_RE.match(s)
    if m and len(s) in (8, 14):
        try:
            parts = [int(x) for x in m.groups() if x is not None]
            return dt.datetime(*parts, tzinfo=KST)
        except (ValueError, TypeError):
            return None
    t = s.replace("Z", "+00:00").replace("z", "+00:00")
    # 3.9 fromisoformat 은 "+0900" · 소수 초 6자리 외를 못 읽는다 → 손본다
    t = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", t)
    t = re.sub(r"\.(\d+)", lambda mm: "." + (mm.group(1) + "000000")[:6], t)
    if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$", t):
        t += ":00"
    try:
        d = dt.datetime.fromisoformat(t)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=KST)
    return d


def normalize_as_of(value: Any) -> Optional[str]:
    """manifest dataAsOf 로 쓸 문자열: 날짜만이면 "yyyy-MM-dd", 시각이 있으면 KST ISO. 못 읽으면 None"""
    if value is None:
        return None
    s = str(value).strip()
    if _DATE_RE.match(s) and parse_date(s):
        return s
    if _MONTH_RE.match(s) and parse_date(s):
        return s + "-01"
    d = parse_date(s)
    if d is None:
        return None
    if _COMPACT_RE.match(s) and len(s) == 8:
        return d.date().isoformat()
    return iso(d)


# 데이터셋 파일 안의 기준 시각 필드 (앞에서부터 처음 찾은 것)
AS_OF_KEYS = ("dataAsOf", "asOf", "date", "generatedAt", "generated", "updated", "reviewedAt")
AS_OF_OVERRIDES = {
    "advisories": ("asOf",),
    "rates": ("date",),
    "countries": ("reviewedAt",),
    "airports": ("generated",),
    "kr-restaurants": ("generatedAt",),
    "kr-attractions": ("generatedAt",),
    "kr-festivals": ("generatedAt",),
}


def data_as_of(dataset_id: str, obj: Any) -> Optional[str]:
    """파일 내용에서 기준 시각을 꺼낸다(정규화). 날짜 필드가 없는 파일(checklist-*, kr-places 배열)은 None.

    앱 번들 쪽 bundled-manifest.json 을 만들 때도 이 함수를 쓰면 원격과 번들의 비교 기준이 같아진다.
    """
    if not isinstance(obj, dict):
        return None
    spec = DATASETS.get(dataset_id)
    keys = ("dataAsOf",) if spec is not None and spec.envelope else AS_OF_OVERRIDES.get(dataset_id, AS_OF_KEYS)
    for k in keys:
        v = normalize_as_of(obj.get(k))
        if v:
            return v
    return None


def later_as_of(a: Optional[str], b: Optional[str]) -> Optional[str]:
    da, db = parse_date(a), parse_date(b)
    if da is None:
        return b
    if db is None:
        return a
    return b if db > da else a


def envelope(dataset_id: str, data_as_of_value: str, data: Any, sources: List[Dict[str, str]]) -> Dict[str, Any]:
    return {"schema": DATASET_SCHEMA, "id": dataset_id, "dataAsOf": data_as_of_value, "sources": sources,
            "data": data}


# ---------------------------------------------------------------------------
# 경로 찾기 (모노레포 · 배포 리포 둘 다)
# ---------------------------------------------------------------------------
def default_curated_dir() -> Path:
    """배포 리포는 curated/, 모노레포는 ../shared/data"""
    for p in (PIPELINE_DIR / "curated", MONOREPO_ROOT / "shared" / "data"):
        if p.is_dir():
            return p
    return PIPELINE_DIR / "curated"


def vendor_script(name: str) -> Optional[Path]:
    """앱 리포 scripts/ 의 스크립트 — 배포 리포에서는 scripts/vendor/ 사본"""
    for p in (SCRIPT_DIR / "vendor" / name, MONOREPO_ROOT / "scripts" / name):
        if p.is_file():
            return p
    return None


def secret(name: str, env: Optional[Dict[str, str]] = None) -> str:
    """환경 변수 키(값은 절대 출력하지 않는다). Encoding 키(% 포함)는 한 번 풀어 Decoding 형태로"""
    import os
    import urllib.parse

    v = ((env if env is not None else os.environ).get(name) or "").strip()
    if "%" in v:
        v = urllib.parse.unquote(v)
    return v


def scrub(text: Any, *secrets: str) -> str:
    """오류 문구에서 키 값과 serviceKey= 파라미터를 가린다"""
    import urllib.parse

    s = str(text)
    for k in secrets:
        if k:
            for form in {k, urllib.parse.quote(k, safe=""), urllib.parse.quote_plus(k)}:
                s = s.replace(form, "***")
    s = re.sub(r"(?i)(service_?key|authkey)=[^&\s]+", r"\1=***", s)
    return s


if __name__ == "__main__":  # publish-data.sh 용: 배포 리포 curated/ 로 옮길 파일 이름
    import sys

    if sys.argv[1:] == ["--curated-files"]:
        names = [s.bundled for s in _SPECS if s.bundled] + ["kr-tour-codes.json"]
        print("\n".join(dict.fromkeys(names)))
    else:
        for s in _SPECS:
            print(f"{s.id:20s} {s.kind:9s} {s.bundled or '-':26s} {'/'.join(s.groups):14s} {s.key or ''}")
