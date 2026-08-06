import json
import queue
import threading
import unittest
import urllib.request
from collections import defaultdict, deque

from desktop_app import BridgeServer, ReplyEngine, bounded_context, clean_reply, normalize_text


class _Message:
    content = "أهلاً، تم استلام رسالتك"


class _Choice:
    message = _Message()


class _Completion:
    choices = [_Choice()]


class _Completions:
    def create(self, **kwargs):
        self.kwargs = kwargs
        return _Completion()


class _Chat:
    def __init__(self):
        self.completions = _Completions()


class _Client:
    def __init__(self):
        self.chat = _Chat()


def fake_engine(settings=None):
    engine = ReplyEngine.__new__(ReplyEngine)
    engine.settings = settings or {
        "target_sender": "",
        "max_context_chars": 6000,
        "model": "test-model",
        "persona": "reply naturally",
        "task_rules": "plain text only",
        "dry_run": False,
        "reply_delay_seconds": 0,
        "history_messages": 40,
    }
    engine.log = lambda *_: None
    engine.client = _Client()
    engine.history = defaultdict(lambda: deque(maxlen=32))
    engine.processed = {}
    engine.lock = threading.Lock()
    return engine


class BridgeTests(unittest.TestCase):
    def test_arabic_and_rtl_normalization(self):
        self.assertEqual(normalize_text("\u200fمرحبا\r\nبك"), "مرحبا\nبك")

    def test_media_placeholder(self):
        self.assertEqual(normalize_text("تم حذف الوسائط"), "[MEDIA_ATTACHMENT]")

    def test_context_is_bounded(self):
        text, truncated = bounded_context([f"رسالة {i}" for i in range(30)], 1000)
        self.assertTrue(truncated)
        self.assertNotIn("رسالة 0\n", text)
        self.assertIn("رسالة 29", text)

    def test_reply_and_duplicate_protection(self):
        engine = fake_engine()
        payload = {"id": "msg-1", "sender": "أحمد", "chatTitle": "أحمد", "text": "مرحبا"}
        first = engine.reply(payload)
        second = engine.reply(payload)
        self.assertTrue(first["send"])
        self.assertIn("أهلاً", first["reply"])
        self.assertTrue(second["duplicate"])
        self.assertFalse(second["send"])

    def test_media_instruction_reaches_model(self):
        engine = fake_engine()
        result = engine.reply({"id": "m2", "sender": "Ali", "text": "", "mediaType": "video"})
        prompt = engine.client.chat.completions.kwargs["messages"][-1]["content"]
        self.assertTrue(result["ok"])
        self.assertIn("attachment", prompt.lower())

    def test_sender_filter(self):
        settings = fake_engine().settings.copy()
        settings["target_sender"] = "Allowed"
        engine = fake_engine(settings)
        result = engine.reply({"id": "m3", "sender": "Other", "chatTitle": "Other", "text": "hello"})
        self.assertTrue(result["ignored"])
        self.assertFalse(result["send"])

    def test_http_status(self):
        settings = fake_engine().settings.copy()
        settings.update({"bridge_port": 18765, "request_timeout": 5, "base_url": "", "dry_run": True})
        bridge = BridgeServer.__new__(BridgeServer)
        bridge.settings = settings
        bridge.output = queue.Queue()
        bridge.engine = fake_engine(settings)
        bridge.httpd = None
        bridge.thread = None
        bridge.start()
        try:
            with urllib.request.urlopen("http://127.0.0.1:18765/v1/status", timeout=2) as response:
                body = json.load(response)
            self.assertTrue(body["running"])
            self.assertTrue(body["dryRun"])
        finally:
            bridge.stop()

    def test_clean_reply(self):
        self.assertEqual(clean_reply('"أهلاً وسهلاً"'), "أهلاً وسهلاً")


if __name__ == "__main__":
    unittest.main()
