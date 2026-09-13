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
from kivy.core.text import LabelBase
from kivy.metrics import sp

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# --- 中文字体注册（关键）---
# Kivy 默认字体 Roboto / DejaVuSans 不含中文字形，安卓上中文会渲染成方块或 "xx"。
# 这里把自带的中文字体（assets/font.ttf）注册覆盖这些默认字体名，
# 之后所有 Label / Button / TextInput 的中文都能正常显示。
_FONT_FILE = HERE / "assets" / "font.ttf"
if _FONT_FILE.exists():
    for _fname in ("Roboto", "DejaVuSans", "DroidSansFallback"):
        try:
            LabelBase.register(name=_fname, fn_regular=str(_FONT_FILE))
        except Exception:
            pass

# --- 音标（IPA）专用字体 ---
# 中文字体（思源黑体）不含 IPA 音标符号（æ ð ɪ ˈ ː 等），音标会渲染成方块/乱码。
# 这里单独找一个含 IPA 的字体注册为 "IPA"，只在显示音标那一行使用。
def _find_ipa_font() -> str:
    p = HERE / "assets" / "ipa.ttf"
    if p.exists():
        return str(p)
    try:
        from kivy import kivy_data_dir
        q = Path(kivy_data_dir) / "fonts" / "DejaVuSans.ttf"
        if q.exists():
            return str(q)
    except Exception:
        pass
    return ""


IPA_FONT = ""
_f = _find_ipa_font()
if _f:
    try:
        LabelBase.register(name="IPA", fn_regular=_f)
        IPA_FONT = "IPA"
    except Exception:
        IPA_FONT = ""

from task_store import (  # noqa: E402
    TaskStore, PRIORITY_MIN, PRIORITY_MAX, PRIORITY_DEFAULT,
)
from flashcard_store import (  # noqa: E402
    Store as FlashcardStore, parse_cards, has_phonetic, extract_phonetic,
    extract_word,
)
from sync_core import (  # noqa: E402
    discover_pc, export_tasks, export_cards, import_tasks, import_cards,
    local_ip, scan_lan,
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

# 窗口背景设为浅色：Kivy 默认窗口底色是黑色，深色主题下界面会显得像"黑屏"
try:
    Window.clearcolor = C["bg"]
except Exception:
    pass


def add_bg(widget, color=None):
    """给容器加背景色。

    Kivy 的 BoxLayout 默认透明，会透出 TabbedPanel 的深色主题背景，
    导致整个界面发暗、看不清内容。这里用 canvas 画一层浅色底。
    """
    from kivy.graphics import Color, Rectangle
    col = color or C["bg"]
    with widget.canvas.before:
        Color(*col)
        rect = Rectangle(pos=widget.pos, size=widget.size)

    def _upd(*_a):
        rect.pos = widget.pos
        rect.size = widget.size

    widget.bind(pos=_upd, size=_upd)
    return widget


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
        width=150, height=62, font_size=20):
    b = Button(text=text, background_color=bg, color=color, size_hint=size_hint,
               width=width, height=height, font_size=sp(font_size))
    if on_press:
        b.bind(on_press=on_press)
    return b


def auto_label(text, font_size, halign="center", color=None, font_name=None):
    """大字号标签：高度随内容自动撑开（问题/答案放大后可能换行成多行）。
    字号直接传像素值（Kivy 内部按屏幕密度换算），用于显示比普通标签大很多的文字。"""
    kw = {"font_name": font_name} if font_name else {}
    l = Label(text=text, size_hint=(1, None), halign=halign, valign="middle",
              font_size=sp(font_size), color=color or C["text"], **kw)
    l.bind(width=lambda inst, w: setattr(inst, "text_size", (w - 12, None)),
           texture_size=lambda inst, ts: setattr(inst, "height", max(ts[1], font_size) + 10))
    return l


def txt_label(text, size_hint=(1, None), height=52, halign="left", font_size=19,
              color=C["text"], markup=False, font_name=None):
    kw = {"font_name": font_name} if font_name else {}
    l = Label(text=text, size_hint=size_hint, height=height, halign=halign,
              valign="middle", font_size=sp(font_size), color=color, markup=markup, **kw)
    l.bind(size=lambda inst, sz: setattr(inst, "text_size", (sz[0] - 16, None)))
    return l


