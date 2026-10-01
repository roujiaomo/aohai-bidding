import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import radar


class DisabledSourceTests(unittest.TestCase):
    def test_crc_is_marked_disabled_and_has_no_adapter(self):
        crc = next(source for source in radar.SOURCES if source[0] == "crc")
        self.assertEqual("disabled", crc[4])
        self.assertIn("crc", radar.DISABLED_SOURCE_CODES)
        self.assertNotIn("crc", radar.ADAPTERS)

    def test_explicit_crc_fetch_is_rejected_without_creating_a_run(self):
        with tempfile.TemporaryDirectory() as temp:
            conn = radar.connect(Path(temp) / "radar.db")
            radar.init_db(conn)
            radar.seed_sources(conn)

            with self.assertRaisesRegex(ValueError, "crc 已停用"):
                radar.fetch_source(conn, "crc")

            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM fetch_runs WHERE source_code='crc'").fetchone()[0])
            self.assertEqual("disabled", conn.execute("SELECT status FROM sources WHERE code='crc'").fetchone()[0])
            conn.close()

    def test_crc_failure_is_not_counted_or_alerted_but_other_sources_still_are(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "radar.db"
            conn = radar.connect(db)
            radar.init_db(conn)
            radar.seed_sources(conn)
            stamp = "2026-10-01T12:00:00+08:00"
            conn.execute(
                "INSERT INTO fetch_runs(source_code,started_at,finished_at,status,error) VALUES(?,?,?,?,?)",
                ("crc", stamp, stamp, "failed", "old CRC failure"),
            )
            conn.execute(
                "INSERT INTO fetch_runs(source_code,started_at,finished_at,status,error) VALUES(?,?,?,?,?)",
                ("csg", stamp, stamp, "failed", "active source failure"),
            )
            conn.execute("UPDATE sources SET last_error='old CRC failure' WHERE code='crc'")
            conn.execute("UPDATE sources SET last_error='active source failure' WHERE code='csg'")
            conn.commit()
            conn.close()

            args = Namespace(db=db, output=None)
            captured = io.StringIO()
            with patch.object(radar, "load_config", return_value={"rules": {}}), \
                    patch.object(radar, "AI_REVIEW_DB", Path(temp) / "missing-ai.db"), \
                    contextlib.redirect_stdout(captured):
                radar.cmd_quality_report(args)

            report = json.loads(captured.getvalue())
            alerts = report["sources"]["alerts"]
            self.assertNotIn("crc", {item["source"] for item in alerts})
            self.assertIn("csg", {item["source"] for item in alerts})
            self.assertEqual(1, report["sources"]["runtime_failed"])
            self.assertTrue(any(run["source_code"] == "crc" for run in report["sources"]["latest_runs"]))


if __name__ == "__main__":
    unittest.main()
