import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from html import escape
from urllib.parse import quote

import aiohttp
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
BRAWLAPI_BASE = "https://api.brawlapi.com/v1"
BRAWLPICK_BASE = "https://www.brawlpick.app"
BRAWLPICK_MAPS = f"{BRAWLPICK_BASE}/map-stats"
BRAWLPICK_FRESHNESS = f"{BRAWLPICK_BASE}/api/public-stats/freshness"
TIMEOUT = aiohttp.ClientTimeout(total=20, connect=10)
USER_AGENT = "BrawlPicksAI/2.0 (Telegram bot; map stats source attribution)"
dp = Dispatcher()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("brawl-picks-ai")

# Cache responses only for this process; sources can still be refreshed by restarting the bot.
cache = {}
CACHE_SECONDS = 15 * 60


def slugify(value: str) -> str:
    value = value.lower().strip().replace("'", "").replace("’", "")
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")


async def fetch_text(session, url):
    async with session.get(url, headers={"User-Agent": USER_AGENT}) as response:
        response.raise_for_status()
        return await response.text()


async def fetch_json(session, url):
    async with session.get(url, headers={"User-Agent": USER_AGENT}) as response:
        response.raise_for_status()
        return await response.json(content_type=None)


async def cached(key, loader):
    now = datetime.now(timezone.utc).timestamp()
    if key in cache and now - cache[key][0] < CACHE_SECONDS:
        return cache[key][1]
    value = await loader()
    cache[key] = (now, value)
    return value


async def get_maps():
    async def load():
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            data = await fetch_json(session, f"{BRAWLAPI_BASE}/maps")
        items = data if isinstance(data, list) else data.get("list", [])
        return [m for m in items if isinstance(m, dict) and m.get("name")]
    return await cached("maps", load)


async def get_brawlers():
    async def load():
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            data = await fetch_json(session, f"{BRAWLAPI_BASE}/brawlers")
        items = data if isinstance(data, list) else data.get("list", [])
        return [b for b in items if isinstance(b, dict) and b.get("name")]
    return await cached("brawlers", load)


async def get_freshness():
    async def load():
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            return await fetch_json(session, BRAWLPICK_FRESHNESS)
    return await cached("freshness", load)


def parse_map_stats(html: str, requested_map: str):
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find("h1")
    title = heading.get_text(" ", strip=True) if heading else requested_map.replace("-", " ").title()
    text = soup.get_text(" ", strip=True)
    # BrawlPick map pages include a summary and a table with measured per-brawler statistics.
    mode = ""
    m = re.search(r"(?:Which brawler should you pick on .+?\?\s*)?([A-Za-z][A-Za-z '!-]+?)\s*[·•]\s*(?:In the Ranked rotation|Not in the Ranked rotation)", text)
    if m:
        mode = m.group(1).strip()
    rows = []
    for tr in soup.find_all("tr"):
        cells = []
        for cell in tr.find_all(["td", "th"]):
            cell_text = cell.get_text(" ", strip=True)
            image_names = [img.get("alt", "").strip() for img in cell.find_all("img") if img.get("alt", "").strip()]
            if image_names and (not cell_text or cell_text.lower() == "image"):
                cell_text = " ".join(image_names)
            if cell_text:
                cells.append(cell_text)
        if not cells or any("Brawler" == c for c in cells):
            continue
        joined = " | ".join(cells)
        # A data row has a win-rate percentage and a sample size.
        if not re.search(r"\d+(?:\.\d+)?%", joined):
            continue
        rate_match = re.search(r"(\d+(?:\.\d+)?)%", joined)
        if not rate_match:
            continue
        # Prefer a cell that looks like the brawler name, excluding rank and statistics.
        name = ""
        for cell in cells:
            cleaned = re.sub(r"^Image\s*", "", cell).strip()
            if (cleaned and not re.fullmatch(r"#?\d+", cleaned)
                    and not re.fullmatch(r"\d+(?:\.\d+)?%", cleaned)
                    and not re.fullmatch(r"\d+[–-]\d+%?", cleaned)
                    and not re.fullmatch(r"[≈~]?\d[\d,]*(?:\s*games?)?", cleaned, re.I)
                    and "rank" not in cleaned.lower()):
                name = cleaned
                break
        if not name:
            continue
        games = None
        # Games are generally the final numeric cell, sometimes with an approximation marker.
        for cell in reversed(cells):
            g = re.search(r"[≈~]?([\d,]+)\s*(?:games?)?", cell, re.I)
            if g and not re.search(r"%", cell) and re.search(r"\d", cell):
                number = g.group(1).replace(",", "")
                if number.isdigit() and int(number) >= 1:
                    games = int(number)
                    break
        if name.lower() in {"rank", "brawler", "win rate", "games"}:
            continue
        if any(r["name"].lower() == name.lower() for r in rows):
            continue
        rows.append({"name": name, "win_rate": float(rate_match.group(1)), "games": games})
        if len(rows) >= 10:
            break
    # Read the data-through date from the public freshness endpoint separately.
    return {"title": title, "mode": mode, "rows": rows, "url": f"{BRAWLPICK_BASE}/map-stats/{requested_map}"}


