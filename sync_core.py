# -*- coding: utf-8 -*-
"""跨设备同步核心（纯逻辑，手机端与电脑同步端共用，可无界面测试）

同步模型：
- 任务用"扁平格式"交换（按项目名称而非项目 id，避免两端项目 id 冲突）。
- 卡片直接按 uid 合并。
- 合并规则：按 uid 求并集，updated_at 后者覆盖前者（末写优先）；deleted 墓碑保留。
"""

from __future__ import annotations

import json
import socket
import time
from datetime import datetime
from typing import Any

from task_store import new_uid, _now_iso

DISCOVER_PORT = 54321
MAGIC = b"FLASHSYNC"

# ------------------------- 任务：扁平格式桥接（PC 项目模型 <-> 同步格式） -------------------------

def _resolve_project(tasks_store, name: str) -> int:
    """按名称取项目 id，不存在则创建。"""
    name = (name or "").strip() or "默认项目"
    proj = tasks_store.find_project_by_name(name)
    if proj is not None:
        return proj["id"]
    return tasks_store.add_project(name)["id"]


def export_tasks(tasks_store) -> list[dict[str, Any]]:
    """把项目型任务导出为扁平同步格式（顺便补全缺失的 uid/updated_at）。"""
    proj_by_id = {p["id"]: p["name"] for p in tasks_store.projects}
    out: list[dict[str, Any]] = []
    changed = False
    for t in tasks_store.tasks:
        if not t.get("uid"):
            t["uid"] = new_uid()
            t["updated_at"] = t.get("updated_at") or _now_iso()
            changed = True
        primary_pid = (t.get("project_ids") or [None])[0]
        out.append({
            "uid": t["uid"],
            "text": t.get("text", ""),
            "priority": t.get("priority", 5),
            "done": bool(t.get("done_project_ids")),
            "project": proj_by_id.get(primary_pid, "默认项目"),
            "review": t.get("review"),
            "created_at": t.get("created_at"),
            "updated_at": t.get("updated_at") or t.get("created_at") or _now_iso(),
            "deleted": bool(t.get("deleted", False)),
        })
    if changed:
        tasks_store.save()
    return out


def _apply_flat_to_task(tasks_store, task: dict[str, Any], f: dict[str, Any]) -> None:
    task["text"] = f.get("text", task.get("text", ""))
    task["priority"] = f.get("priority", task.get("priority", 5))
    task["review"] = f.get("review")
    task["deleted"] = bool(f.get("deleted", False))
    task["updated_at"] = f.get("updated_at") or _now_iso()
    # 完成状态：以扁平 done 为准
    done = bool(f.get("done"))
    if done:
        task["done_project_ids"] = list(task.get("project_ids") or [])
        task["completed_at"] = task.get("completed_at") or _now_iso()
    else:
        task["done_project_ids"] = []
        task["completed_at"] = None
    # 项目归属：按名称解析
    pid = _resolve_project(tasks_store, f.get("project"))
    task["project_ids"] = [pid]


def _create_task_from_flat(tasks_store, f: dict[str, Any]) -> None:
    pid = _resolve_project(tasks_store, f.get("project"))
    now = _now_iso()
    done = bool(f.get("done"))
    task = {
        "id": tasks_store.next_id,
        "project_ids": [pid],
        "done_project_ids": list([pid]) if done else [],
        "text": f.get("text", ""),
        "priority": f.get("priority", 5),
        "created_at": f.get("created_at") or now,
        "completed_at": now if done else None,
        "seq": max([t.get("seq", 0) for t in tasks_store.tasks] or [0]) + 1,
        "uid": f.get("uid") or new_uid(),
        "updated_at": f.get("updated_at") or now,
        "deleted": bool(f.get("deleted", False)),
        "review": f.get("review"),
    }
    tasks_store.next_id += 1
    tasks_store.tasks.append(task)


def import_tasks(tasks_store, flat_list: list[dict[str, Any]]) -> None:
    """把对端发来的扁平任务合并进本地（按 uid 末写优先）。"""
    by_uid = {t["uid"]: t for t in tasks_store.tasks if t.get("uid")}
    for f in flat_list:
        uid = f.get("uid")
        if not uid:
            continue
        existing = by_uid.get(uid)
        if existing is not None:
            local_upd = existing.get("updated_at") or ""
            remote_upd = f.get("updated_at") or ""
            if remote_upd and local_upd and remote_upd <= local_upd:
                continue  # 本地更新或相等，保留本地
            _apply_flat_to_task(tasks_store, existing, f)
        else:
            _create_task_from_flat(tasks_store, f)
    tasks_store.save()


# ------------------------- 卡片：直接按 uid 合并 -------------------------

def export_cards(flashcard_store) -> list[dict[str, Any]]:
    out = []
    for c in flashcard_store.all_cards():
        if not c.get("uid"):
            c["uid"] = new_uid()
            c["updated_at"] = c.get("updated_at") or _now_iso()
            flashcard_store.save()
        out.append(dict(c))
    return out


