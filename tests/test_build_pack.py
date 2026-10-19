import hashlib
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

import _util
import build_pack
import packlib

NOW = "2026-10-20T07:00:00+09:00"


class BuildPackOfflineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pack-test-"))
        self.curated = _util.make_curated(self.tmp / "curated")
        self.out = self.tmp / "public"
        self.saved_producers = dict(build_pack.PRODUCERS)

    def tearDown(self):
        build_pack.PRODUCERS.clear()
        build_pack.PRODUCERS.update(self.saved_producers)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, *extra, now=NOW, env=None):
        code, log = _util.run_build(self.out, self.curated, "--offline", "--now", now, *extra, env=env)
        return code, log

    def manifest(self):
        return _util.read(self.out / "packs" / "v2" / "manifest.json")

    def status(self):
        return _util.read(self.out / "packs" / "v2" / "status.json")

    def test_full_offline_pack_matches_contract(self):
        code, log = self.build()
        self.assertEqual(code, 0, log)
        m = self.manifest()
        self.assertEqual(m["schemaVersion"], 2)
        self.assertEqual(m["sequence"], 1)
        self.assertEqual(m["baseURL"], "datasets/")
        self.assertIsNotNone(packlib.parse_date(m["publishedAt"]))
        self.assertRegex(m["packVersion"], r"^\d{4}\.\d{2}\.\d{2}-\d{4}$")
        self.assertIn("signature", m)
        for ds, e in m["datasets"].items():
            self.assertIn(ds, packlib.DATASETS)
            self.assertRegex(e["path"], rf"^{re.escape(ds)}\.[0-9a-f]{{8}}\.json$")
            data = (self.out / "packs" / "v2" / "datasets" / e["path"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), e["sha256"])
            self.assertEqual(e["sha256"][:8], e["path"].split(".")[1])
            self.assertRegex(e["sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(len(data), e["size"])
            self.assertEqual(e["schema"], 1)
            self.assertIsNotNone(packlib.parse_date(e["dataAsOf"]), ds)
            self.assertIn("ttlHours", e)
        expected = {"advisories", "holidays-kr", "destinations", "countries", "checklist-rules", "checklist-template",
                    "kr-places", "kr-restaurants", "kr-attractions", "kr-festivals"}
        self.assertEqual(set(m["datasets"]), expected)
        # 큐레이션은 바이트 그대로
        self.assertEqual((self.out / "packs/v2/datasets" / m["datasets"]["destinations"]["path"]).read_bytes(),
                         (self.curated / "destinations.json").read_bytes())
        st = self.status()
        self.assertEqual(st["datasets"]["entry"]["state"], "skipped")
        self.assertEqual(st["datasets"]["destinations"]["state"], "updated")
        self.assertEqual(st["alerts"], [])
        # v1 호환
        meta = _util.read(self.out / "v1" / "meta.json")
        self.assertFalse(meta["sample"])
        jp = _util.read(self.out / "v1" / "countries" / "JP.json")
        self.assertEqual(jp["advisory"]["level"], 1)
        self.assertIn("generatedAt", jp)
        kr = _util.read(self.out / "v1" / "countries" / "KR.json")
        self.assertTrue(kr["entry"]["visaFree"])
        self.assertTrue((self.out / "v1" / "advisories.json").is_file())
        self.assertFalse((self.out / "v1" / "rates.json").exists())

    def test_prev_file_whose_bytes_changed_is_rewritten(self):
        # 줄바꿈 정규화(autocrlf) 등으로 public/ 파일 바이트만 바뀌고 JSON 의미는 같으면, 'unchanged' 로 두지 않고 다시 쓴다
        code, log = self.build()
        self.assertEqual(code, 0, log)
        e = self.manifest()["datasets"]["countries"]
        f = self.out / "packs" / "v2" / "datasets" / e["path"]
        f.write_bytes(f.read_bytes().replace(b"\n", b"\r\n") + b"\r\n")
        self.assertNotEqual(hashlib.sha256(f.read_bytes()).hexdigest(), e["sha256"])
        code, log = self.build()
        self.assertEqual(code, 0, log)
        e2 = self.manifest()["datasets"]["countries"]
        data = (self.out / "packs" / "v2" / "datasets" / e2["path"]).read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), e2["sha256"])
        self.assertEqual(len(data), e2["size"])
        self.assertIn("sha256 이 manifest 와 달라", log)

    def test_rerun_without_changes_leaves_manifest_untouched(self):
        self.build()
        before = (self.out / "packs/v2/manifest.json").read_bytes()
        code, log = self.build(now="2026-10-20T09:00:00+09:00")
        self.assertEqual(code, 0)
        self.assertIn("manifest 그대로", log)
        self.assertEqual((self.out / "packs/v2/manifest.json").read_bytes(), before)

    def test_change_bumps_sequence_keeps_old_file_seven_days(self):
        self.build()
        m1 = self.manifest()
        old_path = m1["datasets"]["countries"]["path"]
        doc = _util.read(self.curated / "countries.json")
        doc["countries"][0]["nameKo"] = "바뀐 이름"
        _util.write_json(self.curated / "countries.json", doc)
        self.build(now="2026-10-21T07:00:00+09:00")
        m2 = self.manifest()
        self.assertEqual(m2["sequence"], 2)
        self.assertNotEqual(m2["datasets"]["countries"]["path"], old_path)
        self.assertEqual(m2["datasets"]["destinations"], m1["datasets"]["destinations"])
        ds_dir = self.out / "packs/v2/datasets"
        self.assertTrue((ds_dir / old_path).is_file())
        self.build(now="2026-10-27T07:00:00+09:00")     # 6일 뒤: 아직 보존
        self.assertTrue((ds_dir / old_path).is_file())
        self.build(now="2026-10-28T08:00:00+09:00")     # 7일 넘음: 삭제
        self.assertFalse((ds_dir / old_path).exists())
        self.assertEqual(self.manifest()["sequence"], 2)  # 지우기만 해서는 sequence 가 안 오른다
        hist = _util.read(self.out / "packs/v2/history.json")
        self.assertNotIn(old_path, hist["files"])

    def test_sequence_never_goes_back_even_if_manifest_is_lost(self):
        self.build()
        doc = _util.read(self.curated / "countries.json")
        doc["countries"][0]["nameKo"] = "바뀜"
        _util.write_json(self.curated / "countries.json", doc)
        self.build(now="2026-10-21T07:00:00+09:00")
        self.assertEqual(self.manifest()["sequence"], 2)
        (self.out / "packs/v2/manifest.json").unlink()
        self.build(now="2026-10-21T08:00:00+09:00")
        self.assertEqual(self.manifest()["sequence"], 3)

    def test_photo_gate_rejects_destination_without_bundled_photo(self):
        self.build()
        good = self.manifest()["datasets"]["destinations"]
        doc = _util.read(self.curated / "destinations.json")
        doc["destinations"].append({"id": "newplace", "countryCode": "JP", "lat": 1, "lon": 2,
                                    "photo": {"bundled": "newplace.heic"}})
        _util.write_json(self.curated / "destinations.json", doc)
        code, log = self.build(now="2026-10-21T07:00:00+09:00")
        self.assertEqual(code, 0)
        self.assertEqual(self.manifest()["datasets"]["destinations"], good)  # 이전 것 유지
        st = self.status()
        self.assertEqual(st["datasets"]["destinations"]["state"], "rejected")
        self.assertIn("newplace", st["datasets"]["destinations"]["message"])
        self.assertTrue(any(a["dataset"] == "destinations" for a in st["alerts"]))
        code, _ = self.build("--strict", now="2026-10-21T08:00:00+09:00")
        self.assertEqual(code, 1)

    def test_photo_gate_raises_min_app_version(self):
        ids = [f"d{i:03d}" for i in range(60)]
        _util.write_json(self.curated / "photo-ids.json",
                         {"schemaVersion": 1, "versions": {"1.0.0": ids, "1.1.0": ids + ["newplace"]}})
        doc = _util.read(self.curated / "destinations.json")
        doc["destinations"].append({"id": "newplace", "countryCode": "JP", "lat": 1, "lon": 2})
        _util.write_json(self.curated / "destinations.json", doc)
        self.build()
        e = self.manifest()["datasets"]["destinations"]
        self.assertEqual(e["minAppVersion"], "1.1.0")
        self.assertNotIn("minAppVersion", self.manifest()["datasets"]["countries"])

    def test_invalid_curated_json_is_rejected_and_previous_kept(self):
        self.build()
        good = self.manifest()["datasets"]["checklist-rules"]
        (self.curated / "checklist-rules.json").write_text("{not json", encoding="utf-8")
        self.build(now="2026-10-21T07:00:00+09:00")
        self.assertEqual(self.manifest()["datasets"]["checklist-rules"], good)
        self.assertEqual(self.status()["datasets"]["checklist-rules"]["state"], "rejected")

    def test_advisory_levels_validated(self):
        doc = _util.read(self.curated / "advisories.json")
        doc["countries"]["JP"]["level"] = 7
        _util.write_json(self.curated / "advisories.json", doc)
        self.build()
        self.assertNotIn("advisories", self.manifest()["datasets"])
        self.assertIn("0–4", self.status()["datasets"]["advisories"]["message"])

    def test_heartbeat_republishes_same_content_after_interval(self):
        doc = _util.read(self.curated / "advisories.json")
        doc["asOf"] = "2026-10-20T05:00:00+09:00"
        _util.write_json(self.curated / "advisories.json", doc)
        self.build()
        first = self.manifest()["datasets"]["advisories"]
        doc["asOf"] = "2026-10-20T06:00:00+09:00"        # 내용은 같고 기준 시각만 새로
        _util.write_json(self.curated / "advisories.json", doc)
        self.build(now="2026-10-20T08:00:00+09:00")       # 이전 asOf(05:00)가 12시간 안 → 그대로
        self.assertEqual(self.manifest()["datasets"]["advisories"], first)
        self.build(now="2026-10-20T18:00:00+09:00")       # 이전 asOf 가 12시간 넘음 → 새로
        second = self.manifest()["datasets"]["advisories"]
        self.assertNotEqual(second["path"], first["path"])
        self.assertEqual(second["dataAsOf"], "2026-10-20T06:00:00+09:00")
        self.assertEqual(self.manifest()["sequence"], 2)

    def test_rates_file_feeds_pack_and_v1(self):
        table = {f"C{i:02d}": 1.0 + i for i in range(20)}
        table.update({"USD": 1333.2, "JPY": 8.43, "EUR": 1550.0})
        rates = {"base": "KRW", "date": "2026-10-19", "generatedAt": "x", "rates": table,
                 "sources": {"USD": "frankfurter"}, "dates": {"USD": "2026-10-19"}}
        path = _util.write_json(self.tmp / "rates.json", rates)
        self.build("--rates-file", str(path))
        e = self.manifest()["datasets"]["rates"]
        self.assertEqual(e["dataAsOf"], "2026-10-19")
        body = _util.read(self.out / "packs/v2/datasets" / e["path"])
        self.assertNotIn("generatedAt", body)
        v1 = _util.read(self.out / "v1" / "rates.json")
        self.assertEqual(v1["rates"]["USD"], 1333.2)
        self.assertEqual(v1["sources"]["USD"], "frankfurter")
        meta = _util.read(self.out / "v1" / "meta.json")
        self.assertIn("Frankfurter", [s["name"] for s in meta["sources"]])

    def test_failures_counted_and_alerted_without_leaking_keys(self):
        secret = "SECRET-KEY-12345"

        def boom(ctx, spec):
            raise RuntimeError(f"connection failed serviceKey={secret} ({secret})")

        build_pack.PRODUCERS["rates"] = boom
        code, log = _util.run_build(self.out, self.curated, "--now", NOW, "--only", "rates,countries",
                                    env={"DATA_GO_KR_KEY": secret})
        self.assertEqual(code, 0)
        st = self.status()
        self.assertEqual(st["datasets"]["rates"]["state"], "failed")
        self.assertEqual(st["datasets"]["rates"]["consecutiveFailures"], 1)
        self.assertEqual(st["alerts"], [])
        _util.run_build(self.out, self.curated, "--now", "2026-10-20T13:00:00+09:00", "--only", "rates",
                        env={"DATA_GO_KR_KEY": secret})
        st = self.status()
        self.assertEqual(st["datasets"]["rates"]["consecutiveFailures"], 2)
        self.assertTrue(any(a["dataset"] == "rates" for a in st["alerts"]))
        self.assertEqual(st["datasets"]["countries"]["state"], "updated")  # 이번에 안 돌린 데이터셋은 그대로 이월
        self.assertNotIn(secret, log)
        for f in self.out.rglob("*"):
            if f.is_file():
                self.assertNotIn(secret, f.read_text(encoding="utf-8", errors="replace"), f)

    def test_unknown_only_value_is_usage_error(self):
        code, log = self.build("--only", "nope")
        self.assertEqual(code, 2)
        self.assertIn("nope", log)

    def test_validate_size_cap(self):
        with self.assertRaises(build_pack.Reject):
            build_pack.validate("kr-places", [], b" " * (packlib.MAX_DATASET_BYTES + 1))

    def test_envelope_validation(self):
        good = packlib.envelope("entry", "2026-10-20T07:00:00+09:00", {"JP": {"visaFree": True}}, [])
        build_pack.validate("entry", good, packlib.canonical_bytes(good))
        bad = dict(good, id="rates")
        with self.assertRaises(build_pack.Reject):
            build_pack.validate("entry", bad, packlib.canonical_bytes(bad))
        future = dict(good, schema=2)
        with self.assertRaises(build_pack.Reject):
            build_pack.validate("entry", future, packlib.canonical_bytes(future))


