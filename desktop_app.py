"""AutoReply WhatsApp Bot desktop application for macOS and Windows."""

from __future__ import annotations

import json
import os
import platform
import queue
import re
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

import pyautogui
import pyperclip
from openai import OpenAI

import config as defaults


APP_NAME = "AutoReply WhatsApp Bot"
APP_DIR = Path.home() / ".autoreply-whatsapp-bot"
SETTINGS_FILE = APP_DIR / "settings.json"
COORD_NAMES = ["CHROME_ICON", "CHAT_SELECT_TL", "CHAT_SELECT_BR", "LAST_MSG_CLICK", "INPUT_BOX"]
TIMESTAMP_RE = re.compile(r"\[[^\]\r\n]{0,80}\d[^\]\r\n]{0,80}\]", re.UNICODE)
BIDI_MARKS_RE = re.compile(r"[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")
MEDIA_RE = re.compile(
    r"(?:<\s*media omitted\s*>|image omitted|video omitted|gif omitted|sticker omitted|"
    r"تم حذف الوسائط|تم إرفاق صورة|تم إرفاق فيديو|صورة مرفقة|فيديو مرفق|"
    r"الصورة غير متوفرة|الفيديو غير متوفر|\ufffc)",
    re.IGNORECASE,
)


def load_settings() -> dict:
    data = {
        "provider": defaults.PROVIDER,
        "base_url": defaults.BASE_URL or "",
        "model": defaults.MODEL,
        "target_sender": defaults.TARGET_SENDER,
        "check_interval": defaults.CHECK_INTERVAL,
        "drag_duration": defaults.SELECT_DRAG_DURATION,
        "max_context_chars": 6000,
        "request_timeout": 30,
        "dry_run": True,
        "coords": defaults.COORDS,
        "persona": defaults.PERSONA.strip(),
        "task_rules": defaults.TASK_RULES.strip(),
    }
    try:
        saved = json.loads(SETTINGS_FILE.read_text("utf-8"))
        data.update(saved)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return data


