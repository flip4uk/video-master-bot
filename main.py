import asyncio
import html
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlparse

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    TelegramObject,
)
from aiogram.utils.chat_action import ChatActionSender
from aiogram.utils.keyboard import InlineKeyboardBuilder
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadCancelled, DownloadError

# ================================================================
# НАСТРОЙКИ
# ================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "FetchRemixDeliverBot").strip().lstrip("@")

CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@ads_247_ru").strip()
if not CHANNEL_USERNAME.startswith("@"):
    CHANNEL_USERNAME = "@" + CHANNEL_USERNAME
CHANNEL_URL = f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}"

ADMIN_CONTACT = os.getenv("ADMIN_CONTACT", CHANNEL_USERNAME).strip()
COOKIES_FILE = os.getenv("COOKIES_FILE", "cookies.txt").strip()

MAX_DURATION = int(os.getenv("MAX_DURATION", "1200"))
MAX_PARALLEL = int(os.getenv("MAX_PARALLEL", "3"))
MAX_UPLOAD_BYTES = 49 * 1024 * 1024
QUALITY_LADDER = (720, 480, 360)
MAX_STARS = 10_000
DONATE_PRESETS = (10, 50, 100, 250, 500, 1000)

# Качество перекодирования. CRF меньше = лучше качество и больше файл.
FFMPEG_CRF = os.getenv("FFMPEG_CRF", "21")
FFMPEG_PRESET = os.getenv("FFMPEG_PRESET", "veryfast")

PLATFORMS: dict[str, tuple[str, str]] = {
    "youtube.com": ("YouTube", "▶️"),
    "youtu.be": ("YouTube", "▶️"),
    "instagram.com": ("Instagram", "📸"),
    "tiktok.com": ("TikTok", "🎵"),
}
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
log = logging.getLogger("fetchremix")

WELCOME = (
    "👋 <b>Привет, {name}!</b>\n\n"
    "Я скачиваю видео из <b>YouTube</b>, <b>Instagram</b> и <b>TikTok</b> "
    "в лучшем доступном качестве — быстро и бесплатно.\n\n"
    "<b>Как это работает</b>\n"
    "1️⃣ Скопируйте ссылку на видео\n"
    "2️⃣ Отправьте её мне\n"
    "3️⃣ Получите готовый файл ⚡\n\n"
    "📎 <i>Просто пришлите ссылку прямо сюда 👇</i>"
)
HELP = (
    "📖 <b>Как пользоваться ботом</b>\n\n"
    "Отправьте ссылку на видео — остальное я сделаю сам.\n\n"
    "<b>Поддерживается</b>\n"
    "▶️ <b>YouTube</b> — обычные ролики и Shorts\n"
    "📸 <b>Instagram</b> — Reels и видео-посты\n"
    "🎵 <b>TikTok</b> — любые публичные видео\n\n"
    "<b>Важно знать</b>\n"
    "• Видео должно быть публичным\n"
    "• Размер файла — до 50 МБ, качество подбирается автоматически\n"
    f"• Длительность — до {MAX_DURATION // 60} мин\n"
    "• Скачивайте только тот контент, на который у вас есть права\n\n"
    "💛 Бот бесплатный. Если он вам полезен — можно поддержать проект звёздами ⭐"
)
DONATE_TEXT = (
    "⭐ <b>Поддержать проект</b>\n\n"
    "Бот полностью бесплатный, а донаты — <b>исключительно добровольные</b>. "
    "Они помогают оплачивать сервер и развивать бота 💛\n\n"
    "Выберите сумму в Telegram Stars или введите свою 👇"
)
CUSTOM_AMOUNT_TEXT = (
    "✏️ <b>Своя сумма</b>\n\n"
    f"Отправьте количество звёзд числом — от 1 до {MAX_STARS:,}.".replace(",", " ")
)
SUBSCRIBE_TEXT = (
    "🔐 <b>Один шаг до скачивания</b>\n\n"
    "Чтобы пользоваться ботом, подпишитесь на наш канал "
    f"<b>{html.escape(CHANNEL_USERNAME)}</b> — там новости и обновления.\n\n"
    "После подписки нажмите <b>«✅ Я подписался»</b> 👇"
)
NOT_A_LINK = (
    "🔗 <b>Пришлите ссылку на видео</b>\n\n"
    "Я работаю с YouTube, Instagram и TikTok.\n"
    "<i>Например: https://www.tiktok.com/@user/video/123…</i>"
)
UNSUPPORTED = (
    "🤔 <b>Эта ссылка не поддерживается</b>\n\n"
    "Я умею скачивать только с <b>YouTube</b>, <b>Instagram</b> и <b>TikTok</b>."
)


