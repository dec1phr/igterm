import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import igterm as ig  # noqa: E402


class TextTests(unittest.TestCase):
    def test_clean_strips_escapes_and_bidi(self):
        self.assertNotIn("\x1b", ig.clean("hi\x1b[31mred\x1b[0m"))
        self.assertEqual(ig.clean("a‮b​c"), "abc")
        self.assertEqual(ig.clean("a\tb"), "a    b")
        self.assertEqual(ig.clean("\x9b31m"), "?31m")  # C1 CSI
        self.assertEqual(ig.clean(None), "")

    def test_wrap(self):
        self.assertEqual(ig.wrap("", 10), [""])
        self.assertEqual(ig.wrap("hello world foo", 11), ["hello world", "foo"])
        self.assertEqual(ig.wrap("a" * 25, 10), ["a" * 10, "a" * 10, "a" * 5])
        for line in ig.wrap("日本語のテキストです " * 5, 12):
            self.assertLessEqual(ig.swidth(line), 12)
        self.assertEqual(ig.wrap("a\nb", 5), ["a", "b"])
        self.assertEqual(ig.wrap("x", 0), ["x"])

    def test_clip(self):
        self.assertEqual(ig.clip("日本語", 5), "日本")
        self.assertEqual(ig.clip("abc", 0), "")


class ParseTests(unittest.TestCase):
    def item(self, **kw):
        d = {"item_id": "9", "user_id": 5, "timestamp": 1700000000000000}
        d.update(kw)
        return ig.parse_item(d)

    def test_text(self):
        m = self.item(item_type="text", text="hello")
        self.assertEqual((m.id, m.user_id, m.text), ("9", "5", "hello"))
        self.assertAlmostEqual(m.ts, 1700000000.0)

    def test_clip_becomes_reel_link(self):
        m = self.item(item_type="clip",
                      clip={"clip": {"code": "CxYz123abc", "product_type": "clip"}})
        self.assertEqual(m.text, "[reel] https://www.instagram.com/reel/CxYz123abc/")

    def test_media_share_post(self):
        m = self.item(item_type="media_share",
                      media_share={"code": "Bx12345abcd", "product_type": "feed"})
        self.assertEqual(m.text, "[post] https://www.instagram.com/p/Bx12345abcd/")

    def test_xma_link_strips_tracking_query(self):
        m = self.item(item_type="xma_reel_share", xma_reel_share=[
            {"target_url": "https://www.instagram.com/reel/AbCdEf12/?igsh=zzz&utm=1"}])
        self.assertEqual(m.text, "[reel] https://www.instagram.com/reel/AbCdEf12/")

    def test_cdn_urls_ignored(self):
        m = self.item(item_type="clip", clip={"url": "https://scontent.cdninstagram.com/x.jpg"})
        self.assertEqual(m.text, "[reel]")
        m = self.item(item_type="media", media={"url": "https://scontent.cdninstagram.com/x.jpg"})
        self.assertEqual(m.text, "[photo/video]")

    def test_link_and_like_and_unknown(self):
        m = self.item(item_type="link", link={"text": "see", "link_context": {"link_url": "https://e.com/x"}})
        self.assertEqual(m.text, "see https://e.com/x")
        self.assertEqual(self.item(item_type="like").text, "<3")
        self.assertEqual(self.item(item_type="brand_new_thing").text, "[brand new thing]")
        self.assertEqual(self.item(item_type="voice_media").text, "[voice message]")

    def test_story_reply_and_reply_quote(self):
        m = self.item(item_type="reel_share", reel_share={"text": "nice!", "type": "reply"})
        self.assertEqual(m.text, "[story reply] nice!")
        m = self.item(item_type="text", text="ok", replied_to_message={"text": "you coming?"})
        self.assertEqual(m.text, '(re: "you coming?") ok')

    def test_malformed_never_raises(self):
        self.assertIsNone(ig.parse_item({}))
        self.assertEqual(self.item(item_type=None).text, "[message]")
        self.assertEqual(self.item(item_type="text", text=None).text, "")
        m = ig.parse_item({"item_id": 1, "timestamp": "garbage", "item_type": "text", "text": "x"})
        self.assertEqual(m.ts, 0.0)

    def test_text_is_sanitised(self):
        self.assertNotIn("\x1b", self.item(item_type="text", text="\x1b]0;pwn\x07").text)

    def test_parse_thread(self):
        t = ig.parse_thread({
            "thread_id": "123", "is_group": False, "last_activity_at": 1700000000000000,
            "users": [{"pk": 7, "username": "alice"}],
            "items": [{"item_id": "1", "user_id": 7, "timestamp": 1700000000000000,
                       "item_type": "text", "text": "yo"}],
            "oldest_cursor": "c", "has_older": True})
        self.assertEqual((t.id, t.title, t.users), ("123", "alice", {"7": "alice"}))
        self.assertTrue(t.has_older)
        self.assertEqual(len(t.msgs), 1)
        g = ig.parse_thread({"thread_id": "9", "is_group": True, "thread_title": "Crew",
                             "users": [{"pk": 1, "username": "a"}, {"pk": 2, "username": "b"}]})
        self.assertEqual(g.title, "Crew")
        self.assertIsNone(ig.parse_thread({}))


class DemoBackendTests(unittest.TestCase):
    def test_roundtrip(self):
        b = ig.DemoBackend()
        n = len(b.inbox())
        b.send("t1", "hi")
        threads = {t.id: t for t in b.inbox()}
        self.assertTrue(any(m.text == "echo: hi" for m in threads["t1"].msgs.values()))
        b.start_dm("dave", "yo")
        self.assertEqual(len(b.inbox()), n + 1)


if __name__ == "__main__":
    unittest.main()
