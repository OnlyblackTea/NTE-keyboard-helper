#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""piano.py 的单元测试：python -m unittest discover -s tests -v"""

import contextlib
import io
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import piano  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class KeymapTest(unittest.TestCase):
    """键位表必须严格符合题目给的三行映射"""

    def test_row_endpoints(self):
        cases = (
            ("1", "E5"), ("=", "D#6"),
            ("q", "E4"), ("]", "D#5"),
            ("a", "E3"), ("enter", "D#4"),
        )
        for key, name in cases:
            with self.subTest(key=key):
                self.assertEqual(piano.midi_name(piano.KEY_TO_MIDI[key]), name)

    def test_each_row_starts_on_e_and_runs_chromatically(self):
        for keys, base in piano._ROW_SPEC:
            self.assertEqual(base % 12, 4, "每行都要从 E 开始")
            self.assertEqual(piano.midi_name(base)[0], "E")
            for i, key in enumerate(keys):
                self.assertEqual(piano.KEY_TO_MIDI[key], base + i)
            self.assertEqual(len(keys) + (1 if base == 52 else 0), 12)

    def test_whole_keyboard_is_contiguous(self):
        self.assertEqual(sorted(piano.KEY_TO_MIDI.values()), list(range(52, 88)))
        self.assertEqual(len(piano.KEY_TO_MIDI), 36)

    def test_note_names(self):
        self.assertEqual(piano.midi_name(60), "C4")
        self.assertEqual(piano.midi_name(87), "D#6")
        self.assertEqual(piano.note_name_to_midi("C4"), 60)
        self.assertEqual(piano.note_name_to_midi("F#5"), 78)
        self.assertEqual(piano.note_name_to_midi("Bb3"), 58)

    def test_virtual_key_codes(self):
        cases = {
            52: 0x41,   # A
            60: 0x4C,   # L
            63: 0x0D,   # Enter
            64: 0x51,   # Q
            74: 0xDB,   # [
            75: 0xDD,   # ]
            76: 0x31,   # 1
            87: 0xBB,   # =
        }
        for midi, vk in cases.items():
            with self.subTest(midi=midi):
                self.assertEqual(piano.MIDI_TO_VK[midi], vk)

    def test_every_key_has_a_vk(self):
        self.assertEqual(len(piano.MIDI_TO_VK), len(piano.KEY_TO_MIDI))
        self.assertEqual(piano.MIDI_TO_KEY[63], "ENTER")

    @unittest.skipUnless(piano._IS_WINDOWS, "只在 Windows 上检查 SendInput 结构体")
    def test_sendinput_struct_size(self):
        # Win32 的 INPUT 在 x64 上是 40 字节，结构体对不上时 SendInput 会直接失败
        import ctypes
        expected = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
        self.assertEqual(ctypes.sizeof(piano._INPUT), expected)


