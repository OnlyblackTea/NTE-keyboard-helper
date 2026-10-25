#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""importers.py —— 把 MuseScore / 其它打谱软件导出的谱子读成「音符表」

只做纯数据转换，不依赖 piano.py，返回的都是
    notes  = [(起始位置, 时值, MIDI), ...]      位置与时值单位都是四分音符
    tempos = [(位置, BPM), ...]
    title  = 曲名

支持：MusicXML（.musicxml / .xml）、压缩 MusicXML（.mxl）、MIDI（.mid / .midi）。
MuseScore 里「文件 → 导出」选这三种任意一种即可。
"""

from __future__ import annotations

import os
import struct
import zipfile
from typing import Dict, List, Optional, Sequence, Tuple
from xml.etree import ElementTree as ET

Note = Tuple[float, float, int]          # (四分音符位置, 时值, MIDI)
Tempo = Tuple[float, float]              # (四分音符位置, BPM)

_STEP_SEMITONE = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_BEAT_UNIT_QUARTERS = {
    "whole": 4.0, "half": 2.0, "quarter": 1.0, "eighth": 0.5,
    "16th": 0.25, "32nd": 0.125, "64th": 0.0625,
}


# --------------------------------------------------------------------------- #
# MusicXML
# --------------------------------------------------------------------------- #

def _open_xml(path: str) -> bytes:
    """读 .mxl（其实是 zip）或普通 xml"""
    with open(path, "rb") as handle:
        head = handle.read(4)
    if head[:2] == b"PK":
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            target: Optional[str] = None
            if "META-INF/container.xml" in names:
                container = ET.fromstring(archive.read("META-INF/container.xml"))
                for rootfile in container.iter("rootfile"):
                    candidate = rootfile.get("full-path")
                    if candidate and candidate in names:
                        target = candidate
                        break
            if target is None:
                for name in names:
                    low = name.lower()
                    if "META-INF" in name.upper():
                        continue
                    if low.endswith((".musicxml", ".xml")):
                        target = name
                        break
            if target is None:
                raise ValueError("这个压缩包里没找到 MusicXML 文件")
            return archive.read(target)
    with open(path, "rb") as handle:
        return handle.read()


def _direction_tempo(element: ET.Element) -> Optional[float]:
    """<direction> 里的速度，统一换算成「每分钟四分音符数」"""
    for sound in element.iter("sound"):
        value = sound.get("tempo")
        if value:
            try:
                return float(value)
            except ValueError:
                pass
    for metronome in element.iter("metronome"):
        unit = (metronome.findtext("beat-unit") or "").strip()
        per_minute = metronome.findtext("per-minute")
        if unit in _BEAT_UNIT_QUARTERS and per_minute:
            try:
                return float(per_minute) * _BEAT_UNIT_QUARTERS[unit]
            except ValueError:
                pass
    return None


def read_musicxml(path: str) -> Tuple[List[Note], List[Tempo], str, Optional[Tuple[int, int]]]:
    root = ET.fromstring(_open_xml(path))
    if root.tag == "score-timewise":
        raise ValueError("暂不支持 timewise 格式的 MusicXML，请用分谱（partwise）导出")

    title = (root.findtext("work/work-title")
             or root.findtext("movement-title") or "").strip()

    notes: List[List[float]] = []       # 可变，方便连音线延长时值
    tempos: List[Tempo] = []
    tie_open: Dict[Tuple[str, str, int], int] = {}
    meter: Optional[Tuple[int, int]] = None

    for part in root.iter("part"):
        part_id = part.get("id", "")
        divisions = 1.0
        measure_start = 0.0             # 单位：四分音符
        measure_quarters = 4.0

        for measure in part.iter("measure"):
            implicit = measure.get("implicit") == "yes"
            cursor = 0.0                # 单位：divisions
            max_cursor = 0.0
            last_start = 0.0

            for child in measure:
                tag = child.tag

                if tag == "attributes":
                    raw = child.findtext("divisions")
                    if raw:
                        try:
                            divisions = max(1.0, float(raw))
                        except ValueError:
                            pass
                    beats = child.findtext("time/beats")
                    beat_type = child.findtext("time/beat-type")
                    if beats and beat_type:
                        try:
                            measure_quarters = float(beats) * 4.0 / float(beat_type)
                            if meter is None:
                                meter = (int(round(float(beats))),
                                         int(round(float(beat_type))))
                        except (ValueError, ZeroDivisionError):
                            pass
                    continue

                if tag == "backup":
                    cursor -= float(child.findtext("duration") or 0)
                    continue

                if tag == "forward":
                    cursor += float(child.findtext("duration") or 0)
                    max_cursor = max(max_cursor, cursor)
                    continue

                if tag == "direction":
                    bpm = _direction_tempo(child)
                    if bpm and bpm > 0:
                        tempos.append((measure_start + cursor / divisions, bpm))
                    continue

                if tag != "note":
                    continue

                try:
                    duration = float(child.findtext("duration") or 0)
                except ValueError:
                    duration = 0.0
                if child.find("grace") is not None:
                    duration = 0.0

                if child.find("chord") is not None:
                    start = last_start            # 和弦里的后续音，不推进游标
                else:
                    start = cursor
                    cursor += duration
                    max_cursor = max(max_cursor, cursor)
                    last_start = start

                if child.find("rest") is not None or duration <= 0:
                    continue

                pitch = child.find("pitch")
                if pitch is None:
                    continue
                step = (pitch.findtext("step") or "C").upper()
                alter_text = pitch.findtext("alter") or "0"
                octave_text = pitch.findtext("octave") or "4"
                try:
                    midi = (12 * (int(octave_text) + 1) + _STEP_SEMITONE.get(step, 0)
                            + int(round(float(alter_text))))
                except ValueError:
                    continue

                voice = child.findtext("voice") or "1"
                tie_types = {t.get("type") for t in child.findall("tie")}
                tie_types |= {t.get("type") for t in child.findall("notations/tied")}

                position = measure_start + start / divisions
                length = duration / divisions
                key = (part_id, voice, midi)

                if "stop" in tie_types and key in tie_open:
                    index = tie_open.pop(key)
                    notes[index][1] = max(notes[index][1], position + length - notes[index][0])
                    if "start" in tie_types:
                        tie_open[key] = index
                    continue

                notes.append([position, length, midi])
                if "start" in tie_types:
                    tie_open[key] = len(notes) - 1

            measured = max_cursor / divisions
            if implicit or measured > measure_quarters + 1e-6:
                measure_start += measured if measured > 0 else measure_quarters
            else:
                measure_start += measure_quarters

    if not notes:
        raise ValueError("MusicXML 里没有读到音符")
    return _cleanup(notes), _cleanup_tempos(tempos), title, meter


# --------------------------------------------------------------------------- #
# MIDI
# --------------------------------------------------------------------------- #

def _read_varlen(data: bytes, pos: int) -> Tuple[int, int]:
    value = 0
    while True:
        if pos >= len(data):
            raise ValueError("MIDI 数据在变长整数处意外结束")
        byte = data[pos]
        pos += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, pos


def _parse_track(data: bytes, division: float, notes: List[Note], tempos: List[Tempo],
                 meters: List[Tuple[int, int]]) -> None:
    pos = 0
    tick = 0
    status = 0
    pending: Dict[Tuple[int, int], List[int]] = {}

    while pos < len(data):
        delta, pos = _read_varlen(data, pos)
        tick += delta
        if pos >= len(data):
            break

        byte = data[pos]
        if byte & 0x80:
            status = byte
            pos += 1

        if status == 0xFF:                      # meta
            if pos >= len(data):
                break
            kind = data[pos]
            pos += 1
            length, pos = _read_varlen(data, pos)
            payload = data[pos:pos + length]
            pos += length
            if kind == 0x51 and length == 3:
                micros = (payload[0] << 16) | (payload[1] << 8) | payload[2]
                if micros > 0:
                    tempos.append((tick / division, 60_000_000.0 / micros))
            elif kind == 0x58 and length >= 2:
                num, power = payload[0], payload[1]
                if num and power < 8:
                    meters.append((num, 1 << power))
            elif kind == 0x2F:                  # end of track
                break
            continue

        if status in (0xF0, 0xF7):              # sysex
            length, pos = _read_varlen(data, pos)
            pos += length
            continue

        kind = status & 0xF0
        channel = status & 0x0F
        if kind in (0x80, 0x90, 0xA0, 0xB0, 0xE0):
            if pos + 1 >= len(data):
                break
            first, second = data[pos], data[pos + 1]
            pos += 2
        elif kind in (0xC0, 0xD0):
            if pos >= len(data):
                break
            pos += 1
            continue
        else:
            raise ValueError("不认识的 MIDI 事件：0x{:02X}".format(status))

        if kind == 0x90 and second > 0:
            pending.setdefault((channel, first), []).append(tick)
        elif kind == 0x80 or (kind == 0x90 and second == 0):
            stack = pending.get((channel, first))
            if stack:
                start = stack.pop()
                if tick > start:
                    notes.append((start / division, (tick - start) / division, first))


def read_midi(path: str) -> Tuple[List[Note], List[Tempo], str, Optional[Tuple[int, int]]]:
    with open(path, "rb") as handle:
        data = handle.read()
    if len(data) < 14 or data[:4] != b"MThd":
        raise ValueError("不是 MIDI 文件")

    header_len = struct.unpack(">I", data[4:8])[0]
    division = struct.unpack(">H", data[12:14])[0]
    if division & 0x8000:
        raise ValueError("不支持 SMPTE 计时的 MIDI，请在 MuseScore 里重新导出")

    pos = 8 + header_len
    notes: List[Note] = []
    tempos: List[Tempo] = []
    meters: List[Tuple[int, int]] = []
    while pos + 8 <= len(data) and data[pos:pos + 4] == b"MTrk":
        length = struct.unpack(">I", data[pos + 4:pos + 8])[0]
        _parse_track(data[pos + 8:pos + 8 + length], float(division), notes, tempos, meters)
        pos += 8 + length

    if not notes:
        raise ValueError("MIDI 里没有读到音符")
    return _cleanup(notes), _cleanup_tempos(tempos), "", (meters[0] if meters else None)


# --------------------------------------------------------------------------- #
# 通用收尾 + 适配 36 键音域
# --------------------------------------------------------------------------- #

def _cleanup(notes: Sequence[Sequence[float]]) -> List[Note]:
    """四舍五入到 1/48 个四分音符，去掉重复音"""
    seen = set()
    out: List[Note] = []
    for position, length, midi in notes:
        if length <= 0:
            continue
        item = (round(float(position) * 48) / 48, round(float(length) * 48) / 48,
                int(midi))
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    out.sort()
    return out


def _cleanup_tempos(tempos: Sequence[Tempo]) -> List[Tempo]:
    cleaned: List[Tempo] = []
    for position, bpm in sorted(tempos):
        bpm = round(float(bpm), 4)
        if bpm <= 0:
            continue
        if cleaned and abs(cleaned[-1][0] - position) < 1e-6:
            cleaned[-1] = (position, bpm)
        elif cleaned and abs(cleaned[-1][1] - bpm) < 1e-6:
            continue
        else:
            cleaned.append((float(position), bpm))
    if not cleaned or cleaned[0][0] > 1e-6:
        cleaned.insert(0, (0.0, cleaned[0][1] if cleaned else 120.0))
    return cleaned


def read_any(path: str) -> Tuple[List[Note], List[Tempo], str, Optional[Tuple[int, int]]]:
    ext = os.path.splitext(path)[1].lower()
    if ext in (".mid", ".midi"):
        return read_midi(path)
    return read_musicxml(path)


def choose_octave_shift(notes: Sequence[Note], low: int = 52, high: int = 87) -> int:
    """整体升降几个八度能让最多音符落进键盘音域"""
    best, best_count = 0, -1
    for shift in sorted(range(-5, 7), key=lambda k: (abs(k), k)):
        count = sum(1 for _, _, midi in notes if low <= midi + 12 * shift <= high)
        if count > best_count:
            best, best_count = shift, count
    return best


def fit_to_keyboard(notes: Sequence[Note], mode: str = "auto",
                    low: int = 52, high: int = 87,
                    max_notes: int = 0) -> Tuple[List[Note], dict]:
    """把音符塞进 E3~D#6。

    mode = auto  先整体平移八度，超出的丢掉
    mode = none  不平移，超出的丢掉
    mode = fold  超出的按八度折回音域内
    """
    shift = choose_octave_shift(notes, low, high) if mode == "auto" else 0
    stats = {"input": len(notes), "shift": shift, "dropped": 0, "folded": 0}
    fitted: List[Note] = []

    for position, length, midi in notes:
        value = midi + 12 * shift
        if not (low <= value <= high):
            if mode == "fold":
                while value < low:
                    value += 12
                    stats["folded"] += 1
                while value > high:
                    value -= 12
                    stats["folded"] += 1
            else:
                stats["dropped"] += 1
                continue
        fitted.append((position, length, value))

    # 同一时刻同一个音只留一个，并可选地只保留最高的 N 个
    by_onset: Dict[float, List[int]] = {}
    for position, _, midi in fitted:
        by_onset.setdefault(position, []).append(midi)

    limit = max_notes if max_notes and max_notes > 0 else 0
    stats["max_chord"] = max((len(v) for v in by_onset.values()), default=0)
    if limit and stats["max_chord"] > limit:
        keep = set()
        for position, pitches in by_onset.items():
            for midi in sorted(pitches, reverse=True)[:limit]:
                keep.add((position, midi))
        result: List[Note] = []
        used = set()
        for position, length, midi in fitted:
            key = (position, midi)
            if key in keep and key not in used:
                used.add(key)
                result.append((position, length, midi))
        fitted = result
        stats["trimmed"] = stats["max_chord"]

    stats["kept"] = len(fitted)
    fitted.sort()
    return fitted, stats
