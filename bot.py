"""Telegram-бот: скачивание TikTok без водяного знака."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import FSInputFile, Message
from dotenv import load_dotenv

from downloader import DOWNLOAD_DIR, download_tiktok, extract_tiktok_url

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("tiktok-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
# Лимит отправки через Bot API без локального сервера — 50 МБ
MAX_SEND_BYTES = 49 * 1024 * 1024

dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Пришли ссылку на TikTok — верну видео без водяного знака.\n\n"
        "Поддерживаются ссылки:\n"
        "• tiktok.com/@user/video/...\n"
        "• vm.tiktok.com/...\n"
        "• vt.tiktok.com/..."
    )


@dp.message(F.text)
async def handle_link(message: Message) -> None:
    url = extract_tiktok_url(message.text or "")
    if not url:
        await message.answer("Не вижу ссылку на TikTok. Пришли URL ролика.")
        return

    status = await message.answer("Скачиваю…")
    file_path: Path | None = None

    try:
        file_path, title = await download_tiktok(url)
        size = file_path.stat().st_size

        if size > MAX_SEND_BYTES:
            await status.edit_text(
                f"Видео слишком большое для Telegram ({size / 1024 / 1024:.1f} МБ). "
                "Лимит бота — около 50 МБ."
            )
            return

        caption = (title or "TikTok").strip()
        if len(caption) > 900:
            caption = caption[:897] + "…"

        await message.answer_video(
            video=FSInputFile(file_path),
            caption=caption,
            supports_streaming=True,
        )
        await status.delete()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ошибка скачивания")
        await status.edit_text(f"Не получилось скачать:\n{exc}")
    finally:
        if file_path and file_path.exists():
            try:
                file_path.unlink()
            except OSError:
                logger.warning("Не удалось удалить %s", file_path)


async def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "your_telegram_bot_token_here":
        raise SystemExit(
            "Укажи BOT_TOKEN в файле .env (токен от @BotFather)."
        )

    DOWNLOAD_DIR.mkdir(exist_ok=True)
    bot = Bot(token=BOT_TOKEN)
    logger.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
