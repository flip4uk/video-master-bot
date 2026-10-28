import asyncio
import logging
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from aiogram import Bot, Dispatcher, F
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
# SETTINGS
# =====================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()
SUBSCRIBE_URL = os.getenv("SUBSCRIBE_URL", "").strip()

# Telegram cloud Bot API upload limit may restrict large files.
# This conservative limit can be adjusted to your actual environment.
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

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)


# =====================================
# KEYBOARDS
# =====================================

def main_keyboard():
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
            [
                InlineKeyboardButton(
                    text="💝 Поддержать проект",
                    callback_data="donate",
                )
            ],
        ]
    )


def donation_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"⭐ {amount} Stars",
                    callback_data=f"donate_{amount}",
                )
            ]
            for amount in DONATION_AMOUNTS
        ]
    )


# =====================================
# SUBSCRIPTION
# =====================================

async def is_subscribed(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(
            chat_id=CHANNEL_ID,
            user_id=user_id,
        )

        status = member.status

        if status in {"creator", "administrator", "member"}:
            return True

        # Restricted users may still be channel members.
        if status == "restricted":
            return bool(getattr(member, "is_member", False))

        return False

    except TelegramAPIError:
        logger.exception("Не удалось проверить подписку")
        return False


async def require_subscription(message: Message) -> bool:
    if not message.from_user:
        return False

    if await is_subscribed(message.from_user.id):
        return True

    await message.answer(
        "🔒 Для скачивания видео необходимо подписаться на канал.\n\n"
        "1. Нажми «Подписаться на канал».\n"
        "2. Подпишись.\n"
        "3. Нажми «Проверить подписку».\n\n"
        "Поддержать проект можно и без подписки.",
        reply_markup=main_keyboard(),
    )
    return False


# =====================================
# URL VALIDATION
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
# VIDEO DOWNLOAD
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

    # The final extension may change after merging streams.
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
# START
# =====================================

@dp.message(CommandStart())
async def start_handler(message: Message):
    await message.answer(
        "👋 Привет! Это бот для скачивания видео.\n\n"
        "Поддерживаются ссылки на YouTube, Instagram и TikTok.\n"
        "Для скачивания требуется подписка на канал.\n\n"
        "💝 Если хочешь поддержать развитие проекта, "
        "нажми кнопку доната.",
        reply_markup=main_keyboard(),
    )


# =====================================
# SUBSCRIPTION BUTTON
# =====================================

@dp.callback_query(F.data == "check_subscription")
async def check_subscription_handler(callback: CallbackQuery):
    if await is_subscribed(callback.from_user.id):
        await callback.answer("Подписка подтверждена!")
        if callback.message:
            await callback.message.answer(
                "✅ Подписка подтверждена!\n"
                "Теперь отправь ссылку на видео."
            )
    else:
        await callback.answer(
            "Подписка не найдена. Подпишись и попробуй снова.",
            show_alert=True,
        )


# =====================================
# DONATION MENU
# =====================================

@dp.callback_query(F.data == "donate")
async def donate_handler(callback: CallbackQuery):
    await callback.answer()

    if callback.message:
        await callback.message.answer(
            "💝 Спасибо, что хочешь поддержать проект!\n\n"
            "Выбери сумму добровольного доната в Telegram Stars:",
            reply_markup=donation_keyboard(),
        )


@dp.callback_query(F.data.startswith("donate_"))
async def donate_amount_handler(callback: CallbackQuery):
    try:
        amount = int(callback.data.split("_", 1)[1])
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
            title=f"Донат: {DONATION_AMOUNTS[amount]}",
            description=(
                "Добровольная поддержка проекта "
                "по разработке и обслуживанию Telegram-бота."
            ),
            payload=f"donation_{amount}_{uuid.uuid4().hex}",
            provider_token="",
            currency="XTR",
            prices=[
                LabeledPrice(
                    label="Добровольный донат",
                    amount=amount,
                )
            ],
        )
    except TelegramAPIError:
        logger.exception("Не удалось выставить счёт Stars")
        if callback.message:
            await callback.message.answer(
                "Не удалось создать платёж. Попробуй позже."
            )


# =====================================
# TELEGRAM STARS PAYMENT
# =====================================

@dp.pre_checkout_query()
async def pre_checkout_handler(query: PreCheckoutQuery):
    # Confirm the requested amount and currency before accepting payment.
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
        "💝 Спасибо за поддержку проекта!\n\n"
        f"Получено: ⭐ {payment.total_amount} Stars.\n"
        "Твой вклад помогает поддерживать работу бота."
    )


# =====================================
# PAYMENT SUPPORT
# =====================================

@dp.message(Command("paysupport"))
async def payment_support_handler(message: Message):
    await message.answer(
        "Если у тебя возник вопрос по донату или платёж прошёл некорректно, "
        "напиши администратору проекта.\n\n"
        "Укажи дату платежа и сумму. Не отправляй пароли или коды."
    )


# =====================================
# VIDEO HANDLER
# =====================================

@dp.message(F.text)
async def video_handler(message: Message):
    text = message.text or ""
    url = extract_url(text)

    if not url:
        await message.answer(
            "Отправь ссылку на видео из YouTube, Instagram или TikTok.",
            reply_markup=main_keyboard(),
        )
        return

    if not is_supported_url(url):
        await message.answer(
            "❌ Поддерживаются ссылки только на YouTube, Instagram и TikTok."
        )
        return

    if not await require_subscription(message):
        return

    status_message = await message.answer(
        "⏳ Скачиваю видео в лучшем доступном качестве..."
    )

    async with download_semaphore:
        temp_dir = Path(tempfile.mkdtemp(prefix="video_bot_"))

        try:
            video_path, title = await asyncio.to_thread(
                download_video,
                url,
                str(temp_dir),
            )

            file_size = video_path.stat().st_size

            if file_size > MAX_FILE_SIZE:
                await status_message.edit_text(
                    "❌ Файл получился слишком большим для отправки "
                    "в текущей конфигурации бота.\n\n"
                    "Попробуй видео меньшего размера."
                )
                return

            await status_message.edit_text(
                "📤 Видео скачано. Отправляю файл..."
            )

            # Send as a document to avoid Telegram video processing.
            await message.answer_document(
                document=FSInputFile(
                    path=str(video_path),
                    filename=video_path.name,
                ),
                caption=f"🎬 {title[:900]}",
            )

            await status_message.delete()

        except Exception:
            logger.exception("Ошибка скачивания или отправки")

            try:
                await status_message.edit_text(
                    "❌ Не получилось скачать или отправить видео.\n\n"
                    "Возможно, ссылка недоступна, видео закрыто, "
                    "платформа временно ограничила скачивание "
                    "или файл слишком большой. Попробуй позже."
                )
            except TelegramAPIError:
                pass

        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


# =====================================
# RUN BOT
# =====================================

async def main():
    logger.info("Бот запускается")

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
