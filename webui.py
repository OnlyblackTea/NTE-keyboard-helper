#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""webui.py —— 本地可视化谱面编辑器

在浏览器里点格子写曲子：听得见、看得着，存下来就是 piano.py 能直接演奏的 txt 曲谱。

    python webui.py            # 起服务并打开浏览器
    python webui.py --port 9000 --no-open

解析和「音符 → txt」的转换都交给 piano.py，前端只负责画界面，两边不会出现两套格式。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import piano

ROOT = os.path.dirname(os.path.abspath(__file__))
SCORES = os.path.join(ROOT, "scores")
BACKUP = os.path.join(ROOT, "backup")
PAGE = os.path.join(ROOT, "webui.html")

# 文件名里不能出现的字符（路径分隔符、通配符、控制字符）
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_name(text: str) -> str:
    """把用户给的标题清洗成一个安全的文件名主干"""
    name = _BAD_CHARS.sub("", str(text or "")).strip().strip(".")
    name = name[:60].strip()
    return name or "untitled"


def list_scores():
    items = []
    if not os.path.isdir(SCORES):
        return items
    for name in sorted(os.listdir(SCORES)):
        if not name.lower().endswith((".txt", ".csv")):
            continue
        title = ""
        try:
            with open(os.path.join(SCORES, name), "r", encoding="utf-8-sig") as handle:
                for _ in range(20):
                    line = handle.readline()
                    if not line:
                        break
                    stripped = line.strip()
                    if stripped.lower().startswith("@title"):
                        title = stripped[6:].strip()
                        break
        except OSError:
            pass
        items.append({"name": name, "title": title or name})
    return items


def _notes_from_payload(payload):
    notes = []
    for item in payload.get("notes") or []:
        midis = item.get("midis") or []
        # WebUI 用整数 tick 传位置，避免 1/3 拍这种值反复换算累积误差
        if "tick" in item and "ticks" in item:
            beat = piano.ticks_to_beats(item["tick"])
            beats = piano.ticks_to_beats(item["ticks"])
        else:
            beat = float(item.get("beat", 0))
            beats = float(item.get("beats", 1))
        notes.append((beat, beats, midis, str(item.get("voice") or "1")))
    return notes


def render_payload(payload) -> str:
    meter = payload.get("meter")
    if isinstance(meter, (list, tuple)) and len(meter) == 2:
        meter = (int(meter[0]), int(meter[1]))
    else:
        meter = None
    return piano.dump_txt(
        _notes_from_payload(payload),
        title=str(payload.get("title") or "").strip(),
        bpm=float(payload.get("bpm") or piano.DEFAULT_BPM),
        gate=payload.get("gate"),
        meter=meter,
    )


class Handler(BaseHTTPRequestHandler):
    server_version = "PianoWebUI/1.0"

    def log_message(self, fmt, *args):   # 别把每个请求都刷到终端
        pass

    # ---- 底层 ----
    def _send(self, code: int, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except BrokenPipeError:
            pass

    def _json(self, payload, code: int = 200):
        self._send(code, json.dumps(payload, ensure_ascii=False))

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    # ---- 路由 ----
    def do_GET(self):
        url = urlparse(self.path)
        try:
            if url.path in ("/", "/index.html"):
                with open(PAGE, "rb") as handle:
                    self._send(200, handle.read(), "text/html; charset=utf-8")
            elif url.path == "/api/keys":
                self._json({
                    "low": piano.LOWEST_MIDI,
                    "high": piano.HIGHEST_MIDI,
                    "keys": [
                        {"midi": midi, "name": piano.midi_name(midi),
                         "key": piano.MIDI_TO_KEY[midi]}
                        for midi in sorted(piano.MIDI_TO_KEY)
                    ],
                })
            elif url.path == "/api/scores":
                self._json({"scores": list_scores()})
            elif url.path == "/api/score":
                name = (parse_qs(url.query).get("name") or [""])[0]
                self._open_score(name)
            else:
                self._send(404, "not found", "text/plain; charset=utf-8")
        except Exception as exc:                       # noqa: BLE001
            self._json({"error": str(exc)}, 500)

    def _open_score(self, name: str):
        if not name or "/" in name or "\\" in name or not name.lower().endswith((".txt", ".csv")):
            return self._json({"error": "文件名不合法"}, 400)
        path = os.path.join(SCORES, os.path.basename(name))
        if not os.path.isfile(path):
            return self._json({"error": "找不到 {}".format(name)}, 404)
        try:
            score = piano.load_score(path)
        except piano.ScoreError as exc:
            return self._json({"error": str(exc)}, 400)
        payload = piano.score_to_dict(score)
        payload["name"] = os.path.basename(name)
        self._json(payload)

    def do_POST(self):
        url = urlparse(self.path)
        try:
            payload = self._read_json()
        except Exception:                              # noqa: BLE001
            return self._json({"error": "请求不是合法 JSON"}, 400)

        try:
            if url.path == "/api/render":
                self._json({"text": render_payload(payload)})
            elif url.path == "/api/save":
                self._save(payload)
            else:
                self._send(404, "not found", "text/plain; charset=utf-8")
        except Exception as exc:                       # noqa: BLE001
            self._json({"error": str(exc)}, 400)

    def _save(self, payload):
        name = str(payload.get("name") or payload.get("title") or "").strip()
        if name.lower().endswith(".txt"):
            name = name[:-4]
        name = safe_name(name)
        text = render_payload(payload)
        os.makedirs(SCORES, exist_ok=True)
        path = os.path.join(SCORES, name + ".txt")

        # 覆盖前先把旧内容留一份，改错了还能捞回来
        backup_file = ""
        if os.path.exists(path):
            try:
                os.makedirs(BACKUP, exist_ok=True)
                target = os.path.join(BACKUP, name + ".prev.txt")
                shutil.copyfile(path, target)
                backup_file = os.path.relpath(target, ROOT)
            except OSError:
                backup_file = ""

        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        self._json({"ok": True, "file": name + ".txt", "path": path,
                    "overwrote": bool(backup_file), "backup": backup_file,
                    "text": text})


def serve(host: str, port: int, open_browser: bool) -> int:
    if not os.path.isfile(PAGE):
        print("找不到 webui.html，它应该和 webui.py 放在一起。", file=sys.stderr)
        return 1

    httpd = None
    for candidate in range(port, port + 20):
        try:
            httpd = ThreadingHTTPServer((host, candidate), Handler)
            httpd.daemon_threads = True
            port = candidate
            break
        except OSError:
            continue
    if httpd is None:
        print("端口都占用了，换个 --port 试试。", file=sys.stderr)
        return 1

    url = "http://{}:{}/".format("127.0.0.1" if host in ("", "0.0.0.0") else host, port)
    print("谱面编辑器已启动：{}".format(url))
    print("（Ctrl+C 结束）")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")
    finally:
        httpd.server_close()
    return 0


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        prog="webui.py",
        description="本地可视化谱面编辑器（写出来的 txt 可交给 piano.py 演奏）",
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=8765, help="端口，默认 8765")
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args(argv)

    return serve(args.host, args.port, not args.no_open)


if __name__ == "__main__":
    sys.exit(main())
