#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WebUI 里几个纯函数的测试（不起服务）"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import piano  # noqa: E402
import webui  # noqa: E402


class SafeNameTest(unittest.TestCase):
    def test_strips_path_characters(self):
        self.assertEqual(webui.safe_name("a/b\\c:d*e?f"), "abcdef")
        self.assertEqual(webui.safe_name("haruhikage2.txt"), "haruhikage2.txt")

    def test_keeps_chinese_and_spaces(self):
        self.assertEqual(webui.safe_name("春日 影"), "春日 影")

    def test_falls_back_to_untitled(self):
        self.assertEqual(webui.safe_name(""), "untitled")
        self.assertEqual(webui.safe_name("   "), "untitled")
        self.assertEqual(webui.safe_name("..."), "untitled")

    def test_length_is_capped(self):
        self.assertLessEqual(len(webui.safe_name("x" * 200)), 60)


class RenderPayloadTest(unittest.TestCase):
    def test_builds_txt_from_ticks(self):
        payload = {
            "title": "t",
            "bpm": 120,
            "meter": [3, 8],
            "notes": [
                {"tick": 0, "ticks": 240, "midis": [60], "voice": "1"},
                {"tick": 240, "ticks": 240, "midis": [64, 67], "voice": "1"},
            ],
        }
        text = webui.render_payload(payload)
        self.assertIn("@meter 3/8", text)
        score = piano.parse_txt(text)
        self.assertEqual([tuple(e.midis) for e in score.events], [(60,), (64, 67)])
        self.assertEqual(score.meter, (3, 8))

    def test_keeps_voice_names(self):
        payload = {
            "title": "t",
            "bpm": 120,
            "notes": [
                {"tick": 0, "ticks": 480, "midis": [52, 56, 59], "voice": "伴奏"},
                {"tick": 0, "ticks": 480, "midis": [71], "voice": "旋律"},
            ],
        }
        text = webui.render_payload(payload)
        self.assertIn("@voice 伴奏", text)
        self.assertIn("@voice 旋律", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
