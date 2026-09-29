"""Скачивание TikTok-видео без водяного знака."""

from __future__ import annotations

import asyncio
import re
import tempfile
from pathlib import Path

import aiohttp
import yt_dlp

TIKTOK_URL_RE = re.compile(
    r"https?://(?:(?:www|vm|vt|m)\.)?tiktok\.com/[^\s]+",
    re.IGNORECASE,
)

DOWNLOAD_DIR = Path(__file__).resolve().parent / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)


def extract_tiktok_url(text: str) -> str | None:
    match = TIKTOK_URL_RE.search(text or "")
    return match.group(0).rstrip(").,]}>\"'") if match else None


def _ydl_opts(outtmpl: str) -> dict:
    return {
        "outtmpl": outtmpl,
        "format": "best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 3,
        "socket_timeout": 30,
        # Без водяного знака TikTok отдаёт оригинальный play URL через yt-dlp
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.tiktok.com/",
        },
    }


async def download_with_ytdlp(url: str) -> tuple[Path, str | None]:
    """Скачивает через yt-dlp. Возвращает (путь, заголовок)."""

    def _run() -> tuple[Path, str | None]:
        with tempfile.TemporaryDirectory(dir=DOWNLOAD_DIR) as tmp:
            outtmpl = str(Path(tmp) / "%(id)s.%(ext)s")
            with yt_dlp.YoutubeDL(_ydl_opts(outtmpl)) as ydl:
                info = ydl.extract_info(url, download=True)
                if info is None:
                    raise RuntimeError("yt-dlp не вернул метаданные")

                # Плейлисты / карусели — берём первый ролик
                if "entries" in info and info["entries"]:
                    info = info["entries"][0]

                filename = ydl.prepare_filename(info)
                src = Path(filename)
                if not src.exists():
                    # иногда расширение меняется
                    candidates = list(Path(tmp).glob("*"))
                    if not candidates:
                        raise RuntimeError("Файл после скачивания не найден")
                    src = candidates[0]

                dest = DOWNLOAD_DIR / src.name
                dest.write_bytes(src.read_bytes())
                title = info.get("title") or info.get("description")
                return dest, title

    return await asyncio.to_thread(_run)


async def download_with_tikwm(url: str) -> tuple[Path, str | None]:
    """Запасной способ: публичный API tikwm.com (без водяного знака)."""
    api = "https://www.tikwm.com/api/"
    params = {"url": url, "hd": 1}

    async with aiohttp.ClientSession() as session:
        async with session.get(api, params=params, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            if resp.status != 200:
                raise RuntimeError(f"TikWM HTTP {resp.status}")
            payload = await resp.json(content_type=None)

        if payload.get("code") != 0:
            raise RuntimeError(payload.get("msg") or "TikWM отклонил запрос")

        data = payload.get("data") or {}
        video_url = data.get("hdplay") or data.get("play") or data.get("wmplay")
        if not video_url:
            raise RuntimeError("TikWM не вернул ссылку на видео")

        title = data.get("title")
        video_id = data.get("id") or "tiktok"
        dest = DOWNLOAD_DIR / f"{video_id}.mp4"

        async with session.get(video_url, timeout=aiohttp.ClientTimeout(total=120)) as video_resp:
            if video_resp.status != 200:
                raise RuntimeError(f"Не удалось скачать видео: HTTP {video_resp.status}")
            dest.write_bytes(await video_resp.read())

        return dest, title


async def download_tiktok(url: str) -> tuple[Path, str | None]:
    """Пробует yt-dlp, при ошибке — TikWM."""
    errors: list[str] = []

    try:
        return await download_with_ytdlp(url)
    except Exception as exc:  # noqa: BLE001 — нужен fallback
        errors.append(f"yt-dlp: {exc}")

    try:
        return await download_with_tikwm(url)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"tikwm: {exc}")

    raise RuntimeError("Не удалось скачать видео.\n" + "\n".join(errors))
