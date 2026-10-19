import datetime as dt
import json
import unittest

import _util
import packlib
from sources import apps, fx, net, urlwatch
from sources import holidays as hsrc

FX = (_util.FIXTURES / "frankfurter-v2.json").read_bytes()


# ---------------------------------------------------------------------------
# 환율
# ---------------------------------------------------------------------------
class FxTests(unittest.TestCase):
    def test_parse_frankfurter_cross_rates(self):
        r = fx.parse_frankfurter(FX)
        self.assertEqual(r["USD"], (1333.2, "2026-10-19"))
        self.assertAlmostEqual(r["JPY"][0], 1333.2 / 158.14, places=3)
        self.assertEqual(r["JPY"][1], "2026-10-19")        # 두 기준일 중 이른 쪽
        self.assertAlmostEqual(r["EUR"][0], 1550.23, places=1)
        self.assertNotIn("KRW", r)
        self.assertNotIn("BAD", r)                         # 0 은 버림
        self.assertNotIn("GBP", r)                         # base 가 USD 가 아닌 행은 버림
        self.assertEqual(r["VND"][0], fx.sig(1333.2 / 26300.0))

    def test_parse_requires_krw(self):
        with self.assertRaises(ValueError):
            fx.parse_frankfurter(json.dumps([{"date": "2026-10-19", "base": "USD", "quote": "JPY", "rate": 150}]).encode())

    def test_merge_prefers_koreaexim_and_marks_sources(self):
        doc = fx.merge(("2026-10-19", {"JPY": 8.5, "USD": 1340.0, "CNH": 190.0, "CNY": 190.0}), fx.parse_frankfurter(FX))
        self.assertEqual(doc["base"], "KRW")
        self.assertEqual(doc["date"], "2026-10-19")
        self.assertEqual(doc["rates"]["JPY"], 8.5)
        self.assertEqual(doc["sources"]["JPY"], "koreaexim")
        self.assertEqual(doc["sources"]["MNT"], "frankfurter")
        self.assertEqual(doc["dates"]["MNT"], "2026-10-19")
        self.assertEqual(list(doc["rates"]), sorted(doc["rates"]))

    def test_frankfurter_only_uses_usd_date(self):
        doc = fx.merge(None, fx.parse_frankfurter(FX))
        self.assertEqual(doc["date"], "2026-10-19")
        self.assertEqual(set(doc["sources"].values()), {"frankfurter"})

    def test_previous_comparison(self):
        doc = fx.merge(None, fx.parse_frankfurter(FX))
        prev = {"date": "2026-10-16", "rates": dict(doc["rates"]), "sources": {"MNT": "frankfurter"},
                "dates": {"MNT": "2026-10-16"}}
        prev["rates"]["MNT"] = doc["rates"]["MNT"] * 2       # 한 통화만 튐 → 그 통화만 되돌림
        warnings = fx.check_against_previous(doc, prev)
        self.assertEqual(len(warnings), 1)
        self.assertEqual(doc["rates"]["MNT"], prev["rates"]["MNT"])
        self.assertEqual(doc["dates"]["MNT"], "2026-10-16")
        many = {k: v * 3 for k, v in doc["rates"].items()}  # 전부 튐 → 거부
        with self.assertRaises(fx.RatesRejected):
            fx.check_against_previous(doc, {"date": "2026-10-16", "rates": many})
        # 오래된 이전 파일과는 비교하지 않음
        self.assertEqual(fx.check_against_previous(doc, {"date": "2026-01-01", "rates": many}), [])

    def test_build_with_fake_network_and_exim_failure(self):
        logs = []
        doc, info = fx.build(exim_key="K", today=dt.date(2026, 10, 20), previous=None,
                             getter=lambda url, **kw: (200, {}, FX),
                             exim_fetcher=lambda k, d: (_ for _ in ()).throw(RuntimeError("authkey=K 실패")),
                             log=logs.append)
        self.assertEqual(info["sources"], ["frankfurter"])
        self.assertTrue(any("koreaexim" in w for w in info["warnings"]))
        self.assertFalse(any("authkey=K" in w for w in info["warnings"]))
        self.assertIn("USD", doc["rates"])

    def test_build_fails_when_all_sources_fail(self):
        def bad(url, **kw):
            raise net.FetchError("timeout")
        with self.assertRaises(RuntimeError):
            fx.build(exim_key="", today=dt.date(2026, 10, 20), previous=None, getter=bad, log=lambda m: None)


# ---------------------------------------------------------------------------
# 공휴일
# ---------------------------------------------------------------------------
class FakeCal(dict):
    supported_languages = ("ko", "en_US")
    default_language = "ko"