@unittest.skipUnless((packlib.default_curated_dir() / "destinations.json").is_file(), "큐레이션 폴더 없음")
class RealSharedDataTests(unittest.TestCase):
    """실제 큐레이션(앱 리포 shared/data · 배포 리포 curated/)으로 오프라인 팩을 만들어 모든 데이터셋이 검증을 통과하는지"""

    def test_real_offline_pack(self):
        tmp = Path(tempfile.mkdtemp(prefix="real-pack-"))
        try:
            code, log = _util.run_build(tmp, packlib.default_curated_dir(), "--offline", "--strict")
            self.assertEqual(code, 0, log)
            m = _util.read(tmp / "packs/v2/manifest.json")
            for ds in ("advisories", "destinations", "countries", "kr-restaurants", "kr-attractions", "kr-festivals",
                       "holidays-kr", "dish-wiki", "local-apps", "airports"):
                self.assertIn(ds, m["datasets"], log)
            for e in m["datasets"].values():
                self.assertLessEqual(e["size"], packlib.MAX_DATASET_BYTES)
            self.assertNotIn("large", m["datasets"]["advisories"])
            self.assertTrue(m["datasets"]["destinations"].get("large"))
            # 국내 맛집 · 관광지 · 축제는 커도 large 가 아니다 (앱이 바로 반영 — 셀룰러 사용자도 받아야 한다)
            for ds in ("kr-restaurants", "kr-attractions", "kr-festivals"):
                self.assertNotIn("large", m["datasets"][ds], ds)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