# ================================================================
# КЛАВИАТУРЫ
# ================================================================
def main_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📖 Как пользоваться", callback_data="help")
    kb.button(text="⭐ Поддержать", callback_data="donate")
    kb.button(text="📢 Наш канал", url=CHANNEL_URL)
    kb.adjust(2, 1)
    return kb.as_markup()


def back_kb(target: str = "menu") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data=target)
    return kb.as_markup()


def subscribe_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📢 Подписаться на канал", url=CHANNEL_URL)
    kb.button(text="✅ Я подписался", callback_data="check_sub")
    kb.adjust(1)
    return kb.as_markup()


def donate_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for amount in DONATE_PRESETS:
        kb.button(text=f"⭐ {amount}", callback_data=f"pay:{amount}")
    kb.button(text="✏️ Своя сумма", callback_data="pay_custom")
    kb.button(text="⬅️ Назад", callback_data="menu")
    kb.adjust(3, 3, 1, 1)
    return kb.as_markup()


def after_download_kb() -> InlineKeyboardMarkup:
    share_text = "Скачивай видео из YouTube, Instagram и TikTok прямо в Telegram 🎬"
    share_url = (
        "https://t.me/share/url"
        f"?url={quote(f'https://t.me/{BOT_USERNAME}', safe='')}"
        f"&text={quote(share_text)}"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="⭐ Поддержать бота", callback_data="donate")
    kb.button(text="🔗 Поделиться", url=share_url)
    kb.adjust(2)
    return kb.as_markup()


