"""Получение списка файлов расписания из источника: папка Google Drive или страница со ссылками.

Дополнительно поддерживается источник «google_sheet» — Google Таблица с расписанием,
опубликованная в виде HTML/CSV (используется школой с разделением на смены).
"""

from __future__ import annotations

import csv
import hashlib
import html as html_lib
import io
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import urljoin

import requests

from .config import Channel

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ScheduleBot/1.0",
}

DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")
GDRIVE_FILE_RE = re.compile(
    r'href="https://drive\.google\.com/file/d/(?P<id>[A-Za-z0-9_-]{10,})/[^"]*"[^>]*>(?P<name>.*?)</a>',
    re.S | re.I,
)
PAGE_LINK_RE = re.compile(r'<a[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<name>.*?)</a>', re.S | re.I)

SUPPORTED_SUFFIXES = (".pdf", ".doc", ".docx", ".xls", ".xlsx")

SHEET_ID_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")
GID_RE = re.compile(r"[#?&]gid=(\d+)")


@dataclass
class SourceFile:
    """Один файл с расписанием в источнике."""

    id: str
    name: str
    url: str
    published: date | None = None


@dataclass
class DayBlock:
    """Один блок расписания на конкретный день."""

    title: str
    lessons: list[tuple[str, str]] = field(default_factory=list)


def parse_date(text: str) -> date | None:
    """Достаёт дату вида 26.10.2026 из имени файла."""
    match = DATE_RE.search(text or "")
    if not match:
        return None
    day, month, year = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def pick_relevant(files: list[SourceFile], today: date | None = None) -> SourceFile | None:
    """Выбирает файл с ближайшим расписанием на будущее, иначе — самый свежий."""
    if not files:
        return None
    today = today or date.today()
    future = sorted(
        (item for item in files if item.published and item.published >= today),
        key=lambda item: item.published,
    )
    if future:
        return future[0]
    return max(files, key=lambda item: item.published or date.min)


def list_files(channel: Channel) -> list[SourceFile]:
    """Возвращает все файлы расписания, опубликованные для школы (или её смены)."""
    if channel.source == "gdrive_folder":
        return _list_gdrive_folder(channel)
    if channel.source == "url_list":
        return _list_page(channel)
    if channel.source == "google_sheet":
        return _list_sheet(channel)
    raise ValueError("Неизвестный тип источника: " + channel.source)


def _list_gdrive_folder(channel: Channel) -> list[SourceFile]:
    folder_id = channel.folder_id
    if not folder_id:
        raise ValueError(
            "В ссылке школы «" + channel.name + "» не найден ID папки Google Drive"
        )
    url = "https://drive.google.com/embeddedfolderview?id=" + folder_id + "#list"
    page = _get(url)

    files: list[SourceFile] = []
    seen: set[str] = set()
    for match in GDRIVE_FILE_RE.finditer(page):
        file_id = match.group("id")
        if file_id in seen:
            continue
        seen.add(file_id)
        name = _clean_text(match.group("name"))
        files.append(
            SourceFile(
                id=file_id,
                name=name,
                url="https://drive.google.com/file/d/" + file_id + "/view",
                published=parse_date(name),
            )
        )
    if not files:
        raise RuntimeError(
            "В папке Google Drive школы «" + channel.name + "» не найдено ни одного файла"
        )
    return files


def _list_page(channel: Channel) -> list[SourceFile]:
    page = _get(channel.url)
    files: list[SourceFile] = []
    seen: set[str] = set()
    for match in PAGE_LINK_RE.finditer(page):
        href = match.group("href")
        if href.startswith("?"):
            continue
        name = html_lib.unescape(_clean_text(match.group("name")))
        link = urljoin(channel.url, href)
        if not link.lower().split("?")[0].endswith(SUPPORTED_SUFFIXES):
            continue
        if link in seen:
            continue
        seen.add(link)
        file_name = name or Path(link.split("?")[0]).name
        files.append(
            SourceFile(
                id=hashlib.sha1(link.encode("utf-8")).hexdigest(),
                name=file_name,
                url=link,
                published=parse_date(file_name),
            )
        )
    if not files:
        raise RuntimeError(
            "На странице школы «" + channel.name + "» не найдено ссылок на файлы расписания"
        )
    return files


def _list_sheet(channel: Channel) -> list[SourceFile]:
    """Для Google Таблицы расписание «меняется», когда меняется её нужная секция."""
    fingerprint = sheet_fingerprint(fetch_sheet_csv(channel.url), channel.section)
    return [
        SourceFile(
            id=fingerprint,
            name=channel.name,
            url=sheet_csv_url(channel.url),
            published=None,
        )
    ]


