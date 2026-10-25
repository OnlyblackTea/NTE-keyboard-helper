#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
piano.py —— 照着曲谱自动敲键盘

键盘映射（每行 12 键，纯半音阶，行首都是 E，行间正好差一个八度）

    1 2 3 4 5 6 7 8 9 0 - =      E5 → D#6
    Q W E R T Y U I O P [ ]      E4 → D#5
    A S D F G H J K L ; ' Enter  E3 → D#4

程序只做两件事：读曲谱 → 在对的时刻按下 / 松开对应的物理按键。
对着网页钢琴、DAW、游戏这类「按键盘发声」的窗口用即可，本身不发声。

    python piano.py scores/twinkle.txt           3 秒倒计时后开始敲
    python piano.py scores/twinkle.txt --print   只打印演奏计划，不按键
    python piano.py --keys                       看键位表
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

_IS_WINDOWS = os.name == "nt"

try:
    import ctypes
    from ctypes import wintypes

    _HAS_CTYPES = True
except Exception:  # pragma: no cover
    _HAS_CTYPES = False

if _IS_WINDOWS and _HAS_CTYPES:
    # SendInput 用的结构体，必须和 Win32 的 INPUT 完全一致（x64 下 40 字节）。
    # 联合体里一定要带上 MOUSEINPUT，否则 sizeof(INPUT) 会偏小，SendInput 直接失败。
    _ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", _ULONG_PTR),
        ]

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", _ULONG_PTR),
        ]

    class _HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class _INPUT_UNION(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT)]

    class _INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_UNION)]


# --------------------------------------------------------------------------- #
# 一、键位表：字符 → MIDI 音高 → 虚拟键码
# --------------------------------------------------------------------------- #

NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")

# (一行里的键, 起始 MIDI)。每行 12 个音，行首都是 E。
_ROW_SPEC = (
    ("1234567890-=", 76),   # E5 ~ D#6
    ("qwertyuiop[]", 64),   # E4 ~ D#5
    ("asdfghjkl;'", 52),    # E3 ~ D#4
)
_EXTRA_SPEC = {"enter": 63}  # 回车 = D#4，补齐 A 行

# 按键字符 → Windows 虚拟键码（VK）
_OEM_VK = {
    "-": 0xBD,   # VK_OEM_MINUS
    "=": 0xBB,   # VK_OEM_PLUS
    "[": 0xDB,   # VK_OEM_4
    "]": 0xDD,   # VK_OEM_5
    ";": 0xBA,   # VK_OEM_1
    "'": 0xDE,   # VK_OEM_7
}
_VK_ENTER = 0x0D


def key_vk(key: str) -> int:
    """'a' → 0x41，';' → 0xBA，'enter' → 0x0D"""
    if key == "enter":
        return _VK_ENTER
    if len(key) == 1:
        if "a" <= key <= "z":
            return 0x41 + ord(key) - ord("a")
        if "0" <= key <= "9":
            return 0x30 + ord(key) - ord("0")
        if key in _OEM_VK:
            return _OEM_VK[key]
    raise KeyError("没有键位 {}".format(key))


KEY_TO_MIDI: Dict[str, int] = {}
MIDI_TO_KEY: Dict[int, str] = {}
MIDI_TO_VK: Dict[int, int] = {}
VK_TO_CHAR: Dict[int, str] = {}

for _keys, _base in _ROW_SPEC:
    for _i, _k in enumerate(_keys):
        KEY_TO_MIDI[_k] = _base + _i
for _name, _midi in _EXTRA_SPEC.items():
    KEY_TO_MIDI[_name] = _midi

for _key, _midi in KEY_TO_MIDI.items():
    MIDI_TO_KEY[_midi] = "ENTER" if _key == "enter" else _key.upper()
    MIDI_TO_VK[_midi] = key_vk(_key)
    VK_TO_CHAR[key_vk(_key)] = "\n" if _key == "enter" else _key

# 多字符键名，解析时要优先于单字符键（否则 enter 会被读成 e n t e r 五个音）
_MULTI_KEYS = tuple(sorted((k for k in KEY_TO_MIDI if len(k) > 1), key=len, reverse=True))

LOWEST_MIDI = min(KEY_TO_MIDI.values())
HIGHEST_MIDI = max(KEY_TO_MIDI.values())


