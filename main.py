import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

# ============================================================
# SETTINGS
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()
SUBSCRIBE_URL = os.getenv("SUBSCRIBE_URL", "").strip()
DONATE_STARS = int(os.getenv("DONATE_STARS", "50"))

MAX_FILE_SIZE = 47 * 1024 * 1024
MAX_CONCURRENT_DOWNLOADS = 1
TEMP_PREFIX = "vidmorph_"
TEMP_MAX_AGE_HOURS = 6
FFMPEG_TIMEOUT = 600

SUPPORTED_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "instagram.com",
    "tiktok.com",
)

if not BOT_TOKEN:
    raise RuntimeError("Не задана переменная BOT_TOKEN")
if not CHANNEL_ID:
    raise RuntimeError("Не задана переменная CHANNEL_ID")
if not SUBSCRIBE_URL:
    raise RuntimeError("Не задана переменная SUBSCRIBE_URL")
if DONATE_STARS < 1:
    raise RuntimeError("DONATE_STARS должна быть больше нуля")

# ============================================================
# LOGGING AND BOT
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
# TEMPORARY FILE CLEANUP
# ============================================================

def cleanup_old_temp_dirs():
    """Remove stale temporary folders left after a crash or forced restart."""
    temp_root = Path(tempfile.gettempdir())
    cutoff = time.time() - TEMP_MAX_AGE_HOURS * 3600

    for folder in temp_root.glob(f"{TEMP_PREFIX}*"):
        try:
            if folder.is_dir() and folder.stat().st_mtime < cutoff:
                shutil.rmtree(folder, ignore_errors=True)
                logger.info("Удалена старая временная папка: %s", folder.name)
        except OSError:
            logger.warning("Не удалось очистить папку: %s", folder.name)


# ============================================================
# KEYBOARDS
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
# SUBSCRIPTION CHECK
# ============================================================

async def is_subscribed(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(
            chat_id=CHANNEL_ID,
            user_id=user_id,
        )
        return member.status in ("creator", "administrator", "member")

    except TelegramAPIError:
        logger.exception(
            "Не удалось проверить подписку. "
            "Проверь CHANNEL_ID и доступ бота к каналу."
        )
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
# URL VALIDATION
# ============================================================

def extract_video_url(text: str) -> str | None:
    match = re.search(r"https?://[^\s<>]+", text or "")
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
# DOWNLOAD VIDEO
# ============================================================

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".webm", ".mov", ".m4v", ".flv", ".avi"
}


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
        "retries": 2,
        "fragment_retries": 2,
        "socket_timeout": 30,
        "overwrites": True,
    }

    deno_path = shutil.which("deno")
    if deno_path:
        options["js_runtimes"] = {
            "deno": {"path": deno_path}
        }

    logger.info("Начинаю скачивание видео")

    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([url])

    candidates = [
        path
        for path in folder.iterdir()
        if path.is_file()
        and path.suffix.lower() in VIDEO_EXTENSIONS
        and not path.name.endswith((".part", ".ytdl"))
    ]

    if not candidates:
        raise RuntimeError("Не удалось найти скачанный видеофайл.")

    video_path = max(candidates, key=lambda path: path.stat().st_size)

    if video_path.stat().st_size == 0:
        raise RuntimeError("Скачанный файл пустой.")

    if video_path.stat().st_size > MAX_FILE_SIZE:
        raise RuntimeError(
            "Видео превышает лимит отправки бота (примерно 47 MiB). "
            "Попробуй видео меньшего размера."
        )

    logger.info(
        "Видео скачано: %.2f MiB",
        video_path.stat().st_size / (1024 * 1024),
    )

    return video_path


# ============================================================
# VIDEO INSPECTION
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
        timeout=45,
    )

    return json.loads(result.stdout)


# ============================================================
# PREPARE VIDEO FOR TELEGRAM
# ============================================================

