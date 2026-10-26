"""Нарезка большого PDF с расписанием на отдельную картинку для каждой группы.

Расписание в PDF — это таблица-сетка: по вертикали идут блоки по 5 групп,
каждая группа занимает свою колонку. Модуль находит колонку нужной группы
и вырезает полосу «Время + расписание группы», склеивая такие полосы в одно фото.

Здесь же рендерится картинка расписания для школ, у которых источник — Google Таблица.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pymupdf
from PIL import Image, ImageChops, ImageDraw, ImageFont

DPI = 200
GAP_BETWEEN_COLUMNS = 8
GAP_BETWEEN_BLOCKS = 14
EDGE_INSET = 1.0

GROUP_NAME_RE = re.compile(r"^([А-ЯЁA-Z]{1,8})\s*[-–—\s]*(\d{1,3})\s*-?$")

FONT_CANDIDATES_BOLD = (
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)
FONT_CANDIDATES_REGULAR = (
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def normalize_group_name(raw: str) -> str | None:
    """Приводит «ДПА 24-» к виду «ДПА-24». Возвращает None, если это не группа."""
    match = GROUP_NAME_RE.match((raw or "").strip())
    if not match:
        return None
    return "-".join([match.group(1), match.group(2)])


def safe_file_name(name: str) -> str:
    """Приводит имя группы к безопасному для файловой системы виду."""
    return re.sub(r'[<>:"/\\|?*]', "_", name)


def split_pdf_images(
    pdf_path: Path, out_dir: Path, title_for: Callable[[str], str] | None = None
) -> dict[str, Path]:
    """Один проход по PDF: режет расписание на готовую картинку для каждой группы.

    Табличная сетка документа разбирается один раз и сразу для всех групп, поэтому
    нарезка идёт в разы быстрее, чем отрисовка каждой группы по отдельности.
    Возвращает {группа: путь к PNG}, порядок — как в списке групп школы.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("*.png"):
        stale.unlink()

    strips_by_group: dict[str, list[Image.Image]] = {}
    document = pymupdf.open(str(pdf_path))
    try:
        for page in document:
            for table in page.find_tables().tables:
                for group, strips in _collect_strips(page, table).items():
                    strips_by_group.setdefault(group, []).extend(strips)
    finally:
        document.close()

    images: dict[str, Path] = {}
    for group in sorted(strips_by_group, key=_group_sort_key):
        strips = strips_by_group[group]
        width = max(image.width for image in strips)
        strips = [_fit_width(image, width) for image in strips]
        height = sum(image.height for image in strips) + GAP_BETWEEN_BLOCKS * (
            len(strips) - 1
        )
        result = Image.new("RGB", (width, height), "white")
        y = 0
        for image in strips:
            result.paste(image, (0, y))
            y += image.height + GAP_BETWEEN_BLOCKS
        result = _trim_white(result)
        if title_for is not None:
            result = _add_title(result, title_for(group))
        path = out_dir / f"{safe_file_name(group)}.png"
        result.save(path, format="PNG", compress_level=6)
        images[group] = path
    return images


def render_group_image(pdf_path: Path, group: str, title: str = "") -> Image.Image | None:
    """Собирает картинку с расписанием одной группы. None — если группа не найдена."""
    document = pymupdf.open(str(pdf_path))
    strips: list[Image.Image] = []
    try:
        for page in document:
            for table in page.find_tables().tables:
                for found in _collect_strips(page, table, {group}).values():
                    strips.extend(found)
    finally:
        document.close()

    if not strips:
        return None

    width = max(image.width for image in strips)
    strips = [_fit_width(image, width) for image in strips]
    height = sum(image.height for image in strips) + GAP_BETWEEN_BLOCKS * (len(strips) - 1)
    result = Image.new("RGB", (width, height), "white")
    y = 0
    for image in strips:
        result.paste(image, (0, y))
        y += image.height + GAP_BETWEEN_BLOCKS

    result = _trim_white(result)
    if title:
        result = _add_title(result, title)
    return result