def midi_name(midi: int) -> str:
    """60 → 'C4'"""
    return "{}{}".format(NOTE_NAMES[midi % 12], midi // 12 - 1)


# --------------------------------------------------------------------------- #
# 二、曲谱数据结构
# --------------------------------------------------------------------------- #

DEFAULT_BPM = 120.0
DEFAULT_GATE = 0.9      # 每个键按住「时值 × 0.9」，留缝好让重复音能再触发
MIN_HOLD = 0.02         # 再短的音也按住 20ms，否则目标程序可能收不到

# 内部时间单位：一个四分音符 = 480 tick（和 MIDI 一样）。
# 480 能被 2/3/4/5/6/8 整除，所以附点和三连音都是整数，不会累积浮点误差。
PPQ = 480


def beats_to_ticks(beats: float) -> int:
    return int(round(float(beats) * PPQ))


def ticks_to_beats(ticks: int) -> float:
    return int(ticks) / PPQ


class ScoreError(Exception):
    """曲谱格式错误（带行号）"""


@dataclass
class Event:
    """一个「事件」＝ 同一时刻按下的一个音或一个和弦"""

    beat: float              # 在曲谱里的位置（四分音符为 1）
    beats: float             # 时值（四分音符为 1）
    midis: Tuple[int, ...]   # 同时按下的音高
    keys: str                # 原始键位串，仅用于显示
    voice: str = "1"
    line: int = 0
    time: float = 0.0        # 秒（由拍位置 + 速度表算出）
    dur: float = 0.0         # 秒
    tick: int = 0            # 和 beat 等价的整数刻度
    ticks: int = 0           # 和 beats 等价的整数刻度

    @property
    def names(self) -> str:
        return " ".join(midi_name(m) for m in self.midis)

    @property
    def labels(self) -> str:
        return "+".join(MIDI_TO_KEY.get(m, "?") for m in self.midis)


@dataclass
class Score:
    title: str = ""
    events: List[Event] = field(default_factory=list)
    voices: Tuple[str, ...] = ()
    base_bpm: float = DEFAULT_BPM
    duration: float = 0.0
    gate: float = DEFAULT_GATE
    warnings: List[str] = field(default_factory=list)
    info: List[str] = field(default_factory=list)
    meter: Optional[Tuple[int, int]] = None      # (分子, 分母)，比如 (3, 8)

    def rescale_tempo(self, target_bpm: float) -> None:
        """把整首曲子的速度缩放到 target_bpm（保持曲内相对速度变化）"""
        if target_bpm <= 0 or not self.events:
            return
        k = self.base_bpm / target_bpm
        for ev in self.events:
            ev.time *= k
            ev.dur *= k
        self.duration *= k
        self.base_bpm = target_bpm


class TempoMap:
    """分段速度表：拍位置 → 秒。支持曲中变速。"""

    def __init__(self, base_bpm: float = DEFAULT_BPM):
        self._points: List[Tuple[float, float]] = [(0.0, float(base_bpm))]
        self._segments: List[Tuple[float, float, float]] = []

    def set(self, beat: float, bpm: float) -> None:
        beat = max(0.0, float(beat))
        self._points = [p for p in self._points if abs(p[0] - beat) > 1e-9]
        self._points.append((beat, float(bpm)))
        self._points.sort()

    @property
    def base_bpm(self) -> float:
        return self._points[0][1]

    def build(self) -> "TempoMap":
        points = list(self._points)
        if points[0][0] > 0:
            points.insert(0, (0.0, points[0][1]))
        segments: List[Tuple[float, float, float]] = []
        seconds = 0.0
        for i, (beat, bpm) in enumerate(points):
            if i:
                pbeat, pbpm = points[i - 1]
                seconds += (beat - pbeat) * 60.0 / pbpm
            segments.append((beat, seconds, bpm))
        self._segments = segments
        return self

    def time(self, beat: float) -> float:
        segs = self._segments or self.build()._segments
        lo, hi = 0, len(segs) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if segs[mid][0] <= beat:
                lo = mid
            else:
                hi = mid - 1
        b0, t0, bpm = segs[lo]
        return t0 + (beat - b0) * 60.0 / bpm

    def span(self, beat: float, beats: float) -> float:
        return self.time(beat + beats) - self.time(beat)


# --------------------------------------------------------------------------- #
# 三、曲谱解析
# --------------------------------------------------------------------------- #

_NOTE_NAME_RE = re.compile(r"^([A-Ga-g])([#b]?)(-?\d+)$")
_SEMITONE = {"c": 0, "d": 2, "e": 4, "f": 5, "g": 7, "a": 9, "b": 11}

# ---- 时值：@1 @2 @4 @8 @16 @32 @64，加点 = 附点，加 T = 三连音 ----
_DURATION_RE = re.compile(r"^(\d+)(\.*)([Tt]?)$")


def _duration_table() -> Tuple[Dict[str, int], Dict[int, str]]:
    """返回 (写法→tick, tick→写法)，后者优先保留最普通的写法"""
    forward: Dict[str, int] = {}
    backward: Dict[int, str] = {}
    for value in (1, 2, 4, 8, 16, 32, 64):
        base = PPQ * 4 // value          # 全音符 = 4 拍
        total = base
        extra = base // 2
        for dots in range(3):
            label = "{}{}".format(value, "." * dots)
            forward[label] = total
            backward.setdefault(total, label)
            triplet = total * 2 // 3
            if triplet * 3 == total * 2:
                forward[label + "T"] = triplet
                backward.setdefault(triplet, label + "T")
            total += extra
            extra //= 2
    return forward, backward


_DURATION_TICKS, _TICKS_DURATION = _duration_table()


def duration_ticks(text: str, line: int) -> int:
    """把 @4 / @8. / @4T / @16 解析成 tick 数"""
    key = text.strip().upper()
    match = _DURATION_RE.match(key)
    if not match:
        raise ScoreError(
            "第 {} 行：@{!r} 不是有效时值。写法：@1 全音符、@2 二分、@4 四分、"
            "@8 八分、@16 十六分，后面可以加点（附点）或 T（三连音）".format(line, text))
    value = int(match.group(1))
    if value not in (1, 2, 4, 8, 16, 32, 64):
        raise ScoreError("第 {} 行：@{} 里的 {} 只能是 1/2/4/8/16/32/64".format(
            line, text, value))
    canonical = "{}{}{}".format(value, match.group(2), "T" if match.group(3) else "")
    ticks = _DURATION_TICKS.get(canonical)
    if ticks is None or ticks <= 0:
        raise ScoreError("第 {} 行：@{!r} 这个时值算不出整数 tick".format(line, text))
    return ticks


def duration_label(ticks: int) -> Optional[str]:
    """tick 数 → 记谱写法；不是标准时值就返回 None"""
    return _TICKS_DURATION.get(int(ticks))


def note_name_to_midi(text: str) -> int:
    """'C4' / 'F#5' / 'Bb3' → MIDI"""
    match = _NOTE_NAME_RE.match(text.strip())
    if not match:
        raise ValueError("无法识别的音名：{!r}".format(text))
    letter, accidental, octave = match.group(1), match.group(2), int(match.group(3))
    midi = 12 * (octave + 1) + _SEMITONE[letter.lower()]
    if accidental == "#":
        midi += 1
    elif accidental == "b":
        midi -= 1
    return midi


def _strip_comment(line: str) -> str:
    """去掉 # 注释（要求 # 在行首或前面有空白，避免误伤其他符号）"""
    for i, ch in enumerate(line):
        if ch == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i]
    return line


def _to_float(text: str, line: int, what: str) -> float:
    try:
        return float(text.strip().replace(",", "."))
    except ValueError:
        raise ScoreError("第 {} 行：{} 的值 {!r} 不是数字".format(line, what, text)) from None


def _parse_keys(body: str, line: int) -> Tuple[List[int], str]:
    """把 'lq[' 这样的键串解析成 MIDI 列表（同一串 = 和弦）"""
    midis: List[int] = []
    low = body.lower()
    i = 0
    while i < len(low):
        matched = None
        for name in _MULTI_KEYS:
            if low.startswith(name, i):
                matched = name
                break
        if matched is not None:
            midis.append(KEY_TO_MIDI[matched])
            i += len(matched)
            continue
        ch = low[i]
        if ch not in KEY_TO_MIDI:
            if ch.isspace():
                raise ScoreError("第 {} 行：键位串里不能有空格，和弦请直接并排写".format(line))
            raise ScoreError(
                "第 {} 行：{!r} 不是有效琴键。可用键位：{}".format(
                    line, body[i], "".join(sorted(KEY_TO_MIDI))
                )
            )
        midis.append(KEY_TO_MIDI[ch])
        i += 1
    if not midis:
        raise ScoreError("第 {} 行：空的键位串 {!r}".format(line, body))
    return midis, body


