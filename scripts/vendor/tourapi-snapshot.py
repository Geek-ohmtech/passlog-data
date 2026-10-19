#!/usr/bin/env python3
"""tourapi-snapshot.py — dev-time snapshot of 한국관광공사 TourAPI 4.0 (KorService2) for PASSLOG.

For every domestic destination (countryCode KR in shared/data/destinations.json, plus the
kr-expand ids when their file exists) this collects nearby restaurants (음식점 39), attractions
(관광지 12 · 문화시설 14) and upcoming festivals (축제 15) and writes compact bundled JSON:

  shared/data/kr-restaurants.json   shared/data/kr-attractions.json
  shared/data/kr-festivals.json     shared/data/kr-tour-codes.json (lclsSystm code names)

The iOS app only reads those bundled files. It never calls apis.data.go.kr.
Docs (Korean, incl. JSON schema + 공공누리 display rules): docs/TourAPI-데이터.md

SERVICE KEY ("Decoding" form), first found wins: $DATA_GO_KR_KEY (CI secret) → file named by
$TOURAPI_KEY_FILE → ~/.config/passlog/data_go_kr_key. Without a key the script refuses to run
(except --offline). The key is never printed and never written anywhere (cache files store the
request params *without* serviceKey; error text is scrubbed).

QUOTA (development account): 1,000 calls/day. Every network attempt (including retries) is
counted in <cache>/usage.json per KST day; --budget caps the *day total* across runs.
Raw responses are cached (<cache>/<op>/<sha1>.json.gz) so re-runs only fetch what is missing.
Priority: existing destinations' restaurants → attractions → festivals → kr-expand ids
(restaurants, then attractions). Whatever is left is printed for the next day.
Chosen photos are checked once (ranged GET of tong.visitkorea.or.kr — not an API call) and
items with broken photos are skipped; results cached in <cache>/image-check.json.

  /usr/bin/python3 -I scripts/tourapi-snapshot.py --budget 850          # fetch + build + validate
  /usr/bin/python3 -I scripts/tourapi-snapshot.py --offline             # rebuild JSON from cache only
  /usr/bin/python3 -I scripts/tourapi-snapshot.py --only busan,jeonju   # limit destinations
  /usr/bin/python3 -I scripts/tourapi-snapshot.py raw detailIntro2 contentId=2870801 contentTypeId=39
        (debug: one call through the same budget/cache; prints the JSON body)

Options: --cache-dir (default $PASSLOG_TOURAPI_CACHE or the session scratchpad tourapi-cache),
--out-dir, --catalog, --extras <json> (kr-expand entries; default kr-new-entries.json, else
new-list.json), --no-extras, --spacing 0.3, --check-images N, --no-verify-images, --no-write.

IMPORTABLE (e.g. info-pipeline/sources/tourapi.py) — the file name has a hyphen, so load it by path:

  spec = importlib.util.spec_from_file_location("tourapi_snapshot", ROOT / "scripts/tourapi-snapshot.py")
  mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
  summary = mod.run_snapshot(budget=850, ids=None, out_dir=..., cache_dir=..., log=print)
  # or mod.main(["--budget", "850", "--out-dir", "..."]) → exit code (0 ok, 1 validation problems, 2 no key)

python3 stdlib only. Works with macOS /usr/bin/python3 (3.9).
"""
import argparse
import datetime
import gzip
import hashlib
import html
import json
import math
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "shared" / "data"
CATALOG = DATA / "destinations.json"
DEFAULT_KEY_FILE = Path.home() / ".config" / "passlog" / "data_go_kr_key"
BASE = "https://apis.data.go.kr/B551011/KorService2/"
SCRATCH = Path("/private/tmp/claude-501/-Users-ohmtech-Desktop------PASSLOG/"
               "4df4079b-34a5-42f8-8ac4-872880085c75/scratchpad")
DEFAULT_CACHE = Path(os.environ.get("PASSLOG_TOURAPI_CACHE") or (SCRATCH / "tourapi-cache"))
EXTRA_CANDIDATES = [SCRATCH / "kr-expand" / "kr-new-entries.json",
                    SCRATCH / "kr-expand" / "new-list.json"]

KST = datetime.timezone(datetime.timedelta(hours=9))
SCHEMA_VERSION = 1
SOURCE = "한국관광공사 TourAPI 4.0 KorService2"
LICENSE = "공공누리 제1유형/제3유형 — 출처: 한국관광공사"
ATTRIBUTION = "사진: 한국관광공사"

FEST_FROM = "20261020"
FEST_TO = "20271031"

# Result codes that make further calls pointless today (quota / key problems).
FATAL_CODES = {"20", "22", "30", "31", "32", "33"}
OK_CODES = {"0000", "00", "0"}

# Metropolitan lDongRegnCd (서울 부산 대구 인천 대전 울산): festivals match the whole city.
# 광주(29) is not listed: since 2026 TourAPI files it under 12 "전남광주통합특별시" with 전남, so
# 광주 is matched by its 구 codes like any other 시·군.
METRO_REGN = {"11", "26", "27", "28", "30", "31"}


# ----------------------------------------------------------------------------- utilities

def now_kst():
    return datetime.datetime.now(KST)


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def https(url):
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    return url if url.startswith("https://") else ""


_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t 　]+")


