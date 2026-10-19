#!/usr/bin/env python3
"""공휴일 데이터셋 — 'holidays-kr'(번들 kr-holidays.json 형식) · 'holidays-world'(봉투, 원격 전용).

holidays-kr
  - 바탕: 큐레이션 kr-holidays.json (천문연 월력요항 + 2026 공휴일 규정 개정 반영 — 사람이 확인한 값)
  - python-holidays(KR): 큐레이션에 없는 해(올해~+2년)를 채우고, 겹치는 해는 교차 확인(경고만)
  - 천문연 특일 getRestDeInfo(DATA_GO_KR_KEY 있을 때): 큐레이션에 없는 공휴일(임시공휴일 · 선거일)을 더한다
  - schoolVacations · rules 는 큐레이션 그대로. 아무것도 더하지 못하면 큐레이션 파일을 바이트 그대로 싣는다.
holidays-world
  - python-holidays: 카탈로그 나라(KR 제외)의 올해 · 내년 공휴일
  - data = {"years": [2026, 2027], "countries": {"JP": [{"date": "2026-11-03", "name": "文化の日", "nameEn": "Culture Day"}]}}

python-holidays 는 GitHub 러너에서만 설치한다(requirements.lock). 없으면 해당 부분을 건너뛴다.
이 파일 이름이 패키지 이름과 같으므로, 패키지는 `_holidays_pkg()` 로만 불러온다.
"""
from __future__ import annotations

import datetime as dt
import importlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent
_SCRIPTS = _HERE.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import packlib  # noqa: E402
from sources import net  # noqa: E402

KASI_URL = "https://apis.data.go.kr/B090041/openapi/service/SpcdeInfoService/getRestDeInfo"
KASI_SOURCE = {"name": "한국천문연구원 특일 정보", "url": "https://www.data.go.kr/data/15012690/openapi.do"}
PYHOLIDAYS_SOURCE = {"name": "python-holidays (MIT)", "url": "https://github.com/vacanza/holidays"}


def _holidays_pkg():
    """pip 패키지 holidays (이 파일 자신이 아니라). 없으면 None"""
    mod = sys.modules.get("holidays")
    if mod is not None and getattr(mod, "__file__", None) and Path(mod.__file__).resolve() != Path(__file__).resolve():
        return mod
    saved = list(sys.path)
    try:
        sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() not in (_HERE,)]
        if "holidays" in sys.modules and Path(getattr(sys.modules["holidays"], "__file__", "") or "").resolve() == Path(__file__).resolve():
            del sys.modules["holidays"]
        return importlib.import_module("holidays")
    except Exception:  # noqa: BLE001 — 설치 안 됨
        return None
    finally:
        sys.path[:] = saved


# ---------------------------------------------------------------------------
# 이름 맞추기: python-holidays · 특일 이름 → 앱 표기(kind)
# ---------------------------------------------------------------------------
_RULES: List[Tuple[str, str, str]] = [
    # (정규식, 앱 이름, kind) — 위에서부터 처음 맞는 것
    (r"대체\s*공휴일|대체휴일", "", "substitute"),
    (r"임시\s*공휴일", "임시공휴일", "temporary"),
    (r"선거", "", "election"),
    (r"신정|새해", "새해 첫날", "fixed"),
    (r"설날\s*(전날|다음\s*날|연휴)", "설날 연휴", "lunar"),
    (r"설날", "설날", "lunar"),
    (r"삼일절|3[·.]?1절", "삼일절", "fixed"),
    (r"노동절|근로자의\s*날", "노동절", "fixed"),
    (r"어린이날", "어린이날", "fixed"),
    (r"부처님\s*오신\s*날|석가탄신일", "부처님오신날", "lunar"),
    (r"현충일", "현충일", "fixed"),
    (r"제헌절", "제헌절", "fixed"),
    (r"광복절", "광복절", "fixed"),
    (r"추석\s*(전날|다음\s*날|연휴)", "추석 연휴", "lunar"),
    (r"추석", "추석", "lunar"),
    (r"개천절", "개천절", "fixed"),
    (r"한글날", "한글날", "fixed"),
    (r"기독탄신일|성탄절|크리스마스", "성탄절", "fixed"),
]