def download(source_file: SourceFile, dest_dir: Path) -> Path:
    """Скачивает файл в папку dest_dir и возвращает путь к нему."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r'[<>:"/\\|?*]', "_", source_file.name)
    dest = dest_dir / f"{source_file.id[:16]}_{safe_name}"
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    if source_file.url.startswith("https://drive.google.com/file/"):
        return _download_gdrive(source_file.id, dest)
    return _download_url(source_file.url, dest)


def _download_gdrive(file_id: str, dest: Path) -> Path:
    session = requests.Session()
    response = session.get(
        "https://drive.google.com/uc",
        params={"export": "download", "id": file_id},
        headers=HEADERS,
    )
    if response.headers.get("Content-Type", "").startswith("text/html"):
        action = re.search(r'<form[^>]+action="([^"]+)"', response.text)
        inputs = re.findall(
            r'<input type="hidden" name="([^"]+)" value="([^"]*)"', response.text
        )
        if action:
            data = {name: html_lib.unescape(value) for name, value in inputs}
            response = session.get(
                html_lib.unescape(action.group(1)), params=data, headers=HEADERS
            )
    response.raise_for_status()
    _save_stream(response, dest)
    return dest


def _download_url(url: str, dest: Path) -> Path:
    response = requests.get(url, headers=HEADERS, stream=True, timeout=180)
    response.raise_for_status()
    _save_stream(response, dest)
    return dest


def _save_stream(response: requests.Response, dest: Path) -> None:
    with open(dest, "wb") as handle:
        for chunk in response.iter_content(65536):
            if chunk:
                handle.write(chunk)


def _get(url: str) -> str:
    response = requests.get(url, headers=HEADERS, timeout=60)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or "utf-8"
    return response.text


def _clean_text(raw_html: str) -> str:
    text = re.sub(r"<[^>]+>", "", raw_html or "")
    text = text.replace("\u200b", "")
    return html_lib.unescape(text).strip()


# --- Источник «Google Таблица» -------------------------------------------------


def sheet_csv_url(url: str) -> str:
    """Преобразует ссылку на Google Таблицу в ссылку на её CSV-экспорт."""
    match = SHEET_ID_RE.search(url)
    if not match:
        raise ValueError("В ссылке «" + url + "» не найден ID Google Таблицы")
    csv_url = (
        "https://docs.google.com/spreadsheets/d/" + match.group(1) + "/export?format=csv"
    )
    gid = GID_RE.search(url)
    if gid:
        csv_url += "&gid=" + gid.group(1)
    return csv_url


def fetch_sheet_csv(url: str) -> str:
    """Скачивает содержимое Google Таблицы в формате CSV."""
    return _get(sheet_csv_url(url))


def sheet_fingerprint(csv_text: str, section: str | None = None) -> str:
    """Отпечаток содержимого таблицы — для определения, что расписание изменилось.

    Если задана секция, учитывается только она (например, только временные изменения),
    поэтому правки в другой части таблицы не считаются новым расписанием.
    """
    if section:
        _, data = parse_sheet(csv_text, section)
        parts: list[str] = []
        for group, blocks in data.items():
            parts.append(group)
            for block in blocks:
                parts.append(block.title)
                parts.extend(f"{number}={subject}" for number, subject in block.lessons)
        csv_text = "\u0001".join(parts)
    return hashlib.sha1(csv_text.encode("utf-8", "replace")).hexdigest()


def _is_lesson_number(value: str) -> bool:
    return bool(re.match(r"^\d", value or ""))


SECTION_MAIN_RE = re.compile(r"ПОЛУГОД|ОСНОВНОЕ\s+РАСПИСАНИЕ", re.I)
SECTION_TEMP_RE = re.compile(r"ВРЕМЕННЫЕ\s+ИЗМЕНЕНИЯ", re.I)


def parse_sheet(
    csv_text: str, section: str | None = None
) -> tuple[list[str], dict[str, list[DayBlock]]]:
    """Разбирает CSV таблицы в список групп и расписание по дням для каждой группы.

    section="current" оставляет только временные изменения, "main" — расписание
    на полугодие, None — все части таблицы.
    """
    rows = list(csv.reader(io.StringIO(csv_text)))

    groups: list[str] = []
    blocks: list[dict] = []
    title = ""
    columns: dict[int, str] = {}
    lessons: list[tuple[str, dict[int, str]]] = []
    current = "current"

    def flush() -> None:
        if columns and lessons:
            blocks.append(
                {
                    "title": title,
                    "columns": dict(columns),
                    "lessons": list(lessons),
                    "section": current,
                }
            )

    for row in rows:
        cells = [cell.strip() for cell in row]
        first = cells[0] if cells else ""

        if any(cell == "№ урока" for cell in cells):
            flush()
            columns = {}
            lessons = []
            for index, cell in enumerate(cells):
                if index == 0 or not cell:
                    continue
                columns[index] = cell
                if cell not in groups:
                    groups.append(cell)
            continue

        non_empty = [cell for cell in cells if cell]
        if len(non_empty) == 1 and not _is_lesson_number(first):
            flush()
            title = first
            columns = {}
            lessons = []
            if SECTION_MAIN_RE.search(first):
                current = "main"
            elif SECTION_TEMP_RE.search(first):
                current = "current"
            continue

        if not columns or not first:
            continue
        lessons.append(
            (first, {index: cells[index] for index in columns if index < len(cells) and cells[index]})
        )
    flush()

    if section:
        order: list[str] = []
        for block in blocks:
            if block["section"] != section:
                continue
            for group in block["columns"].values():
                if group not in order:
                    order.append(group)
        allowed = set(order)
        groups = [group for group in groups if group in allowed]

    data: dict[str, list[DayBlock]] = {group: [] for group in groups}
    for block in blocks:
        if section and block["section"] != section:
            continue
        for index, group in block["columns"].items():
            group_lessons = [
                (number, values[index]) for number, values in block["lessons"] if index in values
            ]
            if group_lessons:
                data[group].append(DayBlock(block["title"], group_lessons))
    return groups, data
