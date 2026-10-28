import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    PreCheckoutQuery,
    LabeledPrice,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
)
from aiogram.exceptions import TelegramAPIError


# =====================================
# НАСТРОЙКИ
# =====================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()
SUBSCRIBE_URL = os.getenv("SUBSCRIBE_URL", "").strip()

# Лимит отправки файла. Увеличивай только если это позволяет Bot API.
MAX_FILE_SIZE = 49 * 1024 * 1024
MAX_CONCURRENT_DOWNLOADS = 2

SUPPORTED_DOMAINS = {
    "youtube.com",
    "youtu.be",
    "instagram.com",
    "tiktok.com",
}

DONATION_AMOUNTS = {
    25: "Небольшая поддержка",
    50: "Спасибо за проект",
    100: "Большая поддержка",
}

if not BOT_TOKEN:
    raise RuntimeError("Не задана переменная BOT_TOKEN")
if not CHANNEL_ID:
    raise RuntimeError("Не задана переменная CHANNEL_ID")
if not SUBSCRIBE_URL:
    raise RuntimeError("Не задана переменная SUBSCRIBE_URL")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()
download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)


# =====================================
# КЛАВИАТУРЫ
# =====================================

def subscription_keyboard():
    return InlineKeyboardMarkup(
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


def main_menu_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📥 Скачать видео",
                    callback_data="download_prompt",
                )
            ],
            [
                InlineKeyboardButton(
                    text="💖 Поддержать проект",
                    callback_data="donate",
                )
            ],
        ]
    )


def donation_keyboard():
    rows = [
        [
            InlineKeyboardButton(
                text=f"⭐ {amount} Stars — {description}",
                callback_data=f"donate_{amount}",
            )
        ]
        for amount, description in DONATION_AMOUNTS.items()
    ]
    rows.append([
        InlineKeyboardButton(
            text="↩️ В главное меню",
            callback_data="back_to_menu",
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def donation_back_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="↩️ В главное меню",
                    callback_data="back_to_menu",
                )
            ]
        ]
    )


# =====================================
# ТЕКСТЫ ИНТЕРФЕЙСА
# =====================================

WELCOME_TEXT = (
    "🎬 <b>VIDMORPH</b>\n"
    "<i>Твои видео. Твоё качество. Без лишних действий.</i>\n\n"
    "Добро пожаловать!\n"
    "Скачивай видео из <b>YouTube, Instagram и TikTok</b> "
    "в лучшем доступном качестве, если ссылка и платформа это позволяют.\n\n"
    "🔒 <b>Чтобы начать пользоваться ботом:</b>\n\n"
    "1. Нажми «Подписаться на канал».\n"
    "2. Подпишись на канал.\n"
    "3. Вернись сюда и нажми «Проверить подписку».\n\n"
    "✨ После проверки откроется главное меню."
)

MAIN_MENU_TEXT = (
    "🎬 <b>VIDMORPH</b>\n"
    "<i>Твои видео. Твоё качество. Без лишних действий.</i>\n\n"
    "✅ <b>Доступ открыт!</b>\n\n"
    "Отправь ссылку на видео или нажми кнопку ниже.\n\n"
    "🌐 YouTube · Instagram · TikTok"
)

DONATION_TEXT = (
    "💖 <b>Поддержать VidMorph</b>\n\n"
    "Если бот оказался полезен, ты можешь помочь проекту "
    "развиваться и оплачивать его обслуживание.\n\n"
    "Выбери удобную сумму в Telegram Stars.\n"
    "Каждая поддержка важна. Спасибо! ✨"
)


# =====================================
# ПРОВЕРКА ПОДПИСКИ
# =====================================