class ParseTxtTest(unittest.TestCase):
    def test_single_note_defaults_to_one_beat(self):
        score = piano.parse_txt("@tempo 120\nq")
        self.assertEqual(len(score.events), 1)
        event = score.events[0]
        self.assertEqual(event.midis, (64,))
        self.assertAlmostEqual(event.time, 0.0)
        self.assertAlmostEqual(event.dur, 0.5)

    def test_juxtaposition_is_a_chord(self):
        score = piano.parse_txt("@tempo 120\nlqr")
        self.assertEqual(score.events[0].midis, (60, 64, 67))
        self.assertEqual(score.events[0].labels, "L+Q+R")

    def test_braces_are_optional(self):
        self.assertEqual(piano.parse_txt("lqr").events[0].midis,
                         piano.parse_txt("{lqr}").events[0].midis)

    def test_duration_suffix(self):
        score = piano.parse_txt("@tempo 120\nq*2 w*0.5")
        self.assertAlmostEqual(score.events[0].dur, 1.0)
        self.assertAlmostEqual(score.events[1].dur, 0.25)
        self.assertAlmostEqual(score.events[1].time, 1.0)

    def test_dot_extends_previous_note(self):
        score = piano.parse_txt("@tempo 120\nl*2 . r")
        self.assertAlmostEqual(score.events[0].dur, 1.5)   # 3 拍
        self.assertAlmostEqual(score.events[1].time, 1.5)

    def test_rest(self):
        score = piano.parse_txt("@tempo 120\nq _ r")
        self.assertEqual(len(score.events), 2)
        self.assertAlmostEqual(score.events[1].time, 1.0)

    def test_bar_lines_do_not_affect_rhythm(self):
        plain = piano.parse_txt("q w r")
        barred = piano.parse_txt("q | w | r")
        self.assertEqual([e.midis for e in plain.events], [e.midis for e in barred.events])
        self.assertAlmostEqual(plain.events[2].time, barred.events[2].time)

    def test_comments_and_blank_lines(self):
        self.assertEqual(len(piano.parse_txt("# 标题\n\nq  # 一个音\n").events), 1)

    def test_enter_key(self):
        self.assertEqual(piano.parse_txt("Enter").events[0].midis, (63,))
        self.assertEqual(piano.parse_txt("qEnter").events[0].midis, (64, 63))

    def test_transpose(self):
        score = piano.parse_txt("q", transpose=12)
        self.assertEqual(score.events[0].midis, (76,))
        self.assertEqual(piano.midi_name(score.events[0].midis[0]), "E5")

    def test_voices_are_independent_and_merged(self):
        text = "@tempo 120\n@voice 伴奏\nl*4\n@voice 旋律\nq*4\n"
        score = piano.parse_txt(text)
        self.assertEqual(len(score.events), 2)
        self.assertEqual(sorted(e.beat for e in score.events), [0.0, 0.0])
        self.assertEqual(len(score.voices), 2)

    def test_voice_cursor_is_per_voice(self):
        text = "@tempo 120\n@voice bass\na*4\n@voice lead\n_*2 q\n"
        score = piano.parse_txt(text)
        lead = [e for e in score.events if e.voice == "lead"][0]
        bass = [e for e in score.events if e.voice == "bass"][0]
        self.assertAlmostEqual(bass.time, 0.0)
        self.assertAlmostEqual(lead.time, 1.0)   # 2 拍休止 = 1 秒

    def test_tempo_change_mid_score(self):
        score = piano.parse_txt("@tempo 60\nq\n@tempo 120\nq")
        self.assertAlmostEqual(score.events[0].dur, 1.0)
        self.assertAlmostEqual(score.events[1].time, 1.0)
        self.assertAlmostEqual(score.events[1].dur, 0.5)

    def test_tempo_scaling(self):
        score = piano.parse_txt("@tempo 60\nq q")
        self.assertAlmostEqual(score.duration, 2.0)
        score.rescale_tempo(120)
        self.assertAlmostEqual(score.duration, 1.0)
        self.assertAlmostEqual(score.events[0].dur, 0.5)

    def test_gate_directive(self):
        self.assertAlmostEqual(piano.parse_txt("@gate 0.5\nq").gate, 0.5)
        self.assertAlmostEqual(piano.parse_txt("q").gate, piano.DEFAULT_GATE)


class ParseCsvTest(unittest.TestCase):
    def test_note_names_and_keys(self):
        score = piano.parse_csv("0,1,C4\n1,1,lqr\n2,2,E4\n")
        self.assertEqual([e.midis for e in score.events],
                         [(60,), (60, 64, 67), (64,)])
        self.assertAlmostEqual(score.events[2].dur, 1.0)

    def test_chord_on_same_beat(self):
        score = piano.parse_csv("0,2,C5\n0,2,E4\n")
        self.assertEqual(len(score.events), 1)          # 同一拍的多行并成一个和弦
        self.assertEqual(score.events[0].midis, (64, 72))
        self.assertAlmostEqual(score.events[0].time, 0.0)

    def test_header_and_comments_are_skipped(self):
        score = piano.parse_csv("# 注释\nbeat,dur,note\n@tempo 60\n0,1,C4\n")
        self.assertEqual(len(score.events), 1)
        self.assertAlmostEqual(score.base_bpm, 60.0)


