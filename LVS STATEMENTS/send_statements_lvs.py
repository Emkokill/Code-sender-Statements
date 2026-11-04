"""
Рассылка PDF-стейтментов LVS (DataTruck) водителям в их группы Telegram.

Отличия от send_statements.py (RC):
- Формат имени файла:  ST-001644_NECATI_ABI_None.pdf
    (номер стейтмента спереди, имя водителя в середине - 2 ИЛИ 3 слова,
     в конце Unit Number: "None" или число)
- Названия групп: имя стоит ВПЕРЕДИ, служебные слова сзади, и пишутся
  по-разному:
      NECATI ABI Accounting LVS
      Jean Sony Joseph ACCOUNTING
      JARRETT NICORDO Accounting IVS   (опечатка в названии группы)
      MUSTAFA TEAM ACCOUNTING
  Поэтому при сопоставлении служебные слова (accounting/lvs/Seivs/team)
  отбрасываются, а сравнение идёт по словам имени.

Всё остальное работает так же, как в send_statements.py:
- поиск групп сначала в папке Telegram, потом резервно по всем чатам
- если не удалось сопоставить - показывает пронумерованный список групп
  и запоминает ваш выбор на будущее (в своём отдельном файле карты)
- переименовывает файл в "Имя Фамилия Неделя.pdf" и отправляет под этим именем
"""

import asyncio
import re
import time
from pathlib import Path

from telethon import TelegramClient
from telethon.tl.types import Chat, Channel

import config
import telegram_common as tc

# Служебные слова в названиях групп, которые не являются частью имени водителя
GROUP_STOPWORDS = {"accounting", "acc", "lvs", "ivs", "team", "manager"}


def extract_driver_name(filename: str) -> str | None:
    """
    ST-001644_NECATI_ABI_None.pdf            -> 'NECATI ABI'
    ST-001650_FELIX_GABRIEL_OSORIO_None.pdf  -> 'FELIX GABRIEL OSORIO'
    ST-001651_Jean_Sony_Joseph_412.pdf       -> 'Jean Sony Joseph'
    (последний элемент - Unit Number, отбрасывается: "None" или число)
    """
    stem = Path(filename).stem
    match = re.match(r"ST-?\d+_(.+)$", stem, re.IGNORECASE)
    if not match:
        return None

    raw = match.group(1)
    parts = [p for p in raw.split("_") if p.strip()]

    if parts and (parts[-1].lower() == "none" or re.fullmatch(r"\d+", parts[-1])):
        parts = parts[:-1]

    if not parts:
        return None

    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def name_tokens(text: str) -> set:
    """Слова имени без служебных: 'NECATI ABI Accounting LVS' -> {'necati','abi'}"""
    words = re.findall(r"[a-zA-Zа-яА-Я]+", text.lower())
    return {w for w in words if w not in GROUP_STOPWORDS}


def sanitize_filename(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "-", text).strip()


async def get_lvs_groups(client, folder_name: str):
    """
    Собирает группы в два прохода (как в RC-скрипте):
    1. Все чаты из папки folder_name
    2. Резервно - все чаты, в названии которых есть слово "accounting"

    Возвращает (groups, folder_found, extra_count), где groups - список
    (title, name_part, entity). name_part - название без служебных слов.
    """
    folder_groups = await tc.get_folder_groups(client, folder_name)

    combined = []
    seen_ids = set()

    for title, _remainder, entity in (folder_groups or []):
        eid = getattr(entity, "id", None)
        if eid in seen_ids:
            continue
        seen_ids.add(eid)
        combined.append((title, title, entity))

    extra_count = 0
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if not isinstance(entity, (Chat, Channel)):
            continue
        title = dialog.name or ""
        if "accounting" not in title.lower():
            continue
        eid = getattr(entity, "id", None)
        if eid in seen_ids:
            continue
        seen_ids.add(eid)
        combined.append((title, title, entity))
        extra_count += 1

    return combined, (folder_groups is not None), extra_count


def auto_match(driver_name: str, groups: list):
    """
    Сопоставление по словам имени (служебные слова уже отброшены).
    Возвращает (title, entity), None или список кандидатов.
    """
    driver = name_tokens(driver_name)
    if not driver:
        return None

    exact, subset = [], []
    for title, name_part, entity in groups:
        group = name_tokens(name_part)
        if not group:
            continue
        if group == driver:
            exact.append((title, entity))
        elif driver <= group or group <= driver:
            subset.append((title, entity))

    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return exact
    if len(subset) == 1:
        return subset[0]
    if len(subset) == 0:
        return None
    return subset


