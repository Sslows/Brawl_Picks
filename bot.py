import asyncio
import logging
import os
from datetime import datetime, timezone
from html import escape

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
SUPERCELL_TOKEN = os.getenv("BRAWL_STARS_API_TOKEN", "").strip()
API_BASE = "https://api.brawlstars.com/v1"
BRAWLIFY_EVENTS_PAGE = "https://brawlify.com/events"
TIMEOUT = aiohttp.ClientTimeout(total=15)
dp = Dispatcher()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("brawl-picks-ai")

# These are tactical heuristics, not claimed win-rate statistics.
MODE_TIPS = {
    "Gem Grab": "Часто полезны контроль центра, дальность и способность безопасно удерживать гемы.",
    "Brawl Ball": "Ищи сочетание контроля проходов, давления на ворота и способа пережить вражеский пуш.",
    "Bounty": "На открытых участках ценны дальность и точность; за стенами полезны метатели и контроль.",
    "Wipeout": "Приоритет — стабильный урон на дистанции и возможность не отдавать лишние устранения.",
    "Knockout": "Ценны дальность, выживаемость и безопасное давление; не стоит без причины заходить в размен.",
    "Hot Zone": "Нужны контроль зоны, выживаемость и возможность вытеснять соперников из круга.",
    "Heist": "Сравни способность быстро наносить урон сейфу с возможностью остановить атаку врага.",
    "Solo Showdown": "Учитывай кусты, укрытия, открытые линии и возможность избегать невыгодных дуэлей.",
    "Duo Showdown": "Хорошо дополняют друг друга дальний бой, контроль пространства и поддержка напарника.",
    "Duels": "Подбирай бойцов с разными сильными сторонами, чтобы закрыть больше типов противников.",
}
MODE_PICKS = {
    "Gem Grab": ["Gene", "Max", "Sandy"],
    "Brawl Ball": ["Max", "Buster", "Frank"],
    "Bounty": ["Piper", "Nani", "Brock"],
    "Wipeout": ["Piper", "Nani", "Brock"],
    "Knockout": ["Piper", "Angelo", "Nani"],
    "Hot Zone": ["Sandy", "Emz", "Lou"],
    "Heist": ["Colt", "Colette", "Jessie"],
    "Solo Showdown": ["Cordelius", "Leon", "Kit"],
    "Duo Showdown": ["Cordelius", "Leon", "Max"],
    "Duels": ["Cordelius", "Leon", "Fang"],
}

def get_name(obj, default="Неизвестно"):
    if not isinstance(obj, dict):
        return default
    return obj.get("name") or obj.get("localizedName") or default

async def fetch_json(session, url, headers=None):
    async with session.get(url, headers=headers or {}) as response:
        response.raise_for_status()
        return await response.json(content_type=None)

async def get_official_rotation():
    if not SUPERCELL_TOKEN:
        return None, "Не задан BRAWL_STARS_API_TOKEN"
    headers = {"Authorization": f"Bearer {SUPERCELL_TOKEN}", "Accept": "application/json"}
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        try:
            data = await fetch_json(session, f"{API_BASE}/events/rotation", headers)
        except Exception as exc:
            log.warning("Official rotation request failed: %s", exc)
            return None, f"Официальный API недоступен: {type(exc).__name__}"
    events = data if isinstance(data, list) else data.get("items", data.get("events", []))
    normalized = []
    for item in events:
        if not isinstance(item, dict):
            continue
        map_data = item.get("map") or {}
        mode_data = item.get("gameMode") or item.get("mode") or {}
        normalized.append({
            "map": get_name(map_data),
            "mode": get_name(mode_data),
            "map_url": map_data.get("imageUrl") or "",
            "start": item.get("startTime") or item.get("start_time"),
            "end": item.get("endTime") or item.get("end_time"),
            "stats": map_data.get("stats") or [],
            "teams": map_data.get("teamStats") or [],
            "source": "Supercell Official API",
        })
    return normalized, None

