import datetime as dt
import gzip
import json
import shutil
import tempfile
import types
import unittest
from pathlib import Path

import _util
import packlib
from sources import tourapi

NOW = dt.datetime(2026, 10, 20, 6, 50, tzinfo=packlib.KST)
KEY = "TEST-SERVICE-KEY-zz9"


def put_cache(cache: Path, op: str, params: dict, fetched: dt.datetime, body=None) -> Path:
    p = cache / op / (tourapi.cache_name(op, params) + ".json.gz")
    p.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(p, "wt", encoding="utf-8") as f:
        json.dump({"op": op, "params": {k: str(v) for k, v in params.items()}, "fetchedAt": packlib.iso(fetched),
                   "body": body or {}}, f)
    return p


def tour_file(kind, dests, generated="2026-10-20T06:51:00+09:00", **extra):
    doc = {"schemaVersion": 1, "kind": kind, "source": "한국관광공사 TourAPI 4.0 KorService2", "generatedAt": generated,
           "destinations": dests}
    doc.update(extra)
    return doc


def item(cid, **kw):
    x = {"contentId": cid, "title": f"곳{cid}", "lat": 35.0, "lon": 129.0, "image": "https://tong.visitkorea.or.kr/x.jpg",
         "imageLicense": "Type3"}
    x.update(kw)
    return x


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.cache = Path(tempfile.mkdtemp(prefix="tour-cache-"))

    def tearDown(self):
        shutil.rmtree(self.cache, ignore_errors=True)

    def test_expire_by_ttl(self):
        fresh = put_cache(self.cache, "searchFestival2", {"eventStartDate": "20261020"}, NOW - dt.timedelta(hours=2))
        old = put_cache(self.cache, "searchFestival2", {"eventStartDate": "20261019"}, NOW - dt.timedelta(hours=25))
        area_new = put_cache(self.cache, "areaBasedList2", {"lDongRegnCd": "26"}, NOW - dt.timedelta(days=3))
        area_old = put_cache(self.cache, "areaBasedList2", {"lDongRegnCd": "27"}, NOW - dt.timedelta(days=9))
        detail = put_cache(self.cache, "detailIntro2", {"contentId": "1"}, NOW - dt.timedelta(days=10))
        detail_old = put_cache(self.cache, "detailIntro2", {"contentId": "2"}, NOW - dt.timedelta(days=40))
        removed = tourapi.expire_cache(self.cache, NOW)
        self.assertTrue(fresh.exists())
        self.assertFalse(old.exists())
        self.assertTrue(area_new.exists())
        self.assertFalse(area_old.exists())
        self.assertTrue(detail.exists())
        self.assertFalse(detail_old.exists())
        self.assertEqual(removed, {"searchFestival2": 1, "areaBasedList2": 1, "detailIntro2": 1})
        (self.cache / "usage.json").write_text('{"2026-10-20": 5}')
        tourapi.expire_cache(self.cache, NOW)            # 캐시가 아닌 파일은 건드리지 않음
        self.assertTrue((self.cache / "usage.json").exists())

    def test_invalidate_changed_content_and_areas(self):
        d1 = put_cache(self.cache, "detailCommon2", {"contentId": "111"}, NOW)
        d2 = put_cache(self.cache, "detailCommon2", {"contentId": "222"}, NOW)
        a1 = put_cache(self.cache, "areaBasedList2", {"lDongRegnCd": "26", "lDongSignguCd": "350", "pageNo": 1}, NOW)
        a2 = put_cache(self.cache, "areaBasedList2", {"lDongRegnCd": "26", "lDongSignguCd": "110", "pageNo": 1}, NOW)
        a3 = put_cache(self.cache, "areaBasedList2", {"lDongRegnCd": "11", "pageNo": 1}, NOW)  # 광역 전체 목록
        removed = tourapi.invalidate(self.cache, {"111"}, {("26", "350"), ("11", "680")})
        self.assertFalse(d1.exists())
        self.assertTrue(d2.exists())
        self.assertFalse(a1.exists())
        self.assertTrue(a2.exists())
        self.assertFalse(a3.exists())
        self.assertEqual(removed, {"detailCommon2": 1, "areaBasedList2": 2})

    def test_budget_and_sync_days(self):
        (self.cache / "usage.json").write_text('{"2026-10-20": 450, "2026-10-19": 999}')
        self.assertEqual(tourapi.run_budget(self.cache, "2026-10-20"), 900)     # 하루 상한
        self.assertEqual(tourapi.run_budget(self.cache, "2026-10-21"), 600)     # 실행당 상한
        self.assertEqual(tourapi.sync_days(self.cache, NOW.date()), ([], False))
        tourapi.save_sync_state(self.cache, dt.date(2026, 10, 18))
        days, had = tourapi.sync_days(self.cache, NOW.date())
        self.assertTrue(had)
        self.assertEqual([d.isoformat() for d in days], ["2026-10-18", "2026-10-19", "2026-10-20"])
        tourapi.save_sync_state(self.cache, dt.date(2026, 10, 1))
        self.assertEqual(tourapi.sync_days(self.cache, NOW.date()), ([], True))  # 7일 넘게 비면 나이 만료만

    def test_festival_window(self):
        self.assertEqual(tourapi.festival_window(dt.date(2026, 10, 20)), ("20261020", "20271031"))
        self.assertEqual(tourapi.festival_window(dt.date(2026, 12, 5)), ("20261205", "20271231"))
        self.assertEqual(tourapi.festival_window(dt.date(2027, 2, 1)), ("20270201", "20280229"))