async def is_subscribed(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(
            chat_id=CHANNEL_ID,
            user_id=user_id,
        )

        if member.status in {"creator", "administrator", "member"}:
            return True

        if member.status == "restricted":
            return bool(getattr(member, "is_member", False))

        return False

    except TelegramAPIError:
        logger.exception("Не удалось проверить подписку")
        return False


async def show_subscription_screen(message: Message):
    await message.answer(
        WELCOME_TEXT,
        reply_markup=subscription_keyboard(),
    )


async def show_main_menu(message: Message):
    await message.answer(
        MAIN_MENU_TEXT,
        reply_markup=main_menu_keyboard(),
    )


async def require_subscription(message: Message) -> bool:
    if message.from_user and await is_subscribed(message.from_user.id):
        return True

    await show_subscription_screen(message)
    return False


# =====================================
# РАСПОЗНАВАНИЕ ССЫЛОК
# =====================================

def extract_url(text: str) -> str | None:
    match = re.search(r"https?://[^\s<>]+", text)
    if not match:
        return None
    return match.group(0).rstrip(".,!?)]}»'\"")


def is_supported_url(url: str) -> bool:
    try:
        parsed = urlparse(url.strip())

        if parsed.scheme not in {"http", "https"}:
            return False

        host = (parsed.hostname or "").lower().rstrip(".")
        if host.startswith("www."):
            host = host[4:]

        return any(
            host == domain or host.endswith("." + domain)
            for domain in SUPPORTED_DOMAINS
        )

    except ValueError:
        return False


# =====================================
# СКАЧИВАНИЕ ВИДЕО
# =====================================

def download_video(url: str, folder: str):
    output_template = str(Path(folder) / "video.%(ext)s")

    options = {
        "format": "bestvideo*+bestaudio/best",
        "outtmpl": output_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": False,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "merge_output_format": "mp4",
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)

        if not info:
            raise RuntimeError("Не удалось получить информацию о видео")

        filename = ydl.prepare_filename(info)

    path = Path(filename)

    if not path.is_file():
        mp4_path = path.with_suffix(".mp4")

        if mp4_path.is_file():
            path = mp4_path
        else:
            candidates = [
                file for file in Path(folder).glob("video.*")
                if file.is_file()
            ]

            if not candidates:
                raise RuntimeError("Скачанный файл не найден")

            path = max(candidates, key=lambda file: file.stat().st_size)

    if path.stat().st_size == 0:
        raise RuntimeError("Скачанный файл пуст")

    return path, info.get("title", "Видео")


# =====================================
# ПОДГОТОВКА ВИДЕО ДЛЯ IPHONE
# =====================================

def make_iphone_compatible(video_path: Path) -> Path:
    """Оставляет совместимые файлы без перекодирования."""

    probe = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "stream=codec_type,codec_name",
            "-of", "json",
            str(video_path),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )

    streams = json.loads(probe.stdout).get("streams", [])

    video_codecs = [
        stream["codec_name"]
        for stream in streams
        if stream.get("codec_type") == "video"
    ]
    audio_codecs = [
        stream["codec_name"]
        for stream in streams
        if stream.get("codec_type") == "audio"
    ]

    compatible = (
        video_path.suffix.lower() == ".mp4"
        and video_codecs
        and video_codecs[0] == "h264"
        and (not audio_codecs or audio_codecs[0] == "aac")
    )

    if compatible:
        return video_path

    output_path = video_path.with_name("video_iphone.mp4")

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i", str(video_path),
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=1800,
    )

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError("Не удалось подготовить видео для iPhone")

    return output_path


# =====================================
# /START
# =====================================

@dp.message(CommandStart())
async def start_handler(message: Message):
    if message.from_user and await is_subscribed(message.from_user.id):
        await show_main_menu(message)
    else:
        await show_subscription_screen(message)


# =====================================
# ПРОВЕРКА ПОДПИСКИ
# =====================================

@dp.callback_query(F.data == "check_subscription")
async def check_subscription_handler(callback: CallbackQuery):
    if await is_subscribed(callback.from_user.id):
        await callback.answer("Подписка подтверждена! 🎉")

        if callback.message:
            try:
                await callback.message.edit_text(
                    MAIN_MENU_TEXT,
                    reply_markup=main_menu_keyboard(),
                )
            except TelegramAPIError:
                await callback.message.answer(
                    MAIN_MENU_TEXT,
                    reply_markup=main_menu_keyboard(),
                )
    else:
        await callback.answer(
            "Подписка пока не найдена. Подпишись и проверь ещё раз.",
            show_alert=True,
        )


# =====================================
# КНОПКА СКАЧИВАНИЯ
# =====================================

@dp.callback_query(F.data == "download_prompt")
async def download_prompt_handler(callback: CallbackQuery):
    await callback.answer()

    if not await is_subscribed(callback.from_user.id):
        if callback.message:
            await show_subscription_screen(callback.message)
        return

    if callback.message:
        await callback.message.answer(
            "🔗 <b>Отправь ссылку на видео</b>\n\n"
            "Подойдут ссылки из YouTube, Instagram или TikTok.",
            reply_markup=main_menu_keyboard(),
        )


# =====================================
# ВОЗВРАТ В ГЛАВНОЕ МЕНЮ
# =====================================

@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu_handler(callback: CallbackQuery):
    await callback.answer()

    if not callback.message:
        return

    if await is_subscribed(callback.from_user.id):
        try:
            await callback.message.edit_text(
                MAIN_MENU_TEXT,
                reply_markup=main_menu_keyboard(),
            )
        except TelegramAPIError:
            await callback.message.answer(
                MAIN_MENU_TEXT,
                reply_markup=main_menu_keyboard(),
            )
    else:
        try:
            await callback.message.edit_text(
                WELCOME_TEXT,
                reply_markup=subscription_keyboard(),
            )
        except TelegramAPIError:
            await show_subscription_screen(callback.message)


# =====================================
# ДОНАТЫ
# =====================================

@dp.callback_query(F.data == "donate")
async def donate_handler(callback: CallbackQuery):
    await callback.answer()

    if not callback.message:
        return

    if not await is_subscribed(callback.from_user.id):
        await show_subscription_screen(callback.message)
        return

    await callback.message.answer(
        DONATION_TEXT,
        reply_markup=donation_keyboard(),
    )