def clean_text(s, keep_newlines=False):
    """Strip HTML/<br>/entities, collapse whitespace."""
    if not s:
        return ""
    s = str(s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = _TAG.sub("", s)
    s = html.unescape(html.unescape(s))
    s = s.replace("\r", "\n")
    lines = [_WS.sub(" ", ln).strip() for ln in s.split("\n")]
    lines = [ln for ln in lines if ln]
    return ("\n" if keep_newlines else " ").join(lines).strip()


def clip(s, n):
    s = (s or "").strip()
    if len(s) <= n:
        return s
    cut = s[:n]
    # prefer cutting at a separator
    for sep in (" · ", ", ", " / ", " ", "·", ","):
        i = cut.rfind(sep)
        if i >= n * 0.6:
            cut = cut[:i]
            break
    return cut.rstrip(" ,·/(") + "…"


_SENT_END = re.compile(r"(?<=[.!?。])\s+|(?<=다\.)|(?<=요\.)")


def short_overview(s, max_sentences=2, max_chars=170):
    s = clean_text(s)
    if not s:
        return ""
    # split on sentence ends (Korean overviews mostly end in "다.")
    parts, buf = [], ""
    for ch_i, ch in enumerate(s):
        buf += ch
        if ch in ".!?。" and (ch_i + 1 == len(s) or s[ch_i + 1] == " "):
            # avoid splitting decimals / abbreviations like "1.5km" (next char is not space then)
            parts.append(buf.strip())
            buf = ""
            if len(parts) >= max_sentences:
                break
    if buf.strip() and len(parts) < max_sentences:
        parts.append(buf.strip())
    out = " ".join(p for p in parts if p)
    if len(out) > max_chars:
        # keep first sentence only if two are too long, then clip
        first = parts[0] if parts else out
        out = first if len(first) <= max_chars else clip(first, max_chars)
    return out


def short_menu(s, max_items=4, max_chars=42):
    s = clean_text(s)
    if not s:
        return ""
    items = [x.strip(" -*·") for x in re.split(r"[,/·\n]|\s{2,}|、", s)]
    items = [x for x in items if x]
    seen, out = set(), []
    for x in items:
        x = re.sub(r"\s*\(?\d[\d,]*\s*원\)?", "", x).strip()  # drop prices
        if x and x not in seen:
            seen.add(x)
            out.append(x)
        if len(out) >= max_items:
            break
    return clip("·".join(out), max_chars)


def short_line(s, max_chars=70):
    s = clean_text(s, keep_newlines=True)
    if not s:
        return ""
    lines = [re.sub(r"^[\s\-*※•ㆍ·]+", "", x).strip() for x in s.split("\n")]
    s = " / ".join(x for x in lines if x)
    s = re.sub(r"\s*-\s+(?=\S)", " / ", s)   # "21:00 - 마지막 주문", "23:00- 일요일"
    s = re.sub(r"(\s*/\s*){2,}", " / ", s)
    return clip(s, max_chars)


_PHONE = re.compile(r"(?:0\d{1,3}|1\d{3})-\d{3,4}(?:-\d{4})?")


def short_tel(s):
    s = clean_text(s)
    if not s:
        return ""
    nums = []
    for m in _PHONE.findall(s):
        if m not in nums:
            nums.append(m)
    return " · ".join(nums[:2]) if nums else short_line(s, 30)


def norm_title(s):
    s = clean_text(s)
    s = re.sub(r"\([^)]*\)|\[[^\]]*\]", "", s)
    return re.sub(r"[\s·,.\-]", "", s)


# ----------------------------------------------------------------------------- API client

class KeyMissing(RuntimeError):
    pass


def load_service_key(env=None):
    """Service key ("Decoding" form). Order: $DATA_GO_KR_KEY (CI secret) → file at $TOURAPI_KEY_FILE →
    ~/.config/passlog/data_go_kr_key. Never printed or persisted. Raises KeyMissing."""
    env = os.environ if env is None else env
    k = (env.get("DATA_GO_KR_KEY") or "").strip()
    if k:
        return k
    path = Path(env.get("TOURAPI_KEY_FILE") or DEFAULT_KEY_FILE).expanduser()
    if not path.is_file():
        raise KeyMissing("service key missing — set DATA_GO_KR_KEY, or TOURAPI_KEY_FILE, or create "
                         "~/.config/passlog/data_go_kr_key (refusing to run; --offline rebuilds from cache)")
    k = path.read_text(encoding="utf-8").strip()
    if not k or "\n" in k:
        raise KeyMissing("service key file must contain exactly one non-empty line")
    return k


class TourAPI:
    def __init__(self, cache_dir, budget, spacing=0.3, offline=False, verbose=True, key=None):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.budget = budget
        self.spacing = spacing
        self.offline = offline
        self.verbose = verbose
        self.key = None
        self.fatal = None
        self.exhausted = False
        self.calls_this_run = 0
        self.cache_hits = 0
        self.missing = []      # (op, params) we wanted but could not fetch (budget/offline)
        self.failed = []       # (op, params, error)
        self._last = 0.0
        self.usage_path = self.cache_dir / "usage.json"
        try:
            self.usage = json.loads(self.usage_path.read_text())
        except Exception:
            self.usage = {}
        self.images = None
        if not offline:
            self.key = key or load_service_key()   # never printed, never persisted

    def scrub(self, s):
        s = str(s)
        if self.key:
            for form in {self.key, urllib.parse.quote(self.key, safe=""), urllib.parse.quote_plus(self.key)}:
                s = s.replace(form, "***")
        s = re.sub(r"(?i)serviceKey=[^&\s]+", "serviceKey=***", s)
        return s

    # -- usage accounting (KST day)
    @staticmethod
    def today():
        return now_kst().strftime("%Y-%m-%d")

    def used_today(self):
        return int(self.usage.get(self.today(), 0))

    def remaining(self):
        return max(0, self.budget - self.used_today())

    def _count(self):
        d = self.today()
        self.usage[d] = int(self.usage.get(d, 0)) + 1
        self.calls_this_run += 1
        tmp = self.usage_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.usage, indent=1, sort_keys=True))
        tmp.replace(self.usage_path)

    # -- cache
    @staticmethod
    def _cache_name(op, params):
        canon = json.dumps({"op": op, "params": {k: str(v) for k, v in sorted(params.items())}},
                           ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(canon.encode("utf-8")).hexdigest()

    def cache_path(self, op, params):
        return self.cache_dir / op / (self._cache_name(op, params) + ".json.gz")

    def cached(self, op, params):
        p = self.cache_path(op, params)
        legacy = p.with_name(p.name[:-3])  # early runs wrote plain .json
        try:
            if p.is_file():
                with gzip.open(p, "rt", encoding="utf-8") as f:
                    return json.load(f)["body"]
            if legacy.is_file():
                return json.loads(legacy.read_text(encoding="utf-8"))["body"]
        except Exception:
            return None
        return None

    # -- request
    def get(self, op, params):
        params = {k: str(v) for k, v in params.items()}
        body = self.cached(op, params)
        if body is not None:
            self.cache_hits += 1
            return body
        if self.offline or self.fatal:
            self.missing.append((op, params))
            return None
        last_err = None
        for attempt in range(3):
            if self.remaining() <= 0:
                self.exhausted = True
                self.missing.append((op, params))
                return None
            wait = self.spacing - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            q = dict(params)
            q.update({"MobileOS": "IOS", "MobileApp": "PASSLOG", "_type": "json", "serviceKey": self.key})
            url = BASE + op + "?" + urllib.parse.urlencode(q)
            self._count()
            self._last = time.time()
            code = None
            try:
                req = urllib.request.Request(url, headers={"Accept": "application/json",
                                                           "User-Agent": "PASSLOG-dev-snapshot/1.0"})
                with urllib.request.urlopen(req, timeout=60) as r:
                    raw = r.read()
                body, err, code = self._parse(raw)
            except urllib.error.HTTPError as e:
                body, err = None, "HTTP %s" % e.code
            except Exception as e:  # network errors — scrub, never echo the URL
                body, err = None, type(e).__name__ + ": " + self.scrub(e)[:160]
            del url, q
            if body is not None:
                p = self.cache_path(op, params)
                p.parent.mkdir(parents=True, exist_ok=True)
                rec = {"op": op, "params": params, "fetchedAt": now_kst().isoformat(timespec="seconds"),
                       "body": body}
                tmp = p.with_name(p.name + ".tmp")
                with gzip.open(tmp, "wt", encoding="utf-8") as f:
                    json.dump(rec, f, ensure_ascii=False)
                tmp.replace(p)
                return body
            last_err = self.scrub(err)
            if code in FATAL_CODES:
                self.fatal = "resultCode %s (%s) — stopping all calls" % (code, last_err)
                print("  !! " + self.fatal, file=sys.stderr)
                self.missing.append((op, params))
                return None
            if self.verbose:
                print("  .. retry %d %s: %s" % (attempt + 1, op, last_err), file=sys.stderr)
            time.sleep((1, 3, 6)[attempt])
        self.failed.append((op, params, last_err))
        return None

    @staticmethod
    def _parse(raw):
        text = raw.decode("utf-8", "replace")
        try:
            j = json.loads(text)
        except ValueError:
            m = (re.search(r"<returnReasonCode>\s*(\d+)\s*</returnReasonCode>", text)
                 or re.search(r"<resultCode>\s*(\d+)\s*</resultCode>", text))
            msg = (re.search(r"<returnAuthMsg>([^<]*)</returnAuthMsg>", text)
                   or re.search(r"<resultMsg>([^<]*)</resultMsg>", text)
                   or re.search(r"<errMsg>([^<]*)</errMsg>", text))
            code = m.group(1) if m else None
            return None, "XML error %s %s" % (code, msg.group(1).strip() if msg else text[:80]), code
        hdr = (j.get("response") or {}).get("header") or j
        code = str(hdr.get("resultCode", ""))
        if code in OK_CODES and "response" in j:
            return j, None, code
        return None, "resultCode %s %s" % (code, hdr.get("resultMsg", "")), code


def items_of(body):
    if not body:
        return []
    b = (body.get("response") or {}).get("body") or {}
    it = b.get("items")
    if not it or not isinstance(it, dict):
        return []
    x = it.get("item") or []
    return [x] if isinstance(x, dict) else list(x)


def total_of(body):
    try:
        return int(((body.get("response") or {}).get("body") or {}).get("totalCount") or 0)
    except Exception:
        return 0


# ----------------------------------------------------------------------------- destinations

# Restaurant radius by setting (km): metro 3, city 5, rural 8. Attractions get a wider circle.
METRO_IDS = {"busan", "daegu", "ulsan", "daejeon", "gwangju"}
CITY_IDS = {"jeonju", "gangneung", "gyeongju", "yeosu", "mokpo", "pohang", "gunsan", "chuncheon",
            "suncheon", "andong", "jinhae", "cheongju", "wonju", "jinju", "gimhae", "cheonan-asan",
            "iksan", "chungju", "sokcho-yangyang", "tongyeong-geoje"}


# Spread-out destinations get several search centres ("hubs"); distanceKm is then measured to the
# nearest hub and items carry "hub". Everything else uses the catalog lat/lon as the only centre.
HUBS = {
    "busan": [("해운대", 35.1587, 129.1604), ("광안리", 35.1532, 129.1187), ("서면", 35.1577, 129.0592),
              ("남포·자갈치", 35.0966, 129.0306)],
    "ulsan": [("태화강", 35.5487, 129.2985), ("대왕암·일산", 35.4920, 129.4410)],
    "jeju-east": [("성산", 33.4610, 126.9330), ("세화·월정", 33.5400, 126.8300), ("함덕", 33.5430, 126.6690)],
    "tongyeong-geoje": [("통영", 34.8544, 128.4331), ("거제 구조라", 34.8070, 128.6870)],
    "gongju-buyeo": [("공주", 36.4465, 127.1190), ("부여", 36.2800, 126.9110)],
    "hadong-gurye": [("화개", 35.1880, 127.6240), ("하동읍", 35.0670, 127.7510), ("구례", 35.2020, 127.4630)],
    "jeongseon-yeongwol": [("정선", 37.3806, 128.6608), ("영월", 37.1837, 128.4617)],
    "donghae-samcheok": [("묵호", 37.5480, 129.1160), ("삼척", 37.4450, 129.1650)],
    "sokcho-yangyang": [("속초", 38.2070, 128.5918), ("양양 낙산", 38.1240, 128.6280)],
    "taean": [("태안읍", 36.7456, 126.2979), ("안면도", 36.4970, 126.3360)],
    "boryeong": [("보령", 36.3333, 126.6128), ("대천해수욕장", 36.3050, 126.5150)],
    "namhae": [("남해읍", 34.8376, 127.8924), ("독일마을", 34.8000, 128.0400)],
    "buan": [("부안", 35.7000, 126.6000), ("변산·격포", 35.6230, 126.4700)],
}


def radius_for(d):
    if d["id"] in METRO_IDS or "city" in (d.get("themes") or []):
        return 3, 5, "metro"
    if d["id"] in CITY_IDS:
        return 5, 8, "city"
    return 8, 12, "rural"


def load_destinations(extras_arg=None, use_extras=True, catalog=CATALOG):
    cat = json.loads(Path(catalog).read_text(encoding="utf-8"))["destinations"]
    base = [d for d in cat if d.get("countryCode") == "KR"]
    ids = {d["id"] for d in base}
    extras, extras_src = [], None
    if use_extras:
        cands = [Path(extras_arg)] if extras_arg else EXTRA_CANDIDATES
        for p in cands:
            if p.is_file():
                try:
                    j = json.loads(p.read_text(encoding="utf-8"))
                except Exception as e:
                    print("WARN: cannot read %s: %s" % (p, e), file=sys.stderr)
                    continue
                arr = j.get("destinations", j.get("entries", [])) if isinstance(j, dict) else j
                for d in arr:
                    if isinstance(d, dict) and d.get("id") and d.get("lat") is not None and d["id"] not in ids:
                        if d.get("countryCode", "KR") != "KR":
                            continue
                        extras.append(d)
                        ids.add(d["id"])
                extras_src = p
                break
    out = []
    for d in base:
        out.append(dict(d, _tier="base"))
    for d in extras:
        out.append(dict(d, _tier="extra"))
    for d in out:
        d["_rr"], d["_ar"], d["_kind"] = radius_for(d)
        d["_centers"] = HUBS.get(d["id"]) or [("", float(d["lat"]), float(d["lon"]))]
    return out, extras_src


def nearest_center(d, lat, lon):
    best = None
    for name, clat, clon in d["_centers"]:
        km = haversine_km(clat, clon, lat, lon)
        if best is None or km < best[0]:
            best = (km, name)
    return best


# ----------------------------------------------------------------------------- codes

def fetch_codes(api):
    body = api.get("lclsSystmCode2", {"lclsSystmListYn": "Y", "numOfRows": 3000, "pageNo": 1})
    if body is None:
        return None
    rows = items_of(body)
    names = {}
    for r in rows:
        for lv in ("1", "2", "3"):
            c, n = r.get("lclsSystm%sCd" % lv), r.get("lclsSystm%sNm" % lv)
            if c and n:
                names[str(c)] = str(n).strip()
    return names


def write_codes(names, path):
    rec = {
        "schemaVersion": SCHEMA_VERSION,
        "source": SOURCE + " lclsSystmCode2 (신 분류체계)",
        "generatedAt": now_kst().isoformat(timespec="seconds"),
        "note": "lclsSystm1/2/3 코드 → 이름. 음식점(FD…) 코드는 cuisineLabels 로 앱 표시용 라벨에 매핑.",
        "lclsSystm": dict(sorted(names.items())),
        "cuisineLabels": {c: cuisine_base(c, names) for c in sorted(names) if c.startswith("FD") and len(c) >= 6},
        "koreanSubLabels": [lab for lab, _ in KOREAN_SUB],
    }
    Path(path).write_text(json.dumps(rec, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


# Short Korean cuisine labels for the UI. The official 음식(FD) codes are coarse — 한식 only has
# FD010100 관광식당 / FD010200 모범음식점 — so 한식 gets a sub-label from the signature menu
# (firstmenu → title → treatmenu), e.g. 한식·국밥, 한식·해산물.
CUISINE_BASE = {
    "FD01": "한식",
    "FD020100": "중식", "FD020200": "일식", "FD020300": "양식", "FD020400": "세계음식",
    "FD020500": "퓨전", "FD02": "외국식",
    "FD030100": "베이커리", "FD030200": "피자·버거", "FD030300": "치킨", "FD030400": "분식",
    "FD030500": "푸드트럭", "FD030600": "간편식", "FD03": "간편식",
    "FD040100": "바·펍", "FD040200": "호프", "FD040300": "주점", "FD040400": "전통주점",
    "FD040500": "주점", "FD04": "주점",
    "FD050100": "카페·디저트", "FD050200": "전통찻집", "FD050300": "카페·디저트", "FD05": "카페·디저트",
}

# (sub-label, keywords) — checked in this order inside each text.
KOREAN_SUB = [
    ("해산물", ["횟집", "회센터", "수산", "뽈락", "볼락", "바지락", "밴댕이", "게국지", "대하", "붕장어", "갯장어",
             "물메기", "곰치", "도루묵", "양미리", "홍합", "소라", "삼치", "복불고기", "생선회", "모둠회", "활어", "물회", "회덮밥", "회정식", "회센터", "해물", "해산물", "조개",
             "전복", "대게", "홍게", "킹크랩", "꽃게", "게장", "갈치", "고등어", "장어", "꼼장어", "아귀", "아구",
             "복국", "복어", "복지리", "생선", "매운탕", "대구탕", "굴국밥", "굴밥", "멍게", "성게", "문어", "낙지",
             "쭈꾸미", "주꾸미", "오징어", "과메기", "꼬막", "백합", "해녀", "전어", "우럭", "광어", "방어", "참치",
             "새우", "해초", "보말", "고둥", "재첩", "도다리", "가자미", "황태", "코다리", "명태", "동태", "알탕",
             "짱뚱어", "홍어", "민어", "굴비", "멸치", "해신탕", "어죽"]),
    ("국밥", ["국밥", "해장국", "순대국", "순댓국", "수구레", "소머리", "선지", "올갱이", "다슬기", "뼈다귀",
            "콩나물국", "해장"]),
    ("탕", ["곰탕", "설렁탕", "갈비탕", "감자탕", "추어탕", "삼계탕", "육개장", "내장탕", "우족탕", "도가니탕",
           "꼬리곰탕"]),
    ("면", ["국수", "냉면", "막국수", "칼국수", "밀면", "쫄면", "메밀", "수제비"]),
    ("두부", ["순두부", "두부"]),
    ("비빔밥", ["비빔밥"]),
    ("쌈밥", ["쌈밥", "쌈정식"]),
    ("솥밥", ["솥밥", "돌솥밥", "영양밥", "보리밥", "한방"]),
    ("산채", ["곤드레", "산채", "나물밥", "약선", "사찰음식", "버섯"]),
    ("닭·오리", ["닭갈비", "닭백숙", "백숙", "닭볶음탕", "찜닭", "닭한마리", "오리", "닭구이", "닭요리", "통닭"]),
    ("고기", ["한우", "갈비", "불고기", "삼겹", "흑돼지", "돼지고기", "소고기", "숯불", "석쇠", "목살", "육회", "떡갈비",
            "막창", "곱창", "대창", "고기구이", "생고기", "돼지구이", "양념구이", "주물럭", "등심", "돈가스", "수육",
            "족발", "보쌈", "순대", "제육", "소갈비", "돼지갈비", "고기"]),
    ("찌개·찜", ["찌개", "전골", "청국장", "된장", "샤브", "부대", "김치찜", "묵은지"]),
    ("한정식", ["한정식", "정식", "반상", "백반", "상차림", "한상", "가정식", "떡갈비정식", "궁중"]),
    ("전·분식", ["파전", "빈대떡", "전병", "만두", "떡볶이", "김밥", "호떡"]),
]


def korean_sub(texts):
    """Sub-label from the first menu item that names a dish type. Korean compounds put the head
    noun last (육회비빔밥 → 비빔밥, 고기국수 → 국수), so the match ending furthest right wins,
    ties → the longer keyword (닭갈비 over 갈비)."""
    for t in texts:
        for seg in re.split(r"[·,/]", (t or "").replace(" ", "")):
            if not seg:
                continue
            best = None
            for lab, kws in KOREAN_SUB:
                for k in kws:
                    j = seg.rfind(k)
                    if j >= 0:
                        key = (j + len(k), len(k))
                        if best is None or key > best[0]:
                            best = (key, lab)
            if best:
                return best[1]
    return ""


def cuisine_base(code, names):
    code = str(code or "")
    if code in CUISINE_BASE:
        return CUISINE_BASE[code]
    if code[:4] in CUISINE_BASE:
        return CUISINE_BASE[code[:4]]
    return names.get(code, "") or names.get(code[:4], "")


def cuisine_label(code, names, texts=()):
    base = cuisine_base(code, names)
    if base == "한식" or (not base and texts):
        sub = korean_sub(texts)
        if sub:
            return "한식·" + sub
    return base


# ----------------------------------------------------------------------------- collectors
#
# NOTE (verified 2026-10-20): locationBasedList2 silently misses many flagship items — around
# 전주 경기전 it returned nothing within ~200 m (no 한옥마을/경기전/전동성당/한국집/베테랑칼국수),
# while areaBasedList2 for the same 시·군·구 lists them. So the location list is only used to find
# the destination's lDong codes; the item pool comes from areaBasedList2 per code (all content
# types in one paged call) and is filtered by distance locally.

KIND_LABEL = {"12": "관광지", "14": "문화시설", "28": "레포츠", "38": "쇼핑"}
CLOSED_RE = re.compile(r"폐업|폐점|휴업|영업종료|운영종료")
REGION_PREFIXES = ["서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "경기", "강원", "충북", "충남",
                   "전북", "전남", "경북", "경남", "제주", "충청", "전라", "경상", "특별자치도", "특별시", "광역시"]


def keyword_list(api, kw, ctype, rows=30):
    return api.get("searchKeyword2", {"keyword": kw, "contentTypeId": ctype, "arrange": "O",
                                      "numOfRows": rows, "pageNo": 1})


def area_all(api, regn, sign):
    """Every item (all content types) of one 시·군·구. 1 call normally, ≤ 4 for very large ones."""
    p = {"lDongRegnCd": regn, "arrange": "Q", "numOfRows": 1000, "pageNo": 1}
    if sign:
        p["lDongSignguCd"] = sign
    b = api.get("areaBasedList2", p)
    if b is None:
        return None
    rows, total = items_of(b), total_of(b)
    if total <= 1000:
        return rows
    if total <= 2000:
        b2 = api.get("areaBasedList2", dict(p, pageNo=2))
        return None if b2 is None else rows + items_of(b2)
    for ct in (39, 12, 14):  # huge (e.g. 제주시): typed lists of what we use
        bt = api.get("areaBasedList2", dict(p, contentTypeId=ct))
        if bt is None:
            return None
        rows += items_of(bt)
    return rows


def discover(api, d, state):
    """Nearby items (any type) → the destination's lDong codes → full area pools for those codes."""
    if "all" in state:
        return True

    def code(it):
        return (str(it.get("lDongRegnCd") or ""), str(it.get("lDongSignguCd") or ""))
    codes, fcodes, pool = [], [], {}
    for _, clat, clon in d["_centers"]:
        near = []
        for rad in (d["_rr"], 20):
            b = api.get("locationBasedList2", {"mapX": "%.6f" % clon, "mapY": "%.6f" % clat,
                                               "radius": int(rad * 1000), "arrange": "E",
                                               "numOfRows": 100, "pageNo": 1})
            if b is None:
                return False
            near = items_of(b)
            if near:
                break
        near = [it for it in near if code(it)[0]]
        near.sort(key=lambda it: float(it.get("dist") or 0))
        cnt = Counter(code(it) for it in near)
        mine = [code(near[0])] if near else []
        for c, n in cnt.most_common():
            if c not in mine and n >= max(5, 0.2 * len(near)):
                mine.append(c)
        for c in mine[:2]:
            if c not in codes:
                codes.append(c)
        for c, n in cnt.items():  # festivals: any 시·군·구 with a few items near a centre
            if (n >= 3 or c in mine) and c not in fcodes:
                fcodes.append(c)
        for it in near:
            pool[str(it.get("contentid"))] = it
    codes = codes[:5]
    for regn, sign in codes:
        rows = area_all(api, regn, sign)
        if rows is None:
            return False
        for it in rows:
            pool.setdefault(str(it.get("contentid")), it)
    state["codes"] = codes
    state["fcodes"] = fcodes
    state["all"] = list(pool.values())
    return True


def base_item(d, it):
    try:
        lat, lon = float(it.get("mapy")), float(it.get("mapx"))
    except (TypeError, ValueError):
        return None
    title = re.sub(r"\s*\[[^\]]*\]\s*", " ", clean_text(it.get("title"))).strip()
    if not title or not lat or not lon:
        return None
    lic = (it.get("cpyrhtDivCd") or "").strip()
    km, hub = nearest_center(d, lat, lon)
    return {
        "contentId": str(it.get("contentid")),
        "title": title,
        "addr": clean_text(" ".join(x for x in (it.get("addr1"), it.get("addr2")) if x)),
        "lat": round(lat, 6), "lon": round(lon, 6),
        "image": https(it.get("firstimage")),
        "thumb": https(it.get("firstimage2")),
        "imageLicense": "Type1" if lic == "Type1" else "Type3",
        "distanceKm": round(km, 2),
        "hub": hub,
        "modifiedtime": str(it.get("modifiedtime") or ""),
        "_ct": str(it.get("contenttypeid") or ""),
        "_lcls": str(it.get("lclsSystm3") or it.get("lclsSystm2") or ""),
        "_regn": str(it.get("lDongRegnCd") or ""),
        "_sign": str(it.get("lDongSignguCd") or ""),
        "_tel": clean_text(it.get("tel")),
    }


def is_cafe(x, names):
    return cuisine_base(x.get("_lcls"), names) in ("카페·디저트", "전통찻집", "베이커리")


def food_cores(d):
    out = []
    for f in d.get("foods") or []:
        f = re.sub(r"\s", "", f)
        core = re.sub(r"(구이|정식|요리|백반|한상|코스)$", "", f)
        for x in {f, core}:
            if len(x) >= 2:
                out.append(x)
    return out


def dedupe_titles(items):
    seen, out = set(), []
    for x in items:
        t = norm_title(x["title"])
        t = re.sub(r"(본점|직영점|\S+점)$", "", t) or t
        if t in seen:
            continue
        seen.add(t)
        out.append(x)
    return out


def collect_restaurants(api, d, names, state):
    if not discover(api, d, state):
        return None
    rr = d["_rr"]
    cands = [base_item(d, it) for it in state["all"] if str(it.get("contenttypeid")) == "39"]
    cands = [x for x in cands if x and x["image"] and not CLOSED_RE.search(x["title"])]
    used = rr
    within = [x for x in cands if x["distanceKm"] <= rr]
    if len(within) < 6:
        used = min(16, rr * 2)
        within = [x for x in cands if x["distanceKm"] <= used]
    if len(within) < 4:  # keyword fallback with the destination name / first alias
        kw = re.split(r"[·\s]", (d.get("aliases") or [d.get("nameKo", "")])[0] or d.get("nameKo", ""))[0]
        kb = keyword_list(api, kw, 39) if kw else None
        if kb is not None:
            have = {x["contentId"] for x in within}
            for it in items_of(kb):
                x = base_item(d, it)
                if x and x["image"] and x["distanceKm"] <= 20 and x["contentId"] not in have:
                    within.append(x)
            if within:
                used = max(used, max(x["distanceKm"] for x in within))
    cores = food_cores(d)

    def score(x):  # nearest first; local signature food in the name pulls it 1 km closer, cafés 0.5 km back
        sc = x["distanceKm"] - (1.0 if any(c in x["title"].replace(" ", "") for c in cores) else 0.0)
        return sc + (0.5 if is_cafe(x, names) else 0.0)
    within.sort(key=score)
    within = dedupe_titles(within)
    if len(d["_centers"]) > 1:  # several hubs: take turns so every hub gets restaurants
        queues = [[x for x in within if x["hub"] == name] for name, _, _ in d["_centers"]]
        within = []
        while any(queues):
            for q in queues:
                if q:
                    within.append(q.pop(0))
    state["pool"] = within
    chosen, cafes = [], 0
    for x in within:
        if api.images and not api.images.ok(x["image"]):
            continue
        if is_cafe(x, names):
            if cafes >= 2:
                continue
            cafes += 1
        chosen.append(x)
        if len(chosen) >= 10:
            break
    for x in chosen[:6]:  # detailIntro2: signature menu, hours, closed days, tel
        ib = api.get("detailIntro2", {"contentId": x["contentId"], "contentTypeId": 39})
        if ib is None:
            x["_pending"] = True
            continue
        intro = (items_of(ib) or [{}])[0]
        fm, tm = short_menu(intro.get("firstmenu"), 3, 30), short_menu(intro.get("treatmenu"), 4, 42)
        if fm:
            x["firstmenu"] = fm
        if tm and tm != fm:
            x["treatmenu"] = tm
        h = short_line(intro.get("opentimefood"), 70)
        if h:
            x["hours"] = h
        c = short_line(intro.get("restdatefood"), 40)
        if c:
            x["closed"] = c
        t = short_tel(intro.get("infocenterfood"))
        if t:
            x["tel"] = t
    for x in chosen[:4]:  # detailCommon2: overview (2 sentences)
        cb = api.get("detailCommon2", {"contentId": x["contentId"]})
        if cb is None:
            x["_pending"] = True
            continue
        com = (items_of(cb) or [{}])[0]
        ov = short_overview(com.get("overview"))
        if ov:
            x["overview"] = ov
        if not x.get("tel") and com.get("tel"):
            x["tel"] = short_tel(com.get("tel"))
    for x in chosen:
        if x.get("thumb") and api.images and not api.images.ok(x["thumb"]):
            x["thumb"] = ""   # broken small photo → UI falls back to image
        x["cuisine"] = cuisine_label(x.get("_lcls"), names,
                                     (x.get("firstmenu"), x["title"], x.get("treatmenu"))) or "음식점"
        if not x.get("tel") and x.get("_tel"):
            x["tel"] = short_tel(x["_tel"])
    return {"radiusKm": round(used, 1), "items": chosen}


def hl_text(d):
    """Catalog wording that names places: highlights first (their order = display priority)."""
    parts = list(d.get("highlights") or []) + [d.get("tagline", "")]
    parts += [h.get("text", "") for h in (d.get("seasonalHighlights") or [])]
    parts.append(d.get("hint", ""))
    return re.sub(r"[\s·,.\-]", "", " ".join(parts))


def alias_set(d):
    return {re.sub(r"\s", "", a) for a in (d.get("aliases") or []) if re.search(r"[가-힣]", a)}


def strip_prefixes(t, d):
    prefixes = list(REGION_PREFIXES) + list(alias_set(d)) + re.split(r"[·\s]", d.get("nameKo", ""))
    prefixes = sorted({re.sub(r"\s", "", p) for p in prefixes if p}, key=len, reverse=True)
    changed = True
    while changed:
        changed = False
        for p in prefixes:
            if t.startswith(p) and len(t) - len(p) >= 2:
                t = t[len(p):]
                changed = True
                break
    return t


HL_SUFFIXES = ("해수욕장", "해변", "국립공원", "관광지", "유원지", "공원", "마을", "전망대", "축제", "폭포", "동굴",
               "계곡", "시장", "거리", "대교", "오름", "산책로", "길")


def highlight_pos(title, d, text, aliases):
    """Position of the title inside the catalog highlights (smaller = earlier), or None."""
    t = norm_title(title)
    best = None
    for cand in {t, strip_prefixes(t, d)}:
        hits = []
        if len(cand) >= 3 and cand in text:
            hits.append(text.find(cand))
        if cand in aliases and len(cand) >= 2:
            hits.append(len(text))  # the alias place itself (e.g. 우도) — after highlight hits
        for suf in HL_SUFFIXES:
            stem = cand[:-len(suf)]
            if cand.endswith(suf) and len(stem) >= 2 and stem in text:
                hits.append(text.find(stem))
        for h in hits:
            if best is None or h < best:
                best = h
    return best


PLACE_SUFFIX = ("마을", "시장", "대교", "다리", "공원", "정원", "해수욕장", "해변", "비치", "바위", "폭포", "계곡",
                "사", "암", "성", "봉", "산", "섬", "호", "궁", "릉", "원", "탑", "대", "굴", "재", "항", "숲",
                "강", "전", "관", "당", "루", "정", "곶", "포", "길", "천", "숲")
GENERIC_TERMS = {"야시장", "전통시장", "해수욕장", "전망대", "케이블카", "출렁다리", "스카이워크", "둘레길",
                 "국립공원", "산책길", "자전거길", "캠핑장", "바닷가", "해안길", "드라이브", "벽화마을", "한옥마을",
                 "수목원", "휴양림", "해안도로", "오솔길", "꽃길", "골목길"}


def highlight_terms(d):
    """Proper place names in the catalog highlights, e.g. '불국사와 석굴암' → 불국사, 석굴암."""
    out = []
    for h in list(d.get("highlights") or []) + re.split(r"·", d.get("hint", "")):
        for tok in re.split(r"[\s·,/()]+", h):
            tok = re.sub(r"(과|와|및|의|에서|까지|부터)$", "", tok)
            if (len(tok) >= 3 and re.fullmatch(r"[가-힣0-9]+", tok) and tok.endswith(PLACE_SUFFIX)
                    and tok not in GENERIC_TERMS and tok not in out):
                out.append(tok)
    return out


def collect_attractions(api, d, names, state):
    if not discover(api, d, state):
        return None
    ar = d["_ar"]
    text, aliases = hl_text(d), alias_set(d)
    pool, seen = [], set()

    def consider(it, max_km):
        ct = str(it.get("contenttypeid"))
        if ct not in KIND_LABEL:
            return
        x = base_item(d, it)
        if not x or not x["image"] or x["contentId"] in seen or CLOSED_RE.search(x["title"]):
            return
        pos = highlight_pos(x["title"], d, text, aliases)
        if x["distanceKm"] > (max_km if pos is not None else ar):
            return
        if ct in ("28", "38") and pos is None:
            return  # 레포츠·쇼핑 only when the catalog itself names them (e.g. 남부시장)
        x["_hl"] = pos
        x["kind"] = KIND_LABEL[ct]
        seen.add(x["contentId"])
        pool.append(x)

    for it in state["all"]:
        consider(it, ar)
    # Catalog highlights outside the circle or the fetched 시·군·구 (불국사, 감천문화마을…): one
    # keyword search each (≤ 3 per destination), kept when ≤ 25 km from a centre.
    have = " ".join(norm_title(x["title"]) for x in pool if x["_hl"] is not None)
    for term in [t for t in highlight_terms(d) if t not in have][:3]:
        kb = api.get("searchKeyword2", {"keyword": term, "arrange": "O", "numOfRows": 20, "pageNo": 1})
        if kb is None:
            return None
        for it in items_of(kb):
            if term in norm_title(it.get("title")):
                consider(it, 25)

    def score(x):
        pen = 0.0
        if x["_lcls"].startswith("EX"):
            pen += 1.5   # 체험 업소(한복 대여·공방 등)는 뒤로
        if x["_ct"] == "14":
            pen += 0.5
        return (x["_hl"] is None, x["_hl"] or 0, x["distanceKm"] + pen)
    pool.sort(key=score)
    pool = dedupe_titles(pool)
    state["apool"] = pool
    per_hub = Counter()
    hub_cap = 6 if len(d["_centers"]) == 1 else math.ceil(6 / len(d["_centers"])) + 1
    chosen, culture, exp = [], 0, 0
    for x in pool:
        if api.images and not api.images.ok(x["image"]):
            continue
        hl = x["_hl"] is not None
        if not hl and per_hub[x["hub"]] >= hub_cap:
            continue
        if x["_ct"] == "14" and not hl:
            if culture >= 2:
                continue
            culture += 1
        if x["_lcls"].startswith("EX") and not hl:
            if exp >= 1:
                continue
            exp += 1
        chosen.append(x)
        per_hub[x["hub"]] += 1
        if len(chosen) >= 6:
            break
    if len(chosen) < 6:
        for x in pool:
            if x not in chosen and (not api.images or api.images.ok(x["image"])):
                chosen.append(x)
            if len(chosen) >= 6:
                break
    for x in chosen[:3]:
        cb = api.get("detailCommon2", {"contentId": x["contentId"]})
        if cb is None:
            x["_pending"] = True
            continue
        com = (items_of(cb) or [{}])[0]
        ov = short_overview(com.get("overview"))
        if ov:
            x["overview"] = ov
    for x in chosen:
        if x.get("thumb") and api.images and not api.images.ok(x["thumb"]):
            x["thumb"] = ""
        lab = names.get(x.get("_lcls", ""), "")
        if lab:
            x["category"] = lab
        if x.get("_tel"):
            x["tel"] = short_tel(x["_tel"])
        if x["_hl"] is not None:
            x["highlight"] = True
    radius = max([ar] + [x["distanceKm"] for x in chosen])
    return {"radiusKm": round(radius, 1), "items": chosen}


def fetch_festivals(api):
    """All festivals overlapping FEST_FROM..FEST_TO (paged, nationwide). Returns list or None."""
    out, page = [], 1
    while True:
        b = api.get("searchFestival2", {"eventStartDate": FEST_FROM, "eventEndDate": FEST_TO,
                                        "arrange": "A", "numOfRows": 1000, "pageNo": page})
        if b is None:
            return None
        rows = items_of(b)
        out += rows
        if len(out) >= total_of(b) or not rows or page >= 6:
            break
        page += 1
    return out


def ymd(s):
    s = str(s or "")
    return "%s-%s-%s" % (s[:4], s[4:6], s[6:8]) if len(s) == 8 and s.isdigit() else ""


def festivals_for(d, fests, codes):
    """Festivals in the destination's 시·군·구 (whole city for 특별·광역시), ≤ 30 km, overlapping the window."""
    codes = set(tuple(c) for c in codes)
    regns = {r for r, _ in codes}
    out = []
    for f in fests:
        try:
            lat, lon = float(f.get("mapy")), float(f.get("mapx"))
            dist = nearest_center(d, lat, lon)[0] if lat and lon else None
        except (TypeError, ValueError):
            lat = lon = dist = None
        r, s = str(f.get("lDongRegnCd") or ""), str(f.get("lDongSignguCd") or "")
        if r:
            ok = (r in regns) if r in METRO_REGN else ((r, s) in codes)
        else:
            ok = dist is not None and dist <= d["_ar"]
        if not ok or (dist is not None and dist > 30):
            continue
        st, en = str(f.get("eventstartdate") or ""), str(f.get("eventenddate") or "")
        if (en and en < FEST_FROM) or (st and st > FEST_TO):
            continue
        lic = (f.get("cpyrhtDivCd") or "").strip()
        item = {
            "contentId": str(f.get("contentid")),
            "title": clean_text(f.get("title")),
            "start": ymd(st), "end": ymd(en),
            "place": clean_text(" ".join(x for x in (f.get("addr1"), f.get("addr2")) if x)),
            "lat": round(lat, 6) if lat else None, "lon": round(lon, 6) if lon else None,
            "image": https(f.get("firstimage")), "thumb": https(f.get("firstimage2")),
            "imageLicense": "Type1" if lic == "Type1" else "Type3",
            "distanceKm": round(dist, 2) if dist is not None else None,
            "modifiedtime": str(f.get("modifiedtime") or ""),
        }
        tel = short_tel(f.get("tel"))
        if tel:
            item["tel"] = tel
        out.append(item)
    seen, uniq = set(), []
    for x in sorted(out, key=lambda x: (x["start"] or "9999", x["title"])):
        if x["contentId"] in seen or not x["title"]:
            continue
        seen.add(x["contentId"])
        uniq.append(x)
    return uniq[:8]


# ----------------------------------------------------------------------------- output

PRIVATE = ("_ct", "_lcls", "_regn", "_sign", "_tel", "_hl", "_pending")
FIELD_ORDER = ["contentId", "title", "cuisine", "kind", "category", "firstmenu", "treatmenu", "hours",
               "closed", "start", "end", "place", "tel", "addr", "lat", "lon", "image", "thumb",
               "imageLicense", "overview", "highlight", "distanceKm", "hub", "modifiedtime"]


def public(x):
    y = {k: v for k, v in x.items() if k not in PRIVATE and v not in (None, "")}
    return {k: y[k] for k in sorted(y, key=lambda k: FIELD_ORDER.index(k) if k in FIELD_ORDER else 99)}


def write_bundle(path, kind, per_dest, extra_meta=None):
    rec = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": kind,
        "source": SOURCE,
        "license": LICENSE,
        "attribution": ATTRIBUTION,
        "licenseRules": {"Type1": "공공누리 제1유형 — 출처 표시, 자르기·변형 가능",
                         "Type3": "공공누리 제3유형 — 출처 표시, 변경 금지(자르기·필터 금지, 전체를 비율 유지로 표시)"},
        "generatedAt": now_kst().isoformat(timespec="seconds"),
    }
    if extra_meta:
        rec.update(extra_meta)
    rec["destinations"] = {k: per_dest[k] for k in per_dest}
    txt = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
    # one destination per line keeps diffs readable while staying compact
    txt = txt.replace(',"destinations":{', ',\n"destinations":{\n', 1)
    txt = re.sub(r'\]\},"(?=[a-z0-9-]+":\{"(?:radiusKm|regions)")', ']},\n"', txt)
    path.write_text(txt + "\n", encoding="utf-8")
    return path.stat().st_size


# ----------------------------------------------------------------------------- validation

def validate(kind, per_dest, dests_by_id, problems):
    for did, rec in per_dest.items():
        d = dests_by_id.get(did)
        items = rec["items"]
        ids = [x["contentId"] for x in items]
        if len(ids) != len(set(ids)):
            problems.append("%s/%s: duplicate contentId" % (kind, did))
        lim = (rec.get("radiusKm") or 0) + 2
        for x in items:
            if not x.get("title"):
                problems.append("%s/%s/%s: missing title" % (kind, did, x.get("contentId")))
            for f in ("image", "thumb"):
                if x.get(f) and not x[f].startswith("https://"):
                    problems.append("%s/%s/%s: non-https %s" % (kind, did, x["contentId"], f))
            if kind != "festivals":
                if x.get("lat") is None or x.get("lon") is None:
                    problems.append("%s/%s/%s: no coordinates" % (kind, did, x["contentId"]))
                elif d is not None:
                    dist = nearest_center(d, x["lat"], x["lon"])[0]
                    if dist > lim:
                        problems.append("%s/%s/%s: %.1f km > %.1f km" % (kind, did, x["title"], dist, lim))
                if not x.get("image"):
                    problems.append("%s/%s/%s: no image" % (kind, did, x["contentId"]))
            if x.get("imageLicense") not in ("Type1", "Type3"):
                problems.append("%s/%s/%s: bad license" % (kind, did, x["contentId"]))


# ----------------------------------------------------------------------------- image check

class ImageChecker:
    """Ranged GET (first 1 KB) of tong.visitkorea.or.kr images — HEAD answers 405 there.
    Not an API call (no quota). Results cached in <cache>/image-check.json for 30 days."""

    def __init__(self, cache_dir, enabled=True, spacing=0.1):
        self.enabled = enabled
        self.spacing = spacing
        self.path = Path(cache_dir) / "image-check.json"
        try:
            self.db = json.loads(self.path.read_text())
        except Exception:
            self.db = {}
        self.checked = 0
        self.broken = []
        self._dirty = 0

    def ok(self, url):
        if not url or not self.enabled:
            return bool(url)
        rec = self.db.get(url)
        fresh = rec and (time.time() - rec.get("t", 0)) < 30 * 86400
        if fresh:
            good = rec["ok"]
        else:
            good = probe_image(url)[0]
            self.db[url] = {"ok": good, "t": int(time.time())}
            self.checked += 1
            self._dirty += 1
            if self._dirty >= 50:
                self.save()
            time.sleep(self.spacing)
        if not good and url not in self.broken:
            self.broken.append(url)
        return good

    def save(self):
        if self._dirty:
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self.db))
            tmp.replace(self.path)
            self._dirty = 0