def _collect_strips(
    page: "pymupdf.Page", table, wanted: set[str] | None = None
) -> dict[str, list[Image.Image]]:
    """Полосы «время + группа» из одной таблицы.

    Сетка и текст таблицы разбираются по одному разу на таблицу: раньше это
    повторялось для каждой строки-заголовка и заметно замедляло нарезку.
    wanted=None — берутся все группы, иначе только перечисленные.
    """
    grid = table.extract()
    row_objects = table.rows
    headers = _header_row_indexes(grid)
    collected: dict[str, list[Image.Image]] = {}

    for position, row_index in enumerate(headers):
        columns = _group_columns(grid, row_objects, row_index)
        if wanted is not None:
            columns = [(name, bounds) for name, bounds in columns if name in wanted]
        if not columns:
            continue
        end = headers[position + 1] if position + 1 < len(headers) else len(row_objects)
        block = row_objects[row_index:end]
        if not block:
            continue
        y0 = min(row.bbox[1] for row in block) - EDGE_INSET
        y1 = max(row.bbox[3] for row in block) + EDGE_INSET
        # Слева от расписания группы берём служебные колонки: № пары, № урока и «Время»;
        # они одинаковы для всех групп блока, поэтому рисуются один раз.
        column_left = min(left for _, (left, _) in columns)
        first_cell = block[0].cells[0] or (column_left - EDGE_INSET, y0, column_left, y1)
        time_strip = _render(
            page, pymupdf.Rect(first_cell[0] - EDGE_INSET, y0, column_left, y1)
        )
        for group, (x0, x1) in columns:
            group_rect = pymupdf.Rect(x0, y0, x1, y1)
            if not page.get_text("text", clip=group_rect).strip():
                continue
            collected.setdefault(group, []).append(
                _hstack([time_strip, _render(page, group_rect)], GAP_BETWEEN_COLUMNS)
            )
    return collected


def _header_row_indexes(grid) -> list[int]:
    """Номера строк таблицы, в которых перечислены группы (строка «Группа:»)."""
    return [
        index
        for index, row in enumerate(grid)
        if any((cell or "").strip().startswith("Группа") for cell in row)
    ]


def _group_columns(grid, row_objects, row_index: int) -> list[tuple[str, tuple[float, float]]]:
    """[(название группы, (x0, x1)), ...] из строки-заголовка блока."""
    columns: list[tuple[str, tuple[float, float]]] = []
    cells = row_objects[row_index].cells
    for index, text in enumerate(grid[row_index]):
        name = normalize_group_name(text)
        if not name or index >= len(cells) or not cells[index]:
            continue
        cell = cells[index]
        columns.append((name, (cell[0], cell[2])))
    return columns


def _render(page: "pymupdf.Page", rect: "pymupdf.Rect") -> Image.Image:
    zoom = DPI / 72
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=rect)
    return Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)


def _hstack(images: list[Image.Image], gap: int) -> Image.Image:
    width = sum(image.width for image in images) + gap * (len(images) - 1)
    height = max(image.height for image in images)
    result = Image.new("RGB", (width, height), "white")
    x = 0
    for image in images:
        result.paste(image, (x, 0))
        x += image.width + gap
    return result


