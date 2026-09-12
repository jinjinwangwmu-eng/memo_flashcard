# -*- coding: utf-8 -*-
"""记忆卡片 · 纯逻辑层（无界面，手机端与电脑同步端共用）

- 批量解析：空行分隔卡片；第 1 行=正面，第 2 行起到下一个空行=背面。
- 间隔复习：重来 / 困难(10分钟) / 良好(1天) / 简单(3天)，毕业后 8→16→1月→2月→4月→8月→1年，每年重复。
- 同步：每张卡带 uid / updated_at / deleted 墓碑，供双向合并。
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from task_store import new_uid

# ------------------------- 间隔复习参数 -------------------------
GRADUATED = [
    timedelta(days=8),     # 第 1 轮：8 天
    timedelta(days=16),    # 第 2 轮：16 天
    timedelta(days=30),    # 第 3 轮：1 个月
    timedelta(days=60),    # 第 4 轮：2 个月
    timedelta(days=120),   # 第 5 轮：4 个月
    timedelta(days=240),   # 第 6 轮：8 个月
    timedelta(days=365),   # 第 7 轮：1 年（之后每年重复）
]
LEARNING_HARD = timedelta(minutes=10)   # 困难
GRADUATE_GOOD = timedelta(days=1)       # 良好（毕业首日）
GRADUATE_EASY = timedelta(days=3)       # 简单（毕业首日）


def now_dt() -> datetime:
    return datetime.now().astimezone()


def to_iso(dt: datetime) -> str:
    return dt.isoformat()


def from_iso(s: Any) -> datetime | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def parse_cards(text: str):
    """按空行分隔卡片；每张：第 1 行=正面，第 2 行起到下一个空行=背面。"""
    cards = []
    blocks = re.split(r"\n[ \t]*\n", text.replace("\r\n", "\n"))
    for block in blocks:
        lines = [ln for ln in block.split("\n") if ln.strip() != ""]
        if not lines:
            continue
        front = lines[0].strip()
        if not front:
            continue
        back = "\n".join(ln.rstrip() for ln in lines[1:]).strip()
        cards.append((front, back))
    return cards


# ------------------------- 音标检测（供朗读按钮判断） -------------------------
_IPA_CHARS = "æɑəɜɛɪʊʌɔθðŋʃʒʧʤɡɹɾˈˌː̃"
_IPA_RE = re.compile(r"/[^\n/]+/|\[[^\]\n]+\]")
_PHONETIC_BLOCK_RE = re.compile(r"/([^/\n]+)/|\[([^\]\n]+)\]")


def has_phonetic(text: str) -> bool:
    if _IPA_RE.search(text or ""):
        return True
    return any(ch in _IPA_CHARS for ch in (text or ""))


def extract_phonetic(front: str, back: str) -> str:
    """抽取卡片里的音标文本（只取音标，不含单词/释义）。"""
    text = f"{front}\n{back}"
    blocks = []
    for m in _PHONETIC_BLOCK_RE.finditer(text):
        inner = m.group(1) if m.group(1) is not None else m.group(2)
        if inner and inner.strip():
            blocks.append(inner.strip())
    if blocks:
        return " ".join(blocks)
    runs = re.findall("[" + re.escape(_IPA_CHARS) + "]+", text)
    return " ".join(runs)


def extract_word(front: str, back: str):
    """从卡片抽取要朗读的英文单词/短语（先去掉音标块，再取首个英文词）。

    朗读的是单词正确发音，而非音标符号（音标符号朗读不准）。返回字符串或 None。
    """
    def first_word(text):
        clean = _PHONETIC_BLOCK_RE.sub(" ", text or "")
        m = re.search(
            r"[A-Za-z][A-Za-z'\-]*(?:\s+[A-Za-z][A-Za-z'\-]*){0,2}", clean.strip()
        )
        return m.group(0).strip() if m else None
    return first_word(front) or first_word(back)


# ------------------------- 数据存储 -------------------------
class Store:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else Path(__file__).with_name("flashcards_data.json")
        self.cards: list[dict[str, Any]] = []
        self.next_id = 1
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = {}
            self.next_id = int(data.get("next_id", 1) or 1)
            for c in data.get("cards", []):
                if not isinstance(c, dict):
                    continue
                c.setdefault("uid", new_uid())
                c.setdefault("state", "new")
                c.setdefault("rung", -1)
                c.setdefault("due", to_iso(now_dt()))
                c.setdefault("lapses", 0)
                c.setdefault("reviews", 0)
                c.setdefault("last_reviewed", None)
                c.setdefault("created_at", to_iso(now_dt()))
                c.setdefault("updated_at", c.get("created_at") or to_iso(now_dt()))
                c.setdefault("deleted", False)
                self.cards.append(c)
        if self.cards:
            self.next_id = max(self.next_id, max(c["id"] for c in self.cards) + 1)

    def save(self):
        data = {"version": 1, "next_id": self.next_id, "cards": self.cards}
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        import os
        os.replace(tmp, self.path)

    def add_card(self, front: str, back: str):
        card = {
            "id": self.next_id,
            "uid": new_uid(),
            "front": front,
            "back": back,
            "created_at": to_iso(now_dt()),
            "updated_at": to_iso(now_dt()),
            "state": "new",
            "due": to_iso(now_dt()),
            "rung": -1,
            "lapses": 0,
            "reviews": 0,
            "last_reviewed": None,
            "deleted": False,
        }
        self.next_id += 1
        self.cards.append(card)
        self.save()
        return card

    def add_cards(self, pairs):
        added = 0
        for front, back in pairs:
            if front:
                self.add_card(front, back)
                added += 1
        return added

    def delete_card(self, cid: int):
        """软删除（墓碑），便于跨设备同步删除。"""
        for c in self.cards:
            if c["id"] == cid:
                c["deleted"] = True
                c["updated_at"] = to_iso(now_dt())
                self.save()
                return

    def total(self) -> int:
        return len([c for c in self.cards if not c.get("deleted")])

    def all_cards(self) -> list[dict[str, Any]]:
        """含已删除（用于同步上传）。"""
        return self.cards

    def replace_all(self, cards: list[dict[str, Any]]):
        """用合并后的列表整体替换（同步后调用）。"""
        self.cards = cards
        if cards:
            self.next_id = max(self.next_id, max(c["id"] for c in cards) + 1)
        self.save()

    def _format_due(self, card, now=None) -> str:
        now = now or now_dt()
        due = from_iso(card["due"])
        if due is None:
            return ""
        if due > now:
            days = (due - now).days
            if days >= 1:
                return f"{days} 天后"
            mins = max(1, int((due - now).total_seconds() // 60))
            return f"{mins} 分钟后"
        if card["rung"] == -1 and card["state"] == "new":
            return "新卡"
        overdue = now - due
        days = overdue.days
        if days >= 1:
            return f"逾期 {days} 天"
        mins = max(1, int(overdue.total_seconds() // 60))
        return f"逾期 {mins} 分钟"

    def due_cards(self, now=None):
        now = now or now_dt()
        due = [c for c in self.cards if not c.get("deleted") and from_iso(c["due"]) <= now]
        due.sort(key=lambda c: from_iso(c["due"]) or now)
        return due

    def grade(self, cid: int, button: str, now=None):
        now = now or now_dt()
        card = next((c for c in self.cards if c["id"] == cid), None)
        if card is None:
            return
        if button == "again":
            card["state"] = "learning"
            card["rung"] = -1
            card["due"] = to_iso(now)
            card["lapses"] = card.get("lapses", 0) + 1
            card["last_reviewed"] = to_iso(now)
            card["updated_at"] = to_iso(now)
            self.save()
            return
        if card["rung"] == -1:
            if button == "hard":
                card["due"] = to_iso(now + LEARNING_HARD)
            elif button == "good":
                card["due"] = to_iso(now + GRADUATE_GOOD)
                card["state"] = "review"
                card["rung"] = 0
            elif button == "easy":
                card["due"] = to_iso(now + GRADUATE_EASY)
                card["state"] = "review"
                card["rung"] = 0
        else:
            if button == "hard":
                card["due"] = to_iso(now + LEARNING_HARD)
            elif button == "good":
                nxt = card["rung"]
                card["due"] = to_iso(now + GRADUATED[nxt])
                card["rung"] = min(nxt + 1, len(GRADUATED) - 1)
            elif button == "easy":
                nxt = min(card["rung"] + 1, len(GRADUATED) - 1)
                card["due"] = to_iso(now + GRADUATED[nxt])
                card["rung"] = min(nxt + 1, len(GRADUATED) - 1)
        card["reviews"] = card.get("reviews", 0) + 1
        card["last_reviewed"] = to_iso(now)
        card["updated_at"] = to_iso(now)
        self.save()