def probe_image(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "PASSLOG-dev-snapshot/1.0",
                                                   "Range": "bytes=0-1023"})
        with urllib.request.urlopen(req, timeout=15) as r:
            ctype = r.headers.get("Content-Type", "")
            head = r.read(1024)
            return (r.status in (200, 206) and ctype.startswith("image/") and len(head) > 0), ctype
    except Exception as e:
        return False, type(e).__name__ + " " + str(getattr(e, "code", ""))


def head_check(urls, n, log=print):
    """Spot-check n random URLs with fresh requests (ignores the cache)."""
    sample = random.sample(urls, min(n, len(urls)))
    ok = 0
    for u in sample:
        good, ctype = probe_image(u)
        ok += good
        log("   GET %s %s %s" % ("ok " if good else "BAD", ctype, u))
        time.sleep(0.2)
    return ok, len(sample)


# ----------------------------------------------------------------------------- run (importable)

def estimate_calls(api, dests, states):
    """Calls still needed: the requests this run wanted but could not make, plus ≈16 per destination whose
    area pool is not fetched yet (area list ~1.5 + restaurant details 10 + attraction keyword/overview ~4.5)."""
    wanted = {api._cache_name(op, prm) for op, prm in api.missing}
    undiscovered = [d for d in dests if "all" not in states[d["id"]]]
    return len(wanted) + 16 * len(undiscovered)


