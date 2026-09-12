# -*- coding: utf-8 -*-
"""记忆卡片 + 每日待办 · 安卓端（Kivy）

三页：待办 / 记忆卡片 / 同步
- 待办：项目切换、添加（含间隔复习）、完成/删除、复习状态
- 记忆卡片：批量粘贴导入、手动添加、4 档间隔复习、音标朗读（安卓 TTS）
- 同步：同一 Wi‑Fi 与电脑互传词库/待办（按 uid 合并）
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import urllib.parse
import urllib.request
from pathlib import Path

from kivy.app import App
from kivy.clock import Clock
from kivy.core.audio import SoundLoader
from kivy.core.window import Window
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.checkbox import CheckBox
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.uix.spinner import Spinner
from kivy.uix.tabbedpanel import TabbedPanel, TabbedPanelItem
from kivy.uix.textinput import TextInput
from kivy.utils import platform

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from task_store import (  # noqa: E402
    TaskStore, PRIORITY_MIN, PRIORITY_MAX, PRIORITY_DEFAULT,
)
from flashcard_store import (  # noqa: E402
    Store as FlashcardStore, parse_cards, has_phonetic, extract_phonetic,
    extract_word,
)
from sync_core import (  # noqa: E402
    discover_pc, export_tasks, export_cards, import_tasks, import_cards,
)

C = {
    "bg": (0.95, 0.96, 0.98, 1),
    "card": (1, 1, 1, 1),
    "accent": (0.31, 0.49, 1, 1),
    "review": (0.96, 0.62, 0.04, 1),
    "green": (0.09, 0.64, 0.29, 1),
    "red": (0.94, 0.27, 0.27, 1),
    "muted": (0.42, 0.45, 0.5, 1),
    "text": (0.07, 0.09, 0.12, 1),
    "border": (0.89, 0.9, 0.94, 1),
}


def rgb(ta):
    return tuple(ta)


# ------------------------- 安卓 TTS（朗读音标） -------------------------
class AndroidTTS:
    _inst = None

    @classmethod
    def speak(cls, text: str):
        if not text:
            return
        if platform != "android":
            print("[TTS]", text)
            return
        try:
            from jnius import autoclass
            if cls._inst is None:
                Locale = autoclass("java.util.Locale")
                PythonActivity = autoclass("org.kivy.android.PythonActivity")
                tts = autoclass("android.speech.tts.TextToSpeech")
                ctx = PythonActivity.mActivity
                cls._inst = tts(ctx, None)
                cls._inst.setLanguage(Locale.US)
            cls._inst.speak(text, cls._inst.QUEUE_FLUSH, None)
        except Exception as exc:  # noqa: BLE001
            print("TTS 失败:", exc)


# ------------------------- 朗读：下载真人读音并播放（优先），离线退回安卓 TTS -------------------------
AUDIO_DIR = HERE / "audio_cache"


def _audio_path(word: str, accent: str = "2") -> Path:
    safe = re.sub(r"[^\w\-]", "_", word.lower()).strip("_") or "x"
    return AUDIO_DIR / f"{safe}_{accent}.mp3"


def _download_audio(word: str, accent: str = "2", timeout: int = 10):
    """下载单词发音 mp3（有道 dictvoice，免费、无需密钥、国内可直连）。失败返回 None。"""
    p = _audio_path(word, accent)
    if p.exists() and p.stat().st_size > 200:
        return p
    try:
        AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        url = ("https://dict.youdao.com/dictvoice?audio="
               + urllib.parse.quote(word) + f"&type={accent}")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
        if not data or len(data) < 200:
            return None
        p.write_bytes(data)
        return p
    except Exception:
        return None


def speak_card(c):
    """朗读卡片：优先下载真人读音播放；离线/失败则退回安卓 TTS 读单词（非音标）。"""
    word = extract_word(c["front"], c["back"])
    if not word:
        return

    def worker():
        path = _download_audio(word, accent="2")
        if path is not None:
            Clock.schedule_once(lambda dt: _play_mp3(path, word))
        else:
            Clock.schedule_once(lambda dt: AndroidTTS.speak(word))

    threading.Thread(target=worker, daemon=True).start()


def _play_mp3(path, word):
    try:
        sound = SoundLoader.load(str(path))
        if sound and sound.length > 0:
            sound.play()
        else:
            AndroidTTS.speak(word)
    except Exception:
        AndroidTTS.speak(word)


# ------------------------- 通用控件 -------------------------
def btn(text, bg, on_press=None, color=(1, 1, 1, 1), size_hint=(None, None),
        width=120, height=44, font_size=15):
    b = Button(text=text, background_color=bg, color=color, size_hint=size_hint,
               width=width, height=height, font_size=font_size)
    if on_press:
        b.bind(on_press=on_press)
    return b


def txt_label(text, size_hint=(1, None), height=40, halign="left", font_size=15,
              color=C["text"], markup=False):
    l = Label(text=text, size_hint=size_hint, height=height, halign=halign,
              valign="middle", font_size=font_size, color=color, markup=markup)
    l.bind(size=lambda inst, sz: setattr(inst, "text_size", (sz[0] - 16, None)))
    return l


# ------------------------- 待办页 -------------------------
class TasksScreen(BoxLayout):
    def __init__(self, store: TaskStore, **kw):
        super().__init__(orientation="vertical", padding=10, spacing=8, **kw)
        self.store = store
        self._build()
        self.refresh()

    def _build(self):
        top = BoxLayout(size_hint=(1, None), height=44, spacing=6)
        self.project_spin = Spinner(size_hint=(0.45, 1), text="项目")
        self.project_spin.bind(text=self._on_project)
        top.add_widget(self.project_spin)
        top.add_widget(btn("+项目", C["accent"], self._new_project, width=80))
        self.add_widget(top)

        add = BoxLayout(size_hint=(1, None), height=44, spacing=6)
        self.task_in = TextInput(size_hint=(0.5, 1), hint_text="新待办内容",
                                  multiline=False, font_size=15)
        self.prio_spin = Spinner(size_hint=(0.18, 1),
                                 values=[str(i) for i in range(PRIORITY_MIN, PRIORITY_MAX + 1)],
                                 text=str(PRIORITY_DEFAULT))
        self.review_cb = CheckBox(size_hint=(None, 1), width=28, active=True)
        add.add_widget(self.task_in)
        add.add_widget(self.prio_spin)
        add.add_widget(Label(text="复习", size_hint=(None, 1), width=40, font_size=12))
        add.add_widget(self.review_cb)
        add.add_widget(btn("添加", C["green"], self._add_task, width=80))
        self.add_widget(add)

        self.summary = txt_label("", size_hint=(1, None), height=28, font_size=13,
                                color=C["muted"])
        self.add_widget(self.summary)

        self.scroll = ScrollView(size_hint=(1, 1))
        self.list_box = GridLayout(cols=1, size_hint_y=None, spacing=6)
        self.list_box.bind(minimum_height=self.list_box.setter("height"))
        self.scroll.add_widget(self.list_box)
        self.add_widget(self.scroll)

    def _on_project(self, *_):
        self.store.set_current_project(self._project_id(self.project_spin.text))
        self.refresh()

    def _project_id(self, name):
        p = self.store.find_project_by_name(name)
        return p["id"] if p else self.store.current_project()["id"]

    def _new_project(self, *_):
        from kivy.uix.popup import Popup
        box = BoxLayout(orientation="vertical", spacing=8, padding=10)
        inp = TextInput(hint_text="项目名称", multiline=False, font_size=15)
        box.add_widget(inp)
        row = BoxLayout(size_hint=(1, None), height=40, spacing=6)
        ok = btn("确定", C["green"], None, width=100)
        row.add_widget(ok)
        box.add_widget(row)
        pop = Popup(title="新建项目", content=box, size_hint=(0.8, 0.4))
        def do(_):
            try:
                self.store.add_project(inp.text)
            except ValueError as e:
                pass
            pop.dismiss()
            self.refresh()
        ok.bind(on_press=do)
        pop.open()

    def _add_task(self, *_):
        text = self.task_in.text.strip()
        if not text:
            return
        try:
            self.store.add_task(text, int(self.prio_spin.text),
                                self._project_id(self.project_spin.text),
                                review=self.review_cb.active)
        except ValueError:
            pass
        self.task_in.text = ""
        self.refresh()

    def refresh(self):
        names = self.store.project_names()
        self.project_spin.values = names
        cur = self.store.current_project()["name"]
        if cur in names:
            self.project_spin.text = cur
        proj = self.store.current_project()
        active = self.store.active_tasks(proj["id"])
        completed = self.store.completed_tasks(proj["id"])
        due = self.store.review_tasks_due(proj["id"])
        self.summary.text = (f"{proj['name']} · 待办 {len(active)} · 已完成 {len(completed)}"
                             f" · 待复习 {len(due)}")
        self.list_box.clear_widgets()
        for t in active:
            self.list_box.add_widget(self._row(t))
        for t in completed:
            self.list_box.add_widget(self._row(t, done=True))

    def _row(self, t, done=False):
        row = BoxLayout(size_hint_y=None, height=58, spacing=6, padding=(4, 2))
        cb = CheckBox(size_hint=(None, 1), width=30, active=done)
        pid = self.store.current_project()["id"]

        def toggle(_, val, t=t):
            self.store.set_done(t["id"], val, pid)
            self.refresh()
        cb.bind(active=toggle)
        row.add_widget(cb)
        info = BoxLayout(orientation="vertical", size_hint=(1, 1))
        rev = self.store.review_status_text(t) if t.get("review") else ""
        status = (f"  [{rev}]" if rev else "")
        info.add_widget(txt_label(f"{t['text']}", height=30, font_size=15))
        info.add_widget(txt_label(f"优先级 {t['priority']}{status}", height=22,
                                  font_size=12, color=C["muted"]))
        row.add_widget(info)
        if not t.get("review") and not done:
            row.add_widget(btn("复习", C["review"],
                               lambda *_a, t=t: (self.store.enable_review(t["id"]), self.refresh()),
                               width=60, height=40, font_size=13))
        row.add_widget(btn("删", C["red"],
                           lambda *_a, t=t: (self.store.soft_delete_task(t["id"]), self.refresh()),
                           width=50, height=40, font_size=13))
        return row


# ------------------------- 记忆卡片页 -------------------------
class CardsScreen(BoxLayout):
    def __init__(self, store: FlashcardStore, **kw):
        super().__init__(orientation="vertical", padding=10, spacing=8, **kw)
        self.store = store
        self.queue = []
        self.idx = 0
        self.showing = False
        self._build()
        self.refresh()

    def _build(self):
        imp = BoxLayout(size_hint=(1, None), height=120, spacing=6)
        self.import_ta = TextInput(hint_text="批量导入：空行分隔卡片；第1行=正面，第2行起=背面",
                                   font_size=14, multiline=True)
        imp.add_widget(self.import_ta)
        col = BoxLayout(orientation="vertical", size_hint=(None, 1), width=120, spacing=6)
        col.add_widget(btn("导入卡片", C["accent"], self._do_import, height=44))
        col.add_widget(btn("清空", C["border"], self._clear_import, color=C["text"], height=44))
        imp.add_widget(col)
        self.add_widget(imp)

        manual = BoxLayout(size_hint=(1, None), height=44, spacing=6)
        self.mf = TextInput(size_hint=(0.45, 1), hint_text="正面", multiline=False, font_size=14)
        self.mb = TextInput(size_hint=(0.45, 1), hint_text="背面", multiline=False, font_size=14)
        manual.add_widget(self.mf)
        manual.add_widget(self.mb)
        manual.add_widget(btn("手动添加", C["green"], self._manual, width=100))
        self.add_widget(manual)

        self.stats = txt_label("", size_hint=(1, None), height=28, font_size=13,
                               color=C["muted"])
        self.add_widget(self.stats)
        self.add_widget(btn("开始复习", C["review"], self._start_review,
                            size_hint=(1, None), height=46, font_size=16))

        self.review_box = BoxLayout(size_hint=(1, None), height=200, spacing=6, padding=6)
        self.add_widget(self.review_box)

        self.scroll = ScrollView(size_hint=(1, 1))
        self.list_box = GridLayout(cols=1, size_hint_y=None, spacing=4)
        self.list_box.bind(minimum_height=self.list_box.setter("height"))
        self.scroll.add_widget(self.list_box)
        self.add_widget(self.scroll)

    def _do_import(self, *_):
        pairs = parse_cards(self.import_ta.text)
        if not pairs:
            return
        self.store.add_cards(pairs)
        self.import_ta.text = ""
        self.refresh()

    def _clear_import(self, *_):
        self.import_ta.text = ""

    def _manual(self, *_):
        if self.mf.text.strip():
            self.store.add_card(self.mf.text.strip(), self.mb.text.strip())
            self.mf.text = ""
            self.mb.text = ""
            self.refresh()

    def refresh(self):
        total = self.store.total()
        due = len(self.store.due_cards())
        self.stats.text = f"共 {total} 张 · 今日待复习 {due} 张"
        self.list_box.clear_widgets()
        for c in self.store.cards:
            if c.get("deleted"):
                continue
            self.list_box.add_widget(self._row(c))

    def _row(self, c):
        row = BoxLayout(size_hint_y=None, height=46, spacing=6, padding=(4, 2))
        due = self.store._format_due(c) if hasattr(self.store, "_format_due") else ""
        row.add_widget(txt_label(f"{c['front']}  →  {c['back'][:30]}", height=40, font_size=13))
        row.add_widget(btn("删", C["red"], lambda *_a, cid=c["id"]: (self.store.delete_card(cid), self.refresh()),
                           width=50, height=38, font_size=13))
        return row

    def _start_review(self, *_):
        self.queue = self.store.due_cards()
        self.idx = 0
        self._next()

    def _next(self):
        self.review_box.clear_widgets()
        self.showing = False
        if self.idx >= len(self.queue):
            self.review_box.add_widget(txt_label("🎉 今天的复习完成！", height=120, font_size=18,
                                                color=C["green"]))
            self.queue = []
            self.idx = 0
            self.refresh()
            return
        c = self.queue[self.idx]
        inner = BoxLayout(orientation="vertical", size_hint=(1, 1), spacing=6, padding=8)
        inner.add_widget(txt_label(f"卡片 {self.idx+1}/{len(self.queue)}", height=22, font_size=12,
                                  color=C["muted"]))
        inner.add_widget(txt_label(c["front"], height=50, font_size=20))
        foot = BoxLayout(size_hint=(1, None), height=46, spacing=6)
        if has_phonetic(c["front"]) or has_phonetic(c["back"]):
            foot.add_widget(btn("🔊 朗读", C["green"],
                                lambda *_a, c=c: speak_card(c), width=110, height=44))
        foot.add_widget(btn("显示答案", C["accent"], lambda *_a: self._reveal(inner, c),
                            size_hint=(1, 1), height=44))
        inner.add_widget(foot)
        self.review_box.add_widget(inner)

    def _reveal(self, inner, c):
        if self.showing:
            return
        self.showing = True
        inner.add_widget(txt_label("— 答案 —", height=22, font_size=12, color=C["muted"]))
        inner.add_widget(txt_label(c["back"] or "（背面为空）", height=50, font_size=16))
        grades = [("重来", C["red"], "again"), ("困难10分", C["review"], "hard"),
                  ("良好1天", C["green"], "good"), ("简单3天", C["accent"], "easy")]
        g = GridLayout(cols=4, size_hint=(1, None), height=48, spacing=4)
        for label, color, b in grades:
            g.add_widget(btn(label, color, lambda *_a, b=b: self._grade(b), height=46, font_size=13))
        inner.add_widget(g)

    def _grade(self, button):
        if self.idx >= len(self.queue):
            return
        self.store.grade(self.queue[self.idx]["id"], button)
        self.idx += 1
        self._next()


# ------------------------- 同步页 -------------------------
class SyncScreen(BoxLayout):
    def __init__(self, tasks_store, fc_store, **kw):
        super().__init__(orientation="vertical", padding=14, spacing=10, **kw)
        self.tasks_store = tasks_store
        self.fc_store = fc_store
        self.add_widget(txt_label("与电脑互相同步词库 / 待办", height=30, font_size=17,
                                  color=C["text"]))
        self.add_widget(txt_label("确保手机和电脑连同一个 Wi‑Fi，并已在电脑上双击"
                                  "「启动同步服务.bat」。", height=44, font_size=13,
                                  color=C["muted"]))
        row = BoxLayout(size_hint=(1, None), height=46, spacing=6)
        self.ip_in = TextInput(hint_text="电脑 IP（可选，留空自动发现）", multiline=False,
                               font_size=14)
        row.add_widget(self.ip_in)
        self.add_widget(row)
        self.add_widget(btn("发现并同步", C["accent"], self._sync, size_hint=(1, None),
                           height=48, font_size=16))
        self.status = txt_label("状态：未同步", height=200, font_size=14, color=C["text"])
        self.add_widget(self.status)

    def _sync(self, *_):
        self.status.text = "正在同步…"
        threading.Thread(target=self._do_sync, daemon=True).start()

    def _do_sync(self):
        try:
            import urllib.request
            target = self.ip_in.text.strip()
            port = 8765
            if not target:
                found = discover_pc(timeout=3.0)
                if found:
                    target, port = found
                else:
                    Clock.schedule_once(lambda dt: setattr(self.status, "text",
                        "未发现电脑，请手动填写电脑 IP，或确认已启动同步服务。"), 0)
                    return
            url = f"http://{target}:{port}/data"
            with urllib.request.urlopen(url, timeout=8) as r:
                remote = json.loads(r.read().decode("utf-8"))
            local = {"device": "phone",
                     "tasks": export_tasks(self.tasks_store),
                     "cards": export_cards(self.fc_store)}
            merged = merge_two(local, remote)
            import_tasks(self.tasks_store, merged["tasks"])
            import_cards(self.fc_store, merged["cards"])
            # 回传，让电脑也拿到手机的数据
            req = urllib.request.Request(url, data=json.dumps(merged).encode("utf-8"),
                                         headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=8) as r:
                pc_merged = json.loads(r.read().decode("utf-8"))
            import_tasks(self.tasks_store, pc_merged["tasks"])
            import_cards(self.fc_store, pc_merged["cards"])
            msg = (f"同步完成 ✅\n本地待办 {len(self.tasks_store.tasks)} 条，"
                   f"卡片 {self.fc_store.total()} 张。")
            Clock.schedule_once(lambda dt: setattr(self.status, "text", msg), 0)
        except Exception as exc:  # noqa: BLE001
            Clock.schedule_once(lambda dt, e=exc: setattr(self.status, "text",
                               f"同步失败：{e}\n（检查电脑 IP / 同一 Wi‑Fi / 同步服务是否已启动）"), 0)


def merge_two(local, remote):
    from sync_core import merge_items
    return {
        "tasks": merge_items(local.get("tasks", []), remote.get("tasks", [])),
        "cards": merge_items(local.get("cards", []), remote.get("cards", [])),
    }


# ------------------------- 主程序 -------------------------
class MemoApp(App):
    def __init__(self):
        super().__init__()
        data_dir = self.user_data_dir
        self.tasks_store = TaskStore(Path(data_dir) / "tasks_data.json")
        self.fc_store = FlashcardStore(Path(data_dir) / "flashcards_data.json")

    def build(self):
        self.title = "记忆卡片 · 每日待办"
        panel = TabbedPanel(do_default_tab=False, size_hint=(1, 1))
        t1 = TabbedPanelItem(text="待办")
        t1.add_widget(TasksScreen(self.tasks_store))
        t2 = TabbedPanelItem(text="记忆卡片")
        t2.add_widget(CardsScreen(self.fc_store))
        t3 = TabbedPanelItem(text="同步")
        t3.add_widget(SyncScreen(self.tasks_store, self.fc_store))
        panel.add_widget(t1)
        panel.add_widget(t2)
        panel.add_widget(t3)
        panel.default_tab = t1
        return panel


if __name__ == "__main__":
    try:
        MemoApp().run()
    except Exception:
        import traceback
        log = Path(__file__).with_name("android_error.log")
        try:
            log.write_text(traceback.format_exc(), encoding="utf-8")
        except Exception:
            pass
        raise