def resolve_group(driver_name: str, groups: list, manual_map: dict, context: str = ""):
    """Сохранённый выбор -> автоматическое сопоставление -> вопрос пользователю."""
    key = tc.normalize(driver_name)

    if key in manual_map:
        for title, _name_part, entity in groups:
            if title == manual_map[key]:
                return (title, entity)
        # сохранённая группа исчезла/переименована - определяем заново

    match = auto_match(driver_name, groups)
    if isinstance(match, tuple):
        manual_map[key] = match[0]
        return match

    chosen = tc.prompt_manual_choice(driver_name, groups, context)
    if chosen is None:
        return None

    manual_map[key] = chosen[0]
    return chosen


async def main():
    week_text = input(
        "За какую неделю стейтменты? (например: Sep 06 - 14): "
    ).strip()
    if not week_text:
        print("Неделя не указана, отмена.")
        return

    folder = Path(config.LVS_STATEMENTS_FOLDER)
    if not folder.exists():
        print(f"Папка не найдена: {folder.resolve()}")
        return

    pdf_files = sorted(folder.glob("*.pdf"))
    if not pdf_files:
        print(f"В папке {folder.resolve()} нет PDF-файлов.")
        return

    print(f"Найдено PDF-файлов: {len(pdf_files)}")
    print("Подключаюсь к Telegram...")

    client = TelegramClient(config.SESSION_NAME, config.API_ID, config.API_HASH)
    await client.start(phone=config.PHONE)

    print(f"Ищу папку '{config.LVS_FOLDER_NAME}'...")
    groups, folder_found, extra_count = await get_lvs_groups(client, config.LVS_FOLDER_NAME)

    if not folder_found:
        print(
            f"Папку '{config.LVS_FOLDER_NAME}' не нашёл - использую поиск по всем чатам.\n"
            "Проверьте название в Настройки -> Папки чатов и поправьте "
            "LVS_FOLDER_NAME в config.py, если хотите использовать папку."
        )
    if not groups:
        print("Не нашёл ни одной группы со словом 'Accounting'. Проверьте config.py.")
        await client.disconnect()
        return

    if extra_count:
        print(f"Групп всего: {len(groups)} (+{extra_count} найдено вне папки)")
    else:
        print(f"Групп в папке: {len(groups)}")

    map_path = Path(config.LVS_MAP_FILE)
    manual_map = tc.load_manual_map(map_path)

    sent, not_matched, failed = [], [], []

    for pdf_path in pdf_files:
        driver_name = extract_driver_name(pdf_path.name)
        if driver_name is None:
            not_matched.append((pdf_path.name, "не удалось распознать имя в названии файла"))
            continue

        result = resolve_group(driver_name, groups, manual_map, context=pdf_path.name)
        if result is None:
            not_matched.append((pdf_path.name, "пропущено вручную"))
            continue

        title, entity = result

        new_stem = sanitize_filename(f"{driver_name} {week_text}")
        new_path = pdf_path.with_name(f"{new_stem}.pdf")

        counter = 1
        while new_path.exists() and new_path != pdf_path:
            new_path = pdf_path.with_name(f"{new_stem} ({counter}).pdf")
            counter += 1

        pdf_path.rename(new_path)

        print(f"Отправляю: {new_path.name} -> {title}")
        try:
            await client.send_file(entity, str(new_path))
        except Exception:
            try:
                fresh_entity = await client.get_entity(entity)
                await client.send_file(fresh_entity, str(new_path))
            except Exception as second_error:
                print(f"  ✗ Не удалось отправить: {second_error}")
                failed.append((new_path.name, title, str(second_error)))
                time.sleep(config.DELAY_BETWEEN_SENDS)
                continue

        sent.append((new_path.name, title))
        time.sleep(config.DELAY_BETWEEN_SENDS)

    tc.save_manual_map(map_path, manual_map)
    await client.disconnect()

    print("\n===== ОТЧЁТ =====")
    print(f"Отправлено: {len(sent)}")
    for fname, title in sent:
        print(f"  ✓ {fname} -> {title}")

    if failed:
        print(f"\nОшибка при отправке (файл уже переименован, отправьте вручную): {len(failed)}")
        for fname, title, error in failed:
            print(f"  ✗ {fname} -> {title} — {error}")

    if not_matched:
        print(f"\nНе отправлено: {len(not_matched)}")
        for fname, reason in not_matched:
            print(f"  ✗ {fname} — {reason}")

    if not failed and not not_matched:
        print("\nВсе файлы отправлены успешно.")


if __name__ == "__main__":
    asyncio.run(main())