def run_snapshot(budget=850, ids=None, out_dir=DATA, cache_dir=DEFAULT_CACHE, catalog=CATALOG,
                 extras=None, use_extras=True, offline=False, spacing=0.3, write=True,
                 check_images=8, verify_images=True, key=None, log=print):
    """Fetch (within the day budget) → build → validate → write. Returns a summary dict.

    budget      max API calls for the current KST day, all runs combined (usage.json in cache_dir)
    ids         optional iterable of destination ids to limit the run
    out_dir     where kr-restaurants/attractions/festivals.json + kr-tour-codes.json are written
    cache_dir   raw-response cache (gzip JSON, no service key inside) + usage.json + image-check.json
    catalog     destinations.json path; extras = kr-expand entries JSON (None → auto-detect)
    key         service key string (None → $DATA_GO_KR_KEY, $TOURAPI_KEY_FILE, ~/.config/passlog/data_go_kr_key)
    Raises KeyMissing when a key is needed (not offline) and none is available.
    """
    out_dir = Path(out_dir)
    codes_path = out_dir / "kr-tour-codes.json"
    api = TourAPI(cache_dir, budget, spacing, offline=offline, key=key)
    api.images = ImageChecker(cache_dir, enabled=verify_images)
    dests, extras_src = load_destinations(extras, use_extras=use_extras, catalog=catalog)
    if ids:
        want = set(ids)
        dests = [d for d in dests if d["id"] in want]
    by_id = {d["id"]: d for d in dests}
    base = [d for d in dests if d["_tier"] == "base"]
    extra = [d for d in dests if d["_tier"] == "extra"]
    log("destinations: %d existing + %d kr-expand (%s)" % (len(base), len(extra), extras_src or "none"))
    log("budget %d · used today before run %d · cache %s" % (budget, api.used_today(), api.cache_dir))

    # 0. code table (also kept in kr-tour-codes.json as an offline fallback)
    names = fetch_codes(api)
    if names is None and codes_path.is_file():
        names = json.loads(codes_path.read_text(encoding="utf-8")).get("lclsSystm", {})
    names = names or {}
    if names and write:
        write_codes(names, codes_path)

    rest, attr, fest, states = {}, {}, {}, {d["id"]: {} for d in dests}
    pending = {"restaurants": [], "attractions": [], "festivals": []}

    def run_phase(label, group, fn, store):
        for d in group:
            before = api.calls_this_run
            res = fn(api, d, names, states[d["id"]])
            if res is None or any(x.get("_pending") for x in res["items"]):
                pending[label].append(d["id"])
            if res is not None:
                store[d["id"]] = res
            spent = api.calls_this_run - before
            if spent:
                log("  %-12s %-20s %2d items  (+%d calls, used %d/%d)" % (
                    label, d["id"], len(res["items"]) if res else 0, spent, api.used_today(), budget))

    # Priority: existing restaurants → existing attractions → festivals → kr-expand restaurants → attractions
    run_phase("restaurants", base, collect_restaurants, rest)
    run_phase("attractions", base, collect_attractions, attr)
    fests = fetch_festivals(api)
    run_phase("restaurants", extra, collect_restaurants, rest)
    run_phase("attractions", extra, collect_attractions, attr)

    if fests is None:
        pending["festivals"] = [d["id"] for d in dests]
    else:
        for d in dests:
            codes = states[d["id"]].get("fcodes")
            if not codes:
                pending["festivals"].append(d["id"])
                continue
            items = festivals_for(d, fests, codes)
            for x in items:  # festivals keep their entry even without a working photo
                if x.get("image") and not api.images.ok(x["image"]):
                    x.pop("image", None)
                if x.get("thumb") and not api.images.ok(x["thumb"]):
                    x.pop("thumb", None)
            fest[d["id"]] = {"regions": sorted("%s-%s" % c if c[1] else c[0] for c in codes), "items": items}
    api.images.save()

    order = [d["id"] for d in dests]

    def head(k, block):
        cs = by_id[k]["_centers"]
        if len(cs) > 1:
            block["centers"] = [{"name": n, "lat": la, "lon": lo} for n, la, lo in cs]
        return block
    out_r = {k: head(k, {"radiusKm": rest[k]["radiusKm"], "items": [public(x) for x in rest[k]["items"]]})
             for k in order if k in rest}
    out_a = {k: head(k, {"radiusKm": attr[k]["radiusKm"], "items": [public(x) for x in attr[k]["items"]]})
             for k in order if k in attr}
    out_f = {k: head(k, {"regions": fest[k]["regions"], "items": [public(x) for x in fest[k]["items"]]})
             for k in order if k in fest}

    problems = []
    validate("restaurants", out_r, by_id, problems)
    validate("attractions", out_a, by_id, problems)
    validate("festivals", out_f, by_id, problems)

    provisional = sorted(d["id"] for d in extra)
    sizes = {}
    if write:
        out_dir.mkdir(parents=True, exist_ok=True)
        pm = {"provisionalIds": provisional} if provisional else {}
        fm = dict({"festivalWindow": [ymd(FEST_FROM), ymd(FEST_TO)]}, **pm)
        sizes["kr-restaurants.json"] = write_bundle(out_dir / "kr-restaurants.json", "restaurants", out_r, pm)
        sizes["kr-attractions.json"] = write_bundle(out_dir / "kr-attractions.json", "attractions", out_a, pm)
        sizes["kr-festivals.json"] = write_bundle(out_dir / "kr-festivals.json", "festivals", out_f, fm)

    # ---- summary
    coverage = {}
    log("\n%-20s %5s %5s %5s %6s %6s  %s" % ("destination", "rest", "attr", "fest", "menu%", "T1%", "notes"))
    for k in order:
        r_items = out_r.get(k, {}).get("items", [])
        a_items = out_a.get(k, {}).get("items", [])
        f_items = out_f.get(k, {}).get("items", [])
        top6 = r_items[:6]
        menu = sum(1 for x in top6 if x.get("firstmenu") or x.get("treatmenu"))
        allimg = r_items + a_items + f_items
        t1 = sum(1 for x in allimg if x.get("imageLicense") == "Type1")
        notes = [n for n, cond in (("provisional", k in provisional),
                                   ("pending", k in pending["restaurants"] or k in pending["attractions"])) if cond]
        coverage[k] = {"restaurants": len(r_items) if k in out_r else None,
                       "attractions": len(a_items) if k in out_a else None,
                       "festivals": len(f_items) if k in out_f else None,
                       "menuPct": round(100 * menu / len(top6)) if top6 else None,
                       "type1Pct": round(100 * t1 / len(allimg)) if allimg else None, "notes": notes}
        log("%-20s %5s %5s %5s %5s%% %5s%%  %s" % (
            k, len(r_items) if k in out_r else "-", len(a_items) if k in out_a else "-",
            len(f_items) if k in out_f else "-", coverage[k]["menuPct"] if top6 else "-",
            coverage[k]["type1Pct"] if allimg else "-", ",".join(notes)))
    all_items = [x for o in (out_r, out_a, out_f) for v in o.values() for x in v["items"]]
    if all_items:
        t1 = sum(1 for x in all_items if x.get("imageLicense") == "Type1")
        log("\nimages: %d items · Type1 %d%% · Type3 %d%% · checked %d new URLs, %d broken skipped/removed" % (
            len(all_items), round(100 * t1 / len(all_items)), 100 - round(100 * t1 / len(all_items)),
            api.images.checked, len(api.images.broken)))
    if sizes:
        log("files: " + ", ".join("%s %.0f KB" % (k, v / 1024) for k, v in sizes.items())
            + " (total %.0f KB)" % (sum(sizes.values()) / 1024))
    if problems:
        log("\nVALIDATION: %d problem(s)" % len(problems))
        for p in problems[:40]:
            log("  - " + p)
    else:
        log("\nVALIDATION: ok (titles, coordinates within radius+2 km, https images, unique contentIds)")
    spot = None
    if check_images and all_items:
        urls = [x[f] for x in all_items for f in ("image", "thumb") if x.get(f)]
        spot = head_check(urls, check_images, log=log)
        log("image spot check: %d/%d ok" % spot)

    log("\ncalls this run: %d (cache hits %d) · used today %d/%d" % (
        api.calls_this_run, api.cache_hits, api.used_today(), budget))
    if api.fatal:
        log("STOPPED: " + api.fatal)
    if api.failed:
        log("failed after retries: %d (re-run to retry)" % len(api.failed))
    left = {k: v for k, v in pending.items() if v}
    est = estimate_calls(api, dests, states)
    if left:
        log("LEFT FOR NEXT RUN (≈%d calls):" % est)
        for k, v in left.items():
            log("  %s (%d): %s" % (k, len(v), " ".join(v)))
    else:
        log("nothing left — all destinations complete.")
    return {
        "callsThisRun": api.calls_this_run, "cacheHits": api.cache_hits,
        "usedToday": api.used_today(), "budget": budget, "fatal": api.fatal,
        "failed": len(api.failed), "coverage": coverage, "pending": pending,
        "estimatedCallsLeft": est if left else 0, "files": {k: str(out_dir / k) for k in sizes},
        "sizes": sizes, "problems": problems, "imageSpotCheck": spot,
        "brokenImages": list(api.images.broken), "provisionalIds": provisional,
    }