class ScheduleTest(unittest.TestCase):
    def test_note_becomes_down_then_up(self):
        score = piano.parse_txt("@tempo 120\nl")
        actions, skipped = piano.build_schedule(score)
        self.assertEqual(skipped, 0)
        self.assertEqual([a.down for a in actions], [True, False])
        self.assertAlmostEqual(actions[0].time, 0.0)
        self.assertAlmostEqual(actions[1].time, 0.5 * piano.DEFAULT_GATE)
        self.assertEqual({a.vk for a in actions}, {0x4C})

    def test_chord_presses_all_keys_at_once(self):
        score = piano.parse_txt("lqr")
        actions, _ = piano.build_schedule(score)
        downs = [a for a in actions if a.down]
        self.assertEqual(len(downs), 3)
        self.assertEqual(len({a.time for a in downs}), 1)
        self.assertEqual({a.vk for a in downs}, {0x4C, 0x51, 0x52})

    def test_gate_override(self):
        score = piano.parse_txt("@tempo 120\nl")
        actions, _ = piano.build_schedule(score, gate=0.5)
        self.assertAlmostEqual(actions[1].time, 0.25)

    def test_min_hold(self):
        score = piano.parse_txt("@tempo 120\nl")
        actions, _ = piano.build_schedule(score, gate=0.001)
        self.assertAlmostEqual(actions[1].time, piano.MIN_HOLD)

    def test_release_happens_before_next_press_at_same_instant(self):
        score = piano.parse_txt("@tempo 120\nl l")
        actions, _ = piano.build_schedule(score, gate=1.0)
        at_half = [a for a in actions if abs(a.time - 0.5) < 1e-9]
        self.assertEqual([a.down for a in at_half], [False, True])

    def test_out_of_range_notes_are_skipped(self):
        score = piano.parse_txt("l", transpose=60)
        actions, skipped = piano.build_schedule(score)
        self.assertEqual(skipped, 1)
        self.assertEqual(actions, [])

    def test_overlapping_voices_keep_their_own_releases(self):
        text = "@tempo 120\n@voice a\nl*4\n@voice b\n_*1 l*1\n"
        score = piano.parse_txt(text)
        actions, _ = piano.build_schedule(score)
        downs = [a for a in actions if a.down]
        ups = [a for a in actions if not a.down]
        self.assertEqual(len(downs), 2)
        self.assertEqual(len(ups), 2)


class RunnerTest(unittest.TestCase):
    def test_run_schedule_presses_in_order(self):
        score = piano.parse_txt("@tempo 600\nl r")   # 0.1 秒一拍
        actions, _ = piano.build_schedule(score, gate=0.5)
        recorder = piano.Recorder()
        self.assertTrue(piano.run_schedule(actions, recorder))
        kinds = [(kind, vk) for kind, vk, _ in recorder.events]
        self.assertEqual(kinds, [("down", 0x4C), ("up", 0x4C),
                                 ("down", 0x52), ("up", 0x52)])
        # 两个音之间应隔 0.1 秒（相对第一个音算，避开启动余量）
        gap = recorder.events[2][2] - recorder.events[0][2]
        self.assertAlmostEqual(gap, 0.1, delta=0.04)

    def test_abort_releases_held_keys(self):
        score = piano.parse_txt("@tempo 60\nl*4")    # 4 秒长音
        actions, _ = piano.build_schedule(score)
        recorder = piano.Recorder()
        stop = threading.Event()
        timer = threading.Timer(0.2, stop.set)
        timer.start()
        try:
            finished = piano.run_schedule(actions, recorder, stop)
        finally:
            timer.cancel()
        self.assertFalse(finished)
        self.assertEqual([kind for kind, _, _ in recorder.events], ["down", "up"])

    def test_repeated_key_is_retriggered(self):
        score = piano.parse_txt("@tempo 120\nl l")
        actions, _ = piano.build_schedule(score, gate=1.0)
        recorder = piano.Recorder()
        # gate=1 时上一音的抬起和下一音的按下同时发生，不应卡住键
        self.assertTrue(piano.run_schedule(actions, recorder))
        self.assertEqual([kind for kind, _, _ in recorder.events],
                         ["down", "up", "down", "up"])