class FakeHolidays:
    """python-holidays 대역 (country_holidays 만)"""

    def __init__(self, table):
        self.table = table

    def country_holidays(self, cc, years=None, language=None):
        if cc not in self.table:
            raise NotImplementedError(cc)
        cal = FakeCal()
        for d, name in self.table[cc].items():
            day = dt.date.fromisoformat(d)
            if years is None or day.year in years:
                cal[day] = name if language != "en_US" else "EN " + name
        return cal


class HolidaysTests(unittest.TestCase):
    def test_app_names(self):
        n = hsrc.app_name
        self.assertEqual(n("신정연휴"), ("새해 첫날", "fixed"))
        self.assertEqual(n("설날 전날"), ("설날 연휴", "lunar"))
        self.assertEqual(n("설날"), ("설날", "lunar"))
        self.assertEqual(n("추석 다음날"), ("추석 연휴", "lunar"))
        self.assertEqual(n("기독탄신일"), ("성탄절", "fixed"))
        self.assertEqual(n("근로자의 날"), ("노동절", "fixed"))
        self.assertEqual(n("대체 공휴일 (삼일절)"), ("대체공휴일(삼일절)", "substitute"))
        self.assertEqual(n("대체공휴일(추석)"), ("대체공휴일(추석)", "substitute"))
        self.assertEqual(n("임시 공휴일"), ("임시공휴일", "temporary"))
        self.assertEqual(n("전국동시지방선거"), ("전국동시지방선거", "election"))
        self.assertEqual(n("부처님오신날"), ("부처님오신날", "lunar"))

    def test_parse_kasi_json_and_xml(self):
        rows = hsrc.parse_kasi((_util.FIXTURES / "kasi-2026-10.json").read_bytes())
        self.assertEqual([r["date"] for r in rows], ["2026-10-05", "2026-10-12", "2026-10-09"])
        self.assertEqual(rows[1]["kind"], "temporary")
        rows = hsrc.parse_kasi((_util.FIXTURES / "kasi-2027-06.xml").read_bytes())
        self.assertEqual(rows[0], {"date": "2027-06-03", "name": "전국동시지방선거", "kind": "election", "isSubstitute": False})
        with self.assertRaises(RuntimeError):
            hsrc.parse_kasi(b'{"response":{"header":{"resultCode":"30","resultMsg":"SERVICE KEY IS NOT REGISTERED"}}}')

    def base(self):
        return {"version": 1, "updated": "2026-10", "years": [2026],
                "holidays": [{"date": "2026-10-05", "name": "추석", "kind": "lunar", "isSubstitute": False},
                             {"date": "2026-10-09", "name": "한글날", "kind": "fixed", "isSubstitute": False}],
                "schoolVacations": [{"name": "여름방학", "months": [7, 8]}]}

    def test_build_kr_without_sources_is_identity(self):
        base = self.base()
        doc, used, warnings = hsrc.build_kr(base, today=dt.date(2026, 10, 20), pkg=None, kasi_rows=None)
        self.assertEqual(doc, base)
        self.assertEqual(used, [])

    def test_build_kr_fills_missing_years_and_adds_temporary(self):
        pkg = FakeHolidays({"KR": {"2026-10-05": "추석", "2026-10-09": "한글날", "2026-12-25": "기독탄신일",
                                   "2027-01-01": "신정연휴", "2028-10-09": "한글날"}})
        kasi = hsrc.parse_kasi((_util.FIXTURES / "kasi-2026-10.json").read_bytes())
        doc, used, warnings = hsrc.build_kr(self.base(), today=dt.date(2026, 10, 20), pkg=pkg, kasi_rows=kasi)
        dates = {(h["date"], h["name"]) for h in doc["holidays"]}
        self.assertIn(("2027-01-01", "새해 첫날"), dates)                 # 큐레이션에 없는 해 → 계산값
        self.assertIn(("2028-10-09", "한글날"), dates)
        self.assertNotIn(("2026-12-25", "성탄절"), dates)                 # 큐레이션 해는 바꾸지 않음(경고만)
        self.assertIn(("2026-10-12", "임시공휴일"), dates)                # 특일: 임시공휴일 추가
        self.assertEqual(sum(1 for h in doc["holidays"] if h["date"] == "2026-10-05"), 1)  # 같은 날 중복 없음
        self.assertTrue(any("python-holidays 에만" in w for w in warnings))
        self.assertEqual(doc["years"], [2026, 2027, 2028])
        self.assertEqual(doc["schoolVacations"], self.base()["schoolVacations"])
        self.assertEqual(doc["holidays"], sorted(doc["holidays"], key=lambda h: (h["date"], h["name"])))

    def test_build_world(self):
        pkg = FakeHolidays({"JP": {"2026-11-03": "文化の日", "2027-01-01": "元日", "2028-01-01": "元日"}})
        data, skipped = hsrc.build_world(["JP", "KR", "XX"], today=dt.date(2026, 10, 20), pkg=pkg)
        self.assertEqual(data["years"], [2026, 2027])
        self.assertEqual(skipped, ["XX"])
        self.assertNotIn("KR", data["countries"])
        self.assertEqual(data["countries"]["JP"][0], {"date": "2026-11-03", "name": "文化の日", "nameEn": "EN 文化の日"})
        self.assertEqual(len(data["countries"]["JP"]), 2)

    def test_package_loader_never_returns_this_module(self):
        pkg = hsrc._holidays_pkg()
        if pkg is not None:
            self.assertTrue(hasattr(pkg, "country_holidays"))
            self.assertNotEqual(getattr(pkg, "__file__", ""), hsrc.__file__)


