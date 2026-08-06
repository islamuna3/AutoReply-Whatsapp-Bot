"""AutoReply WhatsApp Bot desktop controller using a local Chrome-extension bridge."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import threading
import time
import tkinter as tk
import webbrowser
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk
from urllib.parse import urlparse

from openai import OpenAI

import config as defaults


APP_NAME = "AutoReply WhatsApp Bot"
APP_DIR = Path.home() / ".autoreply-whatsapp-bot"
SETTINGS_FILE = APP_DIR / "settings.json"
BIDI_MARKS_RE = re.compile(r"[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")
MEDIA_RE = re.compile(
    r"(?:<\s*media omitted\s*>|image omitted|video omitted|gif omitted|sticker omitted|"
    r"تم حذف الوسائط|تم إرفاق صورة|تم إرفاق فيديو|صورة مرفقة|فيديو مرفق|\ufffc)",
    re.IGNORECASE,
)


def load_settings() -> dict:
    data = {
        "provider": defaults.PROVIDER,
        "base_url": defaults.BASE_URL or "",
        "model": defaults.MODEL,
        "target_sender": "",
        "max_context_chars": 6000,
        "request_timeout": 30,
        "bridge_port": 8765,
        "dry_run": True,
        "persona": defaults.PERSONA.strip(),
        "task_rules": defaults.TASK_RULES.strip(),
    }
    try:
        saved = json.loads(SETTINGS_FILE.read_text("utf-8"))
        data.update({key: value for key, value in saved.items() if key in data})
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return data


def save_settings(data: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def normalize_text(text: str) -> str:
    text = BIDI_MARKS_RE.sub("", text or "").replace("\x00", "")
    text = MEDIA_RE.sub("[MEDIA_ATTACHMENT]", text)
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def clean_reply(text: str) -> str:
    text = normalize_text(text)
    text = re.sub(r"^\[[^\]\r\n]{0,80}\d[^\]\r\n]{0,80}\]\s*", "", text)
    text = re.sub(r"^.{1,40}?\s*[:：]\s*", "", text).strip()
    if len(text) >= 2 and ((text[0], text[-1]) in [('\"', '\"'), ('「', '」')]):
        text = text[1:-1].strip()
    return text


def bounded_context(messages: list[str], max_chars: int) -> tuple[str, bool]:
    result = "\n".join(messages[-16:])
    truncated = len(messages) > 16 or len(result) > max_chars
    if len(result) > max_chars:
        result = result[-max_chars:]
    return result, truncated


class ReplyEngine:
    def __init__(self, settings: dict, api_key: str, log):
        kwargs = {
            "api_key": api_key,
            "timeout": float(settings["request_timeout"]),
            "max_retries": 0,
        }
        if settings["base_url"]:
            kwargs["base_url"] = settings["base_url"]
        self.client = OpenAI(**kwargs)
        self.settings = settings
        self.log = log
        self.history: dict[str, deque[str]] = defaultdict(lambda: deque(maxlen=32))
        self.processed: dict[str, float] = {}
        self.lock = threading.Lock()

    def _cleanup(self):
        cutoff = time.time() - 3600
        self.processed = {key: stamp for key, stamp in self.processed.items() if stamp >= cutoff}

    def reply(self, payload: dict) -> dict:
        message_id = str(payload.get("id") or "").strip()
        sender = normalize_text(str(payload.get("sender") or ""))
        chat = normalize_text(str(payload.get("chatTitle") or sender or "default"))
        text = normalize_text(str(payload.get("text") or ""))
        media_type = normalize_text(str(payload.get("mediaType") or ""))
        if not message_id:
            message_id = hashlib.sha256(f"{chat}\n{sender}\n{text}\n{media_type}".encode()).hexdigest()
        with self.lock:
            self._cleanup()
            if message_id in self.processed:
                return {"ok": True, "duplicate": True, "reply": "", "send": False}
        target = normalize_text(self.settings.get("target_sender", ""))
        if target and target not in (sender, chat):
            return {"ok": True, "ignored": True, "reply": "", "send": False}
        if media_type:
            text = f"[MEDIA_ATTACHMENT: {media_type}]" + (f"\nCaption: {text}" if text else "")
        if not text:
            text = "[MEDIA_ATTACHMENT: unknown]"
        line = f"{sender or 'Customer'}: {text}"
        self.history[chat].append(line)
        context, truncated = bounded_context(list(self.history[chat]), int(self.settings["max_context_chars"]))
        if truncated:
            self.log(f"「{chat}」内容较长，已保留最近 16 条并截断。")
        if media_type:
            context += "\nSystem note: An attachment was received. Do not claim to see its contents; acknowledge naturally."
            self.log(f"「{chat}」收到{media_type}附件。")
        self.log(f"正在回复「{chat}」的消息……")
        completion = self.client.chat.completions.create(
            model=self.settings["model"],
            messages=[
                {"role": "system", "content": self.settings["persona"]},
                {"role": "system", "content": self.settings["task_rules"]},
                {"role": "user", "content": context},
            ],
            max_tokens=400,
        )
        reply = clean_reply(completion.choices[0].message.content)
        if not reply:
            return {"ok": False, "error": "AI returned an empty reply", "send": False}
        self.history[chat].append(f"Assistant: {reply}")
        with self.lock:
            self.processed[message_id] = time.time()
        self.log(f"回复「{chat}」：{reply}")
        return {"ok": True, "reply": reply, "send": not self.settings["dry_run"]}


class BridgeServer:
    def __init__(self, settings: dict, api_key: str, output: queue.Queue):
        self.settings = settings
        self.output = output
        self.engine = ReplyEngine(settings, api_key, self.log)
        self.httpd = None
        self.thread = None
        self.extension_connected = False

    def log(self, message: str):
        self.output.put(message)

    def start(self):
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def _headers(self, status=200):
                self.send_response(status)
                origin = self.headers.get("Origin", "")
                allowed = origin if origin.startswith("chrome-extension://") or origin == "https://web.whatsapp.com" else "null"
                self.send_header("Access-Control-Allow-Origin", allowed)
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()

            def _write(self, value, status=200):
                self._headers(status)
                self.wfile.write(json.dumps(value, ensure_ascii=False).encode("utf-8"))

            def do_OPTIONS(self):
                self._headers(204)

            def do_GET(self):
                if urlparse(self.path).path == "/v1/status":
                    if not getattr(bridge, "extension_connected", False):
                        bridge.extension_connected = True
                        bridge.log("已检测到 Chrome 扩展连接，正在监听新消息。")
                    self._write({"ok": True, "running": True, "dryRun": bridge.settings["dry_run"]})
                else:
                    self._write({"ok": False, "error": "not found"}, 404)

            def do_POST(self):
                if urlparse(self.path).path != "/v1/reply":
                    self._write({"ok": False, "error": "not found"}, 404)
                    return
                try:
                    length = min(int(self.headers.get("Content-Length", "0")), 100_000)
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    self._write(bridge.engine.reply(payload))
                except Exception as exc:
                    bridge.log(f"消息处理失败或超时，扩展会继续监听：{exc}")
                    self._write({"ok": False, "error": str(exc), "send": False}, 500)

            def log_message(self, *_):
                return

        port = int(self.settings["bridge_port"])
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.log(f"本地扩展服务已启动：http://127.0.0.1:{port}")

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        self.log("本地扩展服务已停止。")


class DesktopApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("920x720")
        self.minsize(800, 620)
        self.settings = load_settings()
        self.output_queue = queue.Queue()
        self.bridge = None
        self.vars = {}
        self._build_ui()
        self.after(150, self._poll_output)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        outer = ttk.Frame(self, padding=14)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text=APP_NAME, font=("Arial", 20, "bold")).pack(anchor="w")
        ttk.Label(outer, text="Chrome 扩展模式 · 无需坐标校准").pack(anchor="w", pady=(0, 10))
        tabs = ttk.Notebook(outer)
        tabs.pack(fill="both", expand=True)
        basic, prompts, logs = (ttk.Frame(tabs, padding=12) for _ in range(3))
        tabs.add(basic, text="基本设置")
        tabs.add(prompts, text="AI 人设")
        tabs.add(logs, text="运行日志")
        self._build_basic(basic)
        self._build_prompts(prompts)
        self.log_box = scrolledtext.ScrolledText(logs, state="disabled", font=("Menlo", 11))
        self.log_box.pack(fill="both", expand=True)
        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(10, 0))
        self.start_button = ttk.Button(buttons, text="启动扩展服务", command=self.start_bridge)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(buttons, text="停止", command=self.stop_bridge, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        ttk.Button(buttons, text="保存配置", command=self.save).pack(side="left")
        ttk.Button(buttons, text="打开 WhatsApp Web", command=lambda: webbrowser.open("https://web.whatsapp.com/")).pack(side="right")

    def _field(self, parent, row, label, key, value, show=None):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=6)
        var = tk.StringVar(value=value)
        self.vars[key] = var
        ttk.Entry(parent, textvariable=var, show=show).grid(row=row, column=1, sticky="ew", pady=6)

    def _build_basic(self, parent):
        parent.columnconfigure(1, weight=1)
        self._field(parent, 0, "服务商", "provider", self.settings["provider"])
        self._field(parent, 1, "API 地址", "base_url", self.settings["base_url"])
        self._field(parent, 2, "模型", "model", self.settings["model"])
        self._field(parent, 3, "API Key（不保存）", "api_key", "", "•")
        self._field(parent, 4, "指定发送者（留空=全部）", "target_sender", self.settings["target_sender"])
        self._field(parent, 5, "最长上下文（字符）", "max_context_chars", self.settings["max_context_chars"])
        self._field(parent, 6, "AI 超时（秒）", "request_timeout", self.settings["request_timeout"])
        self._field(parent, 7, "扩展服务端口", "bridge_port", self.settings["bridge_port"])
        dry = tk.BooleanVar(value=self.settings["dry_run"])
        self.vars["dry_run"] = dry
        ttk.Checkbutton(parent, text="安全测试模式（生成回复但不发送）", variable=dry).grid(row=8, column=1, sticky="w", pady=8)
        ttk.Label(parent, text="先在 Chrome 扩展管理页加载项目中的 chrome-extension 文件夹，然后启动扩展服务。", foreground="#9a6700", wraplength=720).grid(row=9, column=0, columnspan=2, sticky="w", pady=8)

    def _build_prompts(self, parent):
        ttk.Label(parent, text="机器人人设").pack(anchor="w")
        self.persona = scrolledtext.ScrolledText(parent, height=15)
        self.persona.pack(fill="both", expand=True, pady=(4, 10))
        self.persona.insert("1.0", self.settings["persona"])
        ttk.Label(parent, text="输出规则").pack(anchor="w")
        self.rules = scrolledtext.ScrolledText(parent, height=8)
        self.rules.pack(fill="both", expand=True)
        self.rules.insert("1.0", self.settings["task_rules"])

    def collect(self):
        max_chars = int(self.vars["max_context_chars"].get())
        timeout = float(self.vars["request_timeout"].get())
        port = int(self.vars["bridge_port"].get())
        if not 1000 <= max_chars <= 30000:
            raise ValueError("最长上下文必须在 1000 至 30000 之间。")
        if not 5 <= timeout <= 180:
            raise ValueError("AI 超时必须在 5 至 180 秒之间。")
        if not 1024 <= port <= 65535:
            raise ValueError("端口必须在 1024 至 65535 之间。")
        return {
            "provider": self.vars["provider"].get().strip(),
            "base_url": self.vars["base_url"].get().strip(),
            "model": self.vars["model"].get().strip(),
            "target_sender": self.vars["target_sender"].get().strip(),
            "max_context_chars": max_chars,
            "request_timeout": timeout,
            "bridge_port": port,
            "dry_run": bool(self.vars["dry_run"].get()),
            "persona": self.persona.get("1.0", "end").strip(),
            "task_rules": self.rules.get("1.0", "end").strip(),
        }

    def save(self):
        try:
            self.settings = self.collect()
            save_settings(self.settings)
            messagebox.showinfo(APP_NAME, f"配置已保存到\n{SETTINGS_FILE}")
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))

    def start_bridge(self):
        try:
            settings = self.collect()
            api_key = self.vars["api_key"].get().strip()
            if not api_key:
                raise ValueError("请填写 API Key。")
            save_settings(settings)
            self.bridge = BridgeServer(settings, api_key, self.output_queue)
            self.bridge.start()
            self.start_button.configure(state="disabled")
            self.stop_button.configure(state="normal")
        except Exception as exc:
            self.bridge = None
            messagebox.showerror(APP_NAME, str(exc))

    def stop_bridge(self):
        if self.bridge:
            self.bridge.stop()
            self.bridge = None
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")

    def _poll_output(self):
        try:
            while True:
                self._append_log(self.output_queue.get_nowait())
        except queue.Empty:
            pass
        self.after(150, self._poll_output)

    def _append_log(self, text):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{time.strftime('%H:%M:%S')}] {text}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _on_close(self):
        if self.bridge:
            self.bridge.stop()
        self.destroy()


if __name__ == "__main__":
    DesktopApp().mainloop()
