import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    LabeledPrice,
    PreCheckoutQuery,
    FSInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.exceptions import TelegramAPIError


# ============================================================
# НАСТРОЙКИ
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()
SUBSCRIBE_URL = os.getenv("SUBSCRIBE_URL", "").strip()

DONATE_STARS = int(os.getenv("DONATE_STARS", "50"))

# Telegram Bot API обычно принимает видео размером до 50 MB.
# Оставляем запас, чтобы не упереться в ограничение.
MAX_FILE_SIZE = 49 * 1024 * 1024

MAX_CONCURRENT_DOWNLOADS = 1
DOWNLOAD_TIMEOUT = 600
CONVERT_TIMEOUT = 900

SUPPORTED_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "instagram.com",
    "tiktok.com",
)

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN")

if not CHANNEL_ID:
    raise RuntimeError("Не задан CHANNEL_ID")

if not SUBSCRIBE_URL:
    raise RuntimeError("Не задан SUBSCRIBE_URL")


# ============================================================
# ЛОГИ И ЗАПУСК
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("VidMorph")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)


# ============================================================
# КНОПКИ
# ============================================================

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


def main_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⭐ Поддержать проект",
                    callback_data="donate",
                )
            ]
        ]
    )


# ============================================================
# ПРОВЕРКА ПОДПИСКИ
# ============================================================

async def is_subscribed(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(
            chat_id=CHANNEL_ID,
            user_id=user_id,
        )

        return member.status in ("creator", "administrator", "member")

    except TelegramAPIError as exc:
        logger.error("Ошибка проверки подписки: %s", exc)
        return False


async def require_subscription(message: Message) -> bool:
    if not message.from_user:
        return False

    if await is_subscribed(message.from_user.id):
        return True

    await message.answer(
        "Чтобы пользоваться VidMorph, сначала подпишись на наш канал.",
        reply_markup=subscription_keyboard(),
    )
    return False


# ============================================================
# ПРОВЕРКА ССЫЛКИ
# ============================================================

def extract_video_url(text: str) -> str | None:
    if not text:
        return None

    match = re.search(r"https?://[^\s<>]+", text)

    if not match:
        return None

    url = match.group(0).rstrip(".,!?)]}\"'")

    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()

        if parsed.scheme not in ("http", "https"):
            return None

        if not any(
            hostname == domain or hostname.endswith("." + domain)
            for domain in SUPPORTED_DOMAINS
        ):
            return None

        return url

    except ValueError:
        return None


# ============================================================
# СКАЧИВАНИЕ ВИДЕО
# ============================================================

def download_video(url: str, folder: Path) -> Path:
    output_template = str(folder / "video.%(ext)s")

    options = {
        "format": (
            "best[ext=mp4]/"
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
            "bestvideo+bestaudio/best"
        ),
        "outtmpl": output_template,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": False,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "restrictfilenames": True,
        "overwrites": True,
    }

    # Deno помогает yt-dlp получать некоторые форматы YouTube.
    # Если Deno не установлен, yt-dlp попробует работать без него.
    deno_path = shutil.which("deno")
    if deno_path:
        options["js_runtimes"] = {"deno": {"path": deno_path}}

    logger.info("Начинаю скачивание: %s", url)

    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([url])

    candidates = [
        path
        for path in folder.iterdir()
        if path.is_file()
        and path.name.startswith("video.")
        and path.suffix.lower() not in (".part", ".ytdl", ".json")
    ]

    if not candidates:
        raise RuntimeError(
            "Скачивание завершилось, но видеофайл не найден."
        )

    # При наличии нескольких файлов выбираем самый большой.
    video_path = max(candidates, key=lambda p: p.stat().st_size)

    if video_path.stat().st_size == 0:
        raise RuntimeError("Скачанный файл пустой.")

    logger.info(
        "Видео скачано: %s, %.2f MB",
        video_path.name,
        video_path.stat().st_size / (1024 * 1024),
    )

    return video_path


# ============================================================
# ПОДГОТОВКА ВИДЕО ДЛЯ TELEGRAM
# ============================================================

def probe_video(video_path: Path) -> dict:
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries",
            "stream=codec_type,codec_name,width,height",
            "-of", "json",
            str(video_path),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )

    return json.loads(result.stdout)