# ---------------------------------------------------------------------------
# 현지 앱
# ---------------------------------------------------------------------------
def fake_entries(data):
    for cc, apps_ in data["countries"].items():
        for a in apps_:
            yield f"countries.{cc}", cc, a


class AppsTests(unittest.TestCase):
    DATA = {"countries": {
        "JP": [{"id": "suica", "appStoreId": 1, "kr": True}, {"id": "vjw", "appStoreId": 0, "web": True}],
        "TH": [{"id": "grab", "appStoreId": 2, "storeCountry": "th"}, {"id": "gone", "appStoreId": 3}],
    }}

    def test_targets_skip_web_and_build_store_order(self):
        t = apps.app_targets(self.DATA, fake_entries)
        self.assertEqual(sorted(t), [1, 2, 3])
        self.assertEqual(t[2][:2], ["th", "us"])

    def test_check_and_status(self):
        calls = []

        def lookup(ids, store):
            calls.append((tuple(ids), store))
            if store == "kr":
                return [{"trackId": 1, "version": "2.0", "currentVersionReleaseDate": "2026-09-30T00:00:00Z"}]
            if store == "th":
                return [{"trackId": 2, "version": "5"}]
            return []

        prev = {"data": {"apps": {"3": {"available": False, "missingCount": 1, "missingSince": "2026-10-13"}}}}
        found, failures = apps.check(apps.app_targets(self.DATA, fake_entries), lookup, delay=0, sleep=lambda s: None)
        self.assertEqual(failures, 0)
        status = apps.build_status(apps.app_targets(self.DATA, fake_entries), found, prev, "2026-10-20")["apps"]
        self.assertEqual(status["1"], {"available": True, "store": "kr", "inKR": True, "missingCount": 0, "hidden": False,
                                       "version": "2.0", "releaseDate": "2026-09-30"})
        self.assertEqual(status["2"]["store"], "th")
        self.assertFalse(status["2"]["inKR"])
        self.assertEqual(status["3"], {"available": False, "missingCount": 2, "missingSince": "2026-10-13", "hidden": True})
        self.assertEqual(calls[0], ((1, 2, 3), "kr"))

    def test_mass_missing_is_rejected(self):
        with self.assertRaises(RuntimeError):
            apps.build(self.DATA, previous=None, today="2026-10-20", lookup=lambda ids, st: [],
                       entries=fake_entries, delay=0, sleep=lambda s: None, log=lambda m: None)

    def test_failed_batch_skips_week(self):
        def lookup(ids, store):
            return None if store == "kr" else [{"trackId": i} for i in ids]
        with self.assertRaises(RuntimeError):
            apps.build(self.DATA, previous=None, today="2026-10-20", lookup=lookup, entries=fake_entries,
                       delay=0, sleep=lambda s: None, log=lambda m: None)

    @unittest.skipUnless(packlib.vendor_script("local-apps-check.py")
                         and (packlib.default_curated_dir() / "local-apps.json").is_file(), "local-apps-check.py 없음")
    def test_wraps_real_checker_offline(self):
        mod = apps.load_checker()
        data = _util.read(packlib.default_curated_dir() / "local-apps.json")
        targets = apps.app_targets(data, mod.entries)
        self.assertGreater(len(targets), 100)


