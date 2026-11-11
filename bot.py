"""Telegram-бот: скачивание TikTok и YouTube Shorts."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import FSInputFile, Message
from dotenv import load_dotenv

from db import add_download, get_cached_video, init_db, is_banned, save_video, upsert_user
from downloader import DOWNLOAD_DIR, download_media, extract_link, youtube_setup_error

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


def _caption(title: str | None, fallback: str = "Видео") -> str:
    caption = (title or fallback).strip()
    if len(caption) > 900:
        caption = caption[:897] + "…"
    return caption


def _clip(value: str | None, limit: int = 64) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    return text[:limit]


def _platform_name(platform: str) -> str:
    return {
        "youtube": "YouTube",
        "yandex": "Яндекс Музыка",
        "vk": "ВК",
    }.get(platform, "TikTok")


async def _record_download(
    user_id: int,
    url: str,
    video_id: str | None,
    title: str | None,
    author: str | None,
    source: str | None,
    status: str,
    file_size: int | None = None,
    error: str | None = None,
) -> None:
    try:
        await add_download(
            user_id=user_id,
            url=url,
            video_id=video_id,
            title=title,
            author=author,
            source=source,
            status=status,
            file_size=file_size,
            error=error,
        )
    except Exception:
        logger.exception("Не удалось записать скачивание")


async def _touch_user(message: Message) -> int | None:
    user = message.from_user
    if user is None:
        return None
    await upsert_user(user.id, user.username, user.first_name)
    return user.id


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await _touch_user(message)
    await message.answer(
        "Пришли ссылку — верну файл.\n\n"
        "TikTok, видео без водяного знака:\n"
        "• tiktok.com/@user/video/...\n"
        "• vm.tiktok.com/...\n"
        "• vt.tiktok.com/...\n\n"
        "Яндекс Музыка:\n"
        "• music.yandex.ru/album/…/track/…\n\n"
        "ВКонтакте:\n"
        "• vk.com/audio…"
    )


@dp.message(F.text)
async def handle_link(message: Message) -> None:
    user_id = await _touch_user(message)
    if user_id is None:
        return
    if await is_banned(user_id):
        await message.answer("Доступ закрыт.")
        return

    link = extract_link(message.text or "")
    if link is None:
        await message.answer(
            "Не вижу ссылку. Пришли URL TikTok, Яндекс Музыки или ВК."
        )
        return

    url = link.url
    fallback = _platform_name(link.platform)
    cached = await get_cached_video(url, link.video_id)
    if cached is not None:
        if cached.kind == "audio":
            await message.answer_audio(
                audio=cached.file_id,
                caption=_caption(cached.title, fallback),
                title=_clip(cached.title),
                performer=_clip(cached.author),
            )
        else:
            await message.answer_video(
                video=cached.file_id,
                caption=_caption(cached.title, fallback),
                supports_streaming=True,
            )
        await _record_download(
            user_id=user_id,
            url=url,
            video_id=cached.video_id,
            title=cached.title,
            author=cached.author,
            source="cache",
            status="ok",
            file_size=cached.file_size,
        )
        return

    status = await message.answer("Скачиваю…")
    file_path: Path | None = None
    result = None
    size: int | None = None
    file_id: str | None = None
    download_status = "error"
    error: str | None = None

    try:
        result = await download_media(link)
        file_path = result.path
        size = file_path.stat().st_size

        if size > MAX_SEND_BYTES:
            download_status = "too_large"
            await status.edit_text(
                f"Файл слишком большой для Telegram ({size / 1024 / 1024:.1f} МБ). "
                "Лимит бота — около 50 МБ."
            )
        elif result.kind == "audio":
            sent = await message.answer_audio(
                audio=FSInputFile(file_path),
                caption=_caption(result.title, fallback),
                title=_clip(result.title),
                performer=_clip(result.author),
                **({"duration": result.duration} if result.duration else {}),
            )
            await status.delete()
            file_id = sent.audio.file_id if sent.audio else None
            download_status = "ok"
        else:
            sent = await message.answer_video(
                video=FSInputFile(file_path),
                caption=_caption(result.title, fallback),
                supports_streaming=True,
            )
            await status.delete()
            file_id = sent.video.file_id if sent.video else None
            download_status = "ok"
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ошибка скачивания")
        error = str(exc)
        await status.edit_text(f"Не получилось скачать:\n{exc}")
    finally:
        if file_path and file_path.exists():
            try:
                file_path.unlink()
            except OSError:
                logger.warning("Не удалось удалить %s", file_path)

    if download_status == "ok" and result and result.video_id:
        try:
            await save_video(
                video_id=result.video_id,
                title=result.title,
                author=result.author,
                file_id=file_id,
                file_size=size,
                url=url,
                kind=result.kind,
            )
        except Exception:
            logger.exception("Не удалось сохранить видео в кэш")

    await _record_download(
        user_id=user_id,
        url=url,
        video_id=result.video_id if result else link.video_id,
        title=result.title if result else None,
        author=result.author if result else None,
        source=result.source if result else None,
        status=download_status,
        file_size=size,
        error=error,
    )


async def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "your_telegram_bot_token_here":
        raise SystemExit(
            "Укажи BOT_TOKEN в файле .env (токен от @BotFather)."
        )

    DOWNLOAD_DIR.mkdir(exist_ok=True)
    init_db()
    youtube_error = youtube_setup_error()
    if youtube_error:
        logger.warning("YouTube Shorts недоступны: %s", youtube_error)
    bot = Bot(token=BOT_TOKEN)
    logger.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