def _parse_note_field(text: str, line: int) -> List[int]:
    """csv 里的音符字段：可以是音名（E4）也可以是键位串（lqr）"""
    text = text.strip()
    if not text:
        return []
    if _NOTE_NAME_RE.match(text):
        return [note_name_to_midi(text)]
    parts = [p for p in re.split(r"[\s,+]+", text) if p]
    midis: List[int] = []
    for part in parts:
        if _NOTE_NAME_RE.match(part):
            midis.append(note_name_to_midi(part))
        else:
            midis.extend(_parse_keys(part, line)[0])
    return midis


class VoiceState:
    """一个声部自己的时间游标（以 tick 计，整数不会漂）"""

    __slots__ = ("tick", "bar_start", "bar_count", "events", "last")

    def __init__(self) -> None:
        self.tick = 0
        self.bar_start = 0
        self.bar_count = 0
        self.events: List[Event] = []
        self.last: Optional[Event] = None


def _finish(voices: Dict[str, VoiceState], tempo: TempoMap, title: str,
            gate: float, warnings: List[str],
            meter: Optional[Tuple[int, int]] = None) -> Score:
    """把各声部的「拍」时间线合成一条，并按速度表换算成秒"""
    tempo.build()
    events: List[Event] = []
    used: List[str] = []
    for name, state in voices.items():
        if not state.events:      # @tempo 之类会顺手创建空声部，不算数
            continue
        used.append(name)
        for ev in state.events:
            ev.time = tempo.time(ev.beat)
            ev.dur = tempo.span(ev.beat, ev.beats)
            events.append(ev)
    events.sort(key=lambda e: (e.time, e.voice))
    # csv 里同一个和弦可能写成多行；同一时刻同时值的音并成一个和弦事件
    merged: Dict[Tuple[int, int, str], Event] = {}
    for ev in events:
        key = (ev.tick, ev.ticks, ev.voice)
        existing = merged.get(key)
        if existing is None:
            merged[key] = ev
        else:
            existing.midis = tuple(sorted(set(existing.midis) | set(ev.midis)))
    events = list(merged.values())
    duration = max((e.time + e.dur for e in events), default=0.0)
    return Score(
        title=title,
        events=events,
        voices=tuple(used),
        base_bpm=tempo.base_bpm,
        duration=duration,
        gate=gate,
        warnings=warnings,
        meter=meter,
    )


def parse_txt(text: str, transpose: int = 0) -> Score:
    """解析 .txt 曲谱"""
    voices: Dict[str, VoiceState] = {}
    warnings: List[str] = []
    tempo = TempoMap(DEFAULT_BPM)
    current = "1"
    gate = DEFAULT_GATE
    shift = transpose
    title = ""

    def voice(name: str) -> VoiceState:
        state = voices.get(name)
        if state is None:
            state = VoiceState()
            voices[name] = state
        return state

    meter: Optional[Tuple[int, int]] = None
    bar_ticks = 0

    for lineno, raw in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw).replace("|", " | ").strip()
        if not line:
            continue

        # ---- 指令行 ----
        if line[0] == "@":
            head, _, tail = line.partition(" ")
            key = head[1:].strip().lower()
            value = tail.strip()
            if key == "tempo":
                bpm = _to_float(value, lineno, "@tempo")
                if bpm <= 0:
                    raise ScoreError("第 {} 行：@tempo 必须大于 0".format(lineno))
                tempo.set(ticks_to_beats(voice(current).tick), bpm)
            elif key in ("meter", "time"):
                match = re.match(r"^(\d+)\s*/\s*(\d+)$", value)
                if not match:
                    raise ScoreError("第 {} 行：@meter 要写成 3/8 这种".format(lineno))
                num, den = int(match.group(1)), int(match.group(2))
                if num <= 0 or den not in (1, 2, 4, 8, 16, 32):
                    raise ScoreError("第 {} 行：@meter {}/{} 不是有效拍号".format(
                        lineno, num, den))
                meter = (num, den)
                bar_ticks = PPQ * 4 * num // den
            elif key == "voice":
                if not value:
                    raise ScoreError("第 {} 行：@voice 后面要跟声部名".format(lineno))
                current = value
                voice(current)
            elif key == "gate":
                gate = min(max(_to_float(value, lineno, "@gate"), 0.05), 1.0)
            elif key == "transpose":
                shift = int(_to_float(value, lineno, "@transpose"))
            elif key == "title":
                title = value
            else:
                warnings.append("第 {} 行：未知指令 @{}，已忽略".format(lineno, key))
            continue

        # ---- 音符行 ----
        state = voice(current)
        for token in line.split():
            # 小节线：配合 @meter 检查这一小节写得对不对
            if token == "|":
                if bar_ticks and state.tick != state.bar_start:
                    span = state.tick - state.bar_start
                    # 第一小节短一点算弱起（pickup），不算错；其余必须正好一小节
                    allowed = (span == bar_ticks
                               or (state.bar_count == 0 and 0 < span < bar_ticks))
                    if not allowed:
                        warnings.append(
                            "第 {} 行：这个小节写了 {} 拍，{}/{} 应该是 {} 拍".format(
                                lineno, _num(span / PPQ), meter[0], meter[1],
                                _num(bar_ticks / PPQ)))
                if state.tick != state.bar_start:
                    state.bar_count += 1
                state.bar_start = state.tick
                continue

            # 延长上一个音：. / .. / ...
            if set(token) == {"."}:
                if state.last is None:
                    raise ScoreError("第 {} 行：'.' 前面没有可延长的音".format(lineno))
                extra = PPQ * len(token)
                state.last.ticks += extra
                state.last.beats = ticks_to_beats(state.last.ticks)
                state.tick += extra
                continue

            body, ticks = token, PPQ
            if "*" in token:
                body, dur_text = token.split("*", 1)
                beats = _to_float(dur_text, lineno, "时值")
                if beats <= 0:
                    raise ScoreError("第 {} 行：{!r} 的时值必须大于 0".format(lineno, token))
                ticks = beats_to_ticks(beats)
            elif "@" in token:
                body, dur_text = token.split("@", 1)
                ticks = duration_ticks(dur_text, lineno)
            if ticks <= 0:
                raise ScoreError("第 {} 行：{!r} 的时值必须大于 0".format(lineno, token))
            if not body:
                raise ScoreError("第 {} 行：{!r} 缺少音符".format(lineno, token))

            # 休止符 _
            if set(body) == {"_"}:
                state.tick += ticks
                state.last = None
                continue

            # 和弦可选的 { } 花括号（[ ] 是琴键，不能当括号用）
            if body.startswith("{") and body.endswith("}"):
                body = body[1:-1]

            midis, keys = _parse_keys(body, lineno)
            event = Event(
                beat=ticks_to_beats(state.tick),
                beats=ticks_to_beats(ticks),
                midis=tuple(m + shift for m in midis),
                keys=keys,
                voice=current,
                line=lineno,
                tick=state.tick,
                ticks=ticks,
            )
            state.events.append(event)
            state.tick += ticks
            state.last = event

    if not voices:
        raise ScoreError("曲谱是空的：没有任何音符")
    return _finish(voices, tempo, title, gate, warnings, meter)


