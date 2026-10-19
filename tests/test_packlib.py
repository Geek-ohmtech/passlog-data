import re
import unittest

import _util  # noqa: F401  (경로 · 네트워크 차단)
import packlib

SWIFT = _util.MONOREPO / "ios" / "Packages" / "PasslogKit" / "Sources" / "PasslogDataPack" / "DatasetID.swift"


def swift_cases():
    """DatasetID.swift → {case 이름: rawValue}"""
    text = SWIFT.read_text(encoding="utf-8")
    body = text.split("public enum DatasetID", 1)[1].split("public var bundledFileName", 1)[0]
    out = {}
    for m in re.finditer(r"^\s*case\s+([^\n]+)$", body, re.M):
        for part in m.group(1).split(","):
            part = part.strip()
            if not part:
                continue
            mm = re.match(r'(\w+)\s*=\s*"([^"]+)"', part)
            if mm:
                out[mm.group(1)] = mm.group(2)
            elif re.match(r"^\w+$", part):
                out[part] = part
    return out


@unittest.skipUnless(SWIFT.is_file(), "앱 리포가 아님 (배포 리포에서는 Swift 파일이 없음)")
class SwiftContractTests(unittest.TestCase):
    def test_dataset_ids_match_swift(self):
        self.assertEqual(sorted(swift_cases().values()), sorted(packlib.DATASET_IDS))

    def test_bundled_files_match_swift(self):
        cases = swift_cases()
        text = SWIFT.read_text(encoding="utf-8")
        block = text.split("public var bundledFileName", 1)[1].split("public var appliesLive", 1)[0]
        swift = {cases[n]: f for n, f in re.findall(r'case \.(\w+):\s*"([^"]+)"', block)}
        mine = {s.id: s.bundled for s in packlib.DATASETS.values() if s.bundled}
        self.assertEqual(swift, mine)

    def test_envelope_set_matches_swift(self):
        cases = swift_cases()
        text = SWIFT.read_text(encoding="utf-8")
        block = text.split("public var usesEnvelope", 1)[1].split("public var supportedSchema", 1)[0]
        line = re.search(r"case ([^:]+): true", block).group(1)
        swift = {cases[n.strip().lstrip(".")] for n in line.split(",")}
        self.assertEqual(swift, {s.id for s in packlib.DATASETS.values() if s.envelope})

    def test_priority_matches_swift(self):
        cases = swift_cases()
        text = SWIFT.read_text(encoding="utf-8")
        block = text.split("public var priority", 1)[1].split("public var usesEnvelope", 1)[0]
        swift = {}
        for names, value in re.findall(r"case ([^:]+): (\d+)", block):
            for n in names.split(","):
                swift[cases[n.strip().lstrip(".")]] = int(value)
        self.assertEqual(swift, packlib.PRIORITY)


