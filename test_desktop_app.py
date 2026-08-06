import unittest

from desktop_app import (
    bounded_chat_history,
    clean_reply,
    is_last_message_from_sender,
    last_message,
    message_fingerprint,
    normalize_text,
    split_messages,
)


class WhatsAppParsingTests(unittest.TestCase):
    def test_legacy_english_timestamp(self):
        chat = "[9:25 AM, 8/18/2026] Alice: hello\n[9:26 AM, 8/18/2026] Bob: hi"
        self.assertEqual(len(split_messages(chat)), 2)
        self.assertTrue(is_last_message_from_sender(chat, "Bob"))

    def test_arabic_timestamp_digits_and_rtl_marks(self):
        chat = "[٠٧/٠٨/٢٠٢٦، ١٢:٣٤ م] أحمد: مرحبا\n[٠٧/٠٨/٢٠٢٦، ١٢:٣٥ م] \u200fمحمد: كيف حالك؟"
        self.assertEqual(len(split_messages(chat)), 2)
        self.assertTrue(is_last_message_from_sender(chat, "محمد"))
        self.assertIn("كيف حالك", last_message(chat))

    def test_media_is_replaced_and_deduplicated(self):
        chat = "[12:34, 8/7/2026] أحمد: <Media omitted>"
        normalized = normalize_text(chat)
        self.assertIn("[MEDIA_ATTACHMENT]", normalized)
        prompt, truncated = bounded_chat_history(chat, 6000)
        self.assertFalse(truncated)
        self.assertIn("pixels are unavailable", prompt)
        self.assertEqual(message_fingerprint(chat), message_fingerprint(chat))

    def test_arabic_media_label(self):
        chat = "[٠٧/٠٨/٢٠٢٦، ١٢:٣٤ م] أحمد: تم حذف الوسائط"
        self.assertIn("[MEDIA_ATTACHMENT]", normalize_text(chat))
        self.assertTrue(is_last_message_from_sender(chat, "أحمد"))

    def test_long_arabic_context_is_bounded(self):
        long_text = "مرحبا " * 5000
        chat = f"[٠٧/٠٨/٢٠٢٦، ١٢:٣٤ م] أحمد: {long_text}"
        prompt, truncated = bounded_chat_history(chat, 6000)
        self.assertTrue(truncated)
        self.assertLessEqual(len(prompt), 6000)
        self.assertTrue(prompt.endswith("مرحبا ".strip()) or "مرحبا" in prompt[-10:])

    def test_recent_message_limit(self):
        chat = "\n".join(f"[12:{i:02d}, 8/7/2026] A: message {i}" for i in range(30))
        prompt, truncated = bounded_chat_history(chat, 10000)
        self.assertTrue(truncated)
        self.assertNotIn("message 0\n", prompt)
        self.assertIn("message 29", prompt)

    def test_clean_reply_keeps_arabic(self):
        self.assertEqual(clean_reply('"أهلاً وسهلاً"'), "أهلاً وسهلاً")


if __name__ == "__main__":
    unittest.main()
