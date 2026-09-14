import importlib.util
from pathlib import Path
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location("instruction_updates", Path(__file__).resolve().parents[1] / "scripts/apply_instruction_updates.py")
UPDATES = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(UPDATES)


class InstructionUpdateTests(unittest.TestCase):
    def fixture(self, root):
        content = b"---\r\nname: demo\r\n---\r\nOld sentence\r\n"
        entries = []
        for name in ("demo", "duplicate"):
            relative = f".agents/skills/{name}/SKILL.md"
            target = root / relative
            target.parent.mkdir(parents=True)
            target.write_bytes(content)
            entry = {"path": relative, "sha256": UPDATES.digest(content)}
            if name == "duplicate":
                entry["disable"] = True
            else:
                entry["edits"] = [{"old": "Old sentence", "new": "New sentence"}]
            entries.append(entry)
        return {"id": "test-instructions", "entries": entries}, content

    def test_apply_idempotent_and_byte_exact_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, original = self.fixture(root)
            planned = UPDATES.prepare(root, manifest)
            self.assertEqual(len(planned), 2)
            journal = UPDATES.apply(root, manifest, planned)
            self.assertTrue(journal.exists())
            self.assertEqual(UPDATES.prepare(root, manifest), [])
            self.assertIn(b"New sentence\r\n", (root / manifest["entries"][0]["path"]).read_bytes())
            self.assertFalse((root / manifest["entries"][1]["path"]).exists())
            UPDATES.rollback(root, manifest)
            UPDATES.rollback(root, manifest)
            for entry in manifest["entries"]:
                self.assertEqual((root / entry["path"]).read_bytes(), original)
            self.assertEqual(len(UPDATES.prepare(root, manifest)), 2)

    def test_drift_blocks_before_any_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, original = self.fixture(root)
            (root / manifest["entries"][1]["path"]).write_bytes(b"new upstream version")
            with self.assertRaises(ValueError):
                UPDATES.prepare(root, manifest)
            self.assertEqual((root / manifest["entries"][0]["path"]).read_bytes(), original)
            self.assertFalse(UPDATES.state_directory(root, manifest).exists())

    def test_rollback_does_not_overwrite_later_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, _ = self.fixture(root)
            UPDATES.apply(root, manifest, UPDATES.prepare(root, manifest))
            target = root / manifest["entries"][0]["path"]
            target.write_bytes(b"user changes")
            with self.assertRaises(ValueError):
                UPDATES.rollback(root, manifest)
            self.assertEqual(target.read_bytes(), b"user changes")
            self.assertFalse((root / manifest["entries"][1]["path"]).exists())

    def test_bad_path_and_ambiguous_replacement_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in (".agents/skills/../../outside", "unrelated/file", "C:/outside"):
                with self.assertRaises(ValueError):
                    UPDATES.target_path(root, name)
        entry = {"path": "demo", "edits": [{"old": "same", "new": "new"}]}
        with self.assertRaises(ValueError):
            UPDATES.render(b"same same", entry)
        with self.assertRaises(ValueError):
            UPDATES.render(b"missing", entry)
