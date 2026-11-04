"""
Рассылка отчётов по дизелю водителям-владельцам (жёлтая заливка в Excel).

Как пользоваться:
1. Положите недельный Excel-файл (например Fuel_JULY_26_-_AUG_01.xlsx)
   в папку FUEL_FOLDER (см. config.py). В папке должен быть ровно один .xlsx файл.
2. Запустите:  python send_fuel_reports.py
3. Скрипт сам:
   - возьмёт период из названия файла (Fuel_JULY_26_-_AUG_01.xlsx -> "JULY 26 - AUG 01")
   - прочитает первый лист, игнорируя лист "Summary"
   - возьмёт только строки, где ячейка с именем водителя закрашена жёлтым
   - сгруппирует их по водителю
   - для каждого водителя нарисует табличку (как скриншот) и отправит
     в его группу "Accounting & ..."
   - покажет отчёт: кому отправлено, кого не нашёл, где сомнения (сопоставление
     по словам - если совпало больше одной группы, лучше сверить вручную)
"""

import asyncio
import re
import time
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import openpyxl
from telethon import TelegramClient

import config
import telegram_common as tc

# Жёлтый цвет (без альфа-канала) - только он означает "отправить"
YELLOW_HEX = "FFFF00"


def get_cell_color_hex(cell) -> str | None:
    """
    Возвращает цвет заливки ячейки как 6-значный HEX (без альфа-канала),
    или None, если заливки нет / цвет не удалось определить однозначно
    (например, заливка через тему, а не конкретный цвет).
    """
    fill = cell.fill
    if fill is None or fill.patternType != "solid":
        return None

    color = fill.fgColor
    if color is None:
        return None

    if color.type == "rgb" and isinstance(color.rgb, str) and len(color.rgb) >= 6:
        return color.rgb[-6:].upper()

    if color.type == "indexed":
        try:
            from openpyxl.styles.colors import COLOR_INDEX
            rgb = COLOR_INDEX[color.indexed]
            if isinstance(rgb, str) and len(rgb) >= 6:
                return rgb[-6:].upper()
        except Exception:
            return None

    # theme-цвет или что-то нераспознанное - не считаем жёлтым
    return None


def is_send_row(cell) -> bool:
    """True только если цвет заливки ячейки - точно жёлтый. Всё остальное игнорируем."""
    return get_cell_color_hex(cell) == YELLOW_HEX

# Какие колонки показывать в табличке и в каком порядке
# (ключ - как называется колонка в Excel, значение - как подписать в табличке)
DISPLAY_COLUMNS = {
    "Date": "Date",
    "Location": "Location",
    "State": "State",
    "City": "City",
    "Product": "Product",
    "Gals": "Gals",
    "Retail price": "Price",
    "Discount": "Discount",
    "Gross price": "Total",
}
MONEY_COLUMNS = {"Retail price", "Discount", "Gross price"}


def parse_period_from_filename(path: Path) -> str:
    """Fuel_JULY_26_-_AUG_01.xlsx -> 'JULY 26 - AUG 01'"""
    stem = path.stem
    stem = re.sub(r"^Fuel[_\s]*", "", stem, flags=re.IGNORECASE)
    return stem.replace("_", " ").strip()


def normalize_tokens(name: str) -> set:
    """'Alpinar  Ercan' -> {'alpinar', 'ercan'} - без учёта порядка слов и регистра."""
    return set(re.findall(r"[a-zA-Zа-яА-Я]+", name.lower()))


def read_fuel_rows(xlsx_path: Path):
    """
    Читает первый лист, который не называется 'Summary', и возвращает
    список строк (только с жёлтой заливкой в колонке Driver), сгруппированных
    по имени водителя: {driver_name: [row_dict, ...]}
    """
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)

    sheet_name = None
    for name in wb.sheetnames:
        if name.strip().lower() != "summary":
            sheet_name = name
            break
    if sheet_name is None:
        raise RuntimeError("Не нашёл лист с данными (все листы называются 'Summary'?)")

    ws = wb[sheet_name]

    headers = {}
    for col in range(1, ws.max_column + 1):
        val = ws.cell(row=1, column=col).value
        if val:
            headers[str(val).strip()] = col

    if "Driver" not in headers:
        raise RuntimeError(f"На листе '{sheet_name}' нет колонки 'Driver'")

    driver_col = headers["Driver"]
    grouped = {}
    color_stats = {"yellow": 0, "other": 0}

    for row in range(2, ws.max_row + 1):
        driver_cell = ws.cell(row=row, column=driver_col)
        driver_raw = driver_cell.value
        if not driver_raw or not str(driver_raw).strip():
            continue

        if not is_send_row(driver_cell):
            color_stats["other"] += 1
            continue  # цвет не жёлтый (или не распознан) - пропускаем без исключений

        color_stats["yellow"] += 1
        driver_name = re.sub(r"\s+", " ", str(driver_raw)).strip()

        row_data = {}
        for col_name in DISPLAY_COLUMNS:
            col_idx = headers.get(col_name)
            row_data[col_name] = ws.cell(row=row, column=col_idx).value if col_idx else None

        grouped.setdefault(driver_name, []).append(row_data)

    print(f"Строк с жёлтой заливкой: {color_stats['yellow']}, пропущено (не жёлтые): {color_stats['other']}")
    return grouped


def format_date(value) -> str:
    if isinstance(value, datetime):
        return value.strftime("%m/%d")
    if value is None:
        return ""
    text = str(value).strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).strftime("%m/%d")
        except ValueError:
            continue
    return text