def parse_csv(text: str, transpose: int = 0) -> Score:
    """解析 .csv 曲谱

    列：起始拍, 时长(拍), 音符
    音符可以写键位串（lqr / q / Enter），也可以写音名（C4 / F#5）。
    """
    rows = list(csv.reader(io.StringIO(text)))
    tempo = TempoMap(DEFAULT_BPM)
    voices: Dict[str, VoiceState] = {"1": VoiceState()}
    warnings: List[str] = []
    gate = DEFAULT_GATE
    title = ""
    meter: Optional[Tuple[int, int]] = None
    state = voices["1"]

    for lineno, row in enumerate(rows, 1):
        cells = [c.strip() for c in row]
        if not any(cells) or cells[0].startswith("#"):
            continue
        if cells[0].startswith("@"):
            head, _, tail = cells[0].partition(" ")
            key = head[1:].strip().lower()
            value = (tail.strip() or (cells[1] if len(cells) > 1 else ""))
            if key == "tempo":
                tempo.set(0.0, _to_float(value, lineno, "@tempo"))
            elif key in ("meter", "time"):
                match = re.match(r"^(\d+)\s*/\s*(\d+)$", value)
                if match:
                    meter = (int(match.group(1)), int(match.group(2)))
            elif key == "gate":
                gate = min(max(_to_float(value, lineno, "@gate"), 0.05), 1.0)
            elif key == "title":
                title = value
            continue
        # 表头
        if not re.match(r"^[+-]?[\d.]", cells[0]):
            continue
        if len(cells) < 3:
            raise ScoreError("第 {} 行：csv 至少需要 3 列（起始拍, 时长, 音符）".format(lineno))

        beat = _to_float(cells[0], lineno, "起始拍")
        beats = _to_float(cells[1], lineno, "时长")
        if beats <= 0:
            raise ScoreError("第 {} 行：时长必须大于 0".format(lineno))
        midis = _parse_note_field(cells[2], lineno)
        if not midis:
            continue
        state.events.append(
            Event(
                beat=beat,
                beats=beats,
                midis=tuple(m + transpose for m in midis),
                keys=cells[2],
                line=lineno,
                tick=beats_to_ticks(beat),
                ticks=beats_to_ticks(beats),
            )
        )

    if not any(v.events for v in voices.values()):
        raise ScoreError("csv 里没有读到任何音符")
    return _finish(voices, tempo, title, gate, warnings, meter)


def read_text(path: str) -> str:
    """尽量宽容地读文本：UTF-8(含 BOM) → GB18030 → 替换错误字符"""
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            with open(path, "r", encoding=encoding) as handle:
                return handle.read()
        except UnicodeDecodeError:
            continue
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def load_score(path: str, transpose: int = 0, fit: str = "auto",
               max_notes: int = 0) -> Score:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".mscz":
        raise ScoreError(
            ".mscz 是 MuseScore 的压缩工程文件，这里读不了。"
            "请在 MuseScore 里「文件 → 导出」选 MusicXML 或 MIDI 再导入。"
        )
    if ext in (".musicxml", ".xml", ".mxl", ".mid", ".midi"):
        return load_imported(path, transpose=transpose, fit=fit, max_notes=max_notes)
    text = read_text(path)
    if ext in (".csv", ".tsv"):
        return parse_csv(text, transpose=transpose)
    return parse_txt(text, transpose=transpose)


def load_imported(path: str, transpose: int = 0, fit: str = "auto",
                  max_notes: int = 0) -> Score:
    """读 MusicXML / MIDI，并把音高塞进 36 键音域"""
    import importers  # 只有导入时才需要，放这里免得单独用 txt 时也要带着它

    try:
        notes, tempos, title, meter = importers.read_any(path)
        notes, stats = importers.fit_to_keyboard(
            notes, mode=fit, low=LOWEST_MIDI, high=HIGHEST_MIDI, max_notes=max_notes)
    except ScoreError:
        raise
    except Exception as exc:
        raise ScoreError("读不了这个谱子：{}".format(exc)) from None

    if transpose:
        kept = []
        for position, length, midi in notes:
            value = midi + transpose
            if LOWEST_MIDI <= value <= HIGHEST_MIDI:
                kept.append((position, length, value))
        stats["dropped"] += len(notes) - len(kept)
        notes = kept
    if not notes:
        raise ScoreError("导入后没有落在键盘音域（{} ~ {}）内的音，"
                         "试试 --fit fold".format(midi_name(LOWEST_MIDI),
                                                  midi_name(HIGHEST_MIDI)))

    tempo = TempoMap(DEFAULT_BPM)
    for position, bpm in (tempos or [(0.0, DEFAULT_BPM)]):
        tempo.set(position, bpm)
    tempo.build()

    # 同一时刻、同一时值的音合成一个和弦
    groups: Dict[Tuple[float, float], List[int]] = {}
    for position, length, midi in notes:
        groups.setdefault((round(position, 4), round(length, 4)), []).append(midi)

    events: List[Event] = []
    for (position, length), midis in sorted(groups.items()):
        events.append(Event(
            beat=position,
            beats=length,
            midis=tuple(sorted(set(midis))),
            keys="",
            voice="导入",
            time=tempo.time(position),
            dur=tempo.span(position, length),
            tick=beats_to_ticks(position),
            ticks=beats_to_ticks(length),
        ))

    info = ["导入 {} 个音，保留 {} 个".format(stats["input"], stats["kept"])]
    if stats.get("shift"):
        info.append("整体{}了 {} 个八度才塞进音域".format(
            "升" if stats["shift"] > 0 else "降", abs(stats["shift"])))
    if stats.get("folded"):
        info.append("{} 个音按八度折回音域".format(stats["folded"]))
    if stats.get("dropped"):
        info.append("丢弃 {} 个超出音域的音（想保留可试 --fit fold）".format(stats["dropped"]))
    info.append("最多同时按 {} 个键".format(stats.get("max_chord", 0)))

    return Score(
        title=title,
        events=events,
        voices=("导入",),
        base_bpm=tempo.base_bpm,
        duration=max((e.time + e.dur for e in events), default=0.0),
        info=info,
        meter=meter,
    )


