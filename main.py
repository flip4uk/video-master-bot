
import asyncio
import logging
import os
import re

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
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
        return member.status in ("member", "administrator", "creator")
    except TelegramBadRequest:
        logging.exception("Не удалось проверить подписку")
        return False


async def show_menu(message: Message):
    await message.answer(
        "👋 Добро пожаловать!\n\n"
        "Здесь можно будет скачивать видео и "
        "обрабатывать собственные ролики.\n\n"
        "Выбери нужный раздел:",
        reply_markup=main_keyboard()
    )


@dp.message(CommandStart())
async def start(message: Message):
    if not await is_subscribed(message.from_user.id):
        await message.answer(
            "🔒 Чтобы пользоваться ботом, подпишись на наш канал "
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
        await callback.answer("Сначала подпишись на канал.", show_alert=True)
        return

    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "📥 Отправь ссылку на видео из YouTube, TikTok или Instagram.\n\n"
            "Пока скачивание не подключено — это добавим на следующем этапе."
        )


@dp.callback_query(F.data == "unique")
async def unique_info(callback: CallbackQuery):
    if not await is_subscribed(callback.from_user.id):
        await callback.answer("Сначала подпишись на канал.", show_alert=True)
        return

    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "🎬 Раздел обработки видео готовится.\n"
            "Планируется создание до 5 вариантов за один запуск."
        )


@dp.callback_query(F.data == "donate")
async def donate_info(callback: CallbackQuery):
    if not await is_subscribed(callback.from_user.id):
        await callback.answer("Сначала подпишись на канал.", show_alert=True)
        return

    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "⭐ Спасибо за желание поддержать проект!\n"
            "Приём Telegram Stars подключим отдельным этапом."
        )


@dp.message(F.text)
async def handle_text(message: Message):
    if not await is_subscribed(message.from_user.id):
        await message.answer(
            "🔒 Сначала подпишись на канал.",
            reply_markup=subscription_keyboard()
        )
        return

    text = message.text or ""

    if URL_PATTERN.search(text):
        await message.answer(
            "🔗 Ссылка получена. Загрузчик видео подключим "
            "на следующем этапе."
        )
    else:
        await message.answer(
            "Выбери раздел в меню или отправь ссылку на видео.",
            reply_markup=main_keyboard()
        )


async def main():
    logging.info("Бот запускается")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
