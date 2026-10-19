"""테스트 공용: scripts/ 를 import 경로에 넣고, 네트워크를 막고, 작은 큐레이션 폴더를 만든다."""
from __future__ import annotations

import io
import json
import socket
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

TESTS_DIR = Path(__file__).resolve().parent
PIPELINE_DIR = TESTS_DIR.parent
SCRIPTS_DIR = PIPELINE_DIR / "scripts"
FIXTURES = TESTS_DIR / "fixtures"
MONOREPO = PIPELINE_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


class NetworkBlocked(RuntimeError):
    pass


def _blocked(*_a: Any, **_k: Any) -> Any:
    raise NetworkBlocked("테스트에서 네트워크 사용 금지")


# 테스트 프로세스 안에서는 어떤 네트워크도 열지 않는다
socket.create_connection = _blocked  # type: ignore[assignment]
urllib.request.urlopen = _blocked  # type: ignore[assignment]


def write_json(path: Path, obj: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def make_curated(root: Path, n_dest: int = 60, n_countries: int = 160, photo_versions: Optional[Dict[str, list]] = None
                 ) -> Path:
    """build_pack 검증을 통과하는 최소 큐레이션 폴더 (KR 여행지 2곳 포함)"""
    root.mkdir(parents=True, exist_ok=True)
    codes = [f"{chr(65 + i // 26)}{chr(65 + i % 26)}" for i in range(n_countries)]
    if "KR" not in codes:
        codes[-1] = "KR"
    if "JP" not in codes:
        codes[-2] = "JP"
    countries = [{"code": c, "nameKo": f"나라{c}", "currency": "USD"} for c in codes]
    write_json(root / "countries.json", {"version": 1, "reviewedAt": "2026-10-13", "countries": countries})
    dests = []
    for i in range(n_dest):
        did = f"d{i:03d}"
        cc = "KR" if i < 2 else "JP"
        dests.append({"id": did, "nameKo": did, "countryCode": cc, "lat": 35.0 + i * 0.01, "lon": 129.0,
                      "photo": {"bundled": f"{did}.heic"}})
    write_json(root / "destinations.json", {"version": 1, "updated": "2026-10", "destinations": dests})
    versions = photo_versions if photo_versions is not None else {"1.0.0": [d["id"] for d in dests]}
    write_json(root / "photo-ids.json", {"schemaVersion": 1, "updated": "2026-10-20", "versions": versions})
    adv_countries = {c: {"level": 1 if c == "JP" else 0, "maxLevel": 1 if c == "JP" else 0, "summaryKo": "테스트"} for c in codes}
    write_json(root / "advisories.json", {"schemaVersion": 1, "asOf": "2026-10-19T04:11:15+09:00",
                                          "countries": adv_countries, "destinations": {}, "parents": {}})
    holidays = [{"date": f"2026-{m:02d}-01", "name": f"휴일{m}", "kind": "fixed", "isSubstitute": False} for m in range(1, 13)]
    holidays += [{"date": f"2027-{m:02d}-01", "name": f"휴일{m}", "kind": "fixed", "isSubstitute": False} for m in range(1, 13)]
    write_json(root / "kr-holidays.json", {"version": 1, "updated": "2026-10", "years": [2026, 2027],
                                           "holidays": holidays, "schoolVacations": []})
    write_json(root / "checklist-rules.json", {"version": 1, "rules": [{"id": "r1"}]})
    write_json(root / "checklist-template.json", {"version": 1, "categories": [], "items": []})
    write_json(root / "kr-places.json", [{"name": "부산"}])
    tour_meta = {"schemaVersion": 1, "source": "한국관광공사 TourAPI 4.0 KorService2", "attribution": "사진: 한국관광공사",
                 "generatedAt": "2026-10-20T03:09:21+09:00"}
    item = {"contentId": "100", "title": "가게", "lat": 35.0, "lon": 129.0, "image": "https://tong.visitkorea.or.kr/a.jpg",
            "imageLicense": "Type3", "modifiedtime": "20261001000000"}
    write_json(root / "kr-restaurants.json", dict(tour_meta, kind="restaurants",
                                                  destinations={"d000": {"radiusKm": 3, "items": [item]}}))
    write_json(root / "kr-attractions.json", dict(tour_meta, kind="attractions",
                                                  destinations={"d000": {"radiusKm": 5, "items": [dict(item, kind="관광지")]}}))
    write_json(root / "kr-festivals.json", dict(tour_meta, kind="festivals", festivalWindow=["2026-10-20", "2027-10-31"],
                                                destinations={"d001": {"regions": ["26-350"], "items": [
                                                    dict(item, contentId="200", start="2026-10-01", end="2026-12-31")]}}))
    return root


def run_build(out: Path, curated: Path, *extra: str, env: Optional[Dict[str, str]] = None) -> tuple:
    """build_pack.run 을 같은 프로세스에서 (네트워크 막힌 채) — (종료 코드, 로그)"""
    import os

    import build_pack

    saved = dict(os.environ)
    os.environ.pop("DATA_GO_KR_KEY", None)
    os.environ.pop("KOREAEXIM_KEY", None)
    os.environ.pop("GITHUB_OUTPUT", None)
    os.environ.pop("GITHUB_STEP_SUMMARY", None)
    if env:
        os.environ.update(env)
    buf = io.StringIO()
    try:
        args = build_pack.parse_args(["--out", str(out), "--curated", str(curated), *extra])
        code = build_pack.run(args, lambda m: buf.write(m + "\n"))
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return code, buf.getvalue()


def read(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))