class DurationTest(unittest.TestCase):
    """时值必须能精确表达附点、三连音和 3/8 这种拍子"""

    @staticmethod
    def ticks(token):
        return piano.parse_txt("@tempo 120\n" + token).events[0].ticks

    def test_note_values(self):
        cases = {"q@1": 1920, "q@2": 960, "q@4": 480, "q@8": 240,
                 "q@16": 120, "q@32": 60, "q@64": 30}
        for token, expected in cases.items():
            with self.subTest(token=token):
                self.assertEqual(self.ticks(token), expected)

    def test_dotted(self):
        self.assertEqual(self.ticks("q@4."), 720)
        self.assertEqual(self.ticks("q@8."), 360)
        self.assertEqual(self.ticks("q@4.."), 840)

    def test_tuplets_are_exact_integers(self):
        self.assertEqual(self.ticks("q@4T"), 320)
        self.assertEqual(self.ticks("q@8T"), 160)
        self.assertEqual(self.ticks("q@16T"), 80)

    def test_three_triplet_eighths_fill_one_quarter(self):
        score = piano.parse_txt("@tempo 120\nq@8T w@8T r@8T q@8")
        self.assertEqual(score.events[3].tick, 480)

    def test_dotted_quarter_plus_triplet_eighths(self):
        # 附点四分(720) + 三个三连音八分(160×3) = 两拍
        score = piano.parse_txt("@tempo 120\nq@4. w@8T w@8T w@8T q@8")
        self.assertEqual(score.events[1].tick, 720)
        self.assertEqual(score.events[4].tick, 1200)

    def test_old_star_syntax_still_works(self):
        self.assertEqual(self.ticks("q*1.5"), 720)
        self.assertEqual(self.ticks("q*0.5"), 240)
        self.assertEqual(self.ticks("q"), 480)

    def test_bad_duration_values(self):
        for token in ("q@3", "q@0", "q@x", "q@4.5", "q@128"):
            with self.subTest(token=token):
                with self.assertRaises(piano.ScoreError):
                    piano.parse_txt(token)

    def test_label_roundtrip(self):
        for ticks in (60, 120, 160, 240, 320, 360, 480, 720, 840, 960, 1920):
            with self.subTest(ticks=ticks):
                label = piano.duration_label(ticks)
                self.assertIsNotNone(label)
                self.assertEqual(piano.duration_ticks(label, 1), ticks)