def make_telegram_compatible(video_path: Path) -> Path:
    """
    H.264/AAC MP4 is sent without conversion.
    H.264/AAC in another container is remuxed without quality loss.
    Other codecs are converted only when necessary.
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
    audio_codecs = [
        stream.get("codec_name") for stream in audios
    ]

    audio_ok = all(codec == "aac" for codec in audio_codecs)

    # Most efficient path: send the original file as-is.
    if (
        video_path.suffix.lower() == ".mp4"
        and video_codec == "h264"
        and audio_ok
    ):
        logger.info("Подготовка не требуется.")
        return video_path

    output_path = video_path.with_name("video_telegram.mp4")

    # Remux compatible codecs without re-encoding.
    if video_codec == "h264" and audio_ok:
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-i", str(video_path),
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-c", "copy",
            "-movflags", "+faststart",
            str(output_path),
        ]
    else:
        # Lightweight conversion if the original codecs are unsuitable.
        # This can reduce the original quality.
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-i", str(video_path),
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-vf",
            "scale=w='min(1280,iw)':h='min(720,ih)':"
            "force_original_aspect_ratio=decrease:force_divisible_by=2",
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

    logger.info("Выполняется обработка видео.")

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=FFMPEG_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "Обработка видео заняла слишком много времени."
        ) from exc

    if result.returncode != 0:
        logger.error(
            "FFmpeg завершился с кодом %s: %s",
            result.returncode,
            (result.stderr or "")[-2000:],
        )

        if result.returncode == -9:
            raise RuntimeError(
                "Сервер остановил обработку видео. "
                "Возможно, не хватает оперативной памяти. "
                "Попробуй ролик меньшего размера."
            )

        raise RuntimeError("Не удалось подготовить видео к отправке.")

    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise RuntimeError("Итоговый видеофайл не создан.")

    if output_path.stat().st_size > MAX_FILE_SIZE:
        raise RuntimeError(
            "Подготовленное видео превышает лимит отправки. "
            "Попробуй ролик меньшего размера."
        )

    return output_path


# ============================================================
# START COMMAND
# ============================================================

@dp.message(CommandStart())
async def start_handler(message: Message):
    if not await require_subscription(message):
        return

    await message.answer(
        "👋 Привет! Это VidMorph.\n\n"
        "Отправь ссылку на видео из YouTube, Instagram или TikTok. "
        "Я попробую скачать ролик и отправить его прямо сюда.\n\n"
        "Доступность скачивания зависит от ограничений источника.",
        reply_markup=main_keyboard(),
    )


# ============================================================
# SUBSCRIPTION BUTTON
# ============================================================

@dp.callback_query(F.data == "check_subscription")
async def check_subscription_handler(callback: CallbackQuery):
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
# TELEGRAM STARS DONATIONS
# ============================================================

@dp.callback_query(F.data == "donate")
async def donate_handler(callback: CallbackQuery):
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


@dp.message(Command("donate"))
async def donate_command_handler(message: Message):
    if not await require_subscription(message):
        return

    await message.answer(
        "⭐ Нажми кнопку ниже, чтобы поддержать проект.",
        reply_markup=main_keyboard(),
    )


@dp.pre_checkout_query()
async def pre_checkout_handler(query: PreCheckoutQuery):
    await query.answer(ok=True)


@dp.message(F.successful_payment)
async def successful_payment_handler(message: Message):
    payment = message.successful_payment

    if payment and payment.currency == "XTR":
        logger.info(
            "Получен донат: %s Stars",
            payment.total_amount,
        )
        await message.answer(
            "⭐ Спасибо за поддержку VidMorph!"
        )


# ============================================================
# VIDEO HANDLER
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

    status_message = await message.answer("⏳ Скачиваю видео...")

    async with download_semaphore:
        temp_dir = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX))

        try:
            video_path = await asyncio.to_thread(
                download_video,
                url,
                temp_dir,
            )

            prepared_path = await asyncio.to_thread(
                make_telegram_compatible,
                video_path,
            )

            await status_message.edit_text("📤 Отправляю видео...")

            await message.answer_video(
                video=FSInputFile(
                    prepared_path,
                    filename="video.mp4",
                ),
                supports_streaming=True,
                caption="✅ Готово!",
            )

            await status_message.delete()

        except Exception as exc:
            logger.exception("Ошибка скачивания, обработки или отправки")

            # Avoid exposing internal server paths in user-facing messages.
            error_text = str(exc)

            if "предел" in error_text.lower() or "лимит" in error_text.lower():
                user_error = error_text
            elif "памяти" in error_text.lower():
                user_error = error_text
            elif "слишком много времени" in error_text.lower():
                user_error = error_text
            else:
                user_error = (
                    "Не удалось обработать это видео. "
                    "Проверь ссылку или попробуй другой ролик."
                )

            try:
                await status_message.edit_text(
                    f"❌ {user_error}"
                )
            except TelegramAPIError:
                pass

        finally:
            # Remove downloaded and converted files after success or error.
            shutil.rmtree(temp_dir, ignore_errors=True)


# ============================================================
# STARTUP
# ============================================================

async def main():
    cleanup_old_temp_dirs()

    logger.info("VidMorph запускается")
    logger.info("FFmpeg: %s", shutil.which("ffmpeg"))
    logger.info("FFprobe: %s", shutil.which("ffprobe"))
    logger.info("Deno: %s", shutil.which("deno"))

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        logger.warning("FFmpeg или FFprobe не найден. Проверь Dockerfile.")

    await bot.delete_webhook(drop_pending_updates=False)

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
