#!/usr/bin/env python3
"""PASSLOG 여행지 정보 스냅샷 생성기 (Python 3.11+, 표준 라이브러리만 사용).

외교부 공공데이터(apis.data.go.kr)와 한국수출입은행 환율 API를 조회해
정적 호스팅용 JSON 스냅샷을 만든다.

출력 (기본: info-pipeline/public/v1)
  meta.json               생성 시각 · 출처 · 스키마 버전
  rates.json              통화별 매매기준율 (1단위당 KRW)
  countries/{ISO2}.json   국가별 입국 요건 · 여행경보

환경 변수
  DATA_GO_KR_KEY   공공데이터포털 인증키 (Decoding 키 권장 — 스크립트가 URL 인코딩함)
  KOREAEXIM_KEY    한국수출입은행 Open API 인증키

소스별로 실패하면 기존 public/v1 파일 내용을 그대로 유지한다.
모든 소스가 실패했을 때만 0이 아닌 종료 코드를 반환한다.

여행경보: 같은 출력 폴더에 advisories.json(scripts/advisories.py, 0404 · 키 없음)이 있으면 그것으로
countries/{ISO2}.json 의 advisory 를 채운다 — level = 나라 '나머지 전역' 기본 단계(예전처럼 나라 안 최고 단계를 쓰면
후쿠시마 30km 때문에 일본 전체가 3단계로 보였다), maxLevel · regionsSummary 를 함께 넣는다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

KST = dt.timezone(dt.timedelta(hours=9), "KST")
SCHEMA_VERSION = 1
USER_AGENT = "passlog-info-pipeline/1.0 (+https://github.com/)"

SCRIPT_DIR = Path(__file__).resolve().parent
PIPELINE_DIR = SCRIPT_DIR.parent
DEFAULT_OUT = PIPELINE_DIR / "public" / "v1"
# 모노레포: ../shared/data/countries.json, 별도 리포: data/countries.json (복사본)
DEFAULT_COUNTRIES = next(
    (p for p in (PIPELINE_DIR.parent / "shared" / "data" / "countries.json",
                 PIPELINE_DIR / "data" / "countries.json") if p.exists()),
    PIPELINE_DIR.parent / "shared" / "data" / "countries.json",
)

MOFA_SOURCE = {"name": "외교부 해외안전여행", "url": "https://www.0404.go.kr"}
EXIM_SOURCE = {"name": "한국수출입은행", "url": "https://www.koreaexim.go.kr"}

# ---------------------------------------------------------------------------
# 엔드포인트
# ---------------------------------------------------------------------------
DATA_GO_KR = "https://apis.data.go.kr/1262000"

# 외교부_국가·지역별 입국허가요건 (data.go.kr 15075345) — 공식 페이지에서 확인함.
#   GET .../EntranceVisaService2/getEntranceVisaList2
#   params: serviceKey, returnType=JSON, numOfRows, pageNo, cond[country_iso_alp2::EQ]
#   fields: country_iso_alp2(있을 경우), country_nm, country_eng_nm, iso_num,
#           have_yn, gnrl_pspt_visa_yn, gnrl_pspt_visa_cn, nvisa_entry_evdc_cn, remark
ENTRY_URL = os.environ.get(
    "ENTRY_URL", f"{DATA_GO_KR}/EntranceVisaService2/getEntranceVisaList2"
)

# 여행경보 현재 단계 — 미결 사항(스펙 14 #10).
# [잠정] 외교부_국가·지역별 여행경보 목록 (data.go.kr 15076237)
#   GET .../TravelAlarmService2/getTravelAlarmList2
#   fields: country_iso_alp2, alarm_lvl(경보단계), region_ty(지역유형: 전체/일부), written_dt
#   -> 국가당 여러 행(지역별)이 올 수 있어 alarm_lvl의 최댓값을 현재 단계로 쓴다.
#   alarm_lvl 값 범위가 문서에 명시돼 있지 않아 숫자 추출 방식으로 방어적으로 파싱한다.
#   대안: 여행경보 조정(15076243, CountryHistoryService2/getCountryHistoryList2)은
#   공지 히스토리(title/txt_origin_cn)만 있어 단계를 본문에서 파싱해야 하므로 보조용.
ALARM_URL = os.environ.get(
    "ALARM_URL", f"{DATA_GO_KR}/TravelAlarmService2/getTravelAlarmList2"
)

# [잠정] 외교부_국가·지역별 특별여행주의보 (data.go.kr 15076244)
#   공식 페이지에 오퍼레이션 경로가 노출되지 않음. 무효 키로 탐침했을 때
#   SptravelWarningServiceV2/getSptravelWarningListV2 만 403(등록된 API, 인증 실패)을,
#   다른 후보(…Service2/…List2 등)는 400(미등록)을 반환해 이 경로로 판단함.
#   응답 필드명(철수권고 비고 · 지역유형 등)은 미확인 → 행 존재 여부로 special 판정.
#   첫 실키 실행 후 응답을 확인하고, 다르면 SPECIAL_URL 환경 변수로 바꾼다.
SPECIAL_URL = os.environ.get(
    "SPECIAL_URL", f"{DATA_GO_KR}/SptravelWarningServiceV2/getSptravelWarningListV2"
)

# 외교부_여행금지제도 (data.go.kr 15000504) — 공식 페이지에서 확인함.
#   GET .../TravelBanService/getTravelBanList
#   params: ServiceKey, numOfRows, pageNo, (isoCode1..10)
#   fields: isoCode, countryName, ban(여행금지), banPartial(일부), banNote, wrtDt
#   [잠정] returnType=JSON 지원 여부가 문서에 없어 _type=json 도 함께 보내고 XML이면 XML로 파싱.
BAN_URL = os.environ.get("BAN_URL", f"{DATA_GO_KR}/TravelBanService/getTravelBanList")

# 한국수출입은행 현재환율 (AP01). 2026-04-30부로 www 도메인 종료 → oapi 도메인 사용.
EXIM_URL = os.environ.get(
    "EXIM_URL", "https://oapi.koreaexim.go.kr/site/program/financial/exchangeJSON"
)


class SourceError(Exception):
    pass


# ---------------------------------------------------------------------------
# HTTP 유틸
# ---------------------------------------------------------------------------
def http_get(url: str, params: dict[str, Any], timeout: float = 30.0, retries: int = 3) -> bytes:
    # data.go.kr의 cond[...] 파라미터는 대괄호를 그대로 두어도/인코딩해도 동작하는 것으로 알려져 있다.
    query = urllib.parse.urlencode(params, safe="[]:")
    full = f"{url}?{query}"
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(full, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise SourceError(f"GET 실패: {url} ({last})")


def parse_json_or_xml(raw: bytes) -> dict[str, Any]:
    text = raw.decode("utf-8", errors="replace").strip()
    if text.startswith("{") or text.startswith("["):
        return json.loads(text)
    if text.startswith("<"):
        return {"__xml__": xml_items(text)}
    raise SourceError(f"알 수 없는 응답 형식: {text[:200]!r}")


def xml_items(text: str) -> list[dict[str, str]]:
    import xml.etree.ElementTree as ET  # 표준 라이브러리

    root = ET.fromstring(text)
    # 공공데이터 오류 응답(OpenAPI_ServiceResponse) 처리
    err = root.find(".//returnAuthMsg")
    if err is not None and (err.text or "").strip() not in ("", "NORMAL SERVICE."):
        raise SourceError(f"API 오류: {err.text}")
    code = root.find(".//resultCode")
    if code is not None and (code.text or "").strip() not in ("0", "00", "000"):
        msg = root.find(".//resultMsg")
        raise SourceError(f"API 오류 {code.text}: {msg.text if msg is not None else ''}")
    return [{c.tag: (c.text or "").strip() for c in item} for item in root.iter("item")]


def extract_items(doc: dict[str, Any]) -> tuple[list[dict[str, Any]], int | None]:
    """data.go.kr 응답에서 item 목록과 totalCount를 꺼낸다 (여러 응답 형태 대응)."""
    if "__xml__" in doc:
        return doc["__xml__"], None
    body: Any = doc
    if "response" in doc:
        resp = doc["response"]
        header = resp.get("header", {})
        code = str(header.get("resultCode", "0")).strip()
        if code not in ("0", "00", "000"):
            raise SourceError(f"API 오류 {code}: {header.get('resultMsg')}")
        body = resp.get("body", {})
    else:
        if "resultCode" in doc and str(doc["resultCode"]).strip() not in ("0", "00", "000"):
            raise SourceError(f"API 오류 {doc['resultCode']}: {doc.get('resultMsg')}")
    total = body.get("totalCount") if isinstance(body, dict) else None
    items: Any = body.get("items") if isinstance(body, dict) else None
    if items is None and isinstance(body, dict):
        items = body.get("data")  # 일부 *Service2 응답은 data 배열
    if isinstance(items, dict):
        items = items.get("item", [])
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        items = []
    try:
        total_i = int(total) if total is not None else None
    except (TypeError, ValueError):
        total_i = None
    return items, total_i


def fetch_all_pages(url: str, key: str, extra: dict[str, Any] | None = None,
                    key_param: str = "serviceKey", page_size: int = 300) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    page = 1
    while True:
        params = {key_param: key, "returnType": "JSON", "_type": "json",
                  "numOfRows": page_size, "pageNo": page}
        params.update(extra or {})
        items, total = extract_items(parse_json_or_xml(http_get(url, params)))
        out.extend(items)
        if not items or len(items) < page_size or (total is not None and len(out) >= total):
            break
        page += 1
        if page > 50:
            break
    return out


def iso2_of(item: dict[str, Any]) -> str | None:
    for k in ("country_iso_alp2", "isoCode", "iso_code", "ISO_CODE", "country_iso_alp2_cd"):
        v = item.get(k)
        if isinstance(v, str) and re.fullmatch(r"[A-Za-z]{2}", v.strip()):
            return v.strip().upper()
    return None


def clean_text(v: Any) -> str | None:
    if v is None:
        return None
    s = re.sub(r"<[^>]+>", " ", str(v))
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


# ---------------------------------------------------------------------------
# 소스 1: 입국허가요건
# ---------------------------------------------------------------------------
def parse_entry(item: dict[str, Any]) -> dict[str, Any]:
    cn = clean_text(item.get("gnrl_pspt_visa_cn"))
    basis = clean_text(item.get("nvisa_entry_evdc_cn"))
    remark = clean_text(item.get("remark"))
    yn = str(item.get("gnrl_pspt_visa_yn") or "").strip().upper()
    text = " ".join(x for x in (cn, basis, remark) if x)

    visa_free: bool | None = None
    if re.search(r"비자\s*필요|사증\s*필요|입국\s*불가|사전\s*비자", text):
        visa_free = False
    if re.search(r"무비자|사증\s*면제|비자\s*면제|visa[-\s]?free", text, re.I):
        visa_free = True
    if visa_free is None and yn in ("Y", "N"):
        # [잠정] gnrl_pspt_visa_yn 의미가 문서상 모호함 — 본문으로 판별 못 했을 때만 참고.
        visa_free = yn == "Y" and bool(cn)

    max_days: int | None = None
    m = re.search(r"(\d{1,3})\s*일", cn or text)
    if m:
        max_days = int(m.group(1))
    else:
        m = re.search(r"(\d{1,2})\s*개월", cn or text)
        if m:
            max_days = int(m.group(1)) * 30
    if visa_free is False:
        max_days = None

    note = " / ".join(x for x in (cn, basis, remark) if x) or None
    if note and len(note) > 500:
        note = note[:497] + "..."
    return {"visaFree": visa_free, "maxStayDays": max_days, "note": note}


def fetch_entry(key: str, codes: list[str], name_to_code: dict[str, str]) -> dict[str, dict[str, Any]]:
    items = fetch_all_pages(ENTRY_URL, key)
    result: dict[str, dict[str, Any]] = {}
    for it in items:
        code = iso2_of(it) or name_to_code.get(str(it.get("country_nm", "")).strip())
        if code:
            result[code] = parse_entry(it)
    if not result:
        raise SourceError("입국허가요건: 결과 없음")
    return result


# ---------------------------------------------------------------------------
# 소스 2: 여행경보 · 특별여행주의보 · 여행금지
# ---------------------------------------------------------------------------
def level_of(v: Any) -> int:
    m = re.search(r"[1-4]", str(v or ""))
    return int(m.group(0)) if m else 0


def fetch_alarm(key: str, name_to_code: dict[str, str]) -> dict[str, dict[str, Any]]:
    items = fetch_all_pages(ALARM_URL, key)
    res: dict[str, dict[str, Any]] = {}
    for it in items:
        code = iso2_of(it) or name_to_code.get(str(it.get("country_nm", "")).strip())
        if not code:
            continue
        lvl = level_of(it.get("alarm_lvl"))
        cur = res.setdefault(code, {"level": 0, "partial": False})
        if lvl > cur["level"]:
            cur["level"] = lvl
            cur["partial"] = "일부" in str(it.get("region_ty") or "")
    if not items:
        raise SourceError("여행경보: 결과 없음")
    return res


def fetch_special(key: str, name_to_code: dict[str, str]) -> dict[str, str | None]:
    items = fetch_all_pages(SPECIAL_URL, key)
    res: dict[str, str | None] = {}
    for it in items:
        code = iso2_of(it) or name_to_code.get(str(it.get("country_nm", "")).strip())
        if not code:
            continue
        # 응답에 행이 있으면 특별여행주의보 대상으로 본다. 해제 표시 필드가 있으면 제외.
        flags = " ".join(str(it.get(k) or "") for k in ("splimit", "splimit_partial", "sp_limit", "remark"))
        if re.search(r"해제", flags):
            continue
        note = next((it[k] for k in it if re.search(r"note|remark|rmk", k, re.I) and it[k]), None)
        res[code] = clean_text(note)
    return res


def fetch_ban(key: str, name_to_code: dict[str, str]) -> dict[str, dict[str, Any]]:
    items = fetch_all_pages(BAN_URL, key, key_param="ServiceKey")
    res: dict[str, dict[str, Any]] = {}
    for it in items:
        code = iso2_of(it) or name_to_code.get(str(it.get("countryName", "")).strip())
        if not code:
            continue
        ban = str(it.get("ban") or "").strip()
        partial = str(it.get("banPartial") or "").strip()
        if (ban and ban.upper() not in ("N", "0")) or (partial and partial.upper() not in ("N", "0")):
            res[code] = {"partial": not ban or ban.upper() in ("N", "0"),
                         "note": clean_text(it.get("banNote"))}
    if not items:
        raise SourceError("여행금지: 결과 없음")
    return res


# ---------------------------------------------------------------------------
# 소스 3: 환율
# ---------------------------------------------------------------------------
def fetch_rates(key: str, today: dt.date) -> tuple[str, dict[str, float]]:
    for back in range(0, 8):  # 오늘 + 최대 7일 전까지
        day = today - dt.timedelta(days=back)
        raw = http_get(EXIM_URL, {"authkey": key, "searchdate": day.strftime("%Y%m%d"), "data": "AP01"})
        text = raw.decode("utf-8", errors="replace").strip()
        if not text or text in ("[]", "null"):
            continue  # 비영업일 / 11시 이전
        rows = json.loads(text)
        if not isinstance(rows, list) or not rows:
            continue
        rates: dict[str, float] = {}
        for r in rows:
            if str(r.get("result")) != "1":
                # result: 1 성공, 2 DATA코드 오류, 3 인증코드 오류, 4 일일제한횟수 마감
                raise SourceError(f"환율 API result={r.get('result')}")
            unit = str(r.get("cur_unit", "")).strip()
            m = re.fullmatch(r"([A-Z]{3})(?:\((\d+)\))?", unit)
            if not m or m.group(1) == "KRW":
                continue
            divisor = int(m.group(2) or 1)
            try:
                val = float(str(r.get("deal_bas_r", "")).replace(",", "")) / divisor
            except ValueError:
                continue
            rates[m.group(1)] = round(val, 4)
        # 수출입은행은 위안화를 역외 위안(CNH)으로 고시 → 앱 편의를 위해 CNY 별칭 추가
        if "CNH" in rates and "CNY" not in rates:
            rates["CNY"] = rates["CNH"]
        if rates:
            return day.isoformat(), dict(sorted(rates.items()))
    raise SourceError("환율: 최근 7일간 응답 없음")


# ---------------------------------------------------------------------------
# 파일 입출력
# ---------------------------------------------------------------------------
def read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def run_source(name: str, fn: Callable[[], Any], status: dict[str, bool]) -> Any | None:
    try:
        val = fn()
        status[name] = True
        print(f"[ok]   {name}", file=sys.stderr)
        return val
    except Exception as e:  # 소스별 실패는 전체 실패로 보지 않는다
        status[name] = False
        print(f"[fail] {name}: {e}", file=sys.stderr)
        return None


def advisory_from_index(index: dict[str, Any], code: str) -> dict[str, Any] | None:
    """advisories.json(0404) 나라 기록 → countries/{ISO2}.json 의 advisory (속령은 parents 로)"""
    countries = index.get("countries") or {}
    c = countries.get(code) or countries.get((index.get("parents") or {}).get(code, ""))
    if not c:
        return None
    banned = c.get("banned")
    level = int(c.get("level") or 0)
    summary_ko = c.get("summaryKo")
    note = summary_ko if summary_ko and (int(c.get("maxLevel") or 0) > level or c.get("special") or banned == "partial") else None
    return {"level": level, "special": bool(c.get("baselineSpecial")), "banned": banned is True or level >= 4,
            "note": note, "maxLevel": int(c.get("maxLevel") or level), "regionsSummary": summary_ko}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="PASSLOG 여행지 정보 정적 스냅샷 생성 (외교부 · 한국수출입은행).",
        epilog="환경 변수: DATA_GO_KR_KEY (외교부 API), KOREAEXIM_KEY (환율). "
               "엔드포인트 재정의: ENTRY_URL, ALARM_URL, SPECIAL_URL, BAN_URL, EXIM_URL",
    )
    ap.add_argument("--countries", type=Path, default=DEFAULT_COUNTRIES,
                    help=f"countries.json 경로 (기본: {DEFAULT_COUNTRIES})")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"출력 디렉터리 (기본: {DEFAULT_OUT})")
    ap.add_argument("--date", help="환율 조회 기준일 YYYY-MM-DD (기본: 오늘 KST)")
    ap.add_argument("--advisories", type=Path, help="여행경보 색인 (기본: <out>/advisories.json — advisories.py 가 먼저 만든다)")
    args = ap.parse_args(argv)
    adv_index = read_json(args.advisories or (args.out / "advisories.json"))
    if adv_index and not isinstance(adv_index.get("countries"), dict):
        adv_index = None

    data_key = os.environ.get("DATA_GO_KR_KEY", "").strip()
    exim_key = os.environ.get("KOREAEXIM_KEY", "").strip()
    if "%" in data_key:  # Encoding 키를 넣은 경우 이중 인코딩 방지
        data_key = urllib.parse.unquote(data_key)
    if not data_key and not exim_key and not adv_index:
        print("오류: 인증키가 없습니다. DATA_GO_KR_KEY(공공데이터포털)와 KOREAEXIM_KEY(한국수출입은행) "
              "환경 변수를 설정하세요. 기존 스냅샷은 변경하지 않았습니다.", file=sys.stderr)
        return 2

    countries_doc = read_json(args.countries)
    if not countries_doc or "countries" not in countries_doc:
        print(f"오류: countries.json을 읽을 수 없습니다: {args.countries}", file=sys.stderr)
        return 2
    countries = countries_doc["countries"]
    codes = [c["code"] for c in countries]
    name_ko = {c["code"]: c["nameKo"] for c in countries}
    name_to_code = {c["nameKo"]: c["code"] for c in countries}

    now = dt.datetime.now(KST).replace(microsecond=0)
    generated_at = now.isoformat()
    today = dt.date.fromisoformat(args.date) if args.date else now.date()
    out: Path = args.out
    status: dict[str, bool] = {}

    entry = alarm = special = ban = None
    if data_key:
        entry = run_source("입국허가요건", lambda: fetch_entry(data_key, codes, name_to_code), status)
        alarm = run_source("여행경보", lambda: fetch_alarm(data_key, name_to_code), status)
        special = run_source("특별여행주의보", lambda: fetch_special(data_key, name_to_code), status)
        ban = run_source("여행금지", lambda: fetch_ban(data_key, name_to_code), status)
    else:
        print("[skip] DATA_GO_KR_KEY 없음 — 외교부 데이터는 기존 파일 유지", file=sys.stderr)
        for n in ("입국허가요건", "여행경보", "특별여행주의보", "여행금지"):
            status[n] = False

    rates = None
    if exim_key:
        rates = run_source("환율", lambda: fetch_rates(exim_key, today), status)
    else:
        print("[skip] KOREAEXIM_KEY 없음 — 환율은 기존 파일 유지", file=sys.stderr)
        status["환율"] = False

    status["여행경보(0404)"] = bool(adv_index)
    if not any(status.values()):
        print("오류: 모든 소스 조회에 실패했습니다. 기존 스냅샷을 유지합니다.", file=sys.stderr)
        return 1

    # rates.json
    if rates:
        date_str, table = rates
        write_json(out / "rates.json", {"base": "KRW", "date": date_str,
                                        "generatedAt": generated_at, "rates": table})

    # countries/{ISO2}.json — 실패한 소스의 필드는 기존 파일 값을 유지
    mofa_any = any(status.get(n) for n in ("입국허가요건", "여행경보", "특별여행주의보", "여행금지", "여행경보(0404)"))
    if mofa_any:
        for code in codes:
            path = out / "countries" / f"{code}.json"
            prev = read_json(path) or {}
            prev_entry = prev.get("entry") or {"visaFree": None, "maxStayDays": None, "note": None}
            prev_adv = prev.get("advisory") or {"level": 0, "special": False, "banned": False, "note": None}

            e = prev_entry
            if entry is not None:
                e = entry.get(code) or {"visaFree": None, "maxStayDays": None, "note": None}
            if code == "KR":
                e = {"visaFree": True, "maxStayDays": None, "note": "대한민국 국민은 비자가 필요하지 않습니다."}

            adv = dict(prev_adv)
            notes: list[str] = []
            if alarm is not None:
                a = alarm.get(code, {"level": 0, "partial": False})
                adv["level"] = a["level"]
                if a["level"] and a["partial"]:
                    notes.append(f"일부 지역 {a['level']}단계")
            if special is not None:
                adv["special"] = code in special
                if code in special:
                    notes.append("특별여행주의보" + (f": {special[code]}" if special[code] else ""))
            if ban is not None:
                b = ban.get(code)
                adv["banned"] = bool(b)
                if b:
                    adv["level"] = max(adv.get("level") or 0, 4)
                    notes.append(("일부 지역 " if b["partial"] else "") + "여행금지"
                                 + (f": {b['note']}" if b["note"] else ""))
            if alarm is not None or special is not None or ban is not None:
                adv["note"] = " / ".join(notes) or None
            # 0404 색인이 있으면 그것이 우선 (지역 단위 · 기본 단계)
            from_index = advisory_from_index(adv_index, code) if adv_index else None
            if from_index:
                adv = from_index

            write_json(path, {
                "code": code,
                "nameKo": name_ko[code],
                "entry": {"visaFree": e.get("visaFree"), "maxStayDays": e.get("maxStayDays"),
                          "note": e.get("note")},
                "advisory": {"level": int(adv.get("level") or 0), "special": bool(adv.get("special")),
                             "banned": bool(adv.get("banned")), "note": adv.get("note"),
                             **({"maxLevel": adv["maxLevel"]} if adv.get("maxLevel") is not None else {}),
                             **({"regionsSummary": adv["regionsSummary"]} if adv.get("regionsSummary") else {})},
                "sources": [MOFA_SOURCE["name"]],
                "generatedAt": generated_at,
            })

    write_json(out / "meta.json", {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": generated_at,
        "sources": [MOFA_SOURCE, EXIM_SOURCE],
        "sample": False,
    })

    failed = [n for n, ok in status.items() if not ok]
    print(f"완료: {generated_at} (실패/건너뜀: {', '.join(failed) or '없음'})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