class MeterTest(unittest.TestCase):
    def test_meter_is_parsed(self):
        score = piano.parse_txt("@meter 3/8\na@8 s@8 d@8")
        self.assertEqual(score.meter, (3, 8))

    def test_full_bar_has_no_warning(self):
        score = piano.parse_txt("@meter 3/8\na@8 s@8 d@8 |\na@8 s@8 d@8 |")
        self.assertEqual(score.warnings, [])

    def test_short_bar_warns(self):
        score = piano.parse_txt("@meter 3/8\na@8 s@8 d@8 |\na@8 s@8 |")
        self.assertTrue(score.warnings)
        self.assertIn("3/8", score.warnings[0])

    def test_pickup_bar_is_allowed(self):
        # 第一小节短一点是弱起，不该报错
        score = piano.parse_txt("@meter 3/8\na@8 |\na@8 s@8 d@8 |")
        self.assertEqual(score.warnings, [])

    def test_too_long_first_bar_warns(self):
        score = piano.parse_txt("@meter 3/8\nq t i |\na@8 s@8 d@8 |")
        self.assertTrue(score.warnings)

    def test_input_2_4(self):
        score = piano.parse_txt("@meter 2/4\na@4 s@4 |")
        self.assertEqual(score.warnings, [])

    def test_eighth_notes_step_by_half_a_beat(self):
        score = piano.parse_txt("@meter 3/8\na@8 s@8 d@8")
        self.assertAlmostEqual(score.events[1].beat, 0.5)
        self.assertAlmostEqual(score.events[2].beat, 1.0)
        self.assertAlmostEqual(score.events[2].beats, 0.5)

    def test_bad_meter(self):
        with self.assertRaises(piano.ScoreError):
            piano.parse_txt("@meter 3/5\na")

    def test_meter_survives_dump(self):
        text = piano.dump_txt([(0.0, 0.5, (60,))], bpm=120, meter=(3, 8))
        self.assertIn("@meter 3/8", text)
        self.assertEqual(piano.parse_txt(text).meter, (3, 8))

    def test_triplets_survive_dump(self):
        notes = [(0.0, 1 / 3, (60,)), (1 / 3, 1 / 3, (62,)), (2 / 3, 1 / 3, (64,))]
        text = piano.dump_txt(notes, bpm=120)
        self.assertIn("@8T", text)
        back = piano.parse_txt(text)
        self.assertEqual([e.tick for e in back.events], [0, 160, 320])

    def test_dotted_survives_dump(self):
        text = piano.dump_txt([(0.0, 0.75, (60,)), (0.75, 0.25, (62,))], bpm=120)
        self.assertIn("@8.", text)
        back = piano.parse_txt(text)
        self.assertEqual([e.ticks for e in back.events], [360, 120])


class ErrorTest(unittest.TestCase):
    def test_bad_key(self):
        with self.assertRaises(piano.ScoreError):
            piano.parse_txt("q z r")

    def test_dot_without_note(self):
        with self.assertRaises(piano.ScoreError):
            piano.parse_txt(". q")

    def test_bad_duration(self):
        with self.assertRaises(piano.ScoreError):
            piano.parse_txt("q*abc")

    def test_zero_duration(self):
        with self.assertRaises(piano.ScoreError):
            piano.parse_txt("q*0")

    def test_bad_tempo(self):
        with self.assertRaises(piano.ScoreError):
            piano.parse_txt("@tempo 0\nq")

    def test_empty_score(self):
        with self.assertRaises(piano.ScoreError):
            piano.parse_txt("# 什么都没有\n\n")

    def test_error_message_has_line_number(self):
        try:
            piano.parse_txt("q w\nw z")
        except piano.ScoreError as exc:
            self.assertIn("第 2 行", str(exc))
        else:
            self.fail("应当抛出 ScoreError")


class SampleScoreTest(unittest.TestCase):
    """仓库里的示例曲谱必须都能解析并排得出按键"""

    def test_all_sample_scores(self):
        folder = os.path.join(ROOT, "scores")
        self.assertTrue(os.path.isdir(folder))
        names = sorted(os.listdir(folder))
        self.assertTrue(names)
        for name in names:
            with self.subTest(score=name):
                score = piano.load_score(os.path.join(folder, name))
                self.assertGreater(len(score.events), 0)
                self.assertGreater(score.duration, 0.0)
                actions, skipped = piano.build_schedule(score)
                self.assertEqual(skipped, 0, "示例曲谱不该有超出音域的音")
                self.assertGreater(len(actions), 0)
                # 每次按下都要有一次松开
                downs = [a for a in actions if a.down]
                self.assertEqual(len(actions), 2 * len(downs))


class CliTest(unittest.TestCase):
    def test_keys_option(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(piano.main(["--keys"]), 0)
        self.assertIn("E5", out.getvalue())
        self.assertIn("Enter", out.getvalue())

    def test_print_plan(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = piano.main([os.path.join(ROOT, "scores", "twinkle.txt"), "--print"])
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("小星星", text)
        self.assertIn("按住", text)

    def test_missing_file(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(piano.main(["no_such_score.txt"]), 2)

    def test_usage_without_args(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(piano.main([]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
