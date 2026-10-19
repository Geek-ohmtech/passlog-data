"""워크플로 · lock 파일 · 사진 id · 이슈 알림 (네트워크 없음)"""
import re
import shutil
import tempfile
import unittest
from pathlib import Path

import _util
import make_photo_ids
import packlib
import report_issues

WORKFLOW = _util.PIPELINE_DIR / ".github" / "workflows" / "datapack.yml"
LOCK = _util.PIPELINE_DIR / "requirements.lock"


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")

    def test_crons_match_pipeline_groups(self):
        crons = re.findall(r'-\s*cron:\s*"([^"]+)"', self.text)
        self.assertEqual(sorted(crons), sorted(packlib.SCHEDULES))
        for c in crons:
            self.assertNotEqual(c.split()[0], "0", "정각은 피한다")

    def test_actions_pinned_to_sha_and_node24_versions(self):
        uses = re.findall(r"uses:\s*([^\s#]+)(?:\s*#\s*(\S+))?", self.text)
        self.assertGreaterEqual(len(uses), 6)
        for ref, tag in uses:
            self.assertRegex(ref, r"^actions/[a-z-]+(/[a-z]+)?@[0-9a-f]{40}$", ref)
            self.assertTrue(tag.startswith("v"), ref)
        tags = {ref.split("@")[0]: tag for ref, tag in uses}
        self.assertEqual(tags["actions/checkout"], "v7.0.1")
        self.assertEqual(tags["actions/setup-python"], "v7.0.0")
        self.assertEqual(tags["actions/upload-pages-artifact"], "v5.0.0")
        self.assertEqual(tags["actions/deploy-pages"], "v5.0.1")

    def test_permissions_and_triggers(self):
        for perm in ("contents: write", "issues: write", "pages: write", "id-token: write"):
            self.assertIn(perm, self.text)
        self.assertIn("workflow_dispatch:", self.text)
        self.assertRegex(self.text, r"only:\s*\n\s*description")
        self.assertIn('paths: ["curated/**", "scripts/**"', self.text)
        self.assertIn("--require-hashes", self.text)
        self.assertIn("actions/cache/restore@", self.text)
        self.assertIn("actions/cache/save@", self.text)

    def test_secrets_only_via_env_and_inputs_not_interpolated_in_scripts(self):
        self.assertIn("DATA_GO_KR_KEY: ${{ secrets.DATA_GO_KR_KEY }}", self.text)
        self.assertIn("KOREAEXIM_KEY: ${{ secrets.KOREAEXIM_KEY }}", self.text)
        # run: 블록 안에 ${{ inputs.* }} · ${{ github.event.* }} 를 직접 넣지 않는다(스크립트 주입 방지)
        for block in re.findall(r"run: \|\n((?:\s{10,}.*\n)+)", self.text):
            self.assertNotIn("${{", block)
        for line in re.findall(r"run: (?!\|)(.*)", self.text):
            self.assertNotRegex(line, r"\$\{\{\s*(inputs|github\.event)")

    def test_plan_mode(self):
        import io
        import os

        import build_pack

        out = Path(tempfile.mkdtemp(prefix="plan-"))
        try:
            gh = out / "gh_output"
            saved = dict(os.environ)
            os.environ["GITHUB_OUTPUT"] = str(gh)
            os.environ["DATA_GO_KR_KEY"] = "k"
            try:
                buf = io.StringIO()
                args = build_pack.parse_args(["--plan", "--event", "schedule", "--schedule", "50 21 * * *", "--out", str(out)])
                self.assertEqual(build_pack.run(args, buf.write), 0)
                args = build_pack.parse_args(["--plan", "--event", "schedule", "--schedule", "17 */6 * * *", "--out", str(out)])
                self.assertEqual(build_pack.run(args, buf.write), 0)
            finally:
                os.environ.clear()
                os.environ.update(saved)
            self.assertEqual(gh.read_text().split(), ["tourapi=true", "tourapi=false"])
            self.assertFalse((out / "packs").exists())   # 계획만, 아무것도 쓰지 않음
        finally:
            shutil.rmtree(out, ignore_errors=True)

    def test_build_pack_flags_exist(self):
        import build_pack

        args = build_pack.parse_args(["--event", "schedule", "--schedule", "17 */6 * * *", "--only", "", "--out", "public",
                                      "--tourapi-cache", ".cache/tourapi", "--refresh-climate"])
        self.assertTrue(args.refresh_climate)


