import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from application_feed import save_filtered_jobs


def job(key="A", title="Clinical Fellow"):
    return {"ID": key, "Title": title,
            "Link": "https://www.jobs.nhs.uk/candidate/jobadvert/" + key}


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "filtered_jobs.json"

    def test_cumulative_deduplicated_public_fields(self):
        save_filtered_jobs([job() | {"private": "must not copy"}], self.path)
        first = json.loads(self.path.read_text())["jobs"][0]
        save_filtered_jobs([job(), job("B")], self.path)
        rows = json.loads(self.path.read_text())["jobs"]
        self.assertEqual([r["ID"] for r in rows], ["A", "B"])
        self.assertEqual(rows[0], first)
        self.assertEqual(set(first), {"ID", "Title", "Link", "first_seen_at"})

    def test_quiet_run_unchanged(self):
        save_filtered_jobs([job()], self.path)
        before = self.path.read_bytes()
        save_filtered_jobs([], self.path)
        self.assertEqual(before, self.path.read_bytes())

    def test_corruption_not_overwritten(self):
        self.path.write_text("broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            save_filtered_jobs([job()], self.path)
        self.assertEqual(self.path.read_text(), "broken")

    def test_failed_replace_preserves_original(self):
        save_filtered_jobs([job()], self.path)
        before = self.path.read_bytes()
        with patch("application_feed.os.replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                save_filtered_jobs([job("B")], self.path)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_monitor_exports_exact_telegram_selection(self):
        with patch.dict(os.environ, TELEGRAM_TOKEN="test", TELEGRAM_CHAT_ID="test"):
            import job_monitor as monitor
        incoming = [job(), job("B", "Consultant Physician"), job("C", "Staff Nurse")]
        with patch.object(monitor, "load_previous_job_ids", return_value={"old"}), \
             patch.object(monitor, "scrape_new_jobs", return_value=(incoming, {"A", "B", "C"}, True)), \
             patch.object(monitor, "is_in_failure_state", return_value=False), \
             patch.object(monitor, "notify_new_jobs") as notify, \
             patch.object(monitor, "save_current_job_ids") as save_seen, \
             patch.object(monitor, "save_filtered_jobs") as export:
            monitor.monitor()
            export.assert_called_once_with([job()])
            notify.assert_called_once_with(export.call_args.args[0])
            save_seen.assert_called_once_with({"old", "A", "B", "C"})
            export.side_effect = OSError("disk full")
            save_seen.reset_mock()
            notify.reset_mock()
            with self.assertRaises(OSError):
                monitor.monitor()
            save_seen.assert_not_called()
            notify.assert_not_called()

    def test_scrape_failure_does_not_export(self):
        with patch.dict(os.environ, TELEGRAM_TOKEN="test", TELEGRAM_CHAT_ID="test"):
            import job_monitor as monitor
        with patch.object(monitor, "load_previous_job_ids", return_value={"old"}), \
             patch.object(monitor, "scrape_new_jobs", return_value=([], set(), False)), \
             patch.object(monitor, "is_in_failure_state", return_value=True), \
             patch.object(monitor, "save_filtered_jobs") as export:
            monitor.monitor()
            export.assert_not_called()


if __name__ == "__main__":
    unittest.main()