def recommendations(event):
    mode = event.get("mode", "")
    stats = event.get("stats") or []
    # Use observed map stats if the official response actually contains them.
    if stats:
        sorted_stats = sorted(
            [x for x in stats if isinstance(x, dict) and isinstance(x.get("winRate"), (int, float))],
            key=lambda x: x["winRate"], reverse=True
        )
        if sorted_stats:
            lines = []
            for s in sorted_stats[:5]:
                name = str(s.get("brawlerName") or s.get("name") or s.get("brawler") or "Боец")
                line = f"• {escape(name)} — победы: {s['winRate']:.1f}%"
                if isinstance(s.get("useRate"), (int, float)):
                    line += f", выбор: {s['useRate']:.1f}%"
                lines.append(line)
            return "📈 <b>Статистика источника</b>\n" + "\n".join(lines) + "\n\nПроценты приведены только если пришли от источника."
    picks = MODE_PICKS.get(mode, [])
    tip = MODE_TIPS.get(mode, "Сравни дальность, укрытия, контроль пространства и цели режима.")
    if picks:
        return ("🧠 <b>Предварительные тактические варианты</b>\n"
                + " • ".join(escape(x) for x in picks)
                + f"\n\n<b>Почему:</b> {escape(tip)}\n"
                "⚠️ Это общие варианты по режиму, не статистический рейтинг именно этой карты.")
    return (f"🧠 Для режима «{escape(mode)}»: {escape(tip)}\n"
            "⚠️ Нет проверенной статистики именно по этой карте; это не рейтинг по проценту побед.")

def event_text(event):
    mode = escape(event.get("mode", "Режим не указан"))
    map_name = escape(event.get("map", "Карта не указана"))
    lines = [f"🗺 <b>{map_name}</b>", f"🎮 Режим: <b>{mode}</b>"]
    if event.get("end"):
        lines.append(f"⏳ Окончание по данным API: {escape(str(event['end']))}")
    lines.append("")
    lines.append(recommendations(event))
    if event.get("map_url", "").startswith("http"):
        lines.append(f'\n<a href="{escape(event["map_url"], quote=True)}">Изображение карты</a>')
    lines.append(f'\nИсточник ротации: {escape(event.get("source", "неизвестен"))}')
    return "\n".join(lines)

def main_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="🗺 Актуальные карты", callback_data="maps")
    kb.button(text="📊 Как работает подбор", callback_data="about")
    kb.button(text="🔄 Обновить", callback_data="maps")
    kb.adjust(1)
    return kb.as_markup()

@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "🔥 <b>Brawl Picks AI</b>\n\n"
        "Показываю ротацию карт из официального Brawl Stars API и подбираю предварительные пики.\n"
        "Если источник не отдаёт статистику по карте, я не придумываю win rate.\n\n"
        "Нажми «Актуальные карты» или отправь /maps.",
        reply_markup=main_keyboard(), parse_mode="HTML"
    )

@dp.message(Command("maps"))
async def maps_cmd(message: Message):
    await show_maps(message)

async def show_maps(message: Message):
    events, error = await get_official_rotation()
    if events is None:
        await message.answer(
            "⚠️ Не удалось получить подтверждённую текущую ротацию.\n"
            f"{escape(error or 'Неизвестная ошибка')}.\n\n"
            "Проверь API-ключ и доступность сервиса. Не буду подменять живые данные старым списком карт.\n"
            "Официальный портал: https://developer.brawlstars.com/",
            parse_mode="HTML", reply_markup=main_keyboard()
        )
        return
    if not events:
        await message.answer(
            "API ответил, но не вернул активные события. Не показываю старые карты как актуальные.",
            reply_markup=main_keyboard()
        )
        return
    await message.answer(f"🗺 <b>Активные события: {len(events)}</b>\nИсточник: Supercell Official API",
                         parse_mode="HTML")
    for event in events[:35]:
        await message.answer(event_text(event), parse_mode="HTML", disable_web_page_preview=True)

@dp.callback_query(F.data == "maps")
async def maps_callback(callback: CallbackQuery):
    await callback.answer("Проверяю текущую ротацию…")
    if callback.message:
        await show_maps(callback.message)

@dp.callback_query(F.data == "about")
async def about_callback(callback: CallbackQuery):
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "📊 <b>Логика Brawl Picks AI</b>\n\n"
            "1. Текущая ротация запрашивается у официального API.\n"
            "2. Если ответ содержит статистику бойцов по карте, сортируем реальные значения.\n"
            "3. Если статистики нет, показываем только предварительные тактические варианты по режиму.\n"
            "4. В этой версии нет автоматического чтения вражеского драфта и анализа пикселей карты.\n\n"
            "Чтобы получить актуальную ротацию, нужен API-токен Supercell. Источник данных будет указан в ответе.",
            parse_mode="HTML", reply_markup=main_keyboard()
        )

async def main():
    if not TOKEN:
        raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN в .env")
    bot = Bot(TOKEN)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
