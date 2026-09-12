"""Persistent task storage and sorting for the daily task list app."""

from __future__ import annotations

import calendar
import json
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


def new_uid() -> str:
    """生成本机唯一的任务标识，用于跨设备同步合并。"""
    return uuid.uuid4().hex


PRIORITY_MIN = 1
PRIORITY_MAX = 10
PRIORITY_DEFAULT = 5
PROJECT_NAME_MAX = 40
DEFAULT_PROJECT_NAME = "默认项目"
DATA_FILE_NAME = "tasks_data.json"

# ---------------------------------------------------------------------------
# 间隔复习（spaced repetition）提醒配置
# 写入项目后，按以下节奏提醒复习；点“已执行”后跳到下一档；到 1 年后改为每年一次。
# ---------------------------------------------------------------------------
REVIEW_STAGES = [
    ("24小时后", "days", 1),
    ("2天后", "days", 2),
    ("4天后", "days", 4),
    ("8天后", "days", 8),
    ("16天后", "days", 16),
    ("1个月后", "months", 1),
    ("2个月后", "months", 2),
    ("4个月后", "months", 4),
    ("8个月后", "months", 8),
    ("1年后", "years", 1),
]
REVIEW_ANNUAL_LABEL = "每年"


def review_stage_label(stage: int) -> str:
    """返回某档次的文字标签，例如 '2天后' / '每年'。"""
    if stage >= len(REVIEW_STAGES):
        return REVIEW_ANNUAL_LABEL
    return REVIEW_STAGES[stage][0]


def _add_months(base_dt: datetime, months: int) -> datetime:
    """在不依赖第三方库的前提下给日期加若干个月，自动处理月末溢出。"""
    total = (base_dt.month - 1) + months
    year = base_dt.year + total // 12
    month = total % 12 + 1
    day = min(base_dt.day, calendar.monthrange(year, month)[1])
    return base_dt.replace(year=year, month=month, day=day)


def review_next_due(base_dt: datetime, stage: int) -> datetime:
    """根据当前档次计算下一次复习到期时间。"""
    if stage >= len(REVIEW_STAGES):
        kind, amount = "years", 1
    else:
        _, kind, amount = REVIEW_STAGES[stage]
    if kind == "days":
        return base_dt + timedelta(days=amount)
    if kind == "months":
        return _add_months(base_dt, amount)
    return _add_months(base_dt, 12 * amount)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def validate_priority(value: Any) -> int:
    """Return the priority as an int in the supported 1-10 range."""
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        parsed = -1
    if not PRIORITY_MIN <= parsed <= PRIORITY_MAX:
        raise ValueError(f"优先级必须是 {PRIORITY_MIN}-{PRIORITY_MAX} 的整数")
    return parsed


