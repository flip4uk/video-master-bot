
import asyncio
import logging
import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError


# =========================
# НАСТРОЙКИ
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
SUBSCRIBE_URL = os.getenv("SUBSCRIBE_URL")

MAX_TELEGRAM_FILE_SIZE = 49 * 1024 * 1024

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(__name__)

if not BOT_TOKEN:
    raise RuntimeError("Не задана переменная BOT_TOKEN")

if not CHANNEL_ID:
    raise RuntimeError("Не задана переменная CHANNEL_ID")

if not SUBSCRIBE_URL:
    raise RuntimeError("Не задана переменная SUBSCRIBE_URL")

# Если CHANNEL_ID записан числом, преобразуем его.
try:
    CHANNEL_ID = int(CHANNEL_ID)
except ValueError:
    pass


# =========================
# СОЗДАНИЕ БОТА
# =========================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# =========================
# КНОПКИ
# =========================

main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📥 Скачать видео")],
        [KeyboardButton(text="🎬 Уникализировать видео")],
        [KeyboardButton(text="⭐ Поддержать бота")],
    ],
    resize_keyboard=True,
)

subscribe_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(
                text="📢 Подписаться на канал",
                url=SUBSCRIBE_URL,
            )
        ],
        [
            InlineKeyboardButton(
                text="✅ Проверить подписку",
                callback_data="check_subscription",
            )
        ],
    ]
)


# =========================
# ПРОВЕРКА ПОДПИСКИ
# =========================