def format_cell(col_name: str, value) -> str:
    if col_name == "Date":
        return format_date(value)
    if col_name in MONEY_COLUMNS:
        try:
            return f"${float(value):.2f}"
        except (TypeError, ValueError):
            return ""
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def render_report_image(driver_name: str, rows: list, period_text: str, out_path: Path):
    columns = list(DISPLAY_COLUMNS.keys())
    table_data = [[format_cell(c, r.get(c)) for c in columns] for r in rows]

    total_gals = sum(float(r["Gals"]) for r in rows if r.get("Gals") is not None)
    total_discount = sum(float(r["Discount"]) for r in rows if r.get("Discount") is not None)
    total_gross = sum(float(r["Gross price"]) for r in rows if r.get("Gross price") is not None)

    total_row = ["" for _ in columns]
    total_row[0] = "TOTAL"
    if "Gals" in columns:
        total_row[columns.index("Gals")] = f"{total_gals:.2f}"
    if "Discount" in columns:
        total_row[columns.index("Discount")] = f"${total_discount:.2f}"
    if "Gross price" in columns:
        total_row[columns.index("Gross price")] = f"${total_gross:.2f}"
    table_data.append(total_row)

    col_labels = [DISPLAY_COLUMNS[c] for c in columns]

    # Ширина колонки - под самый длинный текст в ней (включая заголовок)
    col_widths = []
    for i, label in enumerate(col_labels):
        max_len = max([len(label)] + [len(row[i]) for row in table_data])
        col_widths.append(max(max_len, 4))
    total_width = sum(col_widths)
    col_fracs = [w / total_width for w in col_widths]

    n_rows = len(table_data) + 1
    fig_width = max(11, total_width * 0.13)
    fig_height = 0.42 * n_rows + 0.8
    fig, ax = plt.subplots(figsize=(fig_width, fig_height))
    ax.axis("off")

    ax.set_title(f"{driver_name} — Fuel {period_text}", fontsize=14, fontweight="bold", pad=10)

    tbl = ax.table(
        cellText=table_data,
        colLabels=col_labels,
        colWidths=col_fracs,
        loc="center",
        cellLoc="center",
        bbox=[0, 0, 1, 0.92],
    )
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(10)

    for (r, c), cell in tbl.get_celld().items():
        cell.set_height(0.12)
        if r == 0:
            cell.set_facecolor("#2c3e50")
            cell.set_text_props(color="white", fontweight="bold")
        elif r == len(table_data):
            cell.set_facecolor("#f1c40f")
            cell.set_text_props(fontweight="bold")
        elif r % 2 == 0:
            cell.set_facecolor("#f5f5f5")

    fig.savefig(out_path, dpi=200, bbox_inches="tight", pad_inches=0.15)
    plt.close(fig)


async def main():
    folder = Path(config.FUEL_FOLDER)
    if not folder.exists():
        print(f"Папка не найдена: {folder.resolve()}")
        return

    xlsx_files = sorted(folder.glob("*.xlsx"))
    if len(xlsx_files) == 0:
        print(f"В папке {folder.resolve()} нет .xlsx файла.")
        return
    if len(xlsx_files) > 1:
        print("В папке больше одного .xlsx файла, оставьте только один:")
        for f in xlsx_files:
            print(f"  - {f.name}")
        return

    xlsx_path = xlsx_files[0]
    period_text = parse_period_from_filename(xlsx_path)
    print(f"Файл: {xlsx_path.name}")
    print(f"Период: {period_text}")

    grouped = read_fuel_rows(xlsx_path)
    print(f"Водителей с жёлтой заливкой: {len(grouped)}")

    if not grouped:
        print("Нет строк для отправки (никто не закрашен жёлтым).")
        return

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

    tmp_dir = Path("./_fuel_reports_tmp")
    tmp_dir.mkdir(exist_ok=True)

    sent, not_matched, failed = [], [], []

    for driver_name, rows in grouped.items():
        result = tc.resolve_group(driver_name, groups, manual_map, context="дизель-отчёт")

        if result is None:
            not_matched.append(driver_name)
            continue

        title, entity = result

        safe_name = re.sub(r'[\\/:*?"<>|]', "-", driver_name)
        img_path = tmp_dir / f"{safe_name} Fuel {period_text}.png"
        render_report_image(driver_name, rows, period_text, img_path)

        print(f"Отправляю: {driver_name} -> {title}")
        try:
            await client.send_file(entity, str(img_path))
        except Exception as first_error:
            try:
                fresh_entity = await client.get_entity(entity)
                await client.send_file(fresh_entity, str(img_path))
            except Exception as second_error:
                print(f"  ✗ Не удалось отправить: {second_error}")
                failed.append((driver_name, title, str(second_error)))
                time.sleep(config.DELAY_BETWEEN_SENDS)
                continue

        sent.append((driver_name, title))
        time.sleep(config.DELAY_BETWEEN_SENDS)

    tc.save_manual_map(map_path, manual_map)
    await client.disconnect()

    print("\n===== ОТЧЁТ =====")
    print(f"Отправлено: {len(sent)}")
    for name, title in sent:
        print(f"  ✓ {name} -> {title}")

    if failed:
        print(f"\nОшибка при отправке: {len(failed)}")
        for name, title, error in failed:
            print(f"  ✗ {name} -> {title} — {error}")

    if not_matched:
        print(f"\nНе найдена группа (пропущено): {len(not_matched)}")
        for name in not_matched:
            print(f"  ✗ {name}")

    if not failed and not not_matched:
        print("\nВсе отправлены успешно.")


if __name__ == "__main__":
    asyncio.run(main())