@dp.callback_query(F.data.startswith("donate_"))
async def donate_amount_handler(callback: CallbackQuery):
    try:
        amount = int((callback.data or "").split("_", 1)[1])
    except (ValueError, AttributeError):
        await callback.answer("Некорректная сумма.", show_alert=True)
        return

    if amount not in DONATION_AMOUNTS:
        await callback.answer("Такой суммы нет.", show_alert=True)
        return

    await callback.answer()

    try:
        await bot.send_invoice(
            chat_id=callback.from_user.id,
            title=f"Поддержка VidMorph: {amount} Stars",
            description=(
                "Добровольная поддержка проекта по разработке "
                "и обслуживанию Telegram-бота."
            ),
            payload=f"donation_{amount}_{uuid.uuid4().hex}",
            provider_token="",
            currency="XTR",
            prices=[
                LabeledPrice(
                    label="Добровольная поддержка",
                    amount=amount,
                )
            ],
        )
    except TelegramAPIError:
        logger.exception("Не удалось выставить счёт Stars")
        if callback.message:
            await callback.message.answer(
                "Не удалось создать платёж. Попробуй позже.",
                reply_markup=donation_back_keyboard(),
            )


# =====================================
# ПЛАТЕЖИ TELEGRAM STARS
# =====================================

@dp.pre_checkout_query()
async def pre_checkout_handler(query: PreCheckoutQuery):
    try:
        parts = query.invoice_payload.split("_")
        expected_amount = int(parts[1]) if len(parts) >= 3 else -1

        valid = (
            parts[0] == "donation"
            and expected_amount in DONATION_AMOUNTS
            and query.currency == "XTR"
            and query.total_amount == expected_amount
        )

        if not valid:
            await query.answer(
                ok=False,
                error_message="Не удалось проверить платёж. Попробуй снова.",
            )
            return

        await query.answer(ok=True)

    except (ValueError, IndexError):
        await query.answer(
            ok=False,
            error_message="Некорректные данные платежа.",
        )


@dp.message(F.successful_payment)
async def successful_payment_handler(message: Message):
    payment = message.successful_payment

    logger.info(
        "Успешный Stars-платёж: user_id=%s amount=%s currency=%s charge_id=%s",
        message.from_user.id if message.from_user else None,
        payment.total_amount,
        payment.currency,
        payment.telegram_payment_charge_id,
    )

    await message.answer(
        "💖 <b>Спасибо за поддержку!</b>\n\n"
        f"Получено: ⭐ <b>{payment.total_amount} Stars</b>\n"
        "Твой вклад помогает развивать VidMorph.",
        reply_markup=donation_back_keyboard(),
    )


@dp.message(Command("paysupport"))
async def payment_support_handler(message: Message):
    await message.answer(
        "Если возник вопрос по донату или платёж прошёл некорректно, "
        "напиши администратору проекта.\n\n"
        "Укажи дату платежа и сумму. Не отправляй пароли или коды."
    )


# =====================================
# ОБРАБОТКА ССЫЛОК
# =====================================

@dp.message(F.text)
async def video_handler(message: Message):
    text = message.text or ""
    url = extract_url(text)

    if not url:
        if text.startswith("/"):
            return

        if not message.from_user or not await is_subscribed(message.from_user.id):
            await show_subscription_screen(message)
        else:
            await message.answer(
                "👋 Выбери действие в меню или отправь ссылку на видео.",
                reply_markup=main_menu_keyboard(),
            )
        return

    if not is_supported_url(url):
        await message.answer(
            "⚠️ <b>Эта ссылка не поддерживается.</b>\n\n"
            "Отправь ссылку из YouTube, Instagram или TikTok."
        )
        return

    if not await require_subscription(message):
        return

    status_message = await message.answer(
        "⏳ Получаю видео в лучшем доступном качестве…"
    )

    async with download_semaphore:
        temp_dir = Path(tempfile.mkdtemp(prefix="vidmorph_"))

        try:
            video_path, title = await asyncio.to_thread(
                download_video,
                url,
                str(temp_dir),
            )

            await status_message.edit_text(
                "🎬 Проверяю совместимость с iPhone…"
            )

            video_path = await asyncio.to_thread(
                make_iphone_compatible,
                video_path,
            )

            if video_path.stat().st_size > MAX_FILE_SIZE:
                await status_message.edit_text(
                    "📦 <b>Видео слишком большое для отправки.</b>\n\n"
                    "Попробуй видео меньшего размера."
                )
                return

            await status_message.edit_text(
                "✨ Видео готово! Отправляю в видеоплеер Telegram…"
            )

            await message.answer_video(
                video=FSInputFile(
                    path=str(video_path),
                    filename="VidMorph_video.mp4",
                ),
                caption=f"🎬 {title[:900]}",
                supports_streaming=True,
            )

            await status_message.delete()

        except Exception:
            logger.exception("Ошибка скачивания, конвертации или отправки")

            try:
                await status_message.edit_text(
                    "😕 <b>Не получилось подготовить или отправить видео.</b>\n\n"
                    "Возможно, ссылка недоступна, платформа ограничила "
                    "скачивание или файл слишком большой. Попробуй позже."
                )
            except TelegramAPIError:
                pass

        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


# =====================================
# ЗАПУСК БОТА
# =====================================

async def main():
    logger.info("VidMorph запускается")

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