async def is_subscribed(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(
            chat_id=CHANNEL_ID,
            user_id=user_id,
        )

        return member.status in ("member", "administrator", "creator")

    except (TelegramBadRequest, TelegramForbiddenError) as error:
        logger.error("Не удалось проверить подписку: %s", error)
        return False

    except Exception:
        logger.exception("Ошибка при проверке подписки")
        return False


async def ask_to_subscribe(message: Message):
    await message.answer(
        "👋 Для использования VidMorph сначала подпишитесь на наш канал, "
        "а затем нажмите «Проверить подписку».",
        reply_markup=subscribe_keyboard,
    )


# =========================
# ПРОВЕРКА ССЫЛКИ
# =========================

def get_video_url(text: str):
    text = text.strip()

    try:
        parsed = urlparse(text)
    except Exception:
        return None

    if parsed.scheme not in ("http", "https"):
        return None

    host = (parsed.hostname or "").lower()
    allowed_domains = (
        "youtube.com",
        "youtu.be",
        "youtube-nocookie.com",
        "instagram.com",
        "tiktok.com",
    )

    is_supported = any(
        host == domain or host.endswith("." + domain)
        for domain in allowed_domains
    )

    return text if is_supported else None


# =========================
# СКАЧИВАНИЕ ВИДЕО
# =========================

def download_video(url: str, folder: str):
    output_template = str(Path(folder) / "video.%(ext)s")

    options = {
        "outtmpl": output_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": False,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "merge_output_format": "mp4",
        # Не задаём жёсткий формат: yt-dlp выберет доступный.
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)

        if not info:
            raise RuntimeError("Не удалось получить информацию о видео")

        filename = ydl.prepare_filename(info)

    path = Path(filename)

    # При объединении дорожек расширение может измениться на mp4.
    if not path.is_file():
        merged_path = path.with_suffix(".mp4")

        if merged_path.is_file():
            path = merged_path
        else:
            candidates = [
                file
                for file in Path(folder).glob("video.*")
                if file.is_file()
            ]

            if not candidates:
                raise RuntimeError("Загруженный видеофайл не найден")

            path = candidates[0]

    return path, info.get("title", "Видео")


# =========================
# КОМАНДА /START
# =========================

@dp.message(CommandStart())
async def start_handler(message: Message):
    if not message.from_user:
        return

    if not await is_subscribed(message.from_user.id):
        await ask_to_subscribe(message)
        return

    await message.answer(
        "🎬 Добро пожаловать в VidMorph!\n\n"
        "📥 Нажмите «Скачать видео» и отправьте ссылку "
        "на поддерживаемое видео.\n\n"
        "Выберите действие в меню.",
        reply_markup=main_keyboard,
    )


# =========================
# ПРОВЕРКА ПОДПИСКИ ПО КНОПКЕ
# =========================

@dp.callback_query(F.data == "check_subscription")
async def check_subscription_handler(callback):
    if not callback.from_user:
        await callback.answer()
        return

    if await is_subscribed(callback.from_user.id):
        await callback.answer("Подписка подтверждена!")

        if callback.message:
            await callback.message.answer(
                "✅ Подписка подтверждена! Теперь бот доступен.",
                reply_markup=main_keyboard,
            )
    else:
        await callback.answer(
            "Подписка не найдена. Подпишитесь и попробуйте ещё раз.",
            show_alert=True,
        )


# =========================
# КНОПКА СКАЧИВАНИЯ
# =========================

@dp.message(F.text == "📥 Скачать видео")
async def download_button_handler(message: Message):
    if not message.from_user:
        return

    if not await is_subscribed(message.from_user.id):
        await ask_to_subscribe(message)
        return

    await message.answer(
        "📎 Отправьте ссылку на видео из YouTube, Instagram или TikTok."
    )


# =========================
# КНОПКА УНИКАЛИЗАЦИИ
# =========================

@dp.message(F.text == "🎬 Уникализировать видео")
async def unique_button_handler(message: Message):
    if not message.from_user:
        return

    if not await is_subscribed(message.from_user.id):
        await ask_to_subscribe(message)
        return

    await message.answer(
        "🎬 Функция обработки видео пока не подключена. "
        "Сейчас можно воспользоваться скачиванием видео."
    )


# =========================
# КНОПКА ПОДДЕРЖКИ
# =========================

@dp.message(F.text == "⭐ Поддержать бота")
async def donate_button_handler(message: Message):
    if not message.from_user:
        return

    if not await is_subscribed(message.from_user.id):
        await ask_to_subscribe(message)
        return

    await message.answer(
        "⭐ Спасибо за желание поддержать VidMorph!\n"
        "Платежи через Telegram Stars пока не подключены."
    )


# =========================
# ОБРАБОТКА ССЫЛОК
# =========================

@dp.message(F.text)
async def video_link_handler(message: Message):
    if not message.from_user or not message.text:
        return

    url = get_video_url(message.text)

    if not url:
        await message.answer(
            "Отправьте корректную ссылку на видео из YouTube, "
            "Instagram или TikTok либо выберите действие в меню."
        )
        return

    if not await is_subscribed(message.from_user.id):
        await ask_to_subscribe(message)
        return

    status_message = await message.answer(
        "⏳ Загружаю видео. Это может занять некоторое время..."
    )

    try:
        with tempfile.TemporaryDirectory() as temp_folder:
            path, title = await asyncio.to_thread(
                download_video,
                url,
                temp_folder,
            )

            if not path.exists():
                raise RuntimeError("Видео не найдено после скачивания")

            file_size = path.stat().st_size

            if file_size > MAX_TELEGRAM_FILE_SIZE:
                await status_message.edit_text(
                    "⚠️ Видео скачано, но его размер превышает "
                    "лимит отправки, установленный ботом.\n"
                    "Попробуйте ссылку на видео меньшего размера."
                )
                return

            await message.answer_video(
                video=path,
                caption=f"🎬 {title[:900]}",
                supports_streaming=True,
            )

        await status_message.delete()

    except Exception as error:
        logger.exception("Ошибка скачивания видео")

        try:
            await status_message.edit_text(
                "❌ Не удалось скачать видео.\n\n"
                "Возможные причины: видео закрыто, ссылка недоступна, "
                "платформа временно изменила защиту или произошла ошибка "
                "загрузки.\n\n"
                "Попробуйте другую ссылку позже."
            )
        except Exception:
            pass


# =========================
# ЗАПУСК БОТА
# =========================

async def main():
    logger.info("VidMorph запускается...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        logger.info("Работа VidMorph завершена")