async def get_map_stats(map_name: str):
    slug = slugify(map_name)
    if not slug:
        return None, "Укажи название карты. Например: /pick Safe Zone"
    url = f"{BRAWLPICK_BASE}/map-stats/{quote(slug)}"
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            html = await fetch_text(session, url)
            stats = parse_map_stats(html, slug)
            freshness = await fetch_json(session, BRAWLPICK_FRESHNESS)
        stats["url"] = url
        stats["freshness"] = freshness.get("brawlerStats", {}) if isinstance(freshness, dict) else {}
        if not stats["rows"]:
            return stats, "На странице не удалось распознать таблицу статистики. Открой ссылку на источник ниже."
        return stats, None
    except aiohttp.ClientResponseError as exc:
        if exc.status == 404:
            return None, f"Не нашёл страницу статистики для карты «{map_name}». Проверь название на английском."
        log.warning("BrawlPick HTTP error for %s: %s", slug, exc)
        return None, "Источник статистики временно недоступен. Попробуй позже."
    except Exception as exc:
        log.warning("BrawlPick stats failed for %s: %s", slug, exc)
        return None, "Не удалось получить статистику. Попробуй позже."


def main_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="🗺 Список карт", callback_data="maps")
    kb.button(text="📊 Источники и обновление", callback_data="about")
    kb.button(text="🔄 Обновить данные", callback_data="refresh")
    kb.adjust(1)
    return kb.as_markup()


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "🔥 <b>Brawl Picks AI</b> — карты и пики на основе игровых данных.\n\n"
        "<b>Команды:</b>\n"
        "/maps — каталог карт\n"
        "/map Название — найти карту\n"
        "/pick Название — статистика бойцов на карте\n"
        "/brawlers — список бойцов\n"
        "/about — источники данных\n\n"
        "Каталог карт и бойцов берётся из BrawlAPI. Статистика побед по карте — из BrawlPick, если для неё доступна таблица.",
        reply_markup=main_keyboard(), parse_mode="HTML"
    )


@dp.message(Command("maps"))
async def maps_cmd(message: Message):
    await show_maps(message)


async def show_maps(message: Message):
    await message.answer("⏳ Загружаю каталог карт…")
    try:
        maps = await get_maps()
    except Exception as exc:
        log.warning("BrawlAPI maps failed: %s", exc)
        await message.answer("⚠️ BrawlAPI сейчас недоступен. Попробуй ещё раз позже.", reply_markup=main_keyboard())
        return
    # Show enabled maps first; do not claim that this is the live event rotation.
    enabled = [m for m in maps if not m.get("disabled", False)]
    enabled.sort(key=lambda m: int(m.get("lastActive") or 0), reverse=True)
    shown = enabled[:18]
    if not shown:
        shown = maps[:18]
    lines = ["🗺 <b>Каталог карт Brawl Stars</b>", "Источник: BrawlAPI", "⚠️ Это каталог, а не подтверждённая текущая ротация событий.", ""]
    for m in shown:
        name = escape(str(m.get("name", "Без названия")))
        mode = m.get("gameMode") or {}
        mode_name = escape(str(mode.get("name", "режим не указан"))) if isinstance(mode, dict) else "режим не указан"
        lines.append(f"• <b>{name}</b> — {mode_name}")
    lines.append("\nЧтобы посмотреть пики, напиши: <code>/pick Safe Zone</code>\nЧтобы найти карту по названию: <code>/map название</code>")
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=main_keyboard())