def spinner_arrow(spin, color=None):
    """给 Spinner 右侧加下拉三角标（Kivy 默认不带箭头）。

    用 "▼" 而不是 "▾"：中文字体（GBK 字符集）必有 ▼，▾ 可能渲染成方块。
    """
    lab = Label(text="▼", size_hint=(None, None), size=(sp(18), sp(18)),
                font_size=sp(14), color=color or C["muted"])
    def _upd(*_a):
        lab.x = spin.right - lab.width - sp(10)
        lab.top = spin.center_y + lab.height / 2
    spin.bind(pos=_upd, size=_upd)
    spin.add_widget(lab)
    return spin


_PHON_BLOCK_RE = re.compile(r"/[^/\n]+/|\[[^\]\n]+\]")
_IPA_CHARS_RE = re.compile(r"[æɑəɜɛɪʊʌɔθðŋʃʒʧʤɡɹɾˈˌː̃]")


def split_face(text: str):
    """把卡片的一面拆成 (正文, 音标)。

    中文字体不含 IPA 符号，音标必须单独一行用 IPA 字体渲染，否则显示成方块。
    """
    text = text or ""
    ph = extract_phonetic(text, "")
    if not ph:
        return text, ""
    body = _PHON_BLOCK_RE.sub(" ", text)
    body = _IPA_CHARS_RE.sub("", body).strip()
    # 注意：正文为空就返回空串，不能回退到原文，否则音标块又被塞回中文字体里渲染
    return body, ph


# ------------------------- 待办页 -------------------------
class TasksScreen(BoxLayout):
    def __init__(self, store: TaskStore, **kw):
        super().__init__(orientation="vertical", padding=10, spacing=8, **kw)
        add_bg(self)
        self.store = store
        self._build()
        self.refresh()

    def _build(self):
        top = BoxLayout(size_hint=(1, None), height=66, spacing=6)
        self.project_spin = Spinner(size_hint=(1, 1), text="项目", font_size=sp(20))
        spinner_arrow(self.project_spin)
        self.project_spin.bind(text=self._on_project)
        top.add_widget(self.project_spin)
        top.add_widget(btn("+项目", C["accent"], self._new_project, width=84))
        self.add_widget(top)

        add = BoxLayout(size_hint=(1, None), height=66, spacing=6)
        self.task_in = TextInput(size_hint=(1, 1), hint_text="新待办内容",
                                  multiline=False, font_size=sp(20))
        self.prio_spin = Spinner(size_hint=(0.18, 1), font_size=sp(20),
                                 values=[str(i) for i in range(PRIORITY_MIN, PRIORITY_MAX + 1)],
                                 text=str(PRIORITY_DEFAULT))
        spinner_arrow(self.prio_spin)
        add.add_widget(self.task_in)
        add.add_widget(self.prio_spin)
        add.add_widget(btn("添加", C["green"], self._add_task, width=84))
        self.add_widget(add)

        self.summary = txt_label("", size_hint=(1, None), height=34, font_size=16,
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
        inp = TextInput(hint_text="项目名称", multiline=False, font_size=sp(20))
        box.add_widget(inp)
        row = BoxLayout(size_hint=(1, None), height=52, spacing=6)
        ok = btn("确定", C["green"], None, width=120)
        row.add_widget(ok)
        box.add_widget(row)
        pop = Popup(title="新建项目", content=box, size_hint=(0.9, 0.5), title_size=20)
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
                                self._project_id(self.project_spin.text))
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
        self.summary.text = f"{proj['name']} · 待办 {len(active)} · 已完成 {len(completed)}"
        self.list_box.clear_widgets()
        for t in active:
            self.list_box.add_widget(self._row(t))
        for t in completed:
            self.list_box.add_widget(self._row(t, done=True))

    def focus_nonempty(self):
        """同步完成后：若当前项目没内容、而别的项目有，自动切过去。

        否则同步下来的任务会"看不见"——数据其实已经进库，只是停在空项目上。
        """
        try:
            if self.store.active_tasks(self.store.current_project()["id"]):
                self.refresh()
                return
            for n in self.store.project_names():
                p = self.store.find_project_by_name(n)
                if p and self.store.active_tasks(p["id"]):
                    self.store.set_current_project(p["id"])
                    break
        except Exception:
            pass
        self.refresh()

    def _row(self, t, done=False):
        row = BoxLayout(size_hint_y=None, height=88, spacing=6, padding=(4, 2))
        cb = CheckBox(size_hint=(None, 1), width=36, active=done)
        pid = self.store.current_project()["id"]

        def toggle(_, val, t=t):
            self.store.set_done(t["id"], val, pid)
            self.refresh()
        cb.bind(active=toggle)
        row.add_widget(cb)
        info = BoxLayout(orientation="vertical", size_hint=(1, 1))
        info.add_widget(txt_label(f"{t['text']}", height=38, font_size=19))
        info.add_widget(txt_label(f"优先级 {t['priority']}", height=28,
                                  font_size=15, color=C["muted"]))
        row.add_widget(info)
        row.add_widget(btn("删", C["red"],
                           lambda *_a, t=t: (self.store.soft_delete_task(t["id"]), self.refresh()),
                           width=35, height=35, font_size=15))
        return row


