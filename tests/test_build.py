import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pip_build

spec = importlib.util.spec_from_file_location("pnpm_compat", ROOT / "scripts/pnpm_compat.py")
compat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compat)


class PatchSafetyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        with patch.object(pip_build, "host_target", return_value=("mac", "arm64")):
            self.project = pip_build.Project(self.root / "build")
        self.core = self.project.core
        self.core.mkdir(parents=True)
        self.git("init")
        self.git("config", "user.name", "PiP tests")
        self.git("config", "user.email", "pip-tests@example.invalid")
        self.git("config", "core.autocrlf", "false")
        self.target = self.core / "sample.txt"
        self.target.write_text("before\n", encoding="utf-8")
        self.git("add", "sample.txt")
        self.git("commit", "-m", "Fixture")
        self.commit = self.git("rev-parse", "HEAD")
        (self.root / "patches").mkdir()
        (self.root / "patches/firefox-pip.patch").write_text(
            "diff --git a/sample.txt b/sample.txt\n--- a/sample.txt\n+++ b/sample.txt\n"
            "@@ -1 +1 @@\n-before\n+after\n", encoding="utf-8")
        self.root_patch = patch.object(pip_build, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.pin_patch = patch.dict(pip_build.UPSTREAM, {"commit": self.commit})
        self.pin_patch.start()
        self.addCleanup(self.pin_patch.stop)

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.core,
                                       text=True, stderr=subprocess.DEVNULL).strip()

    def test_apply_is_idempotent_and_preserves_unrelated_files(self):
        note = self.core / "my-note.txt"
        note.write_text("keep me", encoding="utf-8")
        self.project.patch()
        self.project.patch()
        self.assertEqual(self.target.read_text(), "after\n")
        self.assertEqual(note.read_text(), "keep me")

    def test_conflicting_user_edit_is_preserved(self):
        self.target.write_text("user edit\n", encoding="utf-8")
        with self.assertRaises(pip_build.BuildError):
            self.project.patch()
        self.assertEqual(self.target.read_text(), "user edit\n")

    def test_wrong_revision_is_rejected_before_applying(self):
        with patch.dict(pip_build.UPSTREAM, {"commit": "0" * 40}):
            with self.assertRaises(pip_build.BuildError):
                self.project.patch()
        self.assertEqual(self.target.read_text(), "before\n")

    def test_build_refuses_incomplete_bootstrap(self):
        with self.assertRaisesRegex(pip_build.BuildError, "Bootstrap did not complete"):
            self.project.build()
        self.assertEqual(self.target.read_text(), "before\n")

    def test_bootstrap_refuses_tracked_changes(self):
        self.target.write_text("user edit\n", encoding="utf-8")
        with patch.object(self.project, "doctor"):
            with self.assertRaisesRegex(pip_build.BuildError, "tracked changes"):
                self.project.bootstrap()
        self.assertEqual(self.target.read_text(), "user edit\n")


class LeoPolicyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.manifest = self.directory / "package.json"
        self.manifest.write_text(json.dumps({
            "name": "@brave/leo",
            "devDependencies": {"figma-api-exporter": "github:brave/figma-api-exporter#" + compat.EXPORTER_COMMIT},
        }), encoding="utf-8")
        self.workspace = self.directory / "pnpm-workspace.yaml"
        self.workspace.write_text(compat.LEO_WORKSPACE.rstrip("\n"), encoding="utf-8")

    def test_repairs_only_exact_rule_and_is_idempotent(self):
        self.assertTrue(compat.repair_leo(self.directory))
        self.assertFalse(compat.repair_leo(self.directory))
        self.assertEqual(self.workspace.read_text(), compat.LEO_WORKSPACE.rstrip("\n").replace(
            "  figma-api-exporter: true", compat.EXACT_RULE))

    def test_rejects_different_policy_without_mutation(self):
        text = compat.LEO_WORKSPACE + "unexpected: true\n"
        self.workspace.write_text(text, encoding="utf-8")
        with self.assertRaises(RuntimeError):
            compat.repair_leo(self.directory)
        self.assertEqual(self.workspace.read_text(), text)

    def test_ignores_other_packages(self):
        self.manifest.write_text('{"name": "another-package"}', encoding="utf-8")
        self.assertFalse(compat.repair_leo(self.directory))

    def test_rejects_changed_dependency_revision(self):
        self.manifest.write_text(self.manifest.read_text().replace(compat.EXPORTER_COMMIT, "0" * 40), encoding="utf-8")
        with self.assertRaises(RuntimeError):
            compat.repair_leo(self.directory)
        self.assertEqual(self.workspace.read_text(), compat.LEO_WORKSPACE.rstrip("\n"))


if __name__ == "__main__":
    unittest.main()