# ================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ================================================================
async def safe_edit(
    message: Message, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            log.warning("edit_text failed: %s", e)


async def show_screen(
    call: CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    msg = call.message
    if msg is None:
        return
    if getattr(msg, "text", None):
        await safe_edit(msg, text, reply_markup)
    else:
        await msg.answer(text, reply_markup=reply_markup)


async def is_subscribed(bot: Bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_USERNAME, user_id)
    except TelegramAPIError as e:
        log.error(
            "Не удалось проверить подписку (бот должен быть администратором канала %s): %s",
            CHANNEL_USERNAME, e,
        )
        return False
    if member.status in (
        ChatMemberStatus.CREATOR,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.MEMBER,
    ):
        return True
    return member.status == ChatMemberStatus.RESTRICTED and bool(
        getattr(member, "is_member", False)
    )


def detect_platform(url: str) -> tuple[str, str] | None:
    host = (urlparse(url).hostname or "").lower()
    for domain, info in PLATFORMS.items():
        if host == domain or host.endswith("." + domain):
            return info
    return None


def extract_url(message: Message) -> str | None:
    text = message.text or message.caption or ""
    url: str | None = None
    for ent in message.entities or []:
        if ent.type == "text_link" and ent.url:
            url = ent.url
            break
        if ent.type == "url":
            url = ent.extract_from(text)
            break
    if url is None:
        m = URL_RE.search(text)
        url = m.group(0).rstrip(".,);]") if m else None
    if url and not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    return url


# ================================================================
# СКАЧИВАНИЕ И ПОДГОТОВКА ВИДЕО
# ================================================================
@dataclass
class DownloadResult:
    path: Path
    title: str
    duration: int | None
    width: int | None
    height: int | None


class DownloadFailed(Exception):
    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


def _humanize_error(raw: str) -> str:
    low = raw.lower()
    if "unsupported url" in low:
        return "🤔 <b>Ссылка не поддерживается.</b>\nПроверьте, что она ведёт на видео."
    if "no video" in low or "there is no video" in low:
        return "🖼 <b>В этой публикации нет видео.</b>"
    if "age" in low and ("restrict" in low or "confirm your age" in low):
        return "🔞 <b>Видео с возрастным ограничением</b> — скачать его не получится."
    if any(w in low for w in ("private", "login", "log in", "sign in", "cookies", "not a bot")):
        return (
            "🔒 <b>Не удалось получить доступ к видео.</b>\n"
            "Возможно, оно приватное или платформа временно требует авторизацию. "
            "Попробуйте позже или другую ссылку."
        )
    if any(w in low for w in ("unavailable", "not available", "removed", "deleted", "404")):
        return "🚫 <b>Видео недоступно или удалено.</b>"
    return "😔 <b>Не удалось скачать видео.</b>\nПопробуйте ещё раз чуть позже."


def _duration_filter(info: dict, *, incomplete: bool = False) -> None:
    duration = info.get("duration")
    if duration and duration > MAX_DURATION:
        raise DownloadCancelled(f"too_long:{int(duration)}")
    return None


def _format_for(height: int) -> str:
    # Приоритет H.264/AVC с M4A-аудио; если платформа не даёт такого
    # потока, скачиваем доступный формат и при необходимости перекодируем.
    h = f"[height<={height}]"
    h264 = "[vcodec~='^(avc1|h264)']"
    return (
        f"bv*{h}{h264}+ba[ext=m4a]"
        f"/b{h}{h264}[ext=mp4]"
        f"/bv*{h}{h264}+ba"
        f"/b{h}{h264}"
        f"/bv*{h}+ba/b{h}"
    )


def _find_file(info: dict[str, Any], workdir: Path) -> Path | None:
    for item in info.get("requested_downloads") or []:
        p = item.get("filepath") or item.get("_filename")
        if p and Path(p).is_file():
            return Path(p)
    candidates = [
        p for p in workdir.iterdir()
        if p.is_file() and p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}
        and p.name != "cookies.txt"
    ]
    return max(candidates, key=lambda p: p.stat().st_size, default=None)


def _probe_media(path: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe не найден")
    result = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_streams", "-show_format",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, timeout=45, check=True,
    )
    return json.loads(result.stdout)


def _transcode_or_remux(path: Path, workdir: Path, max_bytes: int) -> Path:
    """
    Возвращает MP4 с H.264/yuv420p и AAC.
    Совместимые потоки копируются без потери качества; неподходящие перекодируются.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not shutil.which("ffprobe"):
        raise DownloadFailed(
            "⚠️ <b>На сервере не найден FFmpeg/FFprobe.</b>\n"
            "Проверьте сборку Docker-контейнера."
        )

    try:
        data = _probe_media(path)
    except (subprocess.SubprocessError, OSError, ValueError, RuntimeError) as e:
        log.warning("Не удалось определить кодеки: %s", e)
        # Не отправляем непроверенный файл как гарантированно совместимый.
        raise DownloadFailed(
            "😔 <b>Не удалось проверить формат видео.</b>\nПопробуйте другую ссылку."
        ) from e

    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise DownloadFailed("❌ <b>В скачанном файле не найден видеопоток.</b>")

    compatible = (
        video.get("codec_name") == "h264"
        and video.get("pix_fmt") == "yuv420p"
        and (audio is None or audio.get("codec_name") == "aac")
    )

    # Если уже совместимый MP4, достаточно переместить moov atom в начало.
    # Для других контейнеров/кодеков создаём новую MP4-версию.
    output = workdir / f"{path.stem}_iphone.mp4"
    if compatible and path.suffix.lower() == ".mp4":
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(path), "-map", "0:v:0", "-map", "0:a?",
            "-c", "copy", "-movflags", "+faststart", str(output),
        ]
        action = "remux/copy"
    else:
        log.info(
            "Подготовка iPhone MP4: video=%s profile=%s pix_fmt=%s audio=%s",
            video.get("codec_name"), video.get("profile"), video.get("pix_fmt"),
            audio.get("codec_name") if audio else "нет",
        )
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(path),
            "-map", "0:v:0", "-map", "0:a?",
            "-c:v", "libx264", "-preset", FFMPEG_PRESET,
            "-crf", FFMPEG_CRF, "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(output),
        ]
        action = "transcode"

    try:
        proc = subprocess.run(
            command, capture_output=True, text=True, timeout=600, check=True,
        )
        if not output.is_file() or output.stat().st_size == 0:
            raise RuntimeError("FFmpeg создал пустой файл")
        if output.stat().st_size > max_bytes:
            output.unlink(missing_ok=True)
            raise DownloadFailed(
                "📦 <b>После подготовки видео превышен лимит Telegram (49 МБ).</b>\n"
                "Попробуйте более короткое видео."
            )
        log.info(
            "Видео подготовлено (%s), размер %.2f МБ",
            action, output.stat().st_size / 1048576,
        )
        return output
    except DownloadFailed:
        raise
    except (subprocess.SubprocessError, OSError, RuntimeError) as e:
        output.unlink(missing_ok=True)
        details = getattr(e, "stderr", None)
        if details:
            log.error("FFmpeg error: %s", details[-2500:])
        else:
            log.error("FFmpeg error: %s", e)
        raise DownloadFailed(
            "😔 <b>Не удалось подготовить видео для iPhone.</b>\n"
            "Попробуйте другую ссылку позже."
        ) from e


def download_video(url: str, workdir: Path) -> DownloadResult:
    """Блокирующая функция; вызывается через asyncio.to_thread."""
    cookies: Path | None = None
    if COOKIES_FILE and os.path.isfile(COOKIES_FILE):
        cookies = workdir / "cookies.txt"
        shutil.copyfile(COOKIES_FILE, cookies)

    last_error: Exception | None = None
    for height in QUALITY_LADDER:
        opts: dict[str, Any] = {
            "format": _format_for(height),
            "outtmpl": str(workdir / "%(id).60B.%(ext)s"),
            "merge_output_format": "mp4",
            "noplaylist": True,
            "playlist_items": "1",
            "restrictfilenames": True,
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 30,
            "retries": 3,
            "match_filter": _duration_filter,
        }
        if cookies:
            opts["cookiefile"] = str(cookies)

        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except DownloadCancelled as e:
            msg = getattr(e, "msg", str(e))
            if str(msg).startswith("too_long"):
                raise DownloadFailed(
                    f"⏱ <b>Видео слишком длинное.</b>\n"
                    f"Максимальная длительность — {MAX_DURATION // 60} мин."
                ) from e
            raise DownloadFailed(_humanize_error(str(msg))) from e
        except DownloadError as e:
            log.warning("yt-dlp error at <=%sp for %s: %s", height, url, e)
            last_error = e
            continue

        if info and "entries" in info:
            entries = [entry for entry in info["entries"] if entry]
            info = entries[0] if entries else None
        if not info:
            raise DownloadFailed("🖼 <b>В этой публикации нет видео.</b>")

        path = _find_file(info, workdir)
        if path is None:
            last_error = RuntimeError("Downloaded file not found")
            continue

        # Подготовка к iPhone выполняется до проверки лимита: перекодированный
        # файл тоже должен помещаться в лимит отправки.
        try:
            final_path = _transcode_or_remux(path, workdir, MAX_UPLOAD_BYTES)
        except DownloadFailed as e:
            if "превышен лимит" in e.user_message and height != QUALITY_LADDER[-1]:
                log.info("Файл после обработки больше лимита, пробую качество <=%sp", height)
                # Удаляем текущие файлы, чтобы следующий проход не выбрал старый файл.
                for candidate in workdir.iterdir():
                    if candidate.is_file() and candidate.name != "cookies.txt":
                        candidate.unlink(missing_ok=True)
                continue
            raise

        # После успешной подготовки удаляем исходник, если он отличается.
        if final_path != path:
            path.unlink(missing_ok=True)

        try:
            final_info = _probe_media(final_path)
            final_video = next(
                s for s in final_info.get("streams", [])
                if s.get("codec_type") == "video"
            )
            width = final_video.get("width")
            height_out = final_video.get("height")
            duration_raw = final_info.get("format", {}).get("duration")
            duration = int(float(duration_raw)) if duration_raw else (
                int(info["duration"]) if info.get("duration") else None
            )
        except Exception:
            width = info.get("width")
            height_out = info.get("height")
            duration = int(info["duration"]) if info.get("duration") else None

        return DownloadResult(
            path=final_path,
            title=(info.get("title") or "Видео").strip(),
            duration=duration,
            width=width,
            height=height_out,
        )

    if last_error:
        raise DownloadFailed(_humanize_error(str(last_error))) from last_error
    raise DownloadFailed(
        "📦 <b>Не удалось получить видео подходящего размера.</b>\n"
        "Попробуйте более короткий ролик."
    )


# ================================================================
# MIDDLEWARE: ОБЯЗАТЕЛЬНАЯ ПОДПИСКА
# ================================================================
class SubscriptionMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        bot: Bot = data["bot"]
        user = data.get("event_from_user")

        if user is None or user.is_bot:
            return await handler(event, data)
        if isinstance(event, CallbackQuery):
            chat = event.message.chat if event.message else None
            if event.data == "check_sub":
                return await handler(event, data)
        elif isinstance(event, Message):
            chat = event.chat
            if event.successful_payment:
                return await handler(event, data)
        else:
            return await handler(event, data)

        if chat is None or chat.type != ChatType.PRIVATE:
            return await handler(event, data)

        if await is_subscribed(bot, user.id):
            return await handler(event, data)

        if isinstance(event, CallbackQuery):
            await event.answer("🔐 Сначала подпишитесь на канал", show_alert=True)
            if event.message:
                await event.message.answer(SUBSCRIBE_TEXT, reply_markup=subscribe_kb())
        else:
            await event.answer(SUBSCRIBE_TEXT, reply_markup=subscribe_kb())
        return None


# ================================================================
# ХЕНДЛЕРЫ
# ================================================================
router = Router(name="main")
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

DOWNLOAD_SEMAPHORE = asyncio.Semaphore(MAX_PARALLEL)
ACTIVE_USERS: set[int] = set()


class DonateState(StatesGroup):
    custom_amount = State()


def _welcome_for(user_name: str | None) -> str:
    return WELCOME.format(name=html.escape(user_name or "друг"))


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(_welcome_for(message.from_user.first_name), reply_markup=main_kb())


@router.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(HELP, reply_markup=back_kb())


@router.message(Command("donate"))
async def cmd_donate(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(DONATE_TEXT, reply_markup=donate_kb())


@router.message(Command("paysupport"))
async def cmd_paysupport(message: Message) -> None:
    await message.answer(
        "💬 <b>Поддержка по платежам</b>\n\n"
        "Если возникла проблема с донатом, напишите нам: "
        f"{html.escape(ADMIN_CONTACT)}\n"
        "Укажите, пожалуйста, сумму и время платежа."
    )


@router.callback_query(F.data == "check_sub")
async def cb_check_sub(call: CallbackQuery, bot: Bot) -> None:
    if await is_subscribed(bot, call.from_user.id):
        await call.answer("✅ Подписка подтверждена!")
        await show_screen(call, _welcome_for(call.from_user.first_name), main_kb())
    else:
        await call.answer("❌ Вы ещё не подписались на канал", show_alert=True)


@router.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await show_screen(call, _welcome_for(call.from_user.first_name), main_kb())
    await call.answer()


@router.callback_query(F.data == "help")
async def cb_help(call: CallbackQuery) -> None:
    await show_screen(call, HELP, back_kb())
    await call.answer()


async def send_donation_invoice(message: Message, amount: int) -> None:
    await message.answer_invoice(
        title="Поддержка проекта ⭐",
        description=f"Добровольный донат {amount} ⭐ на развитие бота. Спасибо, что вы с нами!",
        payload=f"donate:{amount}",
        currency="XTR",
        prices=[LabeledPrice(label="Донат", amount=amount)],
    )


@router.callback_query(F.data == "donate")
async def cb_donate(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await show_screen(call, DONATE_TEXT, donate_kb())
    await call.answer()


@router.callback_query(F.data.regexp(r"^pay:\d+$"))
async def cb_pay(call: CallbackQuery) -> None:
    amount = int(call.data.split(":")[1])
    if not 1 <= amount <= MAX_STARS or call.message is None:
        await call.answer("Некорректная сумма", show_alert=True)
        return
    await send_donation_invoice(call.message, amount)
    await call.answer()


@router.callback_query(F.data == "pay_custom")
async def cb_pay_custom(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(DonateState.custom_amount)
    await show_screen(call, CUSTOM_AMOUNT_TEXT, back_kb("donate"))
    await call.answer()


@router.message(StateFilter(DonateState.custom_amount), F.text.regexp(r"^\s*\d{1,6}\s*$"))
async def custom_amount(message: Message, state: FSMContext) -> None:
    amount = int(message.text)
    if not 1 <= amount <= MAX_STARS:
        await message.answer(f"⚠️ Введите число от 1 до {MAX_STARS}.")
        return
    await state.clear()
    await send_donation_invoice(message, amount)


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery) -> None:
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def on_paid(message: Message) -> None:
    payment = message.successful_payment
    log.info(
        "Stars donation: user=%s amount=%s %s charge_id=%s",
        message.from_user.id, payment.total_amount, payment.currency,
        payment.telegram_payment_charge_id,
    )
    await message.answer(
        "💛 <b>Спасибо за поддержку!</b>\n\n"
        f"Ваш донат — <b>{payment.total_amount} ⭐</b> — получен.\n"
        "Благодаря вам бот становится лучше!",
        reply_markup=back_kb(),
    )


async def process_link(
    message: Message, bot: Bot, url: str, platform: tuple[str, str]
) -> None:
    user_id = message.from_user.id
    name, icon = platform

    if user_id in ACTIVE_USERS:
        await message.answer("⏳ Подождите, предыдущее видео ещё обрабатывается.")
        return
    ACTIVE_USERS.add(user_id)

    status = await message.answer(
        f"{icon} <b>Ссылка принята</b> · {name}\n<i>Готовлюсь к загрузке…</i>"
    )
    try:
        if DOWNLOAD_SEMAPHORE.locked():
            await safe_edit(status, "🕐 <b>Вы в очереди</b>\n<i>Начну, как только освободится место…</i>")

        async with DOWNLOAD_SEMAPHORE:
            await safe_edit(
                status,
                f"📥 <b>Скачиваю видео…</b>\n<i>{icon} {name} · это займёт несколько секунд</i>",
            )
            async with ChatActionSender.upload_video(bot=bot, chat_id=message.chat.id):
                with tempfile.TemporaryDirectory(prefix="fetch_") as tmp:
                    result = await asyncio.to_thread(download_video, url, Path(tmp))
                    await safe_edit(status, "📤 <b>Отправляю…</b>")
                    title = html.escape(result.title[:150])
                    await message.answer_video(
                        video=FSInputFile(result.path),
                        caption=f"{icon} <b>{title}</b>\n\n📥 Скачано через @{BOT_USERNAME}",
                        duration=result.duration,
                        width=result.width,
                        height=result.height,
                        supports_streaming=True,
                        reply_markup=after_download_kb(),
                    )
        try:
            await status.delete()
        except TelegramAPIError:
            pass

    except DownloadFailed as e:
        await safe_edit(status, e.user_message, back_kb())
    except TelegramAPIError:
        log.exception("Ошибка Telegram при отправке видео")
        await safe_edit(status, "😔 <b>Не удалось отправить видео.</b>\nПопробуйте ещё раз.", back_kb())
    except Exception:
        log.exception("Непредвиденная ошибка при обработке %s", url)
        await safe_edit(status, "😔 <b>Что-то пошло не так.</b>\nПопробуйте ещё раз чуть позже.", back_kb())
    finally:
        ACTIVE_USERS.discard(user_id)


@router.message(F.text)
async def on_text(message: Message, bot: Bot) -> None:
    url = extract_url(message)
    if not url:
        await message.answer(NOT_A_LINK, reply_markup=back_kb())
        return
    platform = detect_platform(url)
    if platform is None:
        await message.answer(UNSUPPORTED, reply_markup=back_kb())
        return
    await process_link(message, bot, url, platform)


@router.message()
async def on_other(message: Message) -> None:
    await message.answer(NOT_A_LINK, reply_markup=back_kb())


# ================================================================
# ЗАПУСК
# ================================================================
async def on_startup(bot: Bot) -> None:
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="🚀 Запустить бота"),
            BotCommand(command="help", description="📖 Как пользоваться"),
            BotCommand(command="donate", description="⭐ Поддержать проект"),
        ]
    )
    await bot.set_my_short_description(
        "📥 Скачиваю видео из YouTube, Instagram и TikTok. Бесплатно и быстро!"
    )
    await bot.set_my_description(
        "📥 Скачиваю видео из YouTube, Instagram и TikTok в лучшем качестве.\n\n"
        "Просто отправьте ссылку — и получите готовый файл ⚡\n"
        "Бот бесплатный, донаты в Telegram Stars — добровольные ⭐"
    )

    me = await bot.get_me()
    log.info("Бот запущен: @%s", me.username)

    try:
        member = await bot.get_chat_member(CHANNEL_USERNAME, me.id)
        if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
            log.warning(
                "Бот НЕ администратор канала %s — проверка подписки работать не будет!",
                CHANNEL_USERNAME,
            )
    except TelegramAPIError as e:
        log.warning("Нет доступа к каналу %s: %s. Добавьте бота в администраторы канала.", CHANNEL_USERNAME, e)


async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("❌ Не задана переменная окружения BOT_TOKEN (токен от @BotFather).")

    bot = Bot(
        token=BOT_TOKEN,
        session=AiohttpSession(timeout=300),
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
            link_preview_is_disabled=True,
        ),
    )
    dp = Dispatcher(storage=MemoryStorage())
    subscription = SubscriptionMiddleware()
    router.message.outer_middleware(subscription)
    router.callback_query.outer_middleware(subscription)
    dp.include_router(router)
    dp.startup.register(on_startup)

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Бот остановлен")