def import_cards(flashcard_store, card_list: list[dict[str, Any]]) -> None:
    by_uid = {c["uid"]: c for c in flashcard_store.all_cards() if c.get("uid")}
    for c in card_list:
        uid = c.get("uid")
        if not uid:
            continue
        existing = by_uid.get(uid)
        if existing is not None:
            local_upd = existing.get("updated_at") or ""
            remote_upd = c.get("updated_at") or ""
            if remote_upd and local_upd and remote_upd <= local_upd:
                continue
            existing.update(c)
        else:
            flashcard_store.cards.append(dict(c))
    flashcard_store.replace_all(flashcard_store.cards)


# ------------------------- 通用合并（用于测试与两端对称） -------------------------

def _item_key(item: dict[str, Any]):
    """合并主键：优先用 uid；缺失时按内容派生（文本+创建时间+正面），避免重复。"""
    uid = item.get("uid")
    if uid:
        return ("u", uuid_key(uid))
    return ("d", item.get("text", ""), item.get("created_at", ""),
            item.get("front", ""), item.get("project", ""))


def merge_items(local: list[dict[str, Any]], remote: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 uid（或内容派生键）合并两个列表，末写优先；保留 deleted 墓碑。"""
    by_key: dict[tuple, dict[str, Any]] = {}
    for item in local:
        by_key[_item_key(item)] = item
    for item in remote:
        key = _item_key(item)
        if key not in by_key:
            by_key[key] = item
        else:
            local_upd = by_key[key].get("updated_at") or ""
            remote_upd = item.get("updated_at") or ""
            # 严格小于才保留本地；相等或更新则采用远端，确保同秒改动也能传播
            if remote_upd and (not local_upd or local_upd < remote_upd):
                by_key[key] = item
    return list(by_key.values())


def uuid_key(uid: str) -> str:
    return str(uid)


def merge_payload(local: dict[str, Any], remote: dict[str, Any]) -> dict[str, Any]:
    return {
        "tasks": merge_items(local.get("tasks", []), remote.get("tasks", [])),
        "cards": merge_items(local.get("cards", []), remote.get("cards", [])),
        "device": local.get("device") or remote.get("device"),
    }


# ------------------------- Wi‑Fi 设备发现（尽力而为；失败可手动输 IP） -------------------------

def local_ip() -> str:
    """获取本机在局域网中的 IP（不实际发包，只是查路由表）。"""
    for probe in (("223.5.5.5", 80), ("8.8.8.8", 80), ("1.1.1.1", 80)):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.5)
            s.connect(probe)
            ip = s.getsockname()[0]
            s.close()
            if ip and ip.count(".") == 3 and not ip.startswith("127."):
                return ip
        except OSError:
            continue
        except Exception:
            continue
    return ""


def _tcp_probe(ip: str, port: int, timeout: float):
    """探测单个 IP 的 TCP 端口是否可连，返回 ip 或 None。"""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return ip
    except OSError:
        return None
    except Exception:
        return None


def scan_lan(port: int, timeout: float = 0.4, max_workers: int = 64) -> list[str]:
    """扫描手机所在网段的全部 IP，返回开放了指定端口的地址列表。

    这是广播发现的兜底方案：很多路由器/手机会吃掉 UDP 广播，
    但同网段 TCP 直连通常仍然可达。
    """
    me = local_ip()
    if not me:
        return []
    prefix = me.rsplit(".", 1)[0]
    cands = [f"{prefix}.{i}" for i in range(1, 255) if f"{prefix}.{i}" != me]
    found: list[str] = []
    try:
        from concurrent.futures import ThreadPoolExecutor
    except Exception:
        # 极端环境无线程池时退化为顺序扫描（慢但可用，限制前 60 个）
        for ip in cands[:60]:
            r = _tcp_probe(ip, port, timeout)
            if r:
                found.append(r)
        return found
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(_tcp_probe, ip, port, timeout) for ip in cands]
        for f in futures:
            try:
                r = f.result()
            except Exception:
                r = None
            if r:
                found.append(r)
    return found


def discover_pc(timeout: float = 3.0):
    """手机端：广播发现电脑同步服务，返回 (ip, tcp_port) 或 None。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(timeout)
    try:
        sock.sendto(MAGIC + b" DISCOVER", ("<broadcast>", DISCOVER_PORT))
        data, addr = sock.recvfrom(1024)
    except socket.timeout:
        return None
    except OSError:
        return None
    finally:
        sock.close()
    if data.startswith(MAGIC + b" SERVER"):
        try:
            port = int(data.split()[-1])
        except (ValueError, IndexError):
            return None
        return (addr[0], port)
    return None


def discovery_responder(tcp_port: int, stop_event):
    """电脑端：监听发现广播并回应本机 TCP 端口。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        sock.bind(("", DISCOVER_PORT))
    except OSError:
        return
    sock.settimeout(0.5)
    while not stop_event.is_set():
        try:
            data, addr = sock.recvfrom(1024)
        except socket.timeout:
            continue
        except OSError:
            break
        if data.startswith(MAGIC + b" DISCOVER"):
            try:
                sock.sendto(MAGIC + b" SERVER " + str(tcp_port).encode(), addr)
            except OSError:
                pass
    sock.close()
