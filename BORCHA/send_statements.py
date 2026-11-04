"""
Рассылка PDF-стейтментов водителям в их группы из папки Telegram
config.TELEGRAM_FOLDER_NAME (например "RC Accounting"), от вашего личного
Telegram-аккаунта.

Как это работает:
1. Первый запуск запросит код из Telegram (или SMS) - это нормально,
   после этого создастся файл сессии и логиниться больше не придётся.
2. Скрипт спросит, за какую неделю стейтменты.
3. Скрипт возьмёт список групп ИЗ ПАПКИ TELEGRAM_FOLDER_NAME (не по всем чатам),
   попробует сам сопоставить каждый PDF-файл с группой:
   - сначала по сохранённому ранее выбору (driver_group_map.json)
   - потом по имени (точно, потом по словам без учёта порядка)
   - если не вышло - спросит вас: покажет пронумерованный список групп из
     папки, вы вводите номер. Выбор запоминается на будущее.
4. Отправляет, показывает итоговый отчёт.

Формат имени файла ожидается такой:
    driver_settlement_3436_Yunus_Kaya.pdf
              (число может быть любым, дальше - Имя_Фамилия)
"""

import asyncio
import re
import time
from pathlib import Path

from telethon import TelegramClient

import config
import telegram_common as tc


def extract_driver_name(filename: str) -> str | None:
    """
    driver_settlement_3463_Yunus_Kaya.pdf                  -> 'Yunus Kaya'
    driver_settlement_3444_Enes_Kaya__41.pdf                -> 'Enes Kaya'
        (хвостовое число после __ - это внутренний номер, отбрасывается)
    driver_settlement_3469_Ermek___Akilay_OWN__507.pdf      -> 'Ermek - Akilay OWN #507'
        (тройное подчёркивание между именами = ' - ',
         двойное подчёркивание перед числом в конце = ' #')
    """
    stem = Path(filename).stem
    match = re.match(r"driver_settlement_\d+_(.+)$", stem, re.IGNORECASE)
    if not match:
        return None
    raw = match.group(1)

    if "___" in raw:
        part1, rest = raw.split("___", 1)
        rest = re.sub(r"__(\d+)$", r" #\1", rest)
        rest = rest.replace("_", " ")
        part1 = part1.replace("_", " ")
        name = f"{part1} - {rest}"
    else:
        raw_no_number = re.sub(r"_+\d+$", "", raw)
        name = raw_no_number.replace("_", " ")

    return re.sub(r"\s+", " ", name).strip()


def sanitize_filename(text: str) -> str:
    """Убирает символы, недопустимые в именах файлов (Windows/Mac/Linux)."""
    return re.sub(r'[\\/:*?"<>|]', "-", text).strip()


async def main():
    week_text = input(
        "За какую неделю стейтменты? (например: June 21 - 27): "
    ).strip()
    if not week_text:
        print("Неделя не указана, отмена.")
        return

    folder = Path(config.STATEMENTS_FOLDER)
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

    print(f"Ищу папку '{config.TELEGRAM_FOLDER_NAME}'...")
    groups, folder_found, extra_count = await tc.get_combined_groups(client, config.TELEGRAM_FOLDER_NAME)
    if not folder_found:
        print(
            f"Не нашёл папку с названием '{config.TELEGRAM_FOLDER_NAME}' в Telegram - "
            "использую поиск по всем чатам как резерв.\n"
            "Проверьте точное название в Настройки -> Папки чатов и поправьте "
            "TELEGRAM_FOLDER_NAME в config.py, если хотите использовать папку."
        )
    if not groups:
        print("Не нашёл вообще ни одной группы 'Accounting & ...'. Проверьте config.py.")
        await client.disconnect()
        return
    if extra_count:
        print(f"Групп в папке + найдено дополнительно по всем чатам: {len(groups)} (+{extra_count} вне папки)")
    else:
        print(f"Групп в папке: {len(groups)}")

    map_path = Path(config.MANUAL_MAP_FILE)
    manual_map = tc.load_manual_map(map_path)

    sent = []
    not_matched = []
    failed = []

    for pdf_path in pdf_files:
        driver_name = extract_driver_name(pdf_path.name)
        if driver_name is None:
            not_matched.append((pdf_path.name, "не удалось распознать имя в названии файла"))
            continue

        result = tc.resolve_group(driver_name, groups, manual_map, context=pdf_path.name)
        if result is None:
            not_matched.append((pdf_path.name, "пропущено вручную"))
            continue

        title, entity = result

        # Физически переименовываем файл в папке в "Имя Фамилия Неделя.pdf"
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
