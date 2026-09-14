import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("change_impact_check", Path(__file__).resolve().parents[1] / "scripts/change_impact_check.py")
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


class ImpactCheckTests(unittest.TestCase):
    def run_check(self, files, diff, args=None):
        with patch.object(CHECK, "collect_changes", return_value=(files, diff)), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return CHECK.main(args or [])

    def test_runtime_changes_still_require_document_and_tests(self):
        self.assertEqual(self.run_check(["app/radar.py"], "changed"), 1)
        self.assertEqual(self.run_check(["app/radar.py", "docs/整体程序规则.md"], "changed"), 1)
        self.assertEqual(self.run_check(["app/radar.py", "docs/整体程序规则.md", "tests/test_sample.py"], "changed"), 0)
        self.assertEqual(self.run_check(["docs/说明.md"], "typo"), 0)

    def test_report_valid_stale_and_malformed(self):
        files, diff = ["app/radar.py"], "comment fix"
        report = {"decision": "no_rule_change", "files": files,
                  "diff_sha256": CHECK.diff_digest(files, diff), "reviewer": "developer",
                  "reason": "Comment only", "validation": "Inspected diff and ran tests"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "review.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            self.assertEqual(self.run_check(files, diff, ["--review-report", str(path)]), 0)
            self.assertEqual(self.run_check(files, diff + "x", ["--review-report", str(path)]), 1)
            path.write_text("broken json", encoding="utf-8")
            self.assertEqual(self.run_check(files, diff, ["--review-report", str(path)]), 1)
            self.assertEqual(self.run_check(files, diff, ["--review-report", str(path) + "missing"]), 1)

    def test_git_inventory_covers_staged_unstaged_and_untracked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
            git("init")
            (root / "staged.py").write_text("before", encoding="utf-8")
            (root / "unstaged.py").write_text("before", encoding="utf-8")
            git("add", ".")
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "baseline")
            (root / "staged.py").write_text("after", encoding="utf-8")
            git("add", "staged.py")
            (root / "unstaged.py").write_text("after", encoding="utf-8")
            (root / "new 中文.py").write_text("new", encoding="utf-8")
            with patch.object(CHECK, "ROOT", root):
                files, diff = CHECK.collect_changes(None)
                self.assertEqual(files, ["new 中文.py", "staged.py", "unstaged.py"])
                digest = CHECK.diff_digest(files, diff)
                (root / "new 中文.py").write_text("changed", encoding="utf-8")
                self.assertNotEqual(CHECK.diff_digest(*CHECK.collect_changes(None)), digest)
                self.assertEqual(CHECK.collect_changes("HEAD")[0], [])