# ---------------------------------------------------------------------------
# URL 감시
# ---------------------------------------------------------------------------
class UrlWatchTests(unittest.TestCase):
    def test_collect_urls(self):
        doc = {"airlines": [{"code": "KE", "officialUrl": "https://a.example/bag",
                             "sources": [{"url": "https://a.example/bag"}, {"url": "http://b.example/x"}],
                             "note": "https://not-a-url-key.example"}]}
        self.assertEqual(urlwatch.collect_urls(doc), ["https://a.example/bag", "http://b.example/x"])

    def test_normalize_drops_noise(self):
        a = b"<html><script>var t=123456789;</script><p>Bag 23kg</p><span>2026-10-19 12:30</span></html>"
        b = b"<html><script>var t=987654321;</script><p>Bag  23kg</p><span>2026-10-20 08:00</span></html>"
        self.assertEqual(urlwatch.normalize_text(a), urlwatch.normalize_text(b))

    def test_state_transitions(self):
        pages = {"https://a/": b"<p>v1</p>", "https://b/": None}

        def getter(url, **kw):
            body = pages[url]
            if body is None:
                raise net.FetchError("HTTP 403")
            return 200, {}, body

        sets = {"airline-baggage": (["https://a/", "https://b/"], "s" * 64)}
        d1, new1 = urlwatch.check(sets, None, "2026-10-20", getter=getter, delay=0, sleep=lambda s: None)
        self.assertEqual(d1["urls"]["https://a/"]["state"], "ok")
        self.assertEqual(d1["urls"]["https://b/"]["state"], "unreachable")
        self.assertEqual(new1, [])
        pages["https://a/"] = b"<p>v2</p>"
        prev = {"data": d1}
        d2, new2 = urlwatch.check(sets, prev, "2026-10-21", getter=getter, delay=0, sleep=lambda s: None)
        self.assertEqual(d2["urls"]["https://a/"]["state"], "changed")
        self.assertEqual(new2, ["https://a/"])
        d3, new3 = urlwatch.check(sets, {"data": d2}, "2026-10-22", getter=getter, delay=0, sleep=lambda s: None)
        self.assertEqual(d3["urls"]["https://a/"]["state"], "changed")   # 사람이 고칠 때까지 유지
        self.assertEqual(d3["urls"]["https://a/"]["changedAt"], "2026-10-21")
        self.assertEqual(new3, [])
        sets2 = {"airline-baggage": (["https://a/", "https://b/"], "t" * 64)}  # 큐레이션 파일이 바뀜 → 해제
        d4, _ = urlwatch.check(sets2, {"data": d3}, "2026-10-23", getter=getter, delay=0, sleep=lambda s: None)
        self.assertEqual(d4["urls"]["https://a/"]["state"], "ok")
        prev = {"data": d4}
        for day in range(6):
            prev = {"data": urlwatch.check(sets2, prev, f"2026-10-{24 + day}", getter=getter, delay=0,
                                           sleep=lambda s: None)[0]}
        self.assertEqual(prev["data"]["urls"]["https://b/"]["state"], "manual")
        self.assertEqual(prev["data"]["urls"]["https://b/"]["unreachableSince"], "2026-10-20")


class _FakeResp:
    def __init__(self, body: bytes, length=None, status=200):
        self.status = status
        self.headers = {"Content-Length": str(length)} if length is not None else {}
        self._body = body

    def read(self, n=-1):
        return self._body if n is None or n < 0 else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class NetSizeCapTests(unittest.TestCase):
    def setUp(self):
        self.saved = net.urllib.request.urlopen

    def tearDown(self):
        net.urllib.request.urlopen = self.saved

    def serve(self, resp):
        calls = []

        def fake(req, timeout=None):
            calls.append(req)
            return resp
        net.urllib.request.urlopen = fake
        return calls

    def test_normal_body_is_returned(self):
        self.serve(_FakeResp(b'{"ok":1}', length=8))
        status, _h, body = net.get("https://example.invalid/x", retries=1)
        self.assertEqual((status, body), (200, b'{"ok":1}'))

    def test_declared_length_over_cap_is_refused_without_retry(self):
        calls = self.serve(_FakeResp(b"x" * 10, length=10_000))
        with self.assertRaises(net.FetchError) as cm:
            net.get("https://example.invalid/x", retries=3, max_bytes=1000)
        self.assertIn("too large", str(cm.exception))
        self.assertEqual(len(calls), 1)

    def test_body_over_cap_without_length_is_refused(self):
        self.serve(_FakeResp(b"x" * 1500))
        with self.assertRaises(net.FetchError):
            net.get("https://example.invalid/x", retries=1, max_bytes=1000)
        self.serve(_FakeResp(b"x" * 1000))
        self.assertEqual(len(net.get("https://example.invalid/x", retries=1, max_bytes=1000)[2]), 1000)


if __name__ == "__main__":
    unittest.main()
