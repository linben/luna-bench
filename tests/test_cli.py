import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from lunabench.__main__ import Bench, build_parser, cmd_report, main


class Artifacts(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "samples.jsonl"

    def bench(self, **kwargs):
        args = build_parser().parse_args([
            "run", "--targets", "openai", "--out", str(self.path),
            "--scenarios", "short", "--concurrency", "3",
        ])
        with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-test-credential"}):
            bench = Bench(args, "run", event=kwargs.get("event", lambda *a, **k: None))
        self.addCleanup(bench.pool.close_all)
        self.addCleanup(bench._fh.close)
        return bench

    @unittest.skipUnless(os.name == "posix", "POSIX file permissions")
    def test_artifacts_are_private_under_permissive_umask(self):
        old_umask = os.umask(0)
        try:
            self.bench().finish()
        finally:
            os.umask(old_umask)
        for path in (self.path, Path(str(self.path) + ".md")):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_existing_report_is_preserved(self):
        bench = self.bench()
        report = Path(str(self.path) + ".md")
        report.write_text("previous report", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            bench.finish()
        self.assertEqual(report.read_text(encoding="utf-8"), "previous report")

    @unittest.skipUnless(os.name == "posix", "POSIX symlinks")
    def test_report_symlink_cannot_replace_another_file(self):
        bench = self.bench()
        victim = Path(self.directory.name) / "unrelated.txt"
        victim.write_text("keep this", encoding="utf-8")
        Path(str(self.path) + ".md").symlink_to(victim)
        with self.assertRaises(FileExistsError):
            bench.finish()
        self.assertEqual(victim.read_text(encoding="utf-8"), "keep this")

    def test_report_export_does_not_overwrite_its_input(self):
        self.path.write_text('{}\n', encoding="utf-8")
        args = build_parser().parse_args([
            "report", str(self.path), "--markdown", str(self.path),
        ])
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(FileExistsError):
            cmd_report(args)
        self.assertEqual(self.path.read_text(encoding="utf-8"), '{}\n')

    def test_warmup_drains_successes_and_preserves_first_failure(self):
        events = []
        bench = self.bench(event=lambda kind, **data: events.append((kind, data)))

        def request(target, api, index, *args):
            if index == 0:
                raise ValueError("first failure")
            if index == 2:
                raise RuntimeError("later failure")
            return {"request_index": index, "status": 200}

        with patch.object(bench, "one_request", side_effect=request):
            with ThreadPoolExecutor(max_workers=3) as executor:
                with self.assertRaisesRegex(ValueError, "first failure"):
                    bench.warmup(executor, bench.targets[0], "chat")
        self.assertEqual(events, [("warmup_result", {"sample": {"request_index": 1, "status": 200}})])


class ReportInput(unittest.TestCase):
    def test_invalid_rows_fail_with_line_context_without_echoing_content(self):
        invalid_rows = [
            "not-json-SENSITIVE-CANARY",
            json.dumps(["SENSITIVE-CANARY"]),
            json.dumps({"target": ["SENSITIVE-CANARY"]}),
            json.dumps({"outcome": {"secret": "SENSITIVE-CANARY"}}),
            json.dumps({"schema_version": "SENSITIVE-CANARY"}),
            json.dumps({"valid_output": "SENSITIVE-CANARY"}),
            '{"arrival_rate": NaN}',
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.jsonl"
            for row in invalid_rows:
                with self.subTest(row=row):
                    path.write_text("\n" + row + "\n", encoding="utf-8")
                    errors = io.StringIO()
                    with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as raised:
                        main(["report", str(path)])
                    self.assertEqual(raised.exception.code, 2)
                    self.assertIn(f"{path}:2:", errors.getvalue())
                    self.assertNotIn("SENSITIVE-CANARY", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
