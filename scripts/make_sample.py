#!/usr/bin/env python3
"""인증키 없이 앱 개발용 샘플 스냅샷을 만든다 (meta.json의 "sample": true).

모든 값은 사람이 입력한 추정치이며 실제 외교부 · 수출입은행 데이터가 아니다.
실행: python3 -I info-pipeline/scripts/make_sample.py [--out DIR] [--countries PATH]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PIPELINE_DIR = SCRIPT_DIR.parent
DEFAULT_OUT = PIPELINE_DIR / "public" / "v1"
DEFAULT_COUNTRIES = PIPELINE_DIR.parent / "shared" / "data" / "countries.json"

GENERATED_AT = "2026-10-13T06:05:00+09:00"
RATE_DATE = "2026-10-12"
TAG = "[샘플 데이터] "

# KRW per 1 unit — 2026년 10월 가정치 (샘플)
RATES = {
    "USD": 1385.2, "EUR": 1612.4, "JPY": 9.2341, "CNY": 194.32, "CNH": 194.05,
    "GBP": 1852.6, "HKD": 177.94, "TWD": 45.31, "SGD": 1078.5, "THB": 42.13,
    "VND": 0.0527, "PHP": 24.12, "MYR": 327.4, "IDR": 0.0848, "MOP": 172.76,
    "AUD": 912.3, "NZD": 823.6, "CAD": 1003.4, "CHF": 1735.2, "CZK": 66.21,
    "TRY": 28.94, "SEK": 147.52, "NOK": 137.84, "DKK": 216.08, "PLN": 378.61,
    "HUF": 4.1123, "AED": 377.16, "SAR": 369.39, "KWD": 4521.7, "BHD": 3674.3,
    "BND": 1078.5, "INR": 15.74, "MXN": 74.88,
}

# code: (visaFree, maxStayDays, entryNote, level, special, banned, advisoryNote)
SCHENGEN = "솅겐 지역 180일 중 90일 무비자. EES(출입국 시스템) 적용, ETIAS 시행 시기 확인 필요"
COUNTRIES = {
    "JP": (True, 90, "관광 목적 90일 무비자", 0, False, False, None),
    "CN": (True, 30, "한시적 무비자 입국(30일) 시행 중 — 시행 기한(2026-12-31 예정) 출국 전 반드시 확인", 0, False, False, None),
    "VN": (True, 45, "45일 무비자. 초과 체류 시 전자비자(e-Visa) 필요", 0, False, False, None),
    "TH": (True, 90, "90일 무비자. 입국 전 TDAC(전자 입국카드) 제출 필요", 3, False, False, "남부 일부 지역(나라티왓 · 얄라 · 빠따니 등) 상향 단계"),
    "PH": (True, 30, "30일 무비자. 입국 전 eTravel 등록 필요", 3, False, False, "민다나오 일부 지역 상향 단계"),
    "TW": (True, 90, "90일 무비자", 0, False, False, None),
    "HK": (True, 90, "90일 무비자", 0, False, False, None),
    "MO": (True, 90, "90일 무비자", 0, False, False, None),
    "SG": (True, 90, "90일 무비자. 입국 전 SG Arrival Card 제출 필요", 0, False, False, None),
    "MY": (True, 90, "90일 무비자. 입국 전 MDAC 제출 필요", 0, False, False, None),
    "ID": (False, None, "도착비자(VOA · e-VOA) 필요, 30일 체류. 발리 관광세 별도", 1, False, False, "파푸아 일부 지역 상향 단계"),
    "US": (True, 90, "비자면제프로그램(VWP) 90일 — ESTA 필요", 0, False, False, None),
    "GU": (True, 45, "괌-북마리아나 비자면제 45일(ESTA 없이). ESTA 소지 시 90일", 0, False, False, None),
    "MP": (True, 45, "괌-북마리아나 비자면제 45일(ESTA 없이). ESTA 소지 시 90일", 0, False, False, None),
    "CA": (True, 180, "최대 6개월 무비자 — 항공 입국 시 eTA 필요", 0, False, False, None),
    "GB": (True, 180, "최대 6개월 무비자 — 전자여행허가(ETA) 필요", 0, False, False, None),
    "FR": (True, 90, SCHENGEN, 1, False, False, "파리 등 일부 지역 여행유의"),
    "DE": (True, 90, SCHENGEN, 0, False, False, None),
    "IT": (True, 90, SCHENGEN, 0, False, False, None),
    "ES": (True, 90, SCHENGEN, 0, False, False, None),
    "CH": (True, 90, SCHENGEN, 0, False, False, None),
    "CZ": (True, 90, SCHENGEN, 0, False, False, None),
    "AU": (True, 90, "전자여행허가(ETA) 필요, 1회 최대 3개월", 0, False, False, None),
    "NZ": (True, 90, "90일 무비자 — NZeTA 필요", 0, False, False, None),
    "TR": (True, 90, "180일 중 90일 무비자", 2, False, False, "시리아 접경 일부 지역 상향 단계"),
    "KR": (True, None, "대한민국 국민은 비자가 필요하지 않습니다.", 0, False, False, None),
    "UA": (True, 90, "90일 무비자이나 여행금지 국가 — 외교부 예외적 여권사용 허가 없이 방문 시 처벌 대상", 4, False, True, "전 지역 여행금지(4단계)"),
}


def write(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="PASSLOG 샘플 스냅샷 생성 (sample: true)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--countries", type=Path, default=DEFAULT_COUNTRIES)
    args = ap.parse_args()

    names = {c["code"]: c["nameKo"] for c in
             json.loads(args.countries.read_text(encoding="utf-8"))["countries"]}

    write(args.out / "meta.json", {
        "schemaVersion": 1,
        "generatedAt": GENERATED_AT,
        "sources": [
            {"name": "외교부 해외안전여행", "url": "https://www.0404.go.kr"},
            {"name": "한국수출입은행", "url": "https://www.koreaexim.go.kr"},
        ],
        "sample": True,
    })
    write(args.out / "rates.json", {
        "base": "KRW", "date": RATE_DATE, "generatedAt": GENERATED_AT,
        "rates": dict(sorted(RATES.items())),
    })
    for code, (vf, days, enote, lvl, sp, banned, anote) in COUNTRIES.items():
        write(args.out / "countries" / f"{code}.json", {
            "code": code,
            "nameKo": names[code],
            "entry": {"visaFree": vf, "maxStayDays": days, "note": TAG + enote},
            "advisory": {"level": lvl, "special": sp, "banned": banned,
                         "note": TAG + (anote or "현재 여행경보 없음")},
            "sources": ["외교부 해외안전여행"],
            "generatedAt": GENERATED_AT,
        })
    print(f"샘플 스냅샷 생성: {args.out} (국가 {len(COUNTRIES)}개, 통화 {len(RATES)}개)")


if __name__ == "__main__":
    main()