class PacklibTests(unittest.TestCase):
    def test_canonical_bytes_is_order_independent(self):
        a = packlib.canonical_bytes({"b": 1, "a": {"y": [1, 2], "x": "가"}})
        b = packlib.canonical_bytes({"a": {"x": "가", "y": [1, 2]}, "b": 1})
        self.assertEqual(a, b)
        self.assertEqual(a.decode("utf-8"), '{"a":{"x":"가","y":[1,2]},"b":1}')
        self.assertEqual(len(packlib.sha256_hex(a)), 64)

    def test_parse_date_variants(self):
        p = packlib.parse_date
        self.assertEqual(p("2026-10-16").isoformat(), "2026-10-16T00:00:00+09:00")
        self.assertEqual(p("2026-10").isoformat(), "2026-10-01T00:00:00+09:00")
        self.assertEqual(p("2026-10-19T03:17:42+09:00").isoformat(), "2026-10-19T03:17:42+09:00")
        self.assertEqual(p("2026-10-18T18:17:42Z").astimezone(packlib.KST).isoformat(), "2026-10-19T03:17:42+09:00")
        self.assertEqual(p("2026-10-19T03:17:42.123+0900").hour, 3)
        self.assertEqual(p("2026-10-19T07:32+09:00").minute, 32)
        self.assertEqual(p("20261001093000").isoformat(), "2026-10-01T09:30:00+09:00")
        self.assertIsNone(p("어제"))
        self.assertIsNone(p(None))

    def test_normalize_as_of(self):
        n = packlib.normalize_as_of
        self.assertEqual(n("2026-10"), "2026-10-01")
        self.assertEqual(n("2026-10-16"), "2026-10-16")
        self.assertEqual(n("2026-10-18T18:17:42Z"), "2026-10-19T03:17:42+09:00")
        self.assertEqual(n("20261020"), "2026-10-20")

    def test_data_as_of_rules(self):
        d = packlib.data_as_of
        self.assertEqual(d("advisories", {"asOf": "2026-10-19T04:11:15+09:00", "updated": "2020-01"}),
                         "2026-10-19T04:11:15+09:00")
        self.assertEqual(d("rates", {"date": "2026-10-16", "generatedAt": "2026-10-17T00:00:00+09:00"}), "2026-10-16")
        self.assertEqual(d("destinations", {"updated": "2026-10"}), "2026-10-01")
        self.assertEqual(d("countries", {"reviewedAt": "2026-10-13"}), "2026-10-13")
        self.assertEqual(d("airports", {"generated": "2026-10-19"}), "2026-10-19")
        self.assertEqual(d("kr-festivals", {"generatedAt": "2026-10-20T03:09:21+09:00"}), "2026-10-20T03:09:21+09:00")
        self.assertEqual(d("entry", {"dataAsOf": "2026-10-20", "asOf": "2000-01-01"}), "2026-10-20")
        self.assertIsNone(d("kr-places", [{"name": "부산"}]))
        self.assertIsNone(d("checklist-rules", {"version": 1}))

    def test_select_datasets(self):
        sel, unknown = packlib.select_datasets("schedule", "17 */6 * * *")
        self.assertIn("advisories", sel)
        self.assertIn("entry", sel)
        self.assertIn("destinations", sel)          # 큐레이션은 매번
        self.assertNotIn("rates", sel)
        self.assertNotIn("kr-restaurants", sel)
        self.assertEqual(unknown, [])
        sel, _ = packlib.select_datasets("schedule", "25 2,5 * * 1-5")
        self.assertIn("rates", sel)
        self.assertNotIn("advisories", sel)
        sel, _ = packlib.select_datasets("schedule", "50 21 * * *")
        self.assertTrue({"kr-restaurants", "kr-attractions", "kr-festivals", "source-status"} <= set(sel))
        sel, _ = packlib.select_datasets("schedule", "40 18 * * 0")
        self.assertTrue({"holidays-kr", "holidays-world", "local-apps-status", "kr-restaurants"} <= set(sel))
        sel, _ = packlib.select_datasets("push")
        self.assertEqual(set(sel), {s.id for s in packlib.DATASETS.values() if "curated" in s.groups})
        self.assertNotIn("advisories", sel)
        sel, _ = packlib.select_datasets("workflow_dispatch")
        self.assertEqual(set(sel), set(packlib.DATASET_IDS))
        sel, unknown = packlib.select_datasets("workflow_dispatch", "", ["rates", " kr-festivals", "nope"])
        self.assertEqual(sel, ["rates", "kr-festivals"])
        self.assertEqual(unknown, ["nope"])
        # 우선순위 순서
        sel, _ = packlib.select_datasets("manual")
        self.assertEqual(sel[0], "advisories")
        self.assertEqual(set(sel[-2:]), {"destinations", "airports"})

    def test_scrub_hides_all_key_forms(self):
        key = "abc+/=KEY%"
        msg = f"GET https://x/?serviceKey={key}&a=1 failed for {key} and abc%2B%2F%3DKEY%25 authkey=zzz"
        out = packlib.scrub(msg, key)
        self.assertNotIn("abc+/=KEY", out)
        self.assertNotIn("abc%2B", out)
        self.assertNotIn("zzz", out)

    def test_secret_unquotes_encoding_key(self):
        self.assertEqual(packlib.secret("K", {"K": " a%2Bb%3D "}), "a+b=")
        self.assertEqual(packlib.secret("K", {}), "")


if __name__ == "__main__":
    unittest.main()