# --------------------------------------------------------------------------- #
# 反向：把音符表写成 txt 曲谱（WebUI 保存、脚本生成都用它）
# --------------------------------------------------------------------------- #

def _num(value: float) -> str:
    text = "{:.4f}".format(float(value)).rstrip("0").rstrip(".")
    return text if text else "0"


def _duration_suffix(ticks: int) -> str:
    """一拍返回空串；标准时值用 @8 / @4. / @8T，其它退回 *拍数"""
    if ticks == PPQ:
        return ""
    label = duration_label(ticks)
    return "@" + label if label else "*" + _num(ticks / PPQ)


# 拆休止符时用的时值（从大到小），刻意只放常用的，避免冒出「双附点三连音」这种怪东西
_REST_SIZES = (1920, 1440, 960, 720, 480, 360, 320, 240, 160, 120, 80, 60, 40, 30)


def _rest_tokens(ticks: int) -> List[str]:
    """把一段空白拆成休止符；长空白直接写拍数，零头用标准时值凑"""
    if ticks == PPQ:
        return ["_"]
    if ticks > PPQ and ticks % PPQ == 0:
        return ["_*" + _num(ticks / PPQ)]
    out: List[str] = []
    remaining = ticks
    while remaining > 0:
        for size in _REST_SIZES:
            if size <= remaining:
                out.append("_" if size == PPQ else "_@" + duration_label(size))
                remaining -= size
                break
        else:
            out.append("_*" + _num(remaining / PPQ))
            break
    return out


def dump_txt(notes: Sequence[tuple], title: str = "", bpm: float = DEFAULT_BPM,
             gate: Optional[float] = None,
             meter: Optional[Tuple[int, int]] = None) -> str:
    """把音符表写成 txt 曲谱。

    每个音符是 (起始拍, 时值, [midi...]) 或者 (起始拍, 时值, [midi...], 声部名)，
    带声部名时按声部原样保留结构。一条音轨里音不能重叠（记谱语法是顺序的），
    所以同一音轨内若还有重叠，会再贪心拆成多条 —— 拆出来仍然是合法的 txt。
    """
    buckets: Dict[str, List[Tuple[int, int, Tuple[int, ...]]]] = {}
    order: List[str] = []
    for item in notes:
        if len(item) < 3:
            continue
        tick = beats_to_ticks(item[0])
        ticks = beats_to_ticks(item[1])
        voice = str(item[3]) if len(item) > 3 and item[3] else "1"
        pitches = tuple(sorted({int(m) for m in item[2]}))
        if ticks <= 0 or not pitches:
            continue
        if voice not in buckets:
            buckets[voice] = []
            order.append(voice)
        buckets[voice].append((tick, ticks, pitches))
    if not order:
        raise ValueError("没有可写出的音符")

    # 每个声部内部先并和弦，再按重叠情况拆轨
    blocks: List[Tuple[str, List[Tuple[int, int, Tuple[int, ...]]]]] = []
    for name in order:
        merged: Dict[Tuple[int, int], set] = {}
        for tick, ticks, pitches in buckets[name]:
            merged.setdefault((tick, ticks), set()).update(pitches)
        items = sorted((tick, ticks, tuple(sorted(pitches)))
                       for (tick, ticks), pitches in merged.items())

        tracks: List[dict] = []
        for tick, ticks, pitches in items:
            for track in tracks:
                if track["end"] <= tick:
                    track["items"].append((tick, ticks, pitches))
                    track["end"] = tick + ticks
                    break
            else:
                tracks.append({"end": tick + ticks, "items": [(tick, ticks, pitches)]})
        for index, track in enumerate(tracks):
            label = name if index == 0 else "{}·{}".format(name, index + 1)
            blocks.append((label, track["items"]))

    lines: List[str] = []
    if title:
        lines.append("@title " + title)
    lines.append("@tempo " + _num(bpm))
    if meter:
        lines.append("@meter {}/{}".format(meter[0], meter[1]))
    if gate is not None and abs(gate - DEFAULT_GATE) > 1e-9:
        lines.append("@gate " + _num(gate))
    lines.append("")

    labelled = len(blocks) > 1
    for label, items in blocks:
        if labelled:
            lines.append("@voice " + label)
        cursor = 0
        tokens: List[str] = []
        for tick, ticks, pitches in items:
            if tick > cursor:
                tokens.extend(_rest_tokens(tick - cursor))
            token = "".join(MIDI_TO_KEY.get(m, "?").lower() for m in pitches)
            tokens.append(token + _duration_suffix(ticks))
            cursor = tick + ticks
        lines.extend(_wrap_tokens(tokens))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _wrap_tokens(tokens: List[str], width: int = 78) -> List[str]:
    lines: List[str] = []
    current = ""
    for token in tokens:
        if current and len(current) + 1 + len(token) > width:
            lines.append(current)
            current = token
        else:
            current = (current + " " + token) if current else token
    if current:
        lines.append(current)
    return lines


def score_to_dict(score: Score) -> dict:
    """给 WebUI 用的 JSON 结构（位置单位是四分音符）"""
    return {
        "title": score.title,
        "bpm": round(score.base_bpm, 4),
        "gate": score.gate,
        "duration": round(score.duration, 4),
        "voices": list(score.voices),
        "info": list(score.info),
        "meter": list(score.meter) if score.meter else None,
        "notes": [
            {"beat": round(ev.beat, 6), "beats": round(ev.beats, 6),
             "tick": ev.tick, "ticks": ev.ticks,
             "midis": list(ev.midis), "voice": ev.voice}
            for ev in score.events
        ],
    }


# --------------------------------------------------------------------------- #
# 四、演奏计划：把音符摊平成「某时刻按下 / 松开某个键」
# --------------------------------------------------------------------------- #

@dataclass
class KeyAction:
    time: float      # 相对开始的秒数
    vk: int          # 虚拟键码
    down: bool       # True=按下，False=松开
    serial: int = 0  # 按下编号，用来配对松开（重叠/重复音不乱序）


def note_hold(event: Event, gate: float) -> float:
    return max(MIN_HOLD, event.dur * gate)