@dp.message(Command("map"))
async def map_cmd(message: Message):
    query = (message.text or "").partition(" ")[2].strip()
    if not query:
        await message.answer("Напиши название карты: <code>/map Safe Zone</code>", parse_mode="HTML")
        return
    try:
        maps = await get_maps()
    except Exception:
        await message.answer("Не удалось загрузить каталог карт. Попробуй позже.")
        return
    q = query.casefold()
    matches = [m for m in maps if q in str(m.get("name", "")).casefold()]
    if not matches:
        await message.answer(f"Не нашёл карту «{escape(query)}» в каталоге BrawlAPI.", parse_mode="HTML")
        return
    lines = [f"🔎 <b>Нашёл совпадения: {len(matches)}</b>", ""]
    for m in matches[:12]:
        name = str(m.get("name", "Без названия"))
        mode = m.get("gameMode") or {}
        mode_name = mode.get("name", "режим не указан") if isinstance(mode, dict) else "режим не указан"
        lines.append(f"• <b>{escape(name)}</b> — {escape(str(mode_name))}\n  Пики: <code>/pick {escape(name)}</code>")
        image_url = m.get("imageUrl")
        if image_url and str(image_url).startswith("https://"):
            lines[-1] += f'\n  <a href="{escape(str(image_url), quote=True)}">Изображение</a>'
    await message.answer("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


@dp.message(Command("pick"))
async def pick_cmd(message: Message):
    query = (message.text or "").partition(" ")[2].strip()
    if not query:
        await message.answer("Напиши название карты: <code>/pick Safe Zone</code>", parse_mode="HTML")
        return
    await message.answer("⏳ Ищу статистику бойцов по этой карте…")
    stats, error = await get_map_stats(query)
    if not stats:
        await message.answer(f"⚠️ {escape(error or 'Не удалось получить данные')}\n\nПроверь точное английское название карты.", parse_mode="HTML")
        return
    lines = [f"📊 <b>Пики на карте: {escape(stats['title'])}</b>"]
    if stats.get("mode"):
        lines.append(f"🎮 Режим: {escape(stats['mode'])}")
    freshness = stats.get("freshness") or {}
    observed_from = freshness.get("observedFrom")
    observed_to = freshness.get("observedTo")
    if observed_from and observed_to:
        lines.append(f"📅 Матчи в выборке: {escape(str(observed_from))} — {escape(str(observed_to))}")
    if freshness.get("battles"):
        lines.append(f"🌐 Вся выборка источника: {int(freshness['battles']):,} матчей".replace(",", " "))
    lines.append("")
    if stats["rows"]:
        for i, row in enumerate(stats["rows"][:10], 1):
            game_text = f", матчей: {row['games']:,}".replace(",", " ") if row.get("games") else ""
            lines.append(f"{i}. <b>{escape(row['name'])}</b> — {row['win_rate']:.1f}% побед{game_text}")
        lines.append("\n⚠️ Это результаты наблюдавшихся матчей, а не гарантия победы и не прогноз для любого драфта.")
    else:
        lines.append("⚠️ " + escape(error or "Для этой карты не удалось прочитать статистику."))
    lines.append(f'\nИсточник: <a href="{escape(stats["url"], quote=True)}">BrawlPick — статистика карты</a>')
    if freshness.get("dataUpdatedAt"):
        lines.append(f"Данные источника обновлены: {escape(str(freshness['dataUpdatedAt']))}")
    await message.answer("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


@dp.message(Command("brawlers"))
async def brawlers_cmd(message: Message):
    try:
        brawlers = await get_brawlers()
    except Exception as exc:
        log.warning("BrawlAPI brawlers failed: %s", exc)
        await message.answer("⚠️ Не удалось загрузить список бойцов из BrawlAPI.")
        return
    names = sorted({str(b.get("name")) for b in brawlers if b.get("name")}, key=str.casefold)
    await message.answer(f"🥊 <b>Бойцы в каталоге: {len(names)}</b>\n\n" + ", ".join(escape(n) for n in names[:100]) + ("…" if len(names) > 100 else ""), parse_mode="HTML")


@dp.message(Command("about"))
async def about_cmd(message: Message):
    await show_about(message)


async def show_about(message: Message):
    try:
        freshness = await get_freshness()
        data = freshness.get("brawlerStats", {}) if isinstance(freshness, dict) else {}
        updated = data.get("dataUpdatedAt", "не указана")
        battles = data.get("battles")
        window = f"\nМатчи в выборке: {escape(str(data.get('observedFrom', '?')))} — {escape(str(data.get('observedTo', '?')))}" if data else ""
        total = f"\nРазмер общей выборки: {int(battles):,} матчей".replace(",", " ") if battles else ""
    except Exception:
        updated, window, total = "не удалось получить", "", ""
    await message.answer(
        "📊 <b>Источники Brawl Picks AI</b>\n\n"
        "• Карты и бойцы: <a href=\"https://brawlapi.com/\">BrawlAPI</a> — статический каталог, обновляемый после обновлений игры.\n"
        "• Статистика пиков: <a href=\"https://www.brawlpick.app/map-stats\">BrawlPick</a> — результаты собранных матчей.\n"
        f"• Последнее обновление статистики: {escape(str(updated))}{window}{total}\n\n"
        "Обновление статистики BrawlPick происходит после сбора достаточного количества новых матчей и не по фиксированному расписанию. Если карта отсутствует или таблица недоступна, бот не выдумывает проценты.\n\n"
        "Команды: /maps, /map название, /pick название, /brawlers, /about",
        parse_mode="HTML", disable_web_page_preview=True, reply_markup=main_keyboard()
    )


@dp.callback_query(F.data == "maps")
async def maps_callback(callback: CallbackQuery):
    await callback.answer("Загружаю каталог…")
    if callback.message:
        await show_maps(callback.message)


@dp.callback_query(F.data == "about")
async def about_callback(callback: CallbackQuery):
    await callback.answer()
    if callback.message:
        await show_about(callback.message)


@dp.callback_query(F.data == "refresh")
async def refresh_callback(callback: CallbackQuery):
    cache.clear()
    await callback.answer("Кэш очищен, загружаю свежие данные…")
    if callback.message:
        await show_maps(callback.message)


async def main():
    if not TOKEN:
        raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN в переменных окружения")
    bot = Bot(TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
