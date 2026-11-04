"""
Общие функции для send_statements.py и send_fuel_reports.py:
- получение списка групп из конкретной папки Telegram (например "RC Accounting")
- сопоставление имени водителя с группой (по точному совпадению, потом по словам)
- запоминание ручного выбора в driver_group_map.json, чтобы в следующий раз
  не спрашивать повторно
"""

import json
import re
from pathlib import Path

from telethon.tl.functions.messages import GetDialogFiltersRequest
from telethon.tl.types import Chat, Channel

import config


def normalize(name: str) -> str:
    """'Ermek   Tuleberdiev ' -> 'ermek tuleberdiev' - для сравнения строк целиком."""
    return re.sub(r"\s+", " ", name).strip().lower()


def normalize_tokens(name: str) -> set:
    """'Alpinar  Ercan' -> {'alpinar', 'ercan'} - для сравнения без учёта порядка слов."""
    return set(re.findall(r"[a-zA-Zа-яА-Я]+", name.lower()))


async def get_folder_groups(client, folder_name: str):
    """
    Возвращает список (title, remainder, entity) для всех групп внутри папки
    Telegram с названием folder_name (например "RC Accounting").
    remainder - название группы без префикса GROUP_PREFIX (для сопоставления).

    Возвращает None, если папка с таким названием не найдена вообще.
    """
    result = await client(GetDialogFiltersRequest())
    filters = getattr(result, "filters", result)

    target = None
    for f in filters:
        title_obj = getattr(f, "title", None)
        if title_obj is None:
            continue
        title_text = getattr(title_obj, "text", None)
        if title_text is None:
            title_text = str(title_obj)
        if title_text.strip().lower() == folder_name.strip().lower():
            target = f
            break

    if target is None:
        return None

    groups = []
    for peer in getattr(target, "include_peers", []):
        try:
            entity = await client.get_entity(peer)
        except Exception:
            continue
        if not isinstance(entity, (Chat, Channel)):
            continue
        title = getattr(entity, "title", "") or ""
        remainder = title
        if title.startswith(config.GROUP_PREFIX):
            remainder = title[len(config.GROUP_PREFIX):].strip()
        groups.append((title, remainder, entity))

    return groups


async def get_all_prefixed_groups(client):
    """
    Резервный поиск: все группы по ВСЕМ вашим чатам, чьё название начинается
    с config.GROUP_PREFIX. Используется как подстраховка для групп, которые
    ещё не добавлены в папку TELEGRAM_FOLDER_NAME.
    """
    groups = []
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if not isinstance(entity, (Chat, Channel)):
            continue
        title = dialog.name or ""
        if title.startswith(config.GROUP_PREFIX):
            remainder = title[len(config.GROUP_PREFIX):].strip()
            groups.append((title, remainder, entity))
    return groups


async def get_combined_groups(client, folder_name: str):
    """
    Собирает список групп в два прохода:
    1. Группы из папки Telegram folder_name - основной, приоритетный список
    2. Группы по всем чатам (по префиксу) - подстраховка для тех, что ещё
       не добавлены в папку, добавляются в конец списка без дублей

    Возвращает (groups, folder_found: bool).
    Если папка не найдена вообще - folder_found=False, но groups всё равно
    заполнится через резервный поиск.
    """
    folder_groups = await get_folder_groups(client, folder_name)
    global_groups = await get_all_prefixed_groups(client)

    combined = []
    seen_ids = set()

    for t, r, e in (folder_groups or []):
        eid = getattr(e, "id", None)
        if eid in seen_ids:
            continue
        seen_ids.add(eid)
        combined.append((t, r, e))

    extra_count = 0
    for t, r, e in global_groups:
        eid = getattr(e, "id", None)
        if eid in seen_ids:
            continue
        seen_ids.add(eid)
        combined.append((t, r, e))
        extra_count += 1

    return combined, (folder_groups is not None), extra_count


def load_manual_map(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_manual_map(path: Path, mapping: dict):
    path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")


def find_group_by_title(groups, title: str):
    for t, remainder, entity in groups:
        if t == title:
            return (t, entity)
    return None


def auto_match(driver_name: str, groups):
    """
    Пытается сопоставить водителя с группой:
    1. Точное совпадение (без учёта регистра/пробелов)
    2. Совпадение по набору слов, без учёта порядка

    Возвращает:
    - (title, entity) - однозначное совпадение
    - None - совпадений нет
    - list[(title, entity), ...] - несколько кандидатов, неоднозначно
    """
    key = normalize(driver_name)
    exact = [(t, e) for t, r, e in groups if normalize(r) == key]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return exact

    driver_tokens = normalize_tokens(driver_name)
    candidates = []
    for t, r, e in groups:
        group_tokens = normalize_tokens(r)
        if not driver_tokens or not group_tokens:
            continue
        if driver_tokens <= group_tokens or group_tokens <= driver_tokens:
            candidates.append((t, e))

    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) == 0:
        return None
    return candidates


def prompt_manual_choice(driver_name: str, groups, context: str = ""):
    """
    Показывает пронумерованный список всех групп из папки и просит выбрать.
    Возвращает (title, entity) или None, если пользователь решил пропустить.
    """
    print(f"\nНе нашёл группу для: {driver_name}" + (f"  ({context})" if context else ""))
    print("Доступные группы в папке:")
    for i, (t, r, e) in enumerate(groups, start=1):
        print(f"  {i}. {t}")
    print("  0. Пропустить (не отправлять)")

    while True:
        choice = input("Введите номер группы: ").strip()
        if choice in ("0", ""):
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(groups):
            t, r, e = groups[int(choice) - 1]
            return (t, e)
        print("Некорректный ввод, попробуйте снова.")


def resolve_group(driver_name: str, groups: list, manual_map: dict, context: str = ""):
    """
    Полный цикл определения группы для водителя:
    1. Смотрим в сохранённом сопоставлении (driver_group_map.json)
    2. Если там нет - пробуем автоматическое сопоставление
    3. Если не вышло - спрашиваем пользователя вручную

    В любом случае, если группа определена, запоминаем выбор в manual_map
    (сам manual_map нужно сохранить на диск после всех вызовов).

    Возвращает (title, entity) или None, если пропущено.
    """
    key = normalize(driver_name)

    if key in manual_map:
        found = find_group_by_title(groups, manual_map[key])
        if found:
            return found
        # сохранённая группа больше не существует (переименована/удалена) -
        # пробуем определить заново

    match = auto_match(driver_name, groups)

    if isinstance(match, tuple):
        manual_map[key] = match[0]
        return match

    # match is None (нет совпадений) или list (неоднозначно) - спрашиваем сами
    chosen = prompt_manual_choice(driver_name, groups, context)
    if chosen is None:
        return None

    manual_map[key] = chosen[0]
    return chosen