def build_schedule(score: Score, gate: Optional[float] = None) -> Tuple[List[KeyAction], int]:
    """生成按键动作表（已按时间排序），返回 (动作表, 被跳过的越界音数)"""
    ratio = score.gate if gate is None else gate
    actions: List[KeyAction] = []
    skipped = 0
    serial = 0
    for ev in score.events:
        vks: List[int] = []
        for midi in ev.midis:
            vk = MIDI_TO_VK.get(midi)
            if vk is None:
                skipped += 1
            else:
                vks.append(vk)
        if not vks:
            continue
        serial += 1
        hold = note_hold(ev, ratio)
        for vk in vks:
            actions.append(KeyAction(ev.time, vk, True, serial))
            actions.append(KeyAction(ev.time + hold, vk, False, serial))
    # 同一时刻先松后按，重复音才能干净地再触发
    actions.sort(key=lambda a: (a.time, a.down))
    return actions, skipped


def plan_duration(actions: Sequence[KeyAction]) -> float:
    return max((a.time for a in actions), default=0.0)


# --------------------------------------------------------------------------- #
# 五、按键注入后端
# --------------------------------------------------------------------------- #

class WindowsKeyboard:
    """用 SendInput 注入硬件级按键事件（Windows）"""

    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_SCANCODE = 0x0008

    def __init__(self, method: str = "vk"):
        if not (_IS_WINDOWS and _HAS_CTYPES):
            raise RuntimeError("SendInput 只在 Windows 上可用")
        if method not in ("vk", "scan"):
            raise ValueError("method 只能是 vk 或 scan")
        self.method = method
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._send_input = self._user32.SendInput
        self._send_input.argtypes = (wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)
        self._send_input.restype = wintypes.UINT
        self._map_vk = self._user32.MapVirtualKeyW
        self._map_vk.argtypes = (wintypes.UINT, wintypes.UINT)
        self._map_vk.restype = wintypes.UINT

    def _make_input(self, vk: int, up: bool) -> "_INPUT":
        item = _INPUT()
        item.type = self.INPUT_KEYBOARD
        flags = self.KEYEVENTF_KEYUP if up else 0
        if self.method == "scan":
            item.u.ki.wScan = self._map_vk(vk, 0)   # MAPVK_VK_TO_VSC
            item.u.ki.wVk = 0
            item.u.ki.dwFlags = flags | self.KEYEVENTF_SCANCODE
        else:
            item.u.ki.wVk = vk
            item.u.ki.wScan = 0
            item.u.ki.dwFlags = flags
        return item

    def _emit(self, vk: int, up: bool) -> None:
        item = self._make_input(vk, up)
        sent = self._send_input(1, ctypes.byref(item), ctypes.sizeof(_INPUT))
        if sent != 1:
            raise ctypes.WinError(ctypes.get_last_error())

    def key_down(self, vk: int) -> None:
        self._emit(vk, False)

    def key_up(self, vk: int) -> None:
        self._emit(vk, True)


class PynputKeyboard:
    """跨平台的兜底方案：需要 pip install pynput"""

    def __init__(self) -> None:
        from pynput.keyboard import Controller, Key  # type: ignore

        self._controller = Controller()
        self._key_enter = Key.enter

    def _resolve(self, vk: int):
        char = VK_TO_CHAR.get(vk)
        if char is None:
            raise ValueError("未登记的键码 {}".format(vk))
        return self._key_enter if char == "\n" else char

    def key_down(self, vk: int) -> None:
        self._controller.press(self._resolve(vk))

    def key_up(self, vk: int) -> None:
        self._controller.release(self._resolve(vk))


class Recorder:
    """不发按键，只记录（测试 / 调试用）"""

    def __init__(self) -> None:
        self.events: List[Tuple[str, int, float]] = []
        self._t0 = time.perf_counter()

    def key_down(self, vk: int) -> None:
        self.events.append(("down", vk, time.perf_counter() - self._t0))

    def key_up(self, vk: int) -> None:
        self.events.append(("up", vk, time.perf_counter() - self._t0))


def create_backend(method: str):
    if method in ("vk", "scan"):
        if _IS_WINDOWS:
            return WindowsKeyboard(method)
        print("· 非 Windows 系统，自动改用 pynput 发送按键", file=sys.stderr)
    try:
        return PynputKeyboard()
    except ImportError:
        raise RuntimeError(
            "当前平台需要 pynput 才能发按键：pip install pynput"
        ) from None


# --------------------------------------------------------------------------- #
# 六、执行：按时间轴触发
# --------------------------------------------------------------------------- #

def _raise_timer_resolution() -> bool:
    """Windows 默认睡眠精度 ~15ms，抬到 1ms 才能卡准拍子"""
    if not (_IS_WINDOWS and _HAS_CTYPES):
        return False
    try:
        return ctypes.WinDLL("winmm").timeBeginPeriod(1) == 0
    except Exception:
        return False


def _lower_timer_resolution() -> None:
    if not (_IS_WINDOWS and _HAS_CTYPES):
        return
    try:
        ctypes.WinDLL("winmm").timeEndPeriod(1)
    except Exception:
        pass


def _wait_until(target: float, stop: Optional[threading.Event]) -> None:
    """睡到 target（perf_counter 时刻），最后 3ms 自旋保证精度"""
    while True:
        remain = target - time.perf_counter()
        if remain <= 0:
            return
        if remain > 0.003:
            if stop is not None:
                if stop.wait(min(remain - 0.002, 0.05)):
                    return
            else:
                time.sleep(remain - 0.002)


def _release_all(backend, held: Dict[int, int]) -> None:
    for vk in list(held):
        try:
            backend.key_up(vk)
        except Exception:
            pass
    held.clear()


def run_schedule(actions: Sequence[KeyAction], backend,
                 stop: Optional[threading.Event] = None) -> bool:
    """按时间轴发按键，返回 True 表示完整演奏完"""
    held: Dict[int, int] = {}
    start = time.perf_counter() + 0.06      # 留一点余量，避免第一个音来不及
    try:
        for act in actions:
            if stop is not None and stop.is_set():
                return False
            _wait_until(start + act.time, stop)
            if stop is not None and stop.is_set():
                return False
            if act.down:
                if act.vk in held:          # 同一个键还按着，先松再按
                    backend.key_up(act.vk)
                backend.key_down(act.vk)
                held[act.vk] = act.serial
            elif held.get(act.vk) == act.serial:
                backend.key_up(act.vk)
                del held[act.vk]
        return True
    finally:
        _release_all(backend, held)


