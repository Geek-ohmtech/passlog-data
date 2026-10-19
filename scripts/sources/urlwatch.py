#!/usr/bin/env python3
"""공식 페이지 변경 감지 'source-status' (봉투, 원격 전용, 매일, 키 없음) — auto-update-design §6.5.

대상: 큐레이션 airline-baggage.json · travel-guide.json 안의 http(s) 출처(url · sourceUrl · officialUrl · sources[].url).
두 파일이 아직 없으면(허브는 뒤 워크플로) 건너뛴다.

방법: GET → 본문에서 script · style · 태그 · 날짜/시각 · 긴 숫자를 지운 텍스트의 sha256 → 이전과 다르면 changed.
  - changed 는 그 URL 을 쓰는 큐레이션 파일이 바뀔 때까지(사람이 검수해 고칠 때까지) 유지한다.
  - 403 · 타임아웃 · 네트워크 오류는 unreachable(변경으로 보지 않음). 7일 연속이면 manual(자동 감시 불가 → 수동 검수).
  data = {"urls": {"<url>": {"state": "ok|changed|unreachable|manual", "sha256": "…"?, "changedAt": "yyyy-MM-dd"?,
                             "unreachableSince": "yyyy-MM-dd"?, "unreachableCount": n?, "datasets": ["airline-baggage"],
                             "curatedSha": "…"}}}
앱은 changed 만 배지로 보여 주고 unreachable · manual 은 숨긴다.
"""
from __future__ import annotations

import hashlib
import html
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import packlib  # noqa: E402
from sources import net  # noqa: E402

WATCHED = ("airline-baggage", "travel-guide")
URL_KEYS = {"url", "sourceUrl", "officialUrl", "sourceURL", "officialURL"}
MANUAL_AFTER = 7
DELAY = 1.0
MAX_URLS = 400
PAGE_MAX_BYTES = 2 * 1024 * 1024     # 감시하는 공식 페이지 하나 최대 크기 (넘으면 unreachable 로 본다)


def collect_urls(doc: Any) -> List[str]:
    found: List[str] = []

    def walk(x: Any, key: str = "") -> None:
        if isinstance(x, dict):
            for k, v in x.items():
                walk(v, k)
        elif isinstance(x, list):
            for v in x:
                walk(v, key)
        elif isinstance(x, str) and key in URL_KEYS and re.match(r"^https?://", x.strip()):
            found.append(x.strip())

    walk(doc)
    return list(dict.fromkeys(found))


_DROP_BLOCKS = re.compile(r"<(script|style|noscript|svg|template)\b.*?</\1\s*>", re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")
_DATES = re.compile(r"\d{4}[-./년]\s?\d{1,2}[-./월]\s?\d{1,2}일?|\d{1,2}:\d{2}(:\d{2})?|\b\d{6,}\b")


def normalize_text(body: bytes) -> str:
    text = body.decode("utf-8", errors="replace")
    text = _DROP_BLOCKS.sub(" ", text)
    text = _TAGS.sub(" ", text)
    text = html.unescape(text)
    text = _DATES.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def check(url_sets: Dict[str, Tuple[List[str], str]], previous: Optional[Dict[str, Any]], today: str,
          getter: Callable[..., Tuple[int, Dict[str, str], bytes]] = net.get, delay: float = DELAY,
          sleep: Callable[[float], None] = time.sleep) -> Tuple[Dict[str, Any], List[str]]:
    """url_sets = {dataset id: (URL 목록, 큐레이션 파일 sha256)} → (data, 새로 바뀐 URL 목록)"""
    prev_urls = ((previous or {}).get("data") or {}).get("urls") or {}
    out: Dict[str, Dict[str, Any]] = {}
    newly_changed: List[str] = []
    owners: Dict[str, List[str]] = {}
    curated_sha: Dict[str, str] = {}
    for ds, (urls, sha) in url_sets.items():
        for u in urls:
            owners.setdefault(u, []).append(ds)
            curated_sha[u] = curated_sha.get(u, "") + sha[:12]
    for u in list(owners)[:MAX_URLS]:
        prev = prev_urls.get(u) or {}
        row: Dict[str, Any] = {"datasets": sorted(owners[u]), "curatedSha": curated_sha[u]}
        curated_changed = prev.get("curatedSha") not in (None, curated_sha[u])
        try:
            status, _h, body = getter(u, headers={"Accept": "text/html,application/xhtml+xml,*/*"}, timeout=20, retries=1,
                                      max_bytes=PAGE_MAX_BYTES)
            if status != 200:
                raise net.FetchError(f"HTTP {status}")
            digest = hashlib.sha256(normalize_text(body).encode("utf-8")).hexdigest()
            row["sha256"] = digest
            if prev.get("state") == "changed" and not curated_changed:
                row["state"] = "changed"
                row["changedAt"] = prev.get("changedAt") or today
            elif prev.get("sha256") and prev["sha256"] != digest and not curated_changed:
                row["state"] = "changed"
                row["changedAt"] = today
                newly_changed.append(u)
            else:
                row["state"] = "ok"
        except net.FetchError:
            count = int(prev.get("unreachableCount") or 0) + 1
            row.update({"state": "manual" if count >= MANUAL_AFTER else "unreachable", "unreachableCount": count,
                        "unreachableSince": prev.get("unreachableSince") or today})
            if prev.get("sha256"):
                row["sha256"] = prev["sha256"]
        out[u] = row
        sleep(delay)
    return {"urls": out}, newly_changed


def url_sets_from(curated_dir: Path) -> Dict[str, Tuple[List[str], str]]:
    sets: Dict[str, Tuple[List[str], str]] = {}
    for ds in WATCHED:
        spec = packlib.DATASETS[ds]
        path = Path(curated_dir) / (spec.bundled or "")
        if not path.is_file():
            continue
        raw = path.read_bytes()
        doc = packlib.read_json(path)
        urls = collect_urls(doc)
        if urls:
            sets[ds] = (urls, packlib.sha256_hex(raw))
    return sets