def make_telegram_compatible(video_path: Path) -> Path:
    """
    Если видео уже H.264/AAC в MP4 — не перекодируем.
    Если можно обойтись без потери качества — перепаковываем.
    Перекодирование используем только при необходимости.
    """

    info = probe_video(video_path)
    streams = info.get("streams", [])

    videos = [
        stream for stream in streams
        if stream.get("codec_type") == "video"
    ]
    audios = [
        stream for stream in streams
        if stream.get("codec_type") == "audio"
    ]

    if not videos:
        raise RuntimeError("В файле не обнаружена видеодорожка.")

    video_codec = videos[0].get("codec_name")
    audio_codecs = [stream.get("codec_name") for stream in audios]

    compatible = (
        video_path.suffix.lower() == ".mp4"
        and video_codec == "h264"
        and all(codec == "aac" for codec in audio_codecs)
    )

    if compatible:
        logger.info("Видео уже совместимо с Telegram — пропускаю FFmpeg.")
        return video_path

    output_path = video_path.with_name("video_telegram.mp4")

    # Если кодеки подходят, просто перепаковываем контейнер.
    # Качество при этом не теряется.
    can_remux = (
        video_codec == "h264"
        and all(codec == "aac" for codec in audio_codecs)
    )

    if can_remux:
        command = [
            "ffmpeg",
            "-y",
            "-i", str(video_path),
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-c", "copy",
            "-movflags", "+faststart",
            str(output_path),
        ]
    else:
        # Более лёгкое перекодирование для ограниченной памяти.
        # Оно может немного снизить качество исходника.
        command = [
            "ffmpeg",
            "-y",
            "-i", str(video_path),
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-vf",
            "scale=1280:720:force_original_aspect_ratio=decrease:force_divisible_by=2",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "28",
            "-threads", "1",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "96k",
            "-ac", "2",
            "-threads:a", "1",
            "-movflags", "+faststart",
            str(output_path),
        ]

    logger.info(
        "Подготавливаю видео. Исходный кодек: %s; перепаковка: %s",
        video_codec,
        can_remux,
    )

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=CONVERT_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "Подготовка видео заняла слишком много времени."
        ) from exc

    if result.returncode != 0:
        error_text = (result.stderr or "")[-2500:]
        logger.error("Ошибка FFmpeg: %s", error_text)

        # Не отправляем неподготовленный файл как заведомо совместимый.
        raise RuntimeError(
            "Не удалось подготовить видео для просмотра в Telegram. "
            "Возможно, на хостинге не хватает памяти."
        )

    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise RuntimeError("FFmpeg не создал итоговый файл.")

    logger.info(
        "Подготовка завершена: %.2f MB",
        output_path.stat().st_size / (1024 * 1024),
    )

    return output_path


# ============================================================
# /START
# ============================================================

@dp.message(CommandStart())
async def start_handler(message: Message):
    if not await require_subscription(message):
        return

    await message.answer(
        "👋 Привет! Это VidMorph.\n\n"
        "Отправь ссылку на видео из YouTube, Instagram или TikTok.\n"
        "Я попробую скачать его и отправить сюда как видео, "
        "чтобы его можно было смотреть прямо в Telegram.\n\n"
        "Важно: доступность скачивания зависит от источника, "
        "ограничений платформы и размера видео.",
        reply_markup=main_keyboard(),
    )


# ============================================================
# ПРОВЕРКА ПОДПИСКИ ПО КНОПКЕ
# ============================================================

@dp.callback_query(F.data == "check_subscription")
async def check_subscription_handler(callback: CallbackQuery):
    if not callback.from_user:
        await callback.answer()
        return

    if await is_subscribed(callback.from_user.id):
        await callback.answer("Подписка подтверждена!")

        if callback.message:
            await callback.message.answer(
                "✅ Подписка подтверждена! Теперь отправь ссылку на видео."
            )
    else:
        await callback.answer(
            "Не вижу подписку. Подпишись и попробуй снова.",
            show_alert=True,
        )


# ============================================================
# ДОНАТЫ TELEGRAM STARS
# ============================================================

