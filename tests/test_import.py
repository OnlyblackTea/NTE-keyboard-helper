#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导入（MusicXML / MIDI）与导出（dump_txt）的测试

    python -m unittest discover -s tests -v
"""

import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import importers  # noqa: E402
import piano  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEMO_XML = os.path.join(ROOT, "samples", "musescore_demo.musicxml")


# --------------------------------------------------------------------------- #
# 一个极小的 MIDI 写出器，测试用
# --------------------------------------------------------------------------- #

def _varlen(value: int) -> bytes:
    out = [value & 0x7F]
    value >>= 7
    while value:
        out.insert(0, (value & 0x7F) | 0x80)
        value >>= 7
    return bytes(out)


def _track(events) -> bytes:
    data = b""
    last = 0
    for tick, payload in events:
        data += _varlen(tick - last) + payload
        last = tick
    data += _varlen(0) + b"\xff\x2f\x00"
    return b"MTrk" + struct.pack(">I", len(data)) + data


def build_midi(notes, bpm=96.0, ticks=480):
    """notes = [(起始拍, 时长拍, 音高)]"""
    events = [(0, b"\xff\x51\x03" + struct.pack(">I", int(60_000_000 / bpm))[1:])]
    for start, dur, pitch in notes:
        events.append((int(start * ticks), bytes([0x90, pitch, 0x64])))
        events.append((int((start + dur) * ticks), bytes([0x80, pitch, 0x40])))
    events.sort(key=lambda item: item[0])
    return (b"MThd" + struct.pack(">IHHH", 6, 0, 1, ticks) + _track(events))


class MusicXMLTest(unittest.TestCase):
    def test_reads_pitch_tempo_and_ties(self):
        notes, tempos, title, meter = importers.read_musicxml(DEMO_XML)
        self.assertEqual(meter, (4, 4))
        self.assertEqual(title, "导入示例（MusicXML）")
        self.assertAlmostEqual(tempos[0][1], 96.0)

        got = sorted((round(b, 4), round(d, 4), m) for b, d, m in notes)
        expected = sorted([
            (0.0, 1.0, 64),    # E4
            (1.0, 1.0, 67),    # G4
            (2.0, 2.0, 71),    # B4 两个四分音符被连音线合成一个二分音符
            (5.0, 2.0, 73),    # C#5 和弦
            (5.0, 2.0, 64),    # E4
            (5.0, 2.0, 67),    # G4
            (8.0, 4.0, 69),    # A4（voice 1）
            (8.0, 4.0, 52),    # E3（voice 2，靠 <backup> 回到同一位置）
        ])
        self.assertEqual(got, expected)

    def test_measure_offsets_are_continuous(self):
        notes, _, _, _ = importers.read_musicxml(DEMO_XML)
        starts = sorted({round(b, 4) for b, _, _ in notes})
        self.assertEqual(starts, [0.0, 1.0, 2.0, 5.0, 8.0])

    def test_word_tempo_units_are_converted(self):
        xml = b"""<?xml version="1.0"?>
        <score-partwise><part-list><score-part id="P1"><part-name>x</part-name></score-part></part-list>
        <part id="P1"><measure number="1"><attributes><divisions>1</divisions>
        <time><beats>4</beats><beat-type>4</beat-type></time></attributes>
        <direction><direction-type><metronome><beat-unit>half</beat-unit>
        <per-minute>60</per-minute></metronome></direction-type></direction>
        <note><pitch><step>C</step><octave>4</octave></pitch><duration>1</duration><voice>1</voice></note>
        </measure></part></score-partwise>"""
        path = os.path.join(HERE, "_tmp_world.xml")
        try:
            with open(path, "wb") as handle:
                handle.write(xml)
            _, tempos, _, _ = importers.read_musicxml(path)
        finally:
            if os.path.exists(path):
                os.remove(path)
        self.assertAlmostEqual(tempos[0][1], 120.0)   # 60 个二分音符/分 = 120 个四分音符/分

    def test_empty_score_raises(self):
        xml = b"""<?xml version="1.0"?>
        <score-partwise><part-list><score-part id="P1"><part-name>x</part-name></score-part></part-list>
        <part id="P1"><measure number="1"><attributes><divisions>1</divisions></attributes>
        <note><rest/><duration>4</duration><voice>1</voice></note></measure></part></score-partwise>"""
        path = os.path.join(HERE, "_tmp_empty.xml")
        try:
            with open(path, "wb") as handle:
                handle.write(xml)
            with self.assertRaises(ValueError):
                importers.read_musicxml(path)
        finally:
            if os.path.exists(path):
                os.remove(path)


class MidiTest(unittest.TestCase):
    def _roundtrip(self, payload: bytes):
        path = os.path.join(HERE, "_tmp_test.mid")
        try:
            with open(path, "wb") as handle:
                handle.write(payload)
            return importers.read_midi(path)
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_reads_notes_and_tempo(self):
        notes, tempos, _, _ = self._roundtrip(
            build_midi([(0, 1, 60), (1, 2, 64), (3, 1, 67)], bpm=120))
        self.assertAlmostEqual(tempos[0][1], 120.0)
        got = sorted((round(b, 3), round(d, 3), m) for b, d, m in notes)
        self.assertEqual(got, [(0.0, 1.0, 60), (1.0, 2.0, 64), (3.0, 1.0, 67)])

    def test_note_on_velocity_zero_means_off(self):
        # 0x90 with velocity 0 结束音符，这是 MIDI 里很常见的写法
        events = [
            (0, b"\xff\x51\x03" + struct.pack(">I", 500000)[1:]),
            (0, bytes([0x90, 60, 100])),
            (480, bytes([0x90, 60, 0])),
        ]
        payload = b"MThd" + struct.pack(">IHHH", 6, 0, 1, 480) + _track(events)
        notes, tempos, _, _ = self._roundtrip(payload)
        self.assertEqual([(round(b, 3), round(d, 3), m) for b, d, m in notes],
                         [(0.0, 1.0, 60)])
        self.assertAlmostEqual(tempos[0][1], 120.0)

    def test_running_status(self):
        events = [
            (0, b"\xff\x51\x03" + struct.pack(">I", 500000)[1:]),
            (0, bytes([0x90, 60, 100])),
            (240, bytes([64, 100])),          # 省略 status，沿用 0x90
            (480, bytes([60, 0])),
            (480, bytes([64, 0])),
        ]
        payload = b"MThd" + struct.pack(">IHHH", 6, 0, 1, 480) + _track(events)
        notes, _, _, _ = self._roundtrip(payload)
        self.assertEqual(sorted((round(b, 3), round(d, 3), m) for b, d, m in notes),
                         [(0.0, 1.0, 60), (0.5, 0.5, 64)])

    def test_not_a_midi_file(self):
        with self.assertRaises(ValueError):
            self._roundtrip(b"definitely not midi")


class FitTest(unittest.TestCase):
    def test_auto_shift_finds_best_octave(self):
        # 整首都在 C2 附近，应该整体升 2~3 个八度
        notes = [(i * 1.0, 1.0, 36 + i) for i in range(12)]
        fitted, stats = importers.fit_to_keyboard(notes, mode="auto")
        self.assertGreater(stats["shift"], 0)
        self.assertEqual(stats["dropped"], 0)
        for _, _, midi in fitted:
            self.assertTrue(52 <= midi <= 87)

    def test_none_drops_out_of_range(self):
        notes = [(0.0, 1.0, 24), (1.0, 1.0, 60), (2.0, 1.0, 100)]
        fitted, stats = importers.fit_to_keyboard(notes, mode="none")
        self.assertEqual(stats["dropped"], 2)
        self.assertEqual([m for _, _, m in fitted], [60])

    def test_fold_brings_back_into_range(self):
        notes = [(0.0, 1.0, 24), (1.0, 1.0, 100)]
        fitted, stats = importers.fit_to_keyboard(notes, mode="fold")
        self.assertEqual(stats["dropped"], 0)
        self.assertGreater(stats["folded"], 0)
        self.assertEqual(sorted(m for _, _, m in fitted), [60, 76])   # C1→C4，100→E5
        for _, _, midi in fitted:
            self.assertTrue(52 <= midi <= 87)

    def test_max_notes_keeps_the_top(self):
        notes = [(0.0, 1.0, 60), (0.0, 1.0, 64), (0.0, 1.0, 67), (0.0, 1.0, 72)]
        fitted, stats = importers.fit_to_keyboard(notes, mode="none", max_notes=2)
        self.assertEqual(sorted(m for _, _, m in fitted), [67, 72])
        self.assertEqual(stats["max_chord"], 4)

    def test_empty_input(self):
        fitted, stats = importers.fit_to_keyboard([], mode="auto")
        self.assertEqual(fitted, [])
        self.assertEqual(stats["kept"], 0)


class DumpTxtTest(unittest.TestCase):
    def test_simple_melody_roundtrip(self):
        notes = [(0.0, 1.0, (64,)), (1.0, 1.0, (67,)), (2.0, 2.0, (72,))]
        text = piano.dump_txt(notes, title="测试", bpm=100)
        self.assertIn("@tempo 100", text)
        score = piano.parse_txt(text)
        self.assertEqual([e.midis for e in score.events], [(64,), (67,), (72,)])
        self.assertAlmostEqual(score.events[2].beats, 2.0)

    def test_chord_is_written_as_one_token(self):
        text = piano.dump_txt([(0.0, 1.0, (60, 64, 67))])
        self.assertIn("lqr", text)
        self.assertEqual(piano.parse_txt(text).events[0].midis, (60, 64, 67))

    def test_gap_becomes_rest(self):
        text = piano.dump_txt([(0.0, 1.0, (60,)), (3.0, 1.0, (62,))])
        self.assertIn("_", text)
        score = piano.parse_txt(text)
        self.assertAlmostEqual(score.events[1].beat, 3.0)

    def test_odd_gap_is_split_into_standard_rests(self):
        # 1/3 拍网格上留下的零头，要拆成标准休止符，不能写成 _*2.3333
        rest = piano._rest_tokens(piano.PPQ * 2 + 160)
        self.assertEqual(rest, ["_@2", "_@8T"])

    def test_overlap_splits_into_voices(self):
        # 低音按住 4 拍，同时旋律每拍一个音 —— 一条音轨装不下，要拆声部
        notes = [(0.0, 4.0, (48 + 12,))]
        notes += [(float(i), 1.0, (60 + i,)) for i in range(4)]
        text = piano.dump_txt(notes, bpm=120)
        self.assertGreaterEqual(text.count("@voice"), 2)
        score = piano.parse_txt(text)
        self.assertEqual(len(score.events), 5)
        self.assertAlmostEqual(score.events[0].time, 0.0)
        self.assertEqual(sorted(m for e in score.events for m in e.midis),
                         sorted([60, 60, 61, 62, 63]))

    def test_all_keys_survive_roundtrip(self):
        notes = [(float(i), 1.0, (midi,)) for i, midi in enumerate(range(52, 88))]
        text = piano.dump_txt(notes)
        back = piano.parse_txt(text)
        self.assertEqual(sorted(m for e in back.events for m in e.midis),
                         list(range(52, 88)))

    def test_enter_key_is_written_as_word(self):
        text = piano.dump_txt([(0.0, 1.0, (63,)), (1.0, 1.0, (64, 63))])
        self.assertIn("enter", text.lower())
        self.assertEqual(piano.parse_txt(text).events[1].midis, (63, 64))


class RoundTripTest(unittest.TestCase):
    """WebUI 的「载入 → 保存」必须不改音乐内容，也不毁掉声部结构"""

    @staticmethod
    def _canon(score):
        # 和弦里音的书写顺序不影响听感，比较时排一下
        return sorted(
            (round(ev.beat, 4), round(ev.beats, 4), tuple(sorted(ev.midis)), ev.voice)
            for ev in score.events
        )

    def test_every_sample_survives_edit_roundtrip(self):
        folder = os.path.join(ROOT, "scores")
        names = [n for n in sorted(os.listdir(folder)) if n.endswith((".txt", ".csv"))]
        self.assertTrue(names)
        for name in names:
            with self.subTest(score=name):
                score = piano.load_score(os.path.join(folder, name))
                payload = piano.score_to_dict(score)
                notes = [
                    (item["beat"], item["beats"], item["midis"], item["voice"])
                    for item in payload["notes"]
                ]
                text = piano.dump_txt(notes, title=score.title,
                                      bpm=score.base_bpm, gate=score.gate)
                back = piano.parse_txt(text)
                self.assertEqual(self._canon(score), self._canon(back))
                self.assertAlmostEqual(back.base_bpm, score.base_bpm)

    def test_voice_names_are_preserved(self):
        text = "@tempo 120\n@voice 伴奏\nagk*4\n@voice 旋律\nq*4\n"
        score = piano.parse_txt(text)
        payload = piano.score_to_dict(score)
        dumped = piano.dump_txt(
            [(n["beat"], n["beats"], n["midis"], n["voice"]) for n in payload["notes"]],
            bpm=120)
        self.assertIn("@voice 伴奏", dumped)
        self.assertIn("@voice 旋律", dumped)
        back = piano.parse_txt(dumped)
        self.assertEqual(sorted(back.voices), ["伴奏", "旋律"])

    def test_same_onset_same_length_stays_in_its_own_voice(self):
        # 两个声部在同一时刻、同样长度各有一个音：不能被并成一个和弦
        notes = [(0.0, 2.0, (52, 56, 59), "伴奏"), (0.0, 2.0, (71,), "旋律")]
        dumped = piano.dump_txt(notes, bpm=120)
        self.assertNotIn("agki", dumped)
        back = piano.parse_txt(dumped)
        self.assertEqual(sorted((tuple(e.midis), e.voice) for e in back.events),
                         [((52, 56, 59), "伴奏"), ((71,), "旋律")])


class DispatchTest(unittest.TestCase):
    def test_musicxml_goes_through_importer(self):
        score = piano.load_score(DEMO_XML)
        self.assertEqual(len(score.events), 5)
        self.assertAlmostEqual(score.base_bpm, 96.0)
        self.assertTrue(score.info)

    def test_mscz_gets_a_helpful_message(self):
        path = os.path.join(HERE, "_tmp_fake.mscz")
        try:
            with open(path, "wb") as handle:
                handle.write(b"PK\x03\x04")
            with self.assertRaises(piano.ScoreError) as ctx:
                piano.load_score(path)
        finally:
            if os.path.exists(path):
                os.remove(path)
        self.assertIn("MusicXML", str(ctx.exception))

    def test_score_to_dict_shape(self):
        score = piano.load_score(DEMO_XML)
        payload = piano.score_to_dict(score)
        self.assertEqual(payload["bpm"], 96.0)
        self.assertEqual(len(payload["notes"]), 5)
        self.assertEqual(sorted(payload["notes"][0]),
                         ["beat", "beats", "midis", "tick", "ticks", "voice"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