def save_settings(data: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def normalize_text(text: str) -> str:
    """Normalize RTL controls and media placeholders from WhatsApp clipboard text."""
    text = BIDI_MARKS_RE.sub("", text or "").replace("\x00", "")
    text = MEDIA_RE.sub("[MEDIA_ATTACHMENT]", text)
    text = re.sub(
        r"(?im)^\s*(?:image|photo|video|gif|sticker|صورة|فيديو|ملصق)\s*$",
        "[MEDIA_ATTACHMENT]",
        text,
    )
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def split_messages(chat_log: str) -> list[str]:
    """Split copied WhatsApp text for Latin and Arabic locale timestamps."""
    log = normalize_text(chat_log)
    parts = [part.strip() for part in TIMESTAMP_RE.split(log) if part.strip()]
    return parts or ([log] if log else [])


def last_message(chat_log: str) -> str:
    messages = split_messages(chat_log)
    return messages[-1] if messages else ""


def is_last_message_from_sender(chat_log: str, sender: str) -> bool:
    last = last_message(chat_log)
    sender = normalize_text(sender)
    if not last or not sender:
        return False
    return bool(re.match(rf"^{re.escape(sender)}\s*(?::|：|\u061b|\s)", last)) or last == sender


def bounded_chat_history(chat_log: str, max_chars: int, max_messages: int = 16) -> tuple[str, bool]:
    """Keep recent context only so long Arabic chats cannot block the API."""
    max_chars = max(1000, int(max_chars))
    messages = split_messages(chat_log)
    recent = messages[-max_messages:]
    result = "\n".join(recent)
    truncated = len(messages) > len(recent) or len(result) > max_chars
    if len(result) > max_chars:
        result = result[-max_chars:]
    media_count = result.count("[MEDIA_ATTACHMENT]")
    if media_count:
        result += (
            f"\n\nSystem note: the recent chat contains {media_count} image/video attachment(s). "
            "The attachment pixels are unavailable; acknowledge naturally without claiming to see their contents."
        )
    return result, truncated


def message_fingerprint(chat_log: str) -> str:
    import hashlib
    return hashlib.sha256(last_message(chat_log).encode("utf-8", "replace")).hexdigest()


def clean_reply(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^\[\s*\d{1,2}:\d{2}\s*(?:[AP]M)?(?:[,，]\s*\d{1,2}/\d{1,2}/\d{2,4})?\s*\]\s*", "", text)
    text = re.sub(r"^[^\s\[\]:：]{1,20}\s*[:：]\s*", "", text).strip()
    if len(text) >= 2 and ((text[0], text[-1]) in [('"', '"'), ('「', '」')]):
        text = text[1:-1].strip()
    return text


class BotWorker(threading.Thread):
    def __init__(self, settings: dict, api_key: str, output: queue.Queue, stop_event: threading.Event):
        super().__init__(daemon=True)
        self.s = settings
        self.api_key = api_key
        self.output = output
        self.stop_event = stop_event

    def log(self, text: str) -> None:
        self.output.put(text)

    def wait(self, seconds: float) -> bool:
        return self.stop_event.wait(seconds)

    def action(self, name: str, *args) -> None:
        if self.s["dry_run"]:
            self.log(f"[安全测试] {name}{args}")
            return
        if name == "click":
            pyautogui.click(*args)
        elif name == "drag":
            pyautogui.moveTo(args[0], args[1])
            pyautogui.dragTo(args[2], args[3], duration=self.s["drag_duration"], button="left")
        elif name == "hotkey":
            pyautogui.hotkey(*args)
        elif name == "press":
            pyautogui.press(*args)

    def run(self) -> None:
        pyautogui.PAUSE = 0.3
        pyautogui.FAILSAFE = True
        try:
            kwargs = {
                "api_key": self.api_key,
                "timeout": float(self.s["request_timeout"]),
                "max_retries": 0,
            }
            if self.s["base_url"]:
                kwargs["base_url"] = self.s["base_url"]
            client = OpenAI(**kwargs)
            coords = self.s["coords"]
            mod = "cmd" if platform.system() == "Darwin" else "ctrl"
            self.log(f"机器人已启动：{self.s['provider']} / {self.s['model']}")
            self.log("安全测试模式开启，不会发送消息。" if self.s["dry_run"] else "5 秒后开始操作 WhatsApp Web。")
            if self.wait(1 if self.s["dry_run"] else 5):
                return
            self.action("click", *coords["CHROME_ICON"])
            if self.wait(1):
                return
            last_handled = None
            cycle = 0
            while not self.stop_event.is_set():
                cycle += 1
                self.log(f"第 {cycle} 轮：检查新消息")
                if self.wait(float(self.s["check_interval"])):
                    break
                self.action("drag", *coords["CHAT_SELECT_TL"], *coords["CHAT_SELECT_BR"])
                self.action("hotkey", mod, "c")
                if self.wait(1.2):
                    break
                self.action("click", *coords["LAST_MSG_CLICK"])
                clipboard = pyperclip.paste()
                chat = clipboard if isinstance(clipboard, str) else ""
                if not chat.strip() or not is_last_message_from_sender(chat, self.s["target_sender"]):
                    self.log("没有检测到目标联系人的新消息。")
                    continue
                fingerprint = message_fingerprint(chat)
                if fingerprint == last_handled:
                    self.log("这条消息已经处理，等待新消息。")
                    continue
                prompt_chat, truncated = bounded_chat_history(chat, self.s["max_context_chars"])
                if truncated:
                    self.log(f"聊天内容较长，已限制为最近 {self.s['max_context_chars']} 个字符。")
                if "[MEDIA_ATTACHMENT]" in prompt_chat:
                    self.log("检测到图片或视频，已转换为附件占位，不读取媒体文件。")
                self.log("正在生成回复……")
                try:
                    completion = client.chat.completions.create(
                        model=self.s["model"],
                        messages=[
                            {"role": "system", "content": self.s["persona"]},
                            {"role": "system", "content": self.s["task_rules"]},
                            {"role": "user", "content": prompt_chat},
                        ],
                        max_tokens=400,
                    )
                except Exception as exc:
                    self.log(f"AI 调用失败或超时，本轮跳过，机器人继续运行：{exc}")
                    continue
                reply = clean_reply(completion.choices[0].message.content)
                if not reply:
                    self.log("AI 返回空内容，本轮跳过。")
                    continue
                last_handled = fingerprint
                self.log(f"回复：{reply}")
                if not self.s["dry_run"]:
                    pyperclip.copy(reply)
                self.action("click", *coords["INPUT_BOX"])
                self.action("hotkey", mod, "v")
                self.action("press", "enter")
                self.log("已发送。" if not self.s["dry_run"] else "安全测试完成，未实际发送。")
        except pyautogui.FailSafeException:
            self.log("已触发紧急停止：鼠标移动到了屏幕角落。")
        except Exception as exc:
            self.log(f"运行错误：{exc}")
        finally:
            self.output.put("__STOPPED__")


class DesktopApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("920x720")
        self.minsize(800, 620)
        self.settings = load_settings()
        self.output_queue: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.vars = {}
        self.coord_vars = {}
        self._build_ui()
        self.after(150, self._poll_output)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        outer = ttk.Frame(self, padding=14)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text=APP_NAME, font=("Arial", 20, "bold")).pack(anchor="w")
        ttk.Label(outer, text="Windows / macOS 桌面自动回复控制台").pack(anchor="w", pady=(0, 10))
        notebook = ttk.Notebook(outer)
        notebook.pack(fill="both", expand=True)
        basic = ttk.Frame(notebook, padding=12)
        coords = ttk.Frame(notebook, padding=12)
        prompts = ttk.Frame(notebook, padding=12)
        logs = ttk.Frame(notebook, padding=12)
        notebook.add(basic, text="基本设置")
        notebook.add(coords, text="坐标校准")
        notebook.add(prompts, text="AI 人设")
        notebook.add(logs, text="运行日志")
        self._build_basic(basic)
        self._build_coords(coords)
        self._build_prompts(prompts)
        self.log_box = scrolledtext.ScrolledText(logs, state="disabled", font=("Menlo", 11))
        self.log_box.pack(fill="both", expand=True)
        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(10, 0))
        self.start_button = ttk.Button(buttons, text="启动机器人", command=self.start_bot)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(buttons, text="停止", command=self.stop_bot, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        ttk.Button(buttons, text="保存配置", command=self.save).pack(side="left")
        ttk.Button(buttons, text="打开 WhatsApp Web", command=self.open_whatsapp).pack(side="right")

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
        self._field(parent, 3, "API Key（不写入配置文件）", "api_key", os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY") or "", "•")
        self._field(parent, 4, "目标联系人", "target_sender", self.settings["target_sender"])
        self._field(parent, 5, "检查间隔（秒）", "check_interval", str(self.settings["check_interval"]))
        self._field(parent, 6, "拖选时长（秒）", "drag_duration", str(self.settings["drag_duration"]))
        self._field(parent, 7, "最长上下文（字符）", "max_context_chars", str(self.settings["max_context_chars"]))
        self._field(parent, 8, "AI 超时（秒）", "request_timeout", str(self.settings["request_timeout"]))
        dry = tk.BooleanVar(value=self.settings["dry_run"])
        self.vars["dry_run"] = dry
        ttk.Checkbutton(parent, text="安全测试模式（不发送消息）", variable=dry).grid(row=9, column=1, sticky="w", pady=10)
        ttk.Label(parent, text="长阿拉伯文自动截断；图片/视频使用附件占位；超时后继续下一轮。", foreground="#9a6700").grid(row=10, column=0, columnspan=2, sticky="w")

    def _build_coords(self, parent):
        parent.columnconfigure(1, weight=1)
        ttk.Label(parent, text="点击“3 秒后取鼠标位置”，然后把鼠标移动到目标位置。", font=("Arial", 12, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))
        for i, name in enumerate(COORD_NAMES, 1):
            ttk.Label(parent, text=name).grid(row=i, column=0, sticky="w", pady=7)
            xy = self.settings["coords"].get(name, [0, 0])
            var = tk.StringVar(value=f"{xy[0]}, {xy[1]}")
            self.coord_vars[name] = var
            ttk.Entry(parent, textvariable=var).grid(row=i, column=1, sticky="ew", padx=8)
            ttk.Button(parent, text="3 秒后取鼠标位置", command=lambda n=name: self.capture_coord(n)).grid(row=i, column=2)
        ttk.Label(parent, text="提示：macOS 首次运行需要在“系统设置 → 隐私与安全性 → 辅助功能”中允许本 App。", wraplength=700).grid(row=7, column=0, columnspan=3, sticky="w", pady=18)

    def _build_prompts(self, parent):
        ttk.Label(parent, text="机器人人设").pack(anchor="w")
        self.persona = scrolledtext.ScrolledText(parent, height=15)
        self.persona.pack(fill="both", expand=True, pady=(4, 10))
        self.persona.insert("1.0", self.settings["persona"])
        ttk.Label(parent, text="输出规则").pack(anchor="w")
        self.rules = scrolledtext.ScrolledText(parent, height=8)
        self.rules.pack(fill="both", expand=True, pady=(4, 0))
        self.rules.insert("1.0", self.settings["task_rules"])

    def capture_coord(self, name):
        self.iconify()
        def capture():
            time.sleep(3)
            point = pyautogui.position()
            self.after(0, lambda: (self.coord_vars[name].set(f"{point.x}, {point.y}"), self.deiconify(), self.lift()))
        threading.Thread(target=capture, daemon=True).start()

    def collect(self) -> dict:
        coords = {}
        for name, var in self.coord_vars.items():
            parts = [int(x.strip()) for x in var.get().split(",")]
            if len(parts) != 2:
                raise ValueError(f"{name} 坐标必须是 x, y")
            coords[name] = parts
        check_interval = float(self.vars["check_interval"].get())
        drag_duration = float(self.vars["drag_duration"].get())
        max_context_chars = int(self.vars["max_context_chars"].get())
        request_timeout = float(self.vars["request_timeout"].get())
        if not 1 <= check_interval <= 3600:
            raise ValueError("检查间隔必须在 1 至 3600 秒之间。")
        if not 0.2 <= drag_duration <= 10:
            raise ValueError("拖选时长必须在 0.2 至 10 秒之间。")
        if not 1000 <= max_context_chars <= 30000:
            raise ValueError("最长上下文必须在 1000 至 30000 字符之间。")
        if not 5 <= request_timeout <= 180:
            raise ValueError("AI 超时必须在 5 至 180 秒之间。")
        return {
            "provider": self.vars["provider"].get().strip(),
            "base_url": self.vars["base_url"].get().strip(),
            "model": self.vars["model"].get().strip(),
            "target_sender": self.vars["target_sender"].get().strip(),
            "check_interval": check_interval,
            "drag_duration": drag_duration,
            "max_context_chars": max_context_chars,
            "request_timeout": request_timeout,
            "dry_run": bool(self.vars["dry_run"].get()),
            "coords": coords,
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

    def start_bot(self):
        try:
            settings = self.collect()
            key = self.vars["api_key"].get().strip()
            if not key:
                raise ValueError("请填写 API Key。")
            if not settings["target_sender"] or not settings["model"]:
                raise ValueError("模型和目标联系人不能为空。")
            save_settings(settings)
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        self.stop_event = threading.Event()
        self.worker = BotWorker(settings, key, self.output_queue, self.stop_event)
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.worker.start()

    def stop_bot(self):
        self.stop_event.set()
        self._append_log("正在停止……")

    def _poll_output(self):
        try:
            while True:
                msg = self.output_queue.get_nowait()
                if msg == "__STOPPED__":
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self._append_log("机器人已停止。")
                else:
                    self._append_log(msg)
        except queue.Empty:
            pass
        self.after(150, self._poll_output)

    def _append_log(self, text):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{time.strftime('%H:%M:%S')}] {text}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def open_whatsapp(self):
        import webbrowser
        webbrowser.open("https://web.whatsapp.com/")

    def _on_close(self):
        self.stop_event.set()
        self.destroy()


if __name__ == "__main__":
    DesktopApp().mainloop()