@dp.callback_query(F.data == "donate")
async def donate_handler(callback: CallbackQuery):
    if not callback.from_user:
        await callback.answer()
        return

    if not await is_subscribed(callback.from_user.id):
        await callback.answer(
            "Сначала подпишись на канал.",
            show_alert=True,
        )
        if callback.message:
            await callback.message.answer(
                "Для доступа к боту нужна подписка на канал.",
                reply_markup=subscription_keyboard(),
            )
        return

    await callback.answer()

    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="Поддержать VidMorph",
        description="Добровольная поддержка развития бота.",
        payload=f"vidmorph_donation_{callback.from_user.id}",
        currency="XTR",
        prices=[
            LabeledPrice(
                label="Поддержка VidMorph",
                amount=DONATE_STARS,
            )
        ],
    )


@dp.pre_checkout_query()
async def pre_checkout_handler(query: PreCheckoutQuery):
    await query.answer(ok=True)


@dp.message(F.successful_payment)
async def successful_payment_handler(message: Message):
    payment = message.successful_payment

    if payment and payment.currency == "XTR":
        await message.answer(
            "⭐ Спасибо за поддержку VidMorph! "
            "Твой вклад помогает развивать проект."
        )
        logger.info(
            "Получен донат Telegram Stars: %s XTR",
            payment.total_amount,
        )


@dp.message(Command("donate"))
async def donate_command_handler(message: Message):
    if not await require_subscription(message):
        return

    await message.answer(
        "⭐ Нажми кнопку ниже, чтобы поддержать проект.",
        reply_markup=main_keyboard(),
    )


# ============================================================
# ОБРАБОТКА ССЫЛОК НА ВИДЕО
# ============================================================

@dp.message(F.text)
async def video_handler(message: Message):
    if not await require_subscription(message):
        return

    url = extract_video_url(message.text or "")

    if not url:
        await message.answer(
            "Пришли ссылку на видео из YouTube, Instagram или TikTok."
        )
        return

    status_message = await message.answer(
        "⏳ Скачиваю видео. Пожалуйста, подожди..."
    )

    async with download_semaphore:
        temp_dir = Path(tempfile.mkdtemp(prefix="vidmorph_"))

        try:
            # yt-dlp и FFmpeg работают в отдельных потоках,
            # чтобы бот продолжал отвечать другим пользователям.
            video_path = await asyncio.wait_for(
                asyncio.to_thread(download_video, url, temp_dir),
                timeout=DOWNLOAD_TIMEOUT,
            )

            if video_path.stat().st_size > MAX_FILE_SIZE:
                await status_message.edit_text(
                    "❌ Видео превышает лимит отправки бота "
                    "(примерно 49 MB). Попробуй ссылку на видео "
                    меньшего размера."
                )
                return

            prepared_path = await asyncio.to_thread(
                make_telegram_compatible,
                video_path,
            )

            if prepared_path.stat().st_size > MAX_FILE_SIZE:
                await status_message.edit_text(
                    "❌ Подготовленное видео превышает лимит "
                    "отправки Telegram (примерно 49 MB)."
                )
                return

            await status_message.edit_text(
                "📤 Отправляю видео в Telegram..."
            )

            await message.answer_video(
                video=FSInputFile(
                    prepared_path,
                    filename="video.mp4",
                ),
                supports_streaming=True,
                caption="✅ Готово!",
            )

            await status_message.delete()

        except asyncio.TimeoutError:
            logger.exception("Истекло время ожидания скачивания")
            await status_message.edit_text(
                "⌛ Скачивание заняло слишком много времени. "
                "Попробуй ещё раз."
            )

        except Exception as exc:
            logger.exception("Ошибка обработки видео")

            error_message = str(exc)
            if len(error_message) > 500:
                error_message = error_message[:500] + "..."

            try:
                await status_message.edit_text(
                    "❌ Не удалось обработать видео.\n\n"
                    "Возможные причины: ограничение источника, "
                    "недоступная ссылка, неподдерживаемый формат "
                    "или нехватка памяти на хостинге.\n\n"
                    f"Подробности: {error_message}"
                )
            except TelegramAPIError:
                pass

        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


# ============================================================
# ЗАПУСК
# ============================================================

async def main():
    logger.info("VidMorph запускается")
    logger.info("FFmpeg: %s", shutil.which("ffmpeg"))
    logger.info("FFprobe: %s", shutil.which("ffprobe"))
    logger.info("Deno: %s", shutil.which("deno"))

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        logger.warning(
            "FFmpeg или FFprobe не найден. Проверь Dockerfile."
        )

    await bot.delete_webhook(drop_pending_updates=False)

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

