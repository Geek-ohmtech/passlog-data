#!/usr/bin/env python3
"""앱 빌드에 들어간 여행지 사진 id 목록 → curated/photo-ids.json (데이터 팩 사진 게이트의 기준).

앱은 여행지 사진을 번들(shared/data/destination-photos/<id>.heic)로만 들고 있다. 원격 destinations 에 번들 사진이 없는
여행지가 들어오면 build_pack.py 가 그 데이터셋의 minAppVersion 을 사진이 들어간 앱 버전으로 올리거나(그런 버전이 없으면)
배포를 거부한다. 그래서 앱을 출시할 때마다 이 스크립트로 그 버전의 사진 목록을 더한다(이전 버전 목록은 그대로 둔다).

  # 앱 리포에서 (버전은 ios/project.yml MARKETING_VERSION)
  python3 -I info-pipeline/scripts/make_photo_ids.py --out ../passlog-data/curated/photo-ids.json
  python3 -I info-pipeline/scripts/make_photo_ids.py --app-version 1.1.0 --out …

형식: {"schemaVersion": 1, "updated": "2026-10-20", "versions": {"1.0.0": ["almaty", "amalfi-naples", …]}}
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import packlib  # noqa: E402

PHOTO_EXTS = {".heic", ".jpg", ".jpeg", ".png", ".webp"}
DEFAULT_PHOTOS = packlib.MONOREPO_ROOT / "shared" / "data" / "destination-photos"
PROJECT_YML = packlib.MONOREPO_ROOT / "ios" / "project.yml"


def photo_ids(photos_dir: Path) -> List[str]:
    return sorted({p.stem for p in Path(photos_dir).iterdir() if p.suffix.lower() in PHOTO_EXTS and not p.name.startswith(".")})


def app_version(project_yml: Path = PROJECT_YML) -> Optional[str]:
    try:
        m = re.search(r"MARKETING_VERSION:\s*\"?([0-9][0-9.]*)\"?", project_yml.read_text(encoding="utf-8"))
    except OSError:
        return None
    return m.group(1) if m else None


def version_key(v: str):
    return tuple(int(x) if x.isdigit() else 0 for x in v.split("."))


def merge(existing: Optional[dict], version: str, ids: Iterable[str], today: str) -> dict:
    doc = dict(existing) if isinstance(existing, dict) else {}
    versions: Dict[str, List[str]] = dict(doc.get("versions") or {})
    versions[version] = sorted(set(ids))
    ordered = {k: versions[k] for k in sorted(versions, key=version_key)}
    if ordered != doc.get("versions") or not doc.get("updated"):
        doc["updated"] = today
    doc["schemaVersion"] = 1
    doc["versions"] = ordered
    return {k: doc[k] for k in ("schemaVersion", "updated", "versions")}


def required_photo(dest: dict) -> str:
    """여행지가 쓰는 번들 사진 id (photo.bundled 의 파일 이름, 없으면 여행지 id)"""
    photo = dest.get("photo") if isinstance(dest.get("photo"), dict) else {}
    bundled = str(photo.get("bundled") or "")
    return Path(bundled).stem if bundled else str(dest.get("id") or "")


def gate(destinations_doc: dict, photo_doc: Optional[dict]) -> Optional[str]:
    """destinations 를 받아도 되는 최소 앱 버전. 어떤 버전도 사진을 다 갖고 있지 않으면 ValueError"""
    if not isinstance(photo_doc, dict) or not photo_doc.get("versions"):
        raise ValueError("photo-ids.json 이 없음 — 사진 게이트를 확인할 수 없어 destinations 를 싣지 않음")
    need: Set[str] = {required_photo(d) for d in destinations_doc.get("destinations") or [] if isinstance(d, dict)}
    need.discard("")
    versions = sorted(photo_doc["versions"], key=version_key)
    ok = [v for v in versions if need <= set(photo_doc["versions"][v])]
    if not ok:
        latest = set(photo_doc["versions"][versions[-1]])
        missing = sorted(need - latest)
        raise ValueError(f"번들 사진이 없는 여행지 {len(missing)}곳: {', '.join(missing[:12])} — 사진을 넣은 앱 버전을 "
                         "출시하고 make_photo_ids.py 로 그 버전을 더하세요")
    first = ok[0]
    # 그 뒤 버전이 사진을 뺐다면(드묾) 가장 마지막으로 다 갖춘 연속 구간의 시작을 쓴다
    later = versions[versions.index(first):]
    if not all(v in ok for v in later):
        tail = [v for v in versions if v in ok]
        first = tail[-1]
    return first


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="번들 여행지 사진 id 목록 → photo-ids.json")
    ap.add_argument("--photos", type=Path, default=DEFAULT_PHOTOS, help=f"사진 폴더 (기본 {DEFAULT_PHOTOS})")
    ap.add_argument("--app-version", help="이 사진이 들어간 앱 버전 (기본: ios/project.yml MARKETING_VERSION)")
    ap.add_argument("--out", type=Path, required=True, help="photo-ids.json 경로 (있으면 다른 버전 목록은 유지)")
    args = ap.parse_args(argv)
    if not args.photos.is_dir():
        print(f"오류: 사진 폴더가 없음: {args.photos}", file=sys.stderr)
        return 2
    version = args.app_version or app_version()
    if not version:
        print("오류: 앱 버전을 모름 — --app-version 을 주세요", file=sys.stderr)
        return 2
    ids = photo_ids(args.photos)
    doc = merge(packlib.read_json(args.out), version, ids, packlib.now_kst().date().isoformat())
    packlib.write_bytes_atomic(args.out, packlib.pretty_bytes(doc))
    print(f"완료: {args.out} — {version} 사진 {len(ids)}개 (버전 {len(doc['versions'])}개)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