class TaskStore:
    """Owns projects, task lists, sorting rules, and JSON persistence."""

    def __init__(self, path: Path | str | None = None) -> None:
        if path is None:
            path = Path(__file__).resolve().parent / DATA_FILE_NAME
        self.path = Path(path)
        self.projects: list[dict[str, Any]] = []
        self.tasks: list[dict[str, Any]] = []
        self.next_id = 1
        self.next_project_id = 1
        self.current_project_id: int | None = None
        self.window_geometry: str | None = None
        self.last_reminder_date: str | None = None
        self._needs_save = False
        self._load()
        self.ensure_default_project()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or not isinstance(payload.get("tasks"), list):
                raise ValueError("invalid data shape")

            loaded_projects: list[dict[str, Any]] = []
            max_project_id = 0
            max_project_seq = 0
            seen_project_ids: set[int] = set()
            raw_projects = payload.get("projects")
            if isinstance(raw_projects, list):
                for index, item in enumerate(raw_projects):
                    if not isinstance(item, dict):
                        continue
                    try:
                        project_id = int(item.get("id"))
                    except (TypeError, ValueError):
                        continue
                    name = str(item.get("name") or "").strip()
                    if project_id <= 0 or not name or project_id in seen_project_ids:
                        continue
                    created_at = item.get("created_at")
                    if not isinstance(created_at, str) or not created_at:
                        created_at = _now_iso()
                    try:
                        seq = int(item.get("seq", index + 1))
                    except (TypeError, ValueError):
                        seq = index + 1
                    loaded_projects.append(
                        {
                            "id": project_id,
                            "name": name,
                            "created_at": created_at,
                            "seq": seq,
                        }
                    )
                    seen_project_ids.add(project_id)
                    max_project_id = max(max_project_id, project_id)
                    max_project_seq = max(max_project_seq, seq)

            needs_save = int(payload.get("version", 1)) < 4
            if not loaded_projects:
                default_project = {
                    "id": 1,
                    "name": DEFAULT_PROJECT_NAME,
                    "created_at": _now_iso(),
                    "seq": 1,
                }
                loaded_projects = [default_project]
                max_project_id = 1
                max_project_seq = 1
                needs_save = True

            valid_project_ids = {project["id"] for project in loaded_projects}
            default_project_id = loaded_projects[0]["id"]
            loaded: list[dict[str, Any]] = []
            max_id = 0
            max_seq = 0
            for index, item in enumerate(payload["tasks"]):
                if not isinstance(item, dict):
                    raise ValueError("invalid task entry")
                task_id = item.get("id")
                text = str(item.get("text") or "").strip()
                if not isinstance(task_id, int) or not text:
                    raise ValueError("invalid task id or text")
                priority = validate_priority(item.get("priority"))
                created_at = item.get("created_at")
                if not isinstance(created_at, str) or not created_at:
                    created_at = _now_iso()
                try:
                    seq = int(item.get("seq", index + 1))
                except (TypeError, ValueError):
                    seq = index + 1
                project_ids: list[int] = []
                raw_project_ids = item.get("project_ids")
                if isinstance(raw_project_ids, list):
                    for value in raw_project_ids:
                        if (
                            isinstance(value, int)
                            and value in valid_project_ids
                            and value not in project_ids
                        ):
                            project_ids.append(value)
                if not project_ids:
                    legacy_project_id = item.get("project_id")
                    if (
                        isinstance(legacy_project_id, int)
                        and legacy_project_id in valid_project_ids
                    ):
                        project_ids = [legacy_project_id]
                    else:
                        project_ids = [default_project_id]
                    needs_save = True
                if raw_project_ids != project_ids:
                    needs_save = True

                raw_done_ids = item.get("done_project_ids")
                done_ids: list[int] = []
                if isinstance(raw_done_ids, list):
                    for value in raw_done_ids:
                        if (
                            isinstance(value, int)
                            and value in project_ids
                            and value not in done_ids
                        ):
                            done_ids.append(value)
                else:
                    legacy_done = bool(item.get("done", False))
                    done_ids = list(project_ids) if legacy_done else []
                    needs_save = True
                completed_at = item.get("completed_at")
                if not isinstance(completed_at, str) or not completed_at:
                    completed_at = created_at if done_ids else None
                if not done_ids:
                    completed_at = None

                loaded.append(
                    {
                        "id": task_id,
                        "project_ids": project_ids,
                        "done_project_ids": done_ids,
                        "text": text,
                        "priority": priority,
                        "created_at": created_at,
                        "completed_at": completed_at,
                        "seq": seq,
                        "uid": item.get("uid") or new_uid(),
                        "updated_at": item.get("updated_at") or created_at,
                        "deleted": bool(item.get("deleted", False)),
                    }
                )
                max_id = max(max_id, task_id)
                max_seq = max(max_seq, seq)

            current_project_id = payload.get("current_project_id")
            if not isinstance(current_project_id, int) or current_project_id not in valid_project_ids:
                current_project_id = default_project_id
                needs_save = True

            self.projects = loaded_projects
            self.tasks = loaded
            self.next_id = max(max_id + 1, self._safe_int(payload.get("next_id"), 1))
            self.next_project_id = max(
                max_project_id + 1,
                self._safe_int(payload.get("next_project_id"), 1),
            )
            self.current_project_id = current_project_id
            geometry = payload.get("window_geometry")
            self.window_geometry = geometry if isinstance(geometry, str) and geometry else None
            self.last_reminder_date = payload.get("last_reminder_date")
            self._needs_save = needs_save
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self._backup_bad_file()
            self.projects = []
            self.tasks = []
            self.next_id = 1
            self.next_project_id = 1
            self.current_project_id = None
            self.window_geometry = None
            self._needs_save = True

    @staticmethod
    def _safe_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _backup_bad_file(self) -> None:
        try:
            if self.path.exists():
                backup_path = self.path.with_suffix(".bak")
                if backup_path.exists():
                    backup_path.unlink()
                os.replace(self.path, backup_path)
        except OSError:
            pass

    def save(self) -> None:
        payload = {
            "version": 4,
            "next_id": self.next_id,
            "next_project_id": self.next_project_id,
            "current_project_id": self.current_project_id,
            "window_geometry": self.window_geometry,
            "projects": self.projects,
            "tasks": self.tasks,
        }
        tmp_path = self.path.with_name(self.path.name + ".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, self.path)
        self._needs_save = False

    def ensure_file(self) -> None:
        if not self.path.exists() or self._needs_save:
            self.save()

    def ensure_default_project(self) -> None:
        if not self.projects:
            self.projects.append(
                {
                    "id": 1,
                    "name": DEFAULT_PROJECT_NAME,
                    "created_at": _now_iso(),
                    "seq": 1,
                }
            )
            self.next_project_id = max(self.next_project_id, 2)
            self._needs_save = True
        valid_ids = {project["id"] for project in self.projects}
        if self.current_project_id not in valid_ids:
            self.current_project_id = self.projects[0]["id"]
            self._needs_save = True

    def projects_list(self) -> list[dict[str, Any]]:
        projects = list(self.projects)
        projects.sort(key=lambda project: (project["seq"], project["id"]))
        return projects

    def project_names(self) -> list[str]:
        return [project["name"] for project in self.projects_list()]

    def current_project(self) -> dict[str, Any]:
        self.ensure_default_project()
        assert self.current_project_id is not None
        return self._find_project(self.current_project_id)

    def _find_project(self, project_id: int) -> dict[str, Any]:
        for project in self.projects:
            if project["id"] == project_id:
                return project
        raise ValueError("任务项目不存在")

    def find_project_by_name(self, name: str) -> dict[str, Any] | None:
        clean_name = str(name).strip().casefold()
        for project in self.projects:
            if project["name"].strip().casefold() == clean_name:
                return project
        return None

    def add_project(self, name: str) -> dict[str, Any]:
        clean_name = str(name).strip()
        if not clean_name:
            raise ValueError("项目名称不能为空")
        if len(clean_name) > PROJECT_NAME_MAX:
            raise ValueError(f"项目名称不能超过 {PROJECT_NAME_MAX} 个字")
        if self.find_project_by_name(clean_name) is not None:
            raise ValueError("已经有这个项目了")
        seq = max([project["seq"] for project in self.projects] or [0]) + 1
        project = {
            "id": self.next_project_id,
            "name": clean_name,
            "created_at": _now_iso(),
            "seq": seq,
        }
        self.next_project_id += 1
        self.projects.append(project)
        self.current_project_id = project["id"]
        self.save()
        return project

    def rename_project(self, project_id: int, name: str) -> dict[str, Any]:
        project = self._find_project(project_id)
        clean_name = str(name).strip()
        if not clean_name:
            raise ValueError("项目名称不能为空")
        if len(clean_name) > PROJECT_NAME_MAX:
            raise ValueError(f"项目名称不能超过 {PROJECT_NAME_MAX} 个字")
        existing = self.find_project_by_name(clean_name)
        if existing is not None and existing["id"] != project["id"]:
            raise ValueError("已经有这个项目了")
        if project["name"] != clean_name:
            project["name"] = clean_name
            self.save()
        return project

    def project_task_stats(self, project_id: int) -> dict[str, int]:
        """How many tasks reference a project, and how many live only in it."""
        total = 0
        only_here = 0
        for task in self.tasks:
            project_ids = task.get("project_ids") or []
            if project_id in project_ids:
                total += 1
                if len(project_ids) <= 1:
                    only_here += 1
        return {"total": total, "only_here": only_here}

    def delete_project(self, project_id: int) -> dict[str, Any]:
        """Delete a project, keeping at least one and never orphaning a task."""
        if len(self.projects) <= 1:
            raise ValueError("至少要保留一个任务项目")
        project = self._find_project(project_id)
        remaining_projects = [item for item in self.projects if item["id"] != project_id]
        remaining_projects.sort(key=lambda item: (item["seq"], item["id"]))
        fallback_id = remaining_projects[0]["id"]
        moved_tasks = 0
        for task in self.tasks:
            project_ids = list(task.get("project_ids") or [])
            if project_id not in project_ids:
                continue
            kept_ids = [value for value in project_ids if value != project_id]
            done_ids = [
                value for value in (task.get("done_project_ids") or []) if value != project_id
            ]
            if not kept_ids:
                kept_ids = [fallback_id]
                moved_tasks += 1
                if project_id in (task.get("done_project_ids") or []):
                    done_ids = [fallback_id]
            task["project_ids"] = kept_ids
            task["done_project_ids"] = done_ids
            if not done_ids:
                task["completed_at"] = None
        self.projects = remaining_projects
        if self.current_project_id == project_id:
            self.current_project_id = fallback_id
        self.save()
        return {
            "name": project["name"],
            "fallback": self._find_project(fallback_id)["name"],
            "moved_tasks": moved_tasks,
        }

    def set_current_project(self, project_id: int) -> None:
        project = self._find_project(project_id)
        if self.current_project_id != project["id"]:
            self.current_project_id = project["id"]
            self.save()

    def active_tasks(self, project_id: int | None = None) -> list[dict[str, Any]]:
        selected_project_id = project_id if project_id is not None else self.current_project_id
        if selected_project_id is None:
            return []
        self._find_project(selected_project_id)
        active = [
            task
            for task in self.tasks
            if selected_project_id in task["project_ids"]
            and selected_project_id not in (task.get("done_project_ids") or [])
            and not task.get("deleted", False)
        ]
        active.sort(key=lambda task: (task["priority"], task["seq"]))
        return active

    def completed_tasks(self, project_id: int | None = None) -> list[dict[str, Any]]:
        selected_project_id = project_id if project_id is not None else self.current_project_id
        if selected_project_id is None:
            return []
        self._find_project(selected_project_id)
        completed = [
            task
            for task in self.tasks
            if selected_project_id in (task.get("done_project_ids") or [])
            and selected_project_id in task["project_ids"]
            and not task.get("deleted", False)
        ]
        completed.sort(key=lambda task: (task["seq"], task["id"]), reverse=True)
        completed.sort(key=lambda task: task["completed_at"] or "", reverse=True)
        return completed

    def add_task(
        self,
        text: str,
        priority: Any,
        project_id: int | None = None,
        review: bool = False,
    ) -> dict[str, Any]:
        clean_text = str(text).strip()
        if not clean_text:
            raise ValueError("任务内容不能为空")
        clean_priority = validate_priority(priority)
        selected_project_id = project_id if project_id is not None else self.current_project_id
        if selected_project_id is None:
            raise ValueError("请先选择任务项目")
        self._find_project(selected_project_id)
        seq = max([task["seq"] for task in self.tasks] or [0]) + 1
        task = {
            "id": self.next_id,
            "project_ids": [selected_project_id],
            "done_project_ids": [],
            "text": clean_text,
            "priority": clean_priority,
            "created_at": _now_iso(),
            "completed_at": None,
            "seq": seq,
            "uid": new_uid(),
            "updated_at": _now_iso(),
            "deleted": False,
        }
        if review:
            now = datetime.now().astimezone()
            task["review"] = {
                "stage": 0,
                "review_created_at": _now_iso(),
                "next_due": review_next_due(now, 0).isoformat(timespec="seconds"),
                "last_reviewed_at": None,
                "review_count": 0,
            }
        self.next_id += 1
        self.tasks.append(task)
        self.save()
        return task

    def _find(self, task_id: int) -> dict[str, Any]:
        for task in self.tasks:
            if task["id"] == task_id:
                return task
        raise ValueError("任务不存在")

    def set_text(self, task_id: int, text: str) -> None:
        clean_text = str(text).strip()
        if not clean_text:
            raise ValueError("任务内容不能为空")
        task = self._find(task_id)
        if task["text"] != clean_text:
            task["text"] = clean_text
            task["updated_at"] = _now_iso()
            self.save()

    def set_priority(self, task_id: int, priority: Any) -> None:
        clean_priority = validate_priority(priority)
        task = self._find(task_id)
        if task["priority"] != clean_priority:
            task["priority"] = clean_priority
            task["updated_at"] = _now_iso()
            self.save()

    def task_project_ids(self, task_id: int) -> list[int]:
        task = self._find(task_id)
        project_ids = task.get("project_ids")
        if not isinstance(project_ids, list):
            return []
        valid_ids = {project["id"] for project in self.projects}
        return [project_id for project_id in project_ids if project_id in valid_ids]

    def set_task_projects(self, task_id: int, project_ids: list[int]) -> None:
        task = self._find(task_id)
        valid_ids = {project["id"] for project in self.projects}
        clean_ids: list[int] = []
        for project_id in project_ids:
            if project_id in valid_ids and project_id not in clean_ids:
                clean_ids.append(project_id)
        if not clean_ids:
            raise ValueError("每个任务至少保留一个项目")
        changed = task.get("project_ids") != clean_ids
        if changed:
            task["project_ids"] = clean_ids
        done_ids = [
            project_id for project_id in (task.get("done_project_ids") or []) if project_id in clean_ids
        ]
        if done_ids != (task.get("done_project_ids") or []):
            task["done_project_ids"] = done_ids
            changed = True
        if not done_ids and task.get("completed_at"):
            task["completed_at"] = None
            changed = True
        if changed:
            self.save()

    def toggle_task_project(self, task_id: int, project_id: int) -> list[int]:
        self._find_project(project_id)
        project_ids = self.task_project_ids(task_id)
        if project_id in project_ids:
            if len(project_ids) <= 1:
                raise ValueError("每个任务至少要保留一个项目")
            project_ids.remove(project_id)
        else:
            project_ids.append(project_id)
        self.set_task_projects(task_id, project_ids)
        return project_ids

    def is_done_in(self, task_id: int, project_id: int) -> bool:
        task = self._find(task_id)
        return project_id in (task.get("done_project_ids") or [])

    def pending_project_names(self, task_id: int, exclude_project_id: int) -> list[str]:
        """Projects that still keep this task open, excluding the given one."""
        task = self._find(task_id)
        done_ids = set(task.get("done_project_ids") or [])
        names = []
        for project_id in task.get("project_ids") or []:
            if project_id == exclude_project_id or project_id in done_ids:
                continue
            try:
                names.append(self._find_project(project_id)["name"])
            except ValueError:
                continue
        return names

    def set_done(self, task_id: int, done: bool, project_id: int | None = None) -> None:
        """Mark a task done/undone for one project only, leaving other projects alone."""
        task = self._find(task_id)
        target_project_id = project_id if project_id is not None else self.current_project_id
        if target_project_id is None:
            raise ValueError("请先选择任务项目")
        self._find_project(target_project_id)
        done_ids = list(task.get("done_project_ids") or [])
        if done:
            if target_project_id in done_ids:
                return
            done_ids.append(target_project_id)
            task["done_project_ids"] = done_ids
            task["completed_at"] = _now_iso()
        else:
            if target_project_id not in done_ids:
                return
            done_ids.remove(target_project_id)
            task["done_project_ids"] = done_ids
            task["completed_at"] = _now_iso() if done_ids else None
        task["updated_at"] = _now_iso()
        self.save()

    def remove_from_project(self, task_id: int, project_id: int) -> bool:
        """Drop a task from one project only. True if the task was deleted entirely."""
        task = self._find(task_id)
        project_ids = [
            value for value in (task.get("project_ids") or []) if value != project_id
        ]
        if not project_ids:
            self.delete_task(task_id)
            return True
        self.set_task_projects(task_id, project_ids)
        return False

    def task_text(self, task_id: int) -> str:
        return str(self._find(task_id).get("text") or "")

    def delete_task(self, task_id: int) -> None:
        task = self._find(task_id)
        self.tasks.remove(task)
        self.save()

    def soft_delete_task(self, task_id: int) -> None:
        """标记删除（保留墓碑，便于跨设备同步删除）。"""
        task = self._find(task_id)
        task["deleted"] = True
        task["updated_at"] = _now_iso()
        self.save()

    # ----- 间隔复习提醒 -----

    @staticmethod
    def _parse_iso(value: Any) -> datetime | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    @staticmethod
    def _normalize_review(raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        stage = raw.get("stage")
        next_due = raw.get("next_due")
        if not isinstance(stage, int) or not isinstance(next_due, str) or not next_due:
            return None
        return {
            "stage": stage,
            "review_created_at": raw.get("review_created_at")
            if isinstance(raw.get("review_created_at"), str)
            else None,
            "next_due": next_due,
            "last_reviewed_at": raw.get("last_reviewed_at")
            if isinstance(raw.get("last_reviewed_at"), str)
            else None,
            "review_count": int(raw.get("review_count", 0) or 0),
        }

    def enable_review(self, task_id: int) -> dict[str, Any]:
        """为某个任务开启间隔复习提醒（首次 24 小时后提醒）。"""
        task = self._find(task_id)
        if task.get("review") is not None:
            return task["review"]
        now = datetime.now().astimezone()
        review = {
            "stage": 0,
            "review_created_at": _now_iso(),
            "next_due": review_next_due(now, 0).isoformat(timespec="seconds"),
            "last_reviewed_at": None,
            "review_count": 0,
        }
        task["review"] = review
        task["updated_at"] = _now_iso()
        self.save()
        return review

    def disable_review(self, task_id: int) -> None:
        task = self._find(task_id)
        if task.get("review") is None:
            return
        task["review"] = None
        task["updated_at"] = _now_iso()
        self.save()

    def execute_review(self, task_id: int) -> dict[str, Any]:
        """点“已执行”：记录本次复习并跳到下一档时间。"""
        task = self._find(task_id)
        review = task.get("review")
        if review is None:
            raise ValueError("该任务未开启复习提醒")
        now = datetime.now().astimezone()
        stage = int(review.get("stage", 0))
        next_stage = stage if stage >= len(REVIEW_STAGES) else stage + 1
        review["stage"] = next_stage
        review["last_reviewed_at"] = _now_iso()
        review["review_count"] = int(review.get("review_count", 0)) + 1
        review["next_due"] = review_next_due(now, next_stage).isoformat(timespec="seconds")
        task["updated_at"] = _now_iso()
        self.save()
        return review

    def is_review_due(self, task: dict[str, Any]) -> bool:
        review = task.get("review")
        if not review:
            return False
        due_dt = self._parse_iso(review.get("next_due"))
        if due_dt is None:
            return False
        return due_dt <= datetime.now().astimezone()

    def review_status_text(self, task: dict[str, Any]) -> str:
        review = task.get("review")
        if not review:
            return ""
        now = datetime.now().astimezone()
        due_dt = self._parse_iso(review.get("next_due"))
        if due_dt is None:
            return "复习数据异常"
        if due_dt > now:
            if due_dt.year == now.year:
                return f"下次复习：{due_dt.month}月{due_dt.day}日"
            return f"下次复习：{due_dt.year}年{due_dt.month}月{due_dt.day}日"
        overdue_days = (now - due_dt).days
        if overdue_days <= 0:
            return "今天待复习"
        return f"已逾期 {overdue_days} 天"

    def review_tasks_due(self, project_id: int | None = None) -> list[dict[str, Any]]:
        """当前项目内、已到期且在该项目仍处待办状态的复习项。"""
        selected = project_id if project_id is not None else self.current_project_id
        if selected is None:
            return []
        self._find_project(selected)
        now = datetime.now().astimezone()
        due: list[dict[str, Any]] = []
        for task in self.tasks:
            if selected not in (task.get("project_ids") or []):
                continue
            if selected in (task.get("done_project_ids") or []):
                continue
            if task.get("deleted", False):
                continue
            review = task.get("review")
            if not review:
                continue
            due_dt = self._parse_iso(review.get("next_due"))
            if due_dt and due_dt <= now:
                due.append(task)
        due.sort(key=lambda item: self._parse_iso(item["review"]["next_due"]) or now)
        return due

    def review_tasks_due_all(self) -> list[dict[str, Any]]:
        """跨所有项目的到期复习项，用于每日启动提醒。"""
        now = datetime.now().astimezone()
        due: list[dict[str, Any]] = []
        for task in self.tasks:
            review = task.get("review")
            if not review:
                continue
            project_ids = task.get("project_ids") or []
            done_ids = set(task.get("done_project_ids") or [])
            if done_ids and all(pid in done_ids for pid in project_ids):
                continue
            due_dt = self._parse_iso(review.get("next_due"))
            if due_dt and due_dt <= now:
                due.append(task)
        due.sort(key=lambda item: self._parse_iso(item["review"]["next_due"]) or now)
        return due