# ----------------------------------------------------------------------------- CLI

def cmd_raw(args):
    api = TourAPI(args.cache_dir, args.budget, args.spacing)
    params = dict(kv.split("=", 1) for kv in args.params)
    body = api.get(args.op, params)
    print(json.dumps(body, ensure_ascii=False, indent=1)[:args.max_chars] if body else "(no body)")
    print("calls this run %d · used today %d/%d" % (api.calls_this_run, api.used_today(), api.budget),
          file=sys.stderr)
    return 0


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--budget", type=int, default=850, help="max API calls for today (KST), all runs combined")
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    ap.add_argument("--out-dir", default=str(DATA), help="where the kr-*.json files are written")
    ap.add_argument("--catalog", default=str(CATALOG), help="destinations.json")
    ap.add_argument("--spacing", type=float, default=0.3)
    ap.add_argument("--offline", action="store_true", help="never call the API; rebuild from cache")
    ap.add_argument("--only", default="", help="comma-separated destination ids")
    ap.add_argument("--extras", default="", help="kr-expand entries JSON (default: auto-detect)")
    ap.add_argument("--no-extras", action="store_true")
    ap.add_argument("--check-images", type=int, default=8, help="spot-check this many random image URLs (0 = skip)")
    ap.add_argument("--no-verify-images", action="store_true", help="do not check every chosen image URL")
    ap.add_argument("--no-write", action="store_true", help="collect/validate but do not write files")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("raw", help="one debug call (budgeted + cached)")
    r.add_argument("op")
    r.add_argument("params", nargs="*")
    r.add_argument("--max-chars", type=int, default=6000)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "raw":
            return cmd_raw(args)
        summary = run_snapshot(
            budget=args.budget, ids=[x for x in args.only.split(",") if x] or None,
            out_dir=args.out_dir, cache_dir=args.cache_dir, catalog=args.catalog,
            extras=args.extras or None, use_extras=not args.no_extras, offline=args.offline,
            spacing=args.spacing, write=not args.no_write, check_images=args.check_images,
            verify_images=not args.no_verify_images)
    except KeyMissing as e:
        print("ERROR: %s" % e, file=sys.stderr)
        return 2
    return 1 if summary["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())