class LockTests(unittest.TestCase):
    def test_every_requirement_is_pinned_with_hashes(self):
        text = LOCK.read_text(encoding="utf-8")
        reqs = re.findall(r"^([A-Za-z0-9_.-]+)==(\S+)", text, re.M)
        self.assertEqual({n for n, _ in reqs}, {"holidays", "python-dateutil", "six"})
        blocks = re.split(r"\n(?=[A-Za-z])", text.split("\n", 3)[-1])
        for b in blocks:
            if "==" in b:
                self.assertRegex(b, r"--hash=sha256:[0-9a-f]{64}")


class PhotoIdsTests(unittest.TestCase):
    def test_merge_keeps_other_versions_and_updated_when_same(self):
        doc = make_photo_ids.merge(None, "1.0.0", ["b", "a"], "2026-10-20")
        self.assertEqual(doc["versions"], {"1.0.0": ["a", "b"]})
        doc2 = make_photo_ids.merge(doc, "1.1.0", ["a", "b", "c"], "2026-11-01")
        self.assertEqual(list(doc2["versions"]), ["1.0.0", "1.1.0"])
        self.assertEqual(doc2["updated"], "2026-11-01")
        doc3 = make_photo_ids.merge(doc2, "1.1.0", ["c", "a", "b"], "2026-12-01")
        self.assertEqual(doc3["updated"], "2026-11-01")          # 같은 내용이면 날짜도 그대로
        doc4 = make_photo_ids.merge(doc3, "1.10.0", ["a"], "2026-12-01")
        self.assertEqual(list(doc4["versions"]), ["1.0.0", "1.1.0", "1.10.0"])  # 숫자 순서

    def test_gate(self):
        dests = {"destinations": [{"id": "a", "photo": {"bundled": "a.heic"}}, {"id": "b"}]}
        self.assertEqual(make_photo_ids.gate(dests, {"versions": {"1.0.0": ["a", "b"]}}), "1.0.0")
        self.assertEqual(make_photo_ids.gate(dests, {"versions": {"1.0.0": ["a"], "1.1.0": ["a", "b"]}}), "1.1.0")
        with self.assertRaises(ValueError):
            make_photo_ids.gate(dests, {"versions": {"1.0.0": ["a"]}})
        with self.assertRaises(ValueError):
            make_photo_ids.gate(dests, None)

    def test_cli_writes_file_from_photo_folder(self):
        tmp = Path(tempfile.mkdtemp(prefix="photos-"))
        try:
            (tmp / "p").mkdir()
            for n in ("seoul.heic", "busan.heic", ".DS_Store", "notes.txt"):
                (tmp / "p" / n).write_bytes(b"x")
            out = tmp / "photo-ids.json"
            self.assertEqual(make_photo_ids.main(["--photos", str(tmp / "p"), "--app-version", "1.0.0", "--out", str(out)]), 0)
            self.assertEqual(_util.read(out)["versions"], {"1.0.0": ["busan", "seoul"]})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @unittest.skipUnless((packlib.MONOREPO_ROOT / "ios" / "project.yml").is_file(), "앱 리포가 아님")
    def test_app_version_from_project(self):
        self.assertRegex(make_photo_ids.app_version() or "", r"^\d+\.\d+(\.\d+)?$")


class ReportIssuesTests(unittest.TestCase):
    def test_plan(self):
        status = {"alerts": [{"dataset": "rates", "kind": "failed", "message": "rates 2회 연속 실패"},
                             {"dataset": "destinations", "kind": "rejected", "message": "사진 게이트"},
                             {"dataset": "source-status", "kind": "changed", "message": "https://a"},
                             {"dataset": "source-status", "kind": "changed", "message": "https://b"}],
                  "datasets": {"rates": {"state": "failed", "consecutiveFailures": 2},
                               "advisories": {"state": "unchanged", "consecutiveFailures": 0}}}
        items, closes = report_issues.plan(status)
        titles = [t for t, _ in items]
        self.assertEqual(titles, ["데이터 팩: rates 실패", "데이터 팩: destinations 거부", "데이터 팩: 공식 페이지 변경 감지"])
        self.assertIn("https://b", items[2][1])
        self.assertIn("데이터 팩: advisories 실패", closes)
        self.assertNotIn("데이터 팩: rates 실패", closes)

    def test_dry_run_does_not_call_gh(self):
        tmp = Path(tempfile.mkdtemp(prefix="issues-"))
        try:
            path = _util.write_json(tmp / "status.json", {"alerts": [{"dataset": "rates", "kind": "failed", "message": "x"}]})
            self.assertEqual(report_issues.main(["--status", str(path), "--dry-run"]), 0)
            self.assertEqual(report_issues.main(["--status", str(tmp / "missing.json"), "--dry-run"]), 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