# ------------------------- 记忆卡片页 -------------------------
class CardsScreen(BoxLayout):
    def __init__(self, store: FlashcardStore, **kw):
        super().__init__(orientation="vertical", padding=10, spacing=8, **kw)
        add_bg(self)
        self.store = store
        self.queue = []
        self.idx = 0
        self.showing = False
        self._build()
        self.refresh()

    def _build(self):
        # 词库导入 / 手动添加 / 词库列表已移到「同步」页（发现并同步按钮下方）
        self.stats = txt_label("", size_hint=(1, None), height=34, font_size=16,
                               color=C["muted"])
        self.add_widget(self.stats)
        self.add_widget(btn("开始复习", C["review"], self._start_review,
                            size_hint=(1, None), height=76, font_size=24))

        # 复习区放进 ScrollView：问题/答案放大到 90/64dp 后可能超过一屏，可滚动才不会被裁掉
        self.review_scroll = ScrollView(size_hint=(1, None), height=560)
        self.review_box = BoxLayout(orientation="vertical", size_hint_y=None,
                                    spacing=8, padding=8)
        self.review_box.bind(minimum_height=self.review_box.setter("height"))
        self.review_scroll.add_widget(self.review_box)
        self.add_widget(self.review_scroll)

    def refresh(self):
        total = self.store.total()
        due = len(self.store.due_cards())
        self.stats.text = f"共 {total} 张 · 今日待复习 {due} 张"

    def _start_review(self, *_):
        self.queue = self.store.due_cards()
        self.idx = 0
        self._next()

    def _next(self):
        self.review_box.clear_widgets()
        self.showing = False
        if self.idx >= len(self.queue):
            self.review_box.add_widget(txt_label("🎉 今天的复习完成！", height=170, font_size=32,
                                                color=C["green"]))
            self.queue = []
            self.idx = 0
            self.refresh()
            return
        c = self.queue[self.idx]
        inner = BoxLayout(orientation="vertical", size_hint_y=None, spacing=8, padding=8)
        inner.bind(minimum_height=inner.setter("height"))
        inner.add_widget(txt_label(f"卡片 {self.idx+1}/{len(self.queue)}", height=34, font_size=18,
                                  color=C["muted"]))
        body, ph = split_face(c["front"])
        # 问题（单词）：原始数值 45，sp() 在 auto_label 内部按屏幕密度换算
        if body:
            inner.add_widget(auto_label(body, 45, color=C["text"]))
        if ph:
            inner.add_widget(auto_label(ph, 16, color=C["accent"], font_name=IPA_FONT or None))
        foot = BoxLayout(size_hint=(1, None), height=62, spacing=6)
        if has_phonetic(c["front"]) or has_phonetic(c["back"]):
            foot.add_widget(btn("🔊 朗读", C["green"],
                                lambda *_a, c=c: speak_card(c), size_hint=(1, 1)))
        foot.add_widget(btn("显示答案", C["accent"], lambda *_a: self._reveal(inner, c),
                            size_hint=(1, 1)))
        inner.add_widget(foot)
        self.review_box.add_widget(inner)

    def _reveal(self, inner, c):
        if self.showing:
            return
        self.showing = True
        inner.add_widget(txt_label("— 答案 —", height=34, font_size=18, color=C["muted"]))
        bbody, bph = split_face(c["back"] or "（背面为空）")
        # 答案同样减半——32，长句换行（sp() 内部换算）
        inner.add_widget(auto_label(bbody, 32, color=C["text"]))
        if bph:
            inner.add_widget(auto_label(bph, 15, color=C["accent"], font_name=IPA_FONT or None))
        grades = [("重来", C["red"], "again"), ("困难10分", C["review"], "hard"),
                  ("良好1天", C["green"], "good"), ("简单3天", C["accent"], "easy")]
        # 四个评分按钮一字排开，与顶部标签栏同高（62）；超宽文字自动缩字号适配
        g = GridLayout(cols=4, size_hint=(1, None), height=62, spacing=4)
        for label, color, b in grades:
            g.add_widget(btn(label, color, lambda *_a, b=b: self._grade(b), size_hint=(1, 1)))
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
        add_bg(self)
        self.tasks_store = tasks_store
        self.fc_store = fc_store
        self.add_widget(txt_label("与电脑互相同步词库 / 待办", height=40, font_size=21,
                                  color=C["text"]))
        self.add_widget(txt_label("先在电脑上点日常任务窗口顶部的「📱 同步」按钮，"
                                  "再点下面的按钮（需连同一个 Wi‑Fi）。", height=64, font_size=16,
                                  color=C["muted"]))
        row = BoxLayout(size_hint=(1, None), height=62, spacing=6)
        self.ip_in = TextInput(hint_text="电脑 IP（可选，留空自动发现）", multiline=False,
                               font_size=sp(20))
        row.add_widget(self.ip_in)
        self.add_widget(row)
        self.add_widget(btn("发现并同步", C["accent"], self._sync, size_hint=(1, None),
                           height=62, font_size=20))
        self.status = txt_label("状态：未同步", height=110, font_size=19, color=C["text"])
        self.add_widget(self.status)

        # 词库管理（原在记忆卡片页，现集中到同步页、「发现并同步」按钮下方）
        imp = BoxLayout(size_hint=(1, None), height=170, spacing=6)
        self.import_ta = TextInput(hint_text="批量导入：空行分隔卡片；第1行=正面，第2行起=背面",
                                   font_size=sp(20), multiline=True)
        imp.add_widget(self.import_ta)
        col = BoxLayout(orientation="vertical", size_hint=(None, 1), width=84, spacing=6)
        col.add_widget(btn("导入卡片", C["accent"], self._do_import,
                           size_hint=(1, None), height=62, width=84, font_size=20))
        col.add_widget(btn("清空", C["border"], self._clear_import, color=C["text"],
                           size_hint=(1, None), height=62, width=84, font_size=20))
        imp.add_widget(col)
        self.add_widget(imp)

        manual = BoxLayout(size_hint=(1, None), height=66, spacing=6)
        self.mf = TextInput(size_hint=(0.45, 1), hint_text="正面", multiline=False, font_size=sp(20))
        self.mb = TextInput(size_hint=(0.45, 1), hint_text="背面", multiline=False, font_size=sp(20))
        manual.add_widget(self.mf)
        manual.add_widget(self.mb)
        manual.add_widget(btn("手动添加", C["green"], self._manual, width=84, height=62, font_size=20))
        self.add_widget(manual)

        self.scroll = ScrollView(size_hint=(1, 1))
        self.list_box = GridLayout(cols=1, size_hint_y=None, spacing=4)
        self.list_box.bind(minimum_height=self.list_box.setter("height"))
        self.scroll.add_widget(self.list_box)
        self.add_widget(self.scroll)
        self.refresh()

    def refresh(self):
        """刷新同步页下方的词库列表。"""
        self.list_box.clear_widgets()
        for c in self.fc_store.cards:
            if c.get("deleted"):
                continue
            self.list_box.add_widget(self._row(c))

    def _row(self, c):
        row = BoxLayout(size_hint_y=None, height=110, spacing=6, padding=(4, 2))
        row.add_widget(txt_label(f"{c['front']}  →  {c['back'][:30]}", height=50, font_size=20))
        row.add_widget(btn("删", C["red"],
                           lambda *_a, cid=c["id"]: (self.fc_store.delete_card(cid), self.refresh()),
                           width=35, height=35, font_size=15))
        return row

    def _do_import(self, *_):
        pairs = parse_cards(self.import_ta.text)
        if not pairs:
            return
        self.fc_store.add_cards(pairs)
        self.import_ta.text = ""
        self.refresh()

    def _clear_import(self, *_):
        self.import_ta.text = ""

    def _manual(self, *_):
        if self.mf.text.strip():
            self.fc_store.add_card(self.mf.text.strip(), self.mb.text.strip())
            self.mf.text = ""
            self.mb.text = ""
            self.refresh()

    def _say(self, msg: str) -> None:
        Clock.schedule_once(lambda dt: setattr(self.status, "text", msg), 0)

    def _sync(self, *_):
        self.status.text = "正在同步…"
        threading.Thread(target=self._do_sync, daemon=True).start()

    def _probe_http(self, ip: str, port: int, timeout: float = 2.0) -> bool:
        """确认这个 IP 上跑的确实是本项目的同步服务。"""
        try:
            with urllib.request.urlopen(f"http://{ip}:{port}/data", timeout=timeout) as r:
                obj = json.loads(r.read().decode("utf-8"))
            return isinstance(obj, dict) and "tasks" in obj
        except Exception:
            return False

    def _find_pc(self) -> tuple[str, int] | None:
        """三级发现：广播 → 扫描网段 → 放弃。"""
        self._say("正在广播发现电脑…")
        found = discover_pc(timeout=4.0)
        if found:
            return found
        me = local_ip()
        self._say(f"广播无响应，正在扫描整个网段…\n（手机 IP：{me or '未取到，请确认已连 Wi‑Fi'}）")
        hits = scan_lan(8765, timeout=0.4)
        if me:
            prefix = me.rsplit(".", 1)[0]
            self._say(f"扫描 {prefix}.1~254，发现 {len(hits)} 台设备，正在识别…")
        for ip in hits:
            if self._probe_http(ip, 8765):
                return (ip, 8765)
        self._last_scan = (me, hits)
        return None

    def _refresh_others(self):
        """同步完成后刷新待办页与卡片页，让新数据立刻可见（必须回主线程）。"""
        try:
            app = App.get_running_app()
        except Exception:
            app = None
        if app is None:
            return
        ts = getattr(app, "tasks_screen", None)
        cs = getattr(app, "cards_screen", None)
        ss = getattr(app, "sync_screen", None)
        if ts is not None:
            try:
                ts.focus_nonempty()
            except Exception:
                pass
        if cs is not None:
            try:
                cs.refresh()
            except Exception:
                pass
        if ss is not None:
            try:
                ss.refresh()
            except Exception:
                pass

    def _do_sync(self):
        try:
            before_t = len([t for t in self.tasks_store.tasks if not t.get("deleted")])
            before_c = self.fc_store.total()
            target = self.ip_in.text.strip()
            port = 8765
            if not target:
                got = self._find_pc()
                if not got:
                    me, hits = getattr(self, "_last_scan", ("", []))
                    self._say(
                        "未发现电脑，请手动填写电脑 IP。\n\n"
                        f"手机 IP：{me or '未取到（请确认连的是 Wi‑Fi，不是流量）'}\n"
                        f"网段内可连通的设备：{len(hits)} 台\n"
                        f"{('，'.join(hits[:6])) if hits else '（一台都连不上，多半是 Wi‑Fi 隔离或电脑服务未启动）'}\n\n"
                        "请检查：\n"
                        "1. 电脑上已点「📱 同步」按钮（或运行 启动同步服务.bat）\n"
                        "2. 手机和电脑连同一个 Wi‑Fi\n"
                        "3. 电脑上以管理员运行过「放行防火墙(管理员运行一次).bat」"
                    )
                    return
                target, port = got
            self._say(f"已找到电脑 {target}，正在同步…")
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
            after_t = len([t for t in self.tasks_store.tasks if not t.get("deleted")])
            after_c = self.fc_store.total()
            msg = (f"同步完成 ✅\n"
                   f"本次新增：待办 {max(0, after_t - before_t)} 条、"
                   f"卡片 {max(0, after_c - before_c)} 张\n"
                   f"手机现有：待办 {after_t} 条、卡片 {after_c} 张\n"
                   f"（电脑端 {len(remote.get('tasks', []))} 条 / "
                   f"{len(remote.get('cards', []))} 张）")
            Clock.schedule_once(lambda dt: setattr(self.status, "text", msg), 0)
            Clock.schedule_once(lambda dt: self._refresh_others(), 0)
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
        panel = TabbedPanel(do_default_tab=False, size_hint=(1, 1),
                            tab_height=62)
        # 注意：TabbedPanel 没有 tab_font_size 属性（传了会直接崩溃），
        # 标签字号必须逐个设置到 TabbedPanelItem（Button 子类）上。
        t1 = TabbedPanelItem(text="待办", font_size=sp(19))
        self.tasks_screen = TasksScreen(self.tasks_store)
        t1.add_widget(self.tasks_screen)
        t2 = TabbedPanelItem(text="记忆卡片", font_size=sp(19))
        self.cards_screen = CardsScreen(self.fc_store)
        t2.add_widget(self.cards_screen)
        t3 = TabbedPanelItem(text="同步", font_size=sp(19))
        self.sync_screen = SyncScreen(self.tasks_store, self.fc_store)
        t3.add_widget(self.sync_screen)
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