class SyncTests(unittest.TestCase):
    class FakeAPI:
        def __init__(self, total, per_page=1000):
            self.total, self.per_page, self.calls = total, per_page, 0

        def cache_path(self, op, params):
            return Path("/nonexistent-passlog-test") / op / "x.json.gz"

        def get(self, op, params):
            self.calls += 1
            page = int(params["pageNo"])
            start = (page - 1) * self.per_page
            n = max(0, min(self.per_page, self.total - start))
            rows = [{"contentid": str(start + i), "contenttypeid": "39", "lDongRegnCd": "26", "lDongSignguCd": "350"}
                    for i in range(n)]
            return {"response": {"body": {"items": {"item": rows} if rows else "", "totalCount": self.total}}}

    def test_complete_day_succeeds(self):
        api = self.FakeAPI(total=2500)
        ids, areas, ok = tourapi.sync_changes(api, [dt.date(2026, 10, 19)])
        self.assertTrue(ok)
        self.assertEqual(len(ids), 2500)
        self.assertEqual(api.calls, 3)

    def test_day_beyond_page_cap_is_not_reported_complete(self):
        # 하루치가 SYNC_MAX_PAGES 쪽을 넘으면 실패 → lastSync 를 옮기지 않아 다음 실행에서 다시 묻는다
        api = self.FakeAPI(total=(tourapi.SYNC_MAX_PAGES + 2) * 1000)
        ids, _areas, ok = tourapi.sync_changes(api, [dt.date(2026, 10, 19), dt.date(2026, 10, 20)])
        self.assertFalse(ok)
        self.assertEqual(api.calls, tourapi.SYNC_MAX_PAGES)
        self.assertEqual(len(ids), tourapi.SYNC_MAX_PAGES * 1000)


class MergeTests(unittest.TestCase):
    def test_merge_keeps_previous_for_pending_and_missing(self):
        prev = tour_file("restaurants", {"busan": {"items": [item("1")]}, "jeonju": {"items": [item("2")]},
                                         "gone": {"items": [item("9")]}}, provisionalIds=["gone"])
        new = tour_file("restaurants", {"busan": {"items": [item("1b")]}, "andong": {"items": [item("3")]}})
        out, notes = tourapi.merge("restaurants", new, prev, {"busan"}, {"busan", "jeonju", "andong"}, "2026-10-20")
        self.assertEqual(out["destinations"]["busan"], prev["destinations"]["busan"])   # 덜 모음 → 이전 블록
        self.assertEqual(out["destinations"]["jeonju"], prev["destinations"]["jeonju"])  # 이번에 없음 → 이전 블록
        self.assertEqual(out["destinations"]["andong"], new["destinations"]["andong"])
        self.assertNotIn("gone", out["destinations"])                                 # 카탈로그에 없음
        self.assertNotIn("provisionalIds", out)
        self.assertEqual(out["generatedAt"], new["generatedAt"])

    def test_merge_drops_ended_festivals(self):
        prev = tour_file("festivals", {"busan": {"regions": ["26"], "items": [item("1", start="2026-09-01", end="2026-10-19"),
                                                                              item("2", start="2026-10-01", end="2026-10-20")]}})
        new = tour_file("festivals", {})
        out, _ = tourapi.merge("festivals", new, prev, set(), {"busan"}, "2026-10-20")
        self.assertEqual([x["contentId"] for x in out["destinations"]["busan"]["items"]], ["2"])

    def test_merge_rejects_big_shrink(self):
        prev = tour_file("attractions", {f"d{i}": {"items": [item(f"{i}-{j}") for j in range(6)]} for i in range(10)})
        new = tour_file("attractions", {f"d{i}": {"items": [item(f"{i}-0")]} for i in range(10)})
        catalog = {f"d{i}" for i in range(10)}
        with self.assertRaises(RuntimeError):                      # 항목이 30% 넘게 줄면 거부
            tourapi.merge("attractions", new, prev, set(), catalog, "2026-10-20")
        # 카탈로그에서 빠진 여행지는 줄어든 것으로 세지 않는다
        same_d0 = tour_file("attractions", {"d0": prev["destinations"]["d0"]})
        out, _ = tourapi.merge("attractions", same_d0, prev, set(), {"d0"}, "2026-10-20")
        self.assertEqual(list(out["destinations"]), ["d0"])
        # 축제는 철이 지나면 크게 줄 수 있어 검사하지 않는다
        fprev = tour_file("festivals", {"d0": {"items": [item(str(j), end="2027-01-01") for j in range(9)]}})
        fnew = tour_file("festivals", {"d0": {"items": [item("1", end="2027-01-01")]}})
        out, _ = tourapi.merge("festivals", fnew, fprev, set(), {"d0"}, "2026-10-20")
        self.assertEqual(len(out["destinations"]["d0"]["items"]), 1)

    def test_problem_ids(self):
        self.assertEqual(tourapi.problem_ids(["restaurants/busan/123: non-https image",
                                              "festivals/jeonju: duplicate contentId", "other"]),
                         {"restaurants": {"busan"}, "festivals": {"jeonju"}})


class RunWithFakeScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tour-run-"))
        self.cache = self.tmp / "cache"
        self.curated = _util.make_curated(self.tmp / "curated")
        self.calls = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake_module(self):
        test = self

        class FakeAPI:
            def __init__(self, cache_dir, budget, spacing=0.3, offline=False, verbose=True, key=None):
                test.calls.append(("init", budget, key == KEY))
                self.cache_dir = Path(cache_dir)

            def cache_path(self, op, params):
                return self.cache_dir / op / (tourapi.cache_name(op, params) + ".json.gz")

            def get(self, op, params):
                test.calls.append(("get", op, params.get("modifiedtime"), params.get("pageNo")))
                return {"response": {"body": {"totalCount": 2, "items": {"item": [
                    {"contentid": "111", "contenttypeid": "39", "lDongRegnCd": "26", "lDongSignguCd": "350"},
                    {"contentid": "999", "contenttypeid": "15", "lDongRegnCd": "11", "lDongSignguCd": "110"}]}}}}

        def run_snapshot(budget=850, ids=None, out_dir=None, cache_dir=None, catalog=None, extras=None, use_extras=True,
                         offline=False, spacing=0.3, write=True, check_images=8, verify_images=True, key=None, log=print):
            mod = test.mod
            test.calls.append(("run", budget, use_extras, check_images, key == KEY, mod.FEST_FROM, mod.FEST_TO))
            log("  restaurants d000 3 items")
            out = Path(out_dir)
            test.calls.append(("codes", (out / "kr-tour-codes.json").is_file()))
            (out / "kr-restaurants.json").write_text(json.dumps(tour_file("restaurants", {
                "d000": {"items": [item("1", title="새 가게")]}, "d001": {"items": [item("2")]}})))
            (out / "kr-attractions.json").write_text(json.dumps(tour_file("attractions", {
                "d000": {"items": [item("5", kind="관광지")]}})))
            (out / "kr-festivals.json").write_text(json.dumps(tour_file("festivals", {
                "d001": {"regions": ["26-350"], "items": [item("7", start="2026-10-25", end="2026-10-27")]}},
                festivalWindow=[mod.FEST_FROM, mod.FEST_TO])))
            return {"callsThisRun": 12, "usedToday": 12, "budget": budget, "fatal": None,
                    "pending": {"restaurants": ["d001"], "attractions": [], "festivals": []},
                    "problems": []}

        self.mod = types.SimpleNamespace(TourAPI=FakeAPI, run_snapshot=run_snapshot, FEST_FROM="20261020",
                                         FEST_TO="20271031")
        return self.mod

    def test_run_wraps_script_and_merges(self):
        (self.curated / "kr-tour-codes.json").write_text("{}")
        tourapi.save_sync_state(self.cache, dt.date(2026, 10, 19))
        detail = put_cache(self.cache, "detailIntro2", {"contentId": "111", "contentTypeId": 39}, NOW)
        previous = {"kr-restaurants": tour_file("restaurants", {"d001": {"items": [item("old")]}}),
                    "kr-attractions": None, "kr-festivals": None}
        logs = []
        out, info = tourapi.run(key=KEY, cache_dir=self.cache, catalog_path=self.curated / "destinations.json",
                                curated_dir=self.curated, previous=previous, now=NOW, module=self.fake_module(),
                                log=logs.append)
        self.assertEqual(info["sync"]["days"], ["2026-10-19", "2026-10-20"])
        self.assertEqual(info["sync"]["changed"], 1)                    # 축제(15)는 증분 대상 아님
        self.assertFalse(detail.exists())                               # 바뀐 콘텐츠 상세 캐시 삭제
        run = [c for c in self.calls if c[0] == "run"][0]
        self.assertEqual(run[1], 600)                                   # 실행당 600
        self.assertFalse(run[2])                                        # kr-expand 후보 안 씀
        self.assertEqual(run[3], 0)
        self.assertTrue(run[4])                                         # 키는 인자로만 넘김
        self.assertEqual(run[5:], ("20261020", "20271031"))
        r = out["kr-restaurants"]["destinations"]
        self.assertEqual(r["d000"]["items"][0]["title"], "새 가게")
        self.assertEqual(r["d001"], previous["kr-restaurants"]["destinations"]["d001"])  # pending → 이전 블록
        self.assertEqual(out["kr-festivals"]["festivalWindow"], ["20261020", "20271031"])
        self.assertEqual(info["errors"], {})
        self.assertIn(("codes", True), self.calls)                     # 분류 코드 표를 큐레이션에서 복사해 둠
        self.assertFalse(any(KEY in m for m in logs))
        state = json.loads((self.cache / tourapi.SYNC_STATE).read_text())
        self.assertEqual(state["lastSync"], "2026-10-20")

    def test_run_requires_key(self):
        with self.assertRaises(RuntimeError):
            tourapi.run(key="", cache_dir=self.cache, catalog_path=self.curated / "destinations.json",
                        curated_dir=self.curated, previous={}, now=NOW, module=self.fake_module())

    def test_build_pack_uses_wrapper_online(self):
        """build_pack 이 TourAPI 데이터셋 셋을 한 번의 실행으로 만들고 키를 어디에도 남기지 않는지"""
        import build_pack
        import os

        saved = build_pack.PRODUCERS.copy()
        fake = self.fake_module()
        orig_run = tourapi.run

        def run(**kw):
            kw["module"] = fake
            return orig_run(**kw)

        tourapi.run = run
        try:
            out_dir = self.tmp / "public"
            code, log = _util.run_build(out_dir, self.curated, "--only", "kr-restaurants,kr-attractions,kr-festivals",
                                        "--now", packlib.iso(NOW), "--tourapi-cache", str(self.cache),
                                        env={"DATA_GO_KR_KEY": KEY})
        finally:
            tourapi.run = orig_run
            build_pack.PRODUCERS.clear()
            build_pack.PRODUCERS.update(saved)
        self.assertEqual(code, 0, log)
        self.assertEqual(sum(1 for c in self.calls if c[0] == "run"), 1)
        m = _util.read(out_dir / "packs/v2/manifest.json")
        self.assertEqual(set(m["datasets"]), {"kr-restaurants", "kr-attractions", "kr-festivals"})
        self.assertEqual(m["datasets"]["kr-festivals"]["dataAsOf"], "2026-10-20T06:51:00+09:00")
        self.assertNotIn(KEY, log)
        for f in list(out_dir.rglob("*")) + list(self.cache.rglob("*")):
            if f.is_file() and not f.name.endswith(".gz"):
                self.assertNotIn(KEY, f.read_text(encoding="utf-8", errors="replace"))


@unittest.skipUnless(packlib.vendor_script("tourapi-snapshot.py"), "tourapi-snapshot.py 없음")
class RealScriptContractTests(unittest.TestCase):
    def test_script_api_matches_wrapper(self):
        import inspect

        mod = tourapi.load_module()
        params = inspect.signature(mod.run_snapshot).parameters
        for name in ("budget", "out_dir", "cache_dir", "catalog", "use_extras", "offline", "key", "log"):
            self.assertIn(name, params)
        self.assertIn("key", inspect.signature(mod.TourAPI).parameters)
        for op, p in (("detailIntro2", {"contentId": "1", "contentTypeId": 39}), ("areaBasedList2", {"lDongRegnCd": "26"})):
            self.assertEqual(mod.TourAPI._cache_name(op, p), tourapi.cache_name(op, p))
        self.assertTrue(hasattr(mod, "FEST_FROM") and hasattr(mod, "FEST_TO"))


if __name__ == "__main__":
    unittest.main()
