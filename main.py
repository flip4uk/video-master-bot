
import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
)
from aiogram.exceptions import TelegramBadRequest

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
SUBSCRIBE_URL = os.getenv("SUBSCRIBE_URL")

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN")

if not CHANNEL_ID or not SUBSCRIBE_URL:
    raise RuntimeError("Задай CHANNEL_ID и SUBSCRIBE_URL")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)

ALLOWED_DOMAINS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtu.be",
    "www.youtu.be",
    "tiktok.com",
    "www.tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
    "instagram.com",
    "www.instagram.com",
}


def subscription_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📢 Подписаться на канал",
            url=SUBSCRIBE_URL
        )],
        [InlineKeyboardButton(
            text="✅ Проверить подписку",
            callback_data="check_sub"
        )],
    ])


def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📥 Скачать видео",
            callback_data="download"
        )],
        [InlineKeyboardButton(
            text="🎬 Уникализировать видео",
            callback_data="unique"
        )],
        [InlineKeyboardButton(
            text="⭐ Поддержать проект",
            callback_data="donate"
        )],
    ])


async def is_subscribed(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(
            chat_id=CHANNEL_ID,
            user_id=user_id
        )
        return member.status in (
            "member", "administrator", "creator"
        )
    except TelegramBadRequest:
        logging.exception("Не удалось проверить подписку")
        return False


def get_video_url(text: str):
    match = URL_PATTERN.search(text)
    if not match:
        return None

    url = match.group(0).rstrip(".,!?;:)]}")
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        return None

    if parsed.hostname not in ALLOWED_DOMAINS:
        return None

    return url


def download_video(url: str, folder: str):
    options = {
        "format": "best[ext=mp4]/best",
        "outtmpl": str(Path(folder) / "video.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 2,
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)

    path = Path(filename)

    if not path.is_file():
        candidates = list(Path(folder).glob("video.*"))
        if not candidates:
            raise RuntimeError("Загруженный файл не найден")
        path = candidates[0]

    return path, info.get("title", "Видео")


async def show_menu(message: Message):
    await message.answer(
        "👋 Добро пожаловать!\n\n"
        "Выбери нужный раздел:",
        reply_markup=main_keyboard()
    )


@dp.message(CommandStart())
async def start(message: Message):
    if not await is_subscribed(message.from_user.id):
        await message.answer(
            "🔒 Чтобы пользоваться ботом, подпишись на канал "
            "и нажми «Проверить подписку».",
            reply_markup=subscription_keyboard()
        )
        return

    await show_menu(message)


@dp.callback_query(F.data == "check_sub")
async def check_subscription(callback: CallbackQuery):
    if not await is_subscribed(callback.from_user.id):
        await callback.answer(
            "Подписка пока не обнаружена. Подпишись и повтори проверку.",
            show_alert=True
        )
        return

    await callback.answer("Подписка подтверждена!")
    if callback.message:
        await callback.message.answer(
            "✅ Доступ открыт!",
            reply_markup=main_keyboard()
        )


@dp.callback_query(F.data == "download")
async def download_info(callback: CallbackQuery):
    if not await is_subscribed(callback.from_user.id):
        await callback.answer(
            "Сначала подпишись на канал.", show_alert=True
        )
        return

    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "📥 Отправь ссылку на видео из YouTube, TikTok или Instagram."
        )


@dp.callback_query(F.data == "unique")
async def unique_info(callback: CallbackQuery):
    if not await is_subscribed(callback.from_user.id):
        await callback.answer(
            "Сначала подпишись на канал.", show_alert=True
        )
        return

    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "🎬 Обработка видео будет добавлена следующим этапом."
        )


@dp.callback_query(F.data == "donate")
async def donate_info(callback: CallbackQuery):
    if not await is_subscribed(callback.from_user.id):
        await callback.answer(
            "Сначала подпишись на канал.", show_alert=True
        )
        return

    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "⭐ Приём Telegram Stars подключим отдельным этапом."
        )


@dp.message(F.text)
async def handle_text(message: Message):
    if not await is_subscribed(message.from_user.id):
        await message.answer(
            "🔒 Сначала подпишись на канал.",
            reply_markup=subscription_keyboard()
        )
        return

    url = get_video_url(message.text or "")

    if not url:
        await message.answer(
            "Отправь корректную ссылку на видео из YouTube, "
            "TikTok или Instagram.",
            reply_markup=main_keyboard()
        )
        return

    status = await message.answer(
        "⏳ Загружаю видео. Это может занять несколько минут..."
    )

    try:
        with tempfile.TemporaryDirectory() as folder:
            path, title = await asyncio.to_thread(
                download_video, url, folder
            )

            # Ограничение размера для отправки через Telegram Bot API.
            if path.stat().st_size > 49 * 1024 * 1024:
                await status.edit_text(
                    "⚠️ Видео превышает допустимый для этого бота "
                    "размер отправки (49 МБ). Попробуй видео меньшего размера."
                )
                return

            await message.answer_video(
                video=FSInputFile(path),
                caption=title[:900]
            )

        await status.delete()

    except Exception:
        logging.exception("Ошибка загрузки видео")
        try:
            await status.edit_text(
                "❌ Не удалось скачать это видео.\n\n"
                "Попробуй другую ссылку. Некоторые ролики недоступны "
                "без авторизации или ограничены платформой."
            )
        except TelegramBadRequest:
            pass


async def main():
    logging.info("Бот запускается")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
