"""Настройки бота: токен, администраторы и список школ из config/schools.json."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Shift:
    """Смена школы: отдельный источник с расписанием (например, 1 и 2 смена)."""

    id: str
    name: str
    url: str
    bells: dict[str, str] = field(default_factory=dict)
    saturday_bells: dict[str, str] = field(default_factory=dict)


@dataclass
class School:
    """Одна школа (учебное заведение), расписание которой умеет присылать бот."""

    id: str
    name: str
    source: str
    url: str
    shifts: list[Shift] = field(default_factory=list)

    @property
    def folder_id(self) -> str | None:
        """ID папки Google Drive, если источник — папка на Диске."""
        match = re.search(r"/folders/([A-Za-z0-9_-]+)", self.url)
        return match.group(1) if match else None

    @property
    def has_shifts(self) -> bool:
        """Есть ли у школы разделение на смены."""
        return bool(self.shifts)

    def shift(self, shift_id: str) -> Shift | None:
        """Возвращает смену по её id."""
        for shift in self.shifts:
            if shift.id == shift_id:
                return shift
        return None


@dataclass
class Channel:
    """Источник расписания: школа целиком или одна из её смен.

    section = "current" — только временные изменения, "main" — расписание на полугодие.
    """

    school: School
    shift: Shift | None = None
    section: str = "current"

    @property
    def id(self) -> str:
        """Уникальный ключ источника: «mgst», «gym6:1» или «gym6:1:main»."""
        base = f"{self.school.id}:{self.shift.id}" if self.shift else self.school.id
        return base + ":main" if self.section == "main" else base

    @property
    def name(self) -> str:
        """Отображаемое название: «Гимназия №6 · 1 смена»."""
        if self.shift:
            name = f"{self.school.name} · {self.shift.name}"
        else:
            name = self.school.name
        if self.section == "main":
            name += " · Основное расписание"
        return name

    @property
    def source(self) -> str:
        return self.school.source

    @property
    def url(self) -> str:
        return self.shift.url if self.shift else self.school.url

    @property
    def bells(self) -> dict[str, str]:
        """Время звонков для отрисовки: номер урока → интервал."""
        return self.shift.bells if self.shift else {}

    @property
    def saturday_bells(self) -> dict[str, str]:
        """Время звонков для субботы."""
        return self.shift.saturday_bells if self.shift else {}

    @property
    def folder_id(self) -> str | None:
        return self.school.folder_id


@dataclass
class Settings:
    """Все настройки приложения, собранные из .env и файла школ."""

    def __init__(self) -> None:
        load_dotenv(BASE_DIR / ".env")

        self.bot_token = os.getenv("BOT_TOKEN", "").strip()
        admins = os.getenv("ADMIN_IDS", "")
        self.admin_ids = {
            int(part) for part in re.split(r"[,\s]+", admins) if part.isdigit()
        }
        self.check_interval_minutes = int(os.getenv("CHECK_INTERVAL_MINUTES", "45"))

        data_dir = os.getenv("DATA_DIR", "data")
        self.data_dir = BASE_DIR / data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.schools_file = BASE_DIR / os.getenv("SCHOOLS_FILE", "config/schools.json")
        self.schools = self._load_schools()

    def _load_schools(self) -> list[School]:
        if not self.schools_file.exists():
            raise FileNotFoundError(
                "Не найден файл со списком школ: " + str(self.schools_file)
            )
        raw = json.loads(self.schools_file.read_text(encoding="utf-8"))
        schools: list[School] = []
        for item in raw.get("schools", []):
            shifts = [
                Shift(
                    id=str(shift.get("id", "")).strip(),
                    name=str(shift.get("name", "")).strip(),
                    url=str(shift.get("url", "")).strip(),
                    bells={
                        str(number).strip(): str(interval).strip()
                        for number, interval in (shift.get("bells") or {}).items()
                    },
                    saturday_bells={
                        str(number).strip(): str(interval).strip()
                        for number, interval in (shift.get("saturday_bells") or {}).items()
                    },
                )
                for shift in item.get("shifts", [])
            ]
            schools.append(
                School(
                    id=str(item["id"]).strip(),
                    name=str(item["name"]).strip(),
                    source=str(item.get("source", "gdrive_folder")).strip(),
                    url=str(item.get("url", "")).strip(),
                    shifts=shifts,
                )
            )
        return schools

    def school(self, school_id: str) -> School | None:
        """Находит школу по id."""
        for school in self.schools:
            if school.id == school_id:
                return school
        return None

    def channels(self) -> list[Channel]:
        """Все источники расписания: каждая школа и каждая её смена."""
        result: list[Channel] = []
        for school in self.schools:
            if school.has_shifts:
                result.extend(Channel(school, shift) for shift in school.shifts)
            else:
                result.append(Channel(school))
        return result

    def channel(self, source_id: str) -> Channel | None:
        """Находит источник по его id (например, «gym6:1» или «gym6:1:main»)."""
        school_id, _, rest = source_id.partition(":")
        school = self.school(school_id)
        if school is None:
            return None
        section = "main" if rest == "main" or rest.endswith(":main") else "current"
        shift_id = rest[: -len(":main")] if rest.endswith(":main") else rest
        if shift_id == "main":
            shift_id = ""
        if not shift_id:
            return Channel(school, section=section)
        shift = school.shift(shift_id)
        return Channel(school, shift, section=section) if shift else None

    def cache_dir(self, source_id: str) -> Path:
        """Папка кэша для источника (создаётся при первом обращении)."""
        safe_id = re.sub(r"[^0-9A-Za-z_.-]+", "_", source_id)
        path = self.data_dir / "cache" / safe_id
        path.mkdir(parents=True, exist_ok=True)
        return path


settings = Settings()
