"""Outbound files: agents hand produced files to the chat via durable delivery."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from openbridge.services.background import parse_file_outputs
from openbridge.services.delivery import OutputService
from openbridge.adapters.channels.telegram.uploads import multipart, read_artifact
from openbridge.adapters.channels.telegram.delivery import TelegramDelivery


def make_delivery(home):
    delivery = TelegramDelivery(Mock(), Mock(), Mock(), Mock(), home=home,
                                uploader=Mock(return_value={"ok": True}))
    delivery.resolve_account.return_value = {"token": "tok"}
    return delivery


class ParseFileOutputs(unittest.TestCase):
    def test_final_report_with_file_and_photo_markers(self):
        text, outputs = parse_file_outputs(
            "DONE. Report produced.\n@file hasil/report.pdf\n@photo hasil/g.png chart per hari\nok.")
        self.assertEqual(text, "DONE. Report produced.\n\nok.")
        self.assertEqual(outputs, [
            {"operation": "document", "path": "hasil/report.pdf", "caption": None},
            {"operation": "photo", "path": "hasil/g.png", "caption": "chart per hari"}])

    def test_plain_text_has_no_outputs(self):
        text, outputs = parse_file_outputs("DONE. hasil/ mentioned but no marker line.")
        self.assertEqual(text, "DONE. hasil/ mentioned but no marker line.")
        self.assertEqual(outputs, [])


class OutboundPathSafety(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        (self.home / "hasil").mkdir()
        (self.home / "hasil" / "report.pdf").write_bytes(b"%PDF-1.4")
        self.delivery = make_delivery(self.home)

    def test_paths_may_resolve_only_inside_home(self):
        from openbridge.adapters.channels.telegram.delivery import outbound_path
        good = outbound_path(self.home, "hasil/report.pdf")
        self.assertTrue(str(good).endswith("report.pdf"))
        self.assertIsNone(outbound_path(self.home, "../secrets.json"))
        self.assertIsNone(outbound_path(self.home, "/etc/passwd"))
        self.assertIsNone(outbound_path(self.home, "hasil/gone.pdf"))

    def test_credentials_inside_bridge_home_are_not_deliverables(self):
        from openbridge.adapters.channels.telegram.delivery import outbound_path
        (self.home / 'private.token').write_text('not-a-real-token')
        self.assertIsNone(outbound_path(self.home, 'private.token'))
        (self.home / 'hasil' / 'alias.txt').symlink_to(self.home / 'private.token')
        self.assertIsNone(outbound_path(self.home, 'hasil/alias.txt'))

    def test_document_delivery_uploads_the_file(self):
        delivery = {"job_id": 1, "id": 1, "account_id": "999", "conversation_id": "777",
                    "payload": {"operation": "document", "path": "hasil/report.pdf",
                                "caption": "laporan"}}
        outcome, error, retry = self.delivery.deliver(delivery)
        self.assertEqual(outcome, "sent")
        self.assertEqual(self.delivery.uploader.call_args[0][1], "sendDocument")
        self.assertIn("chat_id", self.delivery.uploader.call_args[0][2])

    def test_missing_file_is_a_dead_delivery(self):
        delivery = {"job_id": 1, "id": 1, "account_id": "999", "conversation_id": "777",
                    "payload": {"operation": "document", "path": "hasil/gone.pdf", "caption": None}}
        self.assertEqual(self.delivery.deliver(delivery), ("dead", "outbound file missing", None))

    def test_escape_attempt_is_rejected(self):
        delivery = {"job_id": 1, "id": 1, "account_id": "999", "conversation_id": "777",
                    "payload": {"operation": "document", "path": "../../secrets.json", "caption": None}}
        self.assertEqual(self.delivery.deliver(delivery), ("dead", "outbound file missing", None))

    def test_http_400_kills_the_delivery(self):
        self.delivery.uploader.return_value = {"ok": False, "error_code": 400}
        delivery = {"job_id": 1, "id": 1, "account_id": "999", "conversation_id": "777",
                    "payload": {"operation": "document", "path": "hasil/report.pdf", "caption": None}}
        outcome, error, retry = self.delivery.deliver(delivery)
        self.assertEqual((outcome, retry), ("dead", None))
        self.assertIn("400", error)


class MultipartEncoding(unittest.TestCase):
    def test_bounded_reader_refuses_symlinks_and_oversize_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'data.txt'
            path.write_bytes(b'12345')
            self.assertEqual(read_artifact(path, 5), b'12345')
            with self.assertRaises(ValueError):
                read_artifact(path, 4)
            alias = root / 'alias.txt'
            alias.symlink_to(path)
            with self.assertRaises(OSError):
                read_artifact(alias, 10)

    def test_boundary_is_unpredictable_and_filename_cannot_inject_headers(self):
        first, body = multipart({'chat_id': '777'}, 'document', 'bad"\r\nX-Fake: yes.txt', b'data')
        second, _ = multipart({'chat_id': '777'}, 'document', 'ok.txt', b'data')
        self.assertNotEqual(first, second)
        self.assertNotIn(b'\r\nX-Fake:', body)

    def test_fields_and_file_chunks(self):
        content_type, body = multipart({"chat_id": "777", "caption": "hi"}, "document",
                                       "report.pdf", b"%PDF-1.4")
        self.assertIn("multipart/form-data", content_type)
        self.assertIn(b'name="chat_id"', body)
        self.assertIn(b"777", body)
        self.assertIn(b'filename="report.pdf"', body)
        self.assertIn(b"%PDF-1.4", body)


class OutputServiceFiles(unittest.TestCase):
    def setUp(self):
        self.runtime, self.channel = Mock(), Mock()
        self.channel.channel = "telegram"
        self.runtime.current_job.return_value = {"channel": "telegram", "account_id": "999",
                                                 "conversation_id": "777"}
        self.service = OutputService(self.runtime, self.channel)

    def test_in_scope_files_become_durable_outputs(self):
        self.service.send_file("999", "777", "hasil/x.pdf", "laporan")
        output = self.runtime.emit.call_args[0][0]
        self.assertEqual(output["operation"], "document")
        self.assertEqual(output["path"], "hasil/x.pdf")
        self.assertEqual(output["caption"], "laporan")
        self.channel.send_file.assert_not_called()

    def test_out_of_scope_files_go_straight_to_the_channel(self):
        self.runtime.current_job.return_value = None
        self.service.send_file("999", "777", "hasil/x.pdf", "laporan")
        self.channel.send_file.assert_called_once_with("999", "777", "hasil/x.pdf", "laporan")
        self.runtime.emit.assert_not_called()


class ExecutorHandsFilesOver(unittest.TestCase):
    def test_done_report_with_marker_emits_both_text_and_file(self):
        from openbridge.services.background import BackgroundExecutor
        jobs, session = Mock(), Mock()
        jobs.completed_steps.return_value = []
        emitted, files = [], []
        executor = BackgroundExecutor(jobs, session,
            Mock(return_value="DONE. Report ready.\n@file hasil/report.pdf catatan hari ini"),
            lambda text: emitted.append(text),
            lambda path, caption, kind: files.append((path, caption, kind)))
        job = {"id": 5, "kind": "task"}
        executor.execute(job, {"task_id": 2, "goal": "g", "max_steps": 1, "wait_seconds": 1})
        self.assertEqual(emitted, ["📋 Task #2 done\nDONE. Report ready."])
        self.assertEqual(files, [("hasil/report.pdf", "catatan hari ini", "document")])

    def test_plain_reports_do_not_scan_into_files(self):
        from openbridge.services.background import BackgroundExecutor
        jobs, session = Mock(), Mock()
        jobs.completed_steps.return_value = []
        emitted, files = [], []
        executor = BackgroundExecutor(jobs, session,
            Mock(return_value="DONE. Mentioned hasil/x.pdf without a marker line."),
            lambda text: emitted.append(text),
            lambda path, caption, kind: files.append((path, caption, kind)))
        executor.execute({"id": 5, "kind": "task"}, {"task_id": 2, "goal": "g", "max_steps": 1, "wait_seconds": 1})
        self.assertEqual(files, [])
        self.assertIn("x.pdf", emitted[0])


if __name__ == "__main__":
    unittest.main()