def app_name(raw: str) -> Tuple[str, str]:
    """(앱 표기 이름, kind)"""
    s = re.sub(r"\s+", " ", str(raw or "")).strip()
    for pattern, name, kind in _RULES:
        if re.search(pattern, s):
            if kind == "substitute":
                inner = re.search(r"\(([^)]*)\)", s)
                base = inner.group(1).strip() if inner else ""
                base = ", ".join(app_name(x)[0] for x in re.split(r"[,·]\s*", base) if x.strip()) if base else ""
                return (f"대체공휴일({base})" if base else "대체공휴일"), "substitute"
            if kind == "election":
                return s.replace(" ", "") if len(s) <= 20 else "선거일", "election"
            return name, kind
    return s or "공휴일", "fixed"


def _row(date: str, name: str, kind: str) -> Dict[str, Any]:
    return {"date": date, "name": name, "kind": kind, "isSubstitute": kind == "substitute"}


def pyholidays_kr(years: Iterable[int], pkg: Any = None) -> List[Dict[str, Any]]:
    pkg = pkg or _holidays_pkg()
    if pkg is None:
        raise RuntimeError("python-holidays 없음")
    try:
        cal = pkg.country_holidays("KR", years=sorted(set(years)), language="ko")
    except Exception:  # noqa: BLE001 — 오래된 버전은 language 를 모른다
        cal = pkg.country_holidays("KR", years=sorted(set(years)))
    out = []
    for day, raw in sorted(cal.items()):
        for part in str(raw).split("; "):
            name, kind = app_name(part)
            out.append(_row(day.isoformat(), name, kind))
    return out


def parse_kasi(raw: bytes) -> List[Dict[str, Any]]:
    """getRestDeInfo 응답(JSON 또는 XML) → 공휴일 행 (isHoliday=Y 만)"""
    text = raw.decode("utf-8", errors="replace").strip()
    items: List[Dict[str, Any]] = []
    if text.startswith("{"):
        doc = json.loads(text)
        header = (doc.get("response") or {}).get("header") or {}
        code = str(header.get("resultCode", "00"))
        if code not in ("00", "0", "0000"):
            raise RuntimeError(f"특일 API resultCode {code}")
        body = (doc.get("response") or {}).get("body") or {}
        it = body.get("items")
        if isinstance(it, dict):
            it = it.get("item")
        if isinstance(it, dict):
            it = [it]
        items = [x for x in (it or []) if isinstance(x, dict)]
    elif text.startswith("<"):
        import xml.etree.ElementTree as ET

        root = ET.fromstring(text)
        code = root.find(".//resultCode")
        if code is not None and (code.text or "").strip() not in ("00", "0", "0000"):
            raise RuntimeError(f"특일 API resultCode {(code.text or '').strip()}")
        items = [{c.tag: (c.text or "").strip() for c in item} for item in root.iter("item")]
    else:
        raise RuntimeError("특일 API: 알 수 없는 응답")
    out = []
    for x in items:
        if str(x.get("isHoliday", "Y")).upper() != "Y":
            continue
        loc = str(x.get("locdate") or "")
        if not re.fullmatch(r"\d{8}", loc):
            continue
        name, kind = app_name(str(x.get("dateName") or ""))
        out.append(_row(f"{loc[:4]}-{loc[4:6]}-{loc[6:]}", name, kind))
    return out


def fetch_kasi(key: str, years: Iterable[int], getter: Callable[..., Tuple[int, Dict[str, str], bytes]] = net.get
               ) -> List[Dict[str, Any]]:
    import urllib.parse

    out: List[Dict[str, Any]] = []
    for y in sorted(set(years)):
        for m in range(1, 13):
            q = urllib.parse.urlencode({"solYear": y, "solMonth": f"{m:02d}", "numOfRows": 50, "_type": "json",
                                        "ServiceKey": key})
            try:
                status, _h, body = getter(f"{KASI_URL}?{q}", timeout=20)
            except net.FetchError as e:
                raise RuntimeError(f"특일 API {y}-{m:02d}: {e}") from None
            if status != 200:
                raise RuntimeError(f"특일 API {y}-{m:02d}: HTTP {status}")
            out.extend(parse_kasi(body))
    return out


def _key(row: Dict[str, Any]) -> Tuple[str, str]:
    return row["date"], row["name"]