class StopHotkey:
    """全局热键（默认 Esc）：无论当前焦点在哪个窗口，都能中止演奏"""

    WM_HOTKEY = 0x0312
    WM_QUIT = 0x0012
    _ID = 0x5049

    def __init__(self, vk: int, modifiers: int = 0):
        self.vk = vk
        self.modifiers = modifiers
        self.triggered = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._tid = 0
        self._ready = threading.Event()
        self._ok = False

    def start(self) -> bool:
        if not (_IS_WINDOWS and _HAS_CTYPES):
            return False
        self._thread = threading.Thread(target=self._run, name="stop-hotkey", daemon=True)
        self._thread.start()
        self._ready.wait(1.0)
        return self._ok

    def _run(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._tid = ctypes.WinDLL("kernel32").GetCurrentThreadId()
        if not user32.RegisterHotKey(None, self._ID, self.modifiers, self.vk):
            self._ready.set()
            return
        self._ok = True
        self._ready.set()
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == self.WM_HOTKEY:
                    self.triggered.set()
                    break
        finally:
            user32.UnregisterHotKey(None, self._ID)

    def stop(self) -> None:
        if not (_IS_WINDOWS and _HAS_CTYPES):
            return
        try:
            ctypes.WinDLL("user32").UnregisterHotKey(None, self._ID)
            if self._tid:
                ctypes.WinDLL("user32").PostThreadMessageW(self._tid, self.WM_QUIT, 0, 0)
        except Exception:
            pass


_STOP_KEY_CODES = {
    "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "pause": 0x13,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73,
    "f5": 0x74, "f6": 0x75, "f7": 0x76, "f8": 0x77,
    "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
}


def parse_stop_key(text: str) -> Optional[int]:
    if not text:
        return None
    return _STOP_KEY_CODES.get(text.strip().lower())


# --------------------------------------------------------------------------- #
# 七、命令行
# --------------------------------------------------------------------------- #

def print_keymap() -> None:
    rows = (
        ("1 行", "1 2 3 4 5 6 7 8 9 0 - ="),
        ("Q 行", "Q W E R T Y U I O P [ ]"),
        ("A 行", "A S D F G H J K L ; ' Enter"),
    )
    print("键盘 → 音高（每行 12 键，严格半音阶，行首均为 E）\n")
    for label, keys in rows:
        midis = [KEY_TO_MIDI[k.lower()] for k in keys.split()]
        print("  {}  {}  →  {} 到 {}".format(
            label, keys, midi_name(midis[0]), midi_name(midis[-1])))
    print("\n逐键对照：")
    for keys, base in _ROW_SPEC:
        for i, key in enumerate(keys):
            print("  {:<3} {:<5}".format(key.upper(), midi_name(base + i)), end="")
            if (i + 1) % 6 == 0:
                print()
        print()
    print("  {:<3} {:<5}".format("Enter", midi_name(63)))


def _display_width(text: str) -> int:
    """终端显示宽度：中文/全角算 2 格"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _cell(text: str, width: int, align: str = "left") -> str:
    pad = max(0, width - _display_width(text))
    return (" " * pad + text) if align == "right" else (text + " " * pad)


def print_plan(score: Score, gate: Optional[float] = None, skipped: int = 0) -> None:
    ratio = score.gate if gate is None else gate
    if score.title:
        print("曲谱：{}".format(score.title))
    print("速度：{:.0f} BPM　　时长：{:.2f} 秒　　事件：{} 个 / 音 {} 个".format(
        score.base_bpm,
        score.duration,
        len(score.events),
        sum(len(e.midis) for e in score.events),
    ))
    if score.meter:
        print("拍号：{}/{}（每小节 {} 拍）".format(
            score.meter[0], score.meter[1],
            _num(PPQ * 4 * score.meter[0] / score.meter[1] / PPQ)))
    if len(score.voices) > 1:
        print("声部：{}".format("、".join(score.voices)))
    for line in score.info:
        print("· {}".format(line))
    print("按住比例：{}（每个键按住时值的 {}%）".format(ratio, int(ratio * 100)))
    if skipped:
        print("！ 有 {} 个音超出键盘音域（{} ~ {}），已跳过".format(
            skipped, midi_name(LOWEST_MIDI), midi_name(HIGHEST_MIDI)))
    for warning in score.warnings:
        print("！ {}".format(warning), file=sys.stderr)

    rows: List[Tuple[str, ...]] = []
    for i, ev in enumerate(score.events, 1):
        names = ev.names
        if len(names) > 26:
            names = names[:23] + "..."
        rows.append((
            str(i),
            "{:.3f}".format(ev.time),
            "{:.3f}".format(note_hold(ev, ratio)),
            ev.labels,
            names,
            ev.voice,
        ))

    headers = ("#", "时间(s)", "按住(s)", "按键", "音名", "声部")
    aligns = ("right", "right", "right", "left", "left", "left")
    widths = [_display_width(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], _display_width(cell))

    print()
    head = "  " + "  ".join(_cell(h, w, a) for h, w, a in zip(headers, widths, aligns))
    print(head)
    print("  " + "-" * (_display_width(head) - 2))
    for row in rows:
        print(("  " + "  ".join(_cell(c, w, a) for c, w, a in zip(row, widths, aligns))).rstrip())


def build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="piano.py",
        description="读 txt/csv 曲谱，按拍子自动敲键盘（对着网页钢琴 / DAW / 游戏用）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python piano.py scores/twinkle.txt\n"
            "  python piano.py scores/canon.txt --countdown 5\n"
            "  python piano.py scores/twinkle.txt --print\n"
            "  python piano.py scores/ode_to_joy.txt -t 90 --gate 0.5\n"
            "  python piano.py --keys\n"
        ),
    )
    parser.add_argument("score", nargs="?", metavar="曲谱", help="曲谱文件（.txt / .csv）")
    parser.add_argument("-p", "--print", dest="show", action="store_true",
                        help="只打印演奏计划，不按键")
    parser.add_argument("-c", "--countdown", type=float, default=3.0, metavar="秒",
                        help="开始前的倒计时，默认 3 秒（0 = 立刻开始）")
    parser.add_argument("-g", "--gate", type=float, metavar="比例",
                        help="按键按住比例 0.05~1，默认 0.9")
    parser.add_argument("-t", "--tempo", type=float, metavar="BPM",
                        help="整体速度（覆盖曲谱里的基准速度）")
    parser.add_argument("-T", "--transpose", type=int, default=0, metavar="半音",
                        help="整体升降调，比如 -12 降一个八度")
    parser.add_argument("--fit", choices=("auto", "none", "fold"), default="auto",
                        help="导入 MusicXML/MIDI 时如何塞进 36 键音域："
                             "auto 自动平移八度（默认）/ none 直接丢 / fold 折回音域")
    parser.add_argument("--max-notes", type=int, default=0, metavar="N",
                        help="同一时刻最多保留 N 个音（默认不限制）")
    parser.add_argument("--json", action="store_true",
                        help="输出解析结果的 JSON（给 WebUI / 其它程序用）")
    parser.add_argument("-m", "--method", choices=("vk", "scan", "pynput"), default="vk",
                        help="按键注入方式：vk（默认）/ scan / pynput")
    parser.add_argument("--stop-key", default="esc", metavar="键",
                        help="全局中止热键，默认 esc，可填 f1~f12 / space / pause / off")
    parser.add_argument("--self-test", action="store_true",
                        help="敲 a s d f 自检按键注入是否正常")
    parser.add_argument("--keys", action="store_true", help="打印键盘键位表")
    return parser


def _countdown(seconds: float, stop: Optional[threading.Event]) -> bool:
    if seconds <= 0:
        return True
    end = time.perf_counter() + seconds
    while True:
        left = end - time.perf_counter()
        if left <= 0:
            break
        if stop is not None and stop.is_set():
            print("\r" + " " * 46 + "\r", end="", file=sys.stderr)
            return False
        print("\r{:>4.1f}s 后开始敲键盘，请切到目标窗口（Esc 停止）".format(left),
              end="", file=sys.stderr)
        time.sleep(min(0.1, max(0.01, left)))
    print("\r" + " " * 46 + "\r", end="", file=sys.stderr)
    return True


def run_self_test(method: str, countdown: float) -> int:
    """敲 a s d f 给你看，确认按键注入这条链路是通的"""
    try:
        backend = create_backend(method)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print("自检：把光标放到一个能输入文字的地方（记事本、浏览器地址栏都行），")
    print("接下来会依次敲出 a s d f，每个按住 0.12 秒。")
    actions: List[KeyAction] = []
    for i, vk in enumerate((0x41, 0x53, 0x44, 0x46)):   # a s d f
        serial = i + 1
        actions.append(KeyAction(i * 0.35, vk, True, serial))
        actions.append(KeyAction(i * 0.35 + 0.12, vk, False, serial))

    _raise_timer_resolution()
    try:
        if not _countdown(countdown, None):
            return 130
        run_schedule(actions, backend)
    except Exception as exc:
        print("发送按键失败：{}".format(exc), file=sys.stderr)
        print("若目标程序是管理员权限，本程序也要用管理员权限运行。", file=sys.stderr)
        return 1
    finally:
        _lower_timer_resolution()

    print("已发送 a s d f —— 看到了说明按键注入正常。")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    args = build_argparser().parse_args(argv)

    if args.keys:
        print_keymap()
        return 0
    if args.self_test:
        return run_self_test(args.method, args.countdown)
    if not args.score:
        print("请给出曲谱文件，例如：python piano.py scores/twinkle.txt", file=sys.stderr)
        print("（用 --keys 可以查看键位表）", file=sys.stderr)
        return 2
    if not os.path.isfile(args.score):
        print("找不到曲谱文件：{}".format(args.score), file=sys.stderr)
        return 2

    try:
        score = load_score(args.score, transpose=args.transpose,
                           fit=args.fit, max_notes=args.max_notes)
    except ScoreError as exc:
        print("曲谱有误：{}".format(exc), file=sys.stderr)
        return 1

    if args.tempo:
        score.rescale_tempo(args.tempo)
    if not score.events:
        print("曲谱里没有音符。", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(score_to_dict(score), ensure_ascii=False))
        return 0

    gate = score.gate if args.gate is None else min(max(args.gate, 0.05), 1.0)
    actions, skipped = build_schedule(score, gate)
    if skipped and not args.show:
        print("！ 有 {} 个音超出键盘音域（{} ~ {}），已跳过".format(
            skipped, midi_name(LOWEST_MIDI), midi_name(HIGHEST_MIDI)), file=sys.stderr)
    if not actions:
        print("没有落在键盘音域内（{} ~ {}）的音，无事可做；检查一下 --transpose。".format(
            midi_name(LOWEST_MIDI), midi_name(HIGHEST_MIDI)), file=sys.stderr)
        return 1

    if args.show:
        print_plan(score, gate, skipped)
        print("\n共 {} 次按键动作，预计 {:.2f} 秒。".format(len(actions), plan_duration(actions)))
        return 0

    stop_vk = None if args.stop_key.strip().lower() in ("off", "none", "") \
        else parse_stop_key(args.stop_key)
    if args.stop_key and args.stop_key.strip().lower() not in ("off", "none", "") and stop_vk is None:
        print("！ 不认识的 --stop-key {!r}，已关闭中止热键".format(args.stop_key),
              file=sys.stderr)

    hotkey: Optional[StopHotkey] = None
    if stop_vk is not None:
        hotkey = StopHotkey(stop_vk)
        if not hotkey.start():
            print("！ 中止热键注册失败（可能被其它程序占用），"
                  "可用 Ctrl+C 或 --stop-key off", file=sys.stderr)
            hotkey = None

    stop = hotkey.triggered if hotkey is not None else None
    try:
        backend = create_backend(args.method)
    except RuntimeError as exc:
        if hotkey is not None:
            hotkey.stop()
        print(str(exc), file=sys.stderr)
        return 1

    _raise_timer_resolution()
    try:
        if not _countdown(args.countdown, stop):
            print("已取消。", file=sys.stderr)
            return 130
        print("▶ 开始演奏{}（{:.1f} 秒，{} 次按键）".format(
            "：" + score.title if score.title else "",
            plan_duration(actions), len(actions)), file=sys.stderr)
        try:
            finished = run_schedule(actions, backend, stop)
        except KeyboardInterrupt:
            print("\n已中断。", file=sys.stderr)
            return 130
        except Exception as exc:                     # 例如权限不足（UIPI）
            print("发送按键失败：{}".format(exc), file=sys.stderr)
            print("如果目标程序以管理员身份运行，本程序也需要用管理员权限启动。",
                  file=sys.stderr)
            return 1
    finally:
        _lower_timer_resolution()
        if hotkey is not None:
            hotkey.stop()

    if not finished:
        print("已中止（{}）。".format(args.stop_key), file=sys.stderr)
        return 130
    print("完成。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