def _fit_width(image: Image.Image, width: int) -> Image.Image:
    if image.width == width:
        return image
    if image.width > width:
        ratio = width / image.width
        return image.resize((width, max(1, int(image.height * ratio))), Image.LANCZOS)
    result = Image.new("RGB", (width, image.height), "white")
    result.paste(image, ((width - image.width) // 2, 0))
    return result


def _trim_white(image: Image.Image, margin: int = 0) -> Image.Image:
    """Обрезает белые поля вокруг содержимого."""
    background = Image.new("RGB", image.size, "white")
    bbox = ImageChops.difference(image, background).getbbox()
    if not bbox:
        return image
    box = (
        max(bbox[0] - margin, 0),
        max(bbox[1] - margin, 0),
        min(bbox[2] + margin, image.width),
        min(bbox[3] + margin, image.height),
    )
    return image.crop(box)


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for path in (FONT_CANDIDATES_BOLD if bold else FONT_CANDIDATES_REGULAR):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _text_width(font, text: str) -> float:
    return font.getlength(text)


def _add_title(image: Image.Image, title: str) -> Image.Image:
    """Добавляет сверху подпись. Первая строка — заголовок, остальные — уточнения."""
    lines = [line.strip() for line in (title or "").split("\n") if line.strip()]
    if not lines:
        return image

    scale = DPI / 150
    title_font = _load_font(int(22 * scale), bold=True)
    sub_font = _load_font(int(17 * scale))
    title_height = int(30 * scale)
    sub_height = int(24 * scale)
    padding = int(12 * scale)

    widths = [_text_width(title_font, lines[0])]
    widths += [_text_width(sub_font, line) for line in lines[1:]]
    header_width = int(max(widths)) + padding * 2
    header_height = title_height + sub_height * (len(lines) - 1) + padding * 2

    result = Image.new(
        "RGB", (max(image.width, header_width), image.height + header_height), "white"
    )
    result.paste(image, (0, header_height))

    draw = ImageDraw.Draw(result)
    draw.text(
        ((result.width - widths[0]) / 2, padding), lines[0], font=title_font, fill="black"
    )
    y = padding + title_height
    for line, width in zip(lines[1:], widths[1:]):
        draw.text(((result.width - width) / 2, y), line, font=sub_font, fill="black")
        y += sub_height
    return result


def _group_sort_key(name: str):
    letters, _, number = name.partition("-")
    return (letters, int(number) if number.isdigit() else 0)


def _lesson_time(bells: dict | None, sat_bells: dict | None, day_title: str, number: str) -> str:
    """Возвращает время звонка для номера урока («7 (0)» → по цифре 7)."""
    target = bells
    if sat_bells and "СУББОТА" in (day_title or "").upper():
        target = sat_bells
    if not target:
        return ""
    match = re.match(r"\s*(\d+)", number or "")
    if not match:
        return ""
    return target.get(match.group(1), "")


def render_sheet_image(
    blocks, title: str = "", bells: dict | None = None, sat_bells: dict | None = None
) -> Image.Image | None:
    """Рисует картинку расписания одной группы по разобранным блокам Google Таблицы.

    bells/sat_bells — время звонков, выводится отдельной левой колонкой.
    """
    scale = DPI / 150
    title_font = _load_font(int(22 * scale), bold=True)
    day_font = _load_font(int(18 * scale), bold=True)
    text_font = _load_font(int(16 * scale))
    time_font = _load_font(int(14 * scale))
    header_font = _load_font(int(15 * scale), bold=True)

    padding = int(20 * scale)
    row_height = int(36 * scale)
    day_row_height = int(44 * scale)
    section_gap = int(12 * scale)

    # Определяем ширину колонок
    col_num_w = int(45 * scale)
    col_time_w = int(120 * scale)
    col_subject_min_w = int(350 * scale)

    # Собираем данные для отрисовки
    items = []
    max_subject_w = col_subject_min_w

    for block in blocks:
        items.append({"type": "day", "text": block.title or "Расписание"})
        items.append({"type": "header", "num": "№", "time": "Время", "subject": "Предмет"})
        for number, subject in block.lessons:
            text = (subject or "").strip()
            if text in ("", "-------"):
                text = "—"
            time_str = _lesson_time(bells, sat_bells, block.title, number)
            items.append({"type": "lesson", "num": number, "time": time_str, "subject": text})
            # Оцениваем ширину текста предмета
            max_subject_w = max(max_subject_w, int(_text_width(text_font, text) + 20 * scale))

    if not items:
        return None

    # Итоговая ширина
    width = col_num_w + col_time_w + max_subject_w + padding * 2
    
    # Считаем высоту
    total_height = padding * 2
    if title:
        total_height += int(60 * scale) # Место под заголовок (добавится позже функцией _add_title)
    
    current_y = padding
    for item in items:
        if item["type"] == "day":
            total_height += day_row_height + section_gap
        else:
            total_height += row_height

    image = Image.new("RGB", (width, total_height), "white")
    draw = ImageDraw.Draw(image)
    
    y = padding
    for item in items:
        if item["type"] == "day":
            y += section_gap // 2
            # Отрисовка дня недели
            draw.rectangle([padding, y, width - padding, y + day_row_height], fill="#f0f0f0")
            draw.text(
                (padding + 10 * scale, y + (day_row_height - day_font.size) // 2 - 2),
                item["text"], font=day_font, fill="black"
            )
            y += day_row_height + section_gap // 2
        elif item["type"] == "header":
            # Заголовок таблицы
            draw.rectangle([padding, y, width - padding, y + row_height], outline="black", width=2)
            # Линии колонок
            draw.line([padding + col_num_w, y, padding + col_num_w, y + row_height], fill="black", width=2)
            draw.line([padding + col_num_w + col_time_w, y, padding + col_num_w + col_time_w, y + row_height], fill="black", width=2)
            
            draw.text((padding + 10 * scale, y + 8 * scale), item["num"], font=header_font, fill="black")
            draw.text((padding + col_num_w + 10 * scale, y + 8 * scale), item["time"], font=header_font, fill="black")
            draw.text((padding + col_num_w + col_time_w + 10 * scale, y + 8 * scale), item["subject"], font=header_font, fill="black")
            y += row_height
        else:
            # Строка урока
            draw.rectangle([padding, y, width - padding, y + row_height], outline="black", width=1)
            draw.line([padding + col_num_w, y, padding + col_num_w, y + row_height], fill="black", width=1)
            draw.line([padding + col_num_w + col_time_w, y, padding + col_num_w + col_time_w, y + row_height], fill="black", width=1)
            
            draw.text((padding + 15 * scale, y + 8 * scale), item["num"], font=text_font, fill="black")
            draw.text((padding + col_num_w + 10 * scale, y + 9 * scale), item["time"], font=time_font, fill="#444444")
            draw.text((padding + col_num_w + col_time_w + 10 * scale, y + 8 * scale), item["subject"], font=text_font, fill="black")
            y += row_height

    return _trim_white(image, int(10 * scale))