def build_kr(base: Dict[str, Any], *, today: dt.date, pkg: Any = None, kasi_rows: Optional[List[Dict[str, Any]]] = None,
             ) -> Tuple[Dict[str, Any], List[str], List[str]]:
    """(새 kr-holidays 문서, 쓴 출처 이름, 경고). 바뀐 게 없으면 base 와 같은 내용"""
    warnings: List[str] = []
    used: List[str] = []
    holidays = [dict(h) for h in base.get("holidays") or [] if isinstance(h, dict) and h.get("date")]
    base_years = sorted({int(y) for y in base.get("years") or []} | {int(h["date"][:4]) for h in holidays})
    target = [today.year, today.year + 1, today.year + 2]
    have = {_key(h) for h in holidays}
    have_dates = {h["date"] for h in holidays}

    pkg = pkg if pkg is not None else _holidays_pkg()
    if pkg is not None:
        try:
            computed = pyholidays_kr(sorted(set(target) | set(base_years)), pkg)
            used.append(PYHOLIDAYS_SOURCE["name"])
            comp_dates = {}
            for r in computed:
                comp_dates.setdefault(r["date"], r)
            # 겹치는 해: 교차 확인만
            for y in base_years:
                cd = {d for d in comp_dates if d.startswith(str(y))}
                bd = {d for d in have_dates if d.startswith(str(y))}
                only_c = sorted(cd - bd)
                only_b = sorted(bd - cd)
                if only_c:
                    warnings.append(f"python-holidays 에만 있는 {y} 공휴일: {', '.join(only_c[:6])}")
                if only_b:
                    warnings.append(f"큐레이션에만 있는 {y} 공휴일: {', '.join(only_b[:6])}")
            # 큐레이션에 없는 해는 계산값으로 채운다
            for r in computed:
                y = int(r["date"][:4])
                if y in target and y not in base_years and _key(r) not in have:
                    holidays.append(r)
                    have.add(_key(r))
        except Exception as e:  # noqa: BLE001
            warnings.append(f"python-holidays 실패: {e}")
    if kasi_rows:
        used.append(KASI_SOURCE["name"])
        for r in kasi_rows:
            same_day = [h for h in holidays if h["date"] == r["date"]]
            # 이미 있는 날은 이름 표기만 다른 것 → 건너뜀. 임시공휴일 · 선거일은 같은 종류가 없을 때만 더한다
            if same_day and (r["kind"] not in ("temporary", "election")
                             or any(h["kind"] == r["kind"] for h in same_day)):
                continue
            holidays.append(r)
            have.add(_key(r))
            have_dates.add(r["date"])
    if len(holidays) == len([h for h in base.get("holidays") or [] if isinstance(h, dict) and h.get("date")]):
        return dict(base), used, warnings  # 더한 것이 없으면 큐레이션 그대로 (순서도 그대로)
    holidays.sort(key=lambda h: (h["date"], h["name"]))
    years = sorted({int(h["date"][:4]) for h in holidays} | set(base_years))
    doc = dict(base)
    doc["years"] = years
    doc["holidays"] = holidays
    return doc, used, warnings


def build_world(country_codes: Iterable[str], *, today: dt.date, pkg: Any = None) -> Tuple[Dict[str, Any], List[str]]:
    pkg = pkg if pkg is not None else _holidays_pkg()
    if pkg is None:
        raise RuntimeError("python-holidays 없음")
    years = [today.year, today.year + 1]
    out: Dict[str, List[Dict[str, Any]]] = {}
    skipped: List[str] = []
    for cc in sorted({c.upper() for c in country_codes if c and c.upper() != "KR"}):
        try:
            cal = pkg.country_holidays(cc, years=years)
        except Exception:  # noqa: BLE001 — 지원 안 하는 나라
            skipped.append(cc)
            continue
        en = None
        langs = getattr(cal, "supported_languages", ()) or ()
        if "en_US" in langs and getattr(cal, "default_language", None) != "en_US":
            try:
                en = pkg.country_holidays(cc, years=years, language="en_US")
            except Exception:  # noqa: BLE001
                en = None
        rows = []
        for day, name in sorted(cal.items()):
            row: Dict[str, Any] = {"date": day.isoformat(), "name": str(name)}
            if en is not None and en.get(day) and str(en.get(day)) != str(name):
                row["nameEn"] = str(en.get(day))
            rows.append(row)
        if rows:
            out[cc] = rows
    return {"years": years, "countries": out}, skipped


if __name__ == "__main__":
    # 진단용: python3 -I scripts/sources/holidays.py → 패키지 버전과 한국 2027년 공휴일 수
    p = _holidays_pkg()
    if p is None:
        print("python-holidays 없음 (GitHub 러너에서는 requirements.lock 으로 설치)")
        sys.exit(0)
    print("python-holidays", getattr(p, "__version__", "?"), len(pyholidays_kr([dt.date.today().year + 1], p)))
