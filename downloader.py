"""Скачивание TikTok и YouTube Shorts."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import aiohttp
import yt_dlp
from dotenv import load_dotenv

load_dotenv()

TIKTOK_URL_RE = re.compile(
    r"https?://(?:(?:www|vm|vt|m)\.)?tiktok\.com/[^\s]+",
    re.IGNORECASE,
)
TIKTOK_ID_RE = re.compile(r"/video/(\d+)")
YOUTUBE_URL_RE = re.compile(
    r"https?://(?:(?:www|m|music)\.)?(?:youtube\.com|youtube-nocookie\.com|youtu\.be)/[^\s]+",
    re.IGNORECASE,
)
YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
# Официальный потолок YouTube Shorts.
SHORTS_MAX_SECONDS = 180
_TRAILING_URL_JUNK = ").,]}>\"'"

DOWNLOAD_DIR = Path(__file__).resolve().parent / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    title: str | None
    video_id: str | None
    author: str | None
    source: str


@dataclass(frozen=True)
class IncomingLink:
    url: str
    video_id: str | None
    platform: str


def _clean_url(url: str) -> str:
    return url.rstrip(_TRAILING_URL_JUNK)


def extract_tiktok_url(text: str) -> str | None:
    match = TIKTOK_URL_RE.search(text or "")
    return _clean_url(match.group(0)) if match else None


def extract_video_id(url: str) -> str | None:
    match = TIKTOK_ID_RE.search(url or "")
    return match.group(1) if match else None


def extract_youtube_id(url: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    if host.startswith("m."):
        host = host[2:]
    parts = [part for part in parsed.path.split("/") if part]

    candidate: str | None = None
    if host == "youtu.be":
        candidate = parts[0] if parts else None
    elif host in {"youtube.com", "music.youtube.com", "youtube-nocookie.com"}:
        if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live", "v"}:
            candidate = parts[1]
        else:
            candidate = parse_qs(parsed.query).get("v", [None])[0]

    if candidate and YOUTUBE_ID_RE.fullmatch(candidate):
        return candidate
    return None


def extract_youtube_url(text: str) -> str | None:
    match = YOUTUBE_URL_RE.search(text or "")
    if not match:
        return None
    url = _clean_url(match.group(0))
    return url if extract_youtube_id(url) else None


def extract_link(text: str) -> IncomingLink | None:
    """Берёт первую ссылку TikTok или YouTube в сообщении."""
    found: list[tuple[int, IncomingLink]] = []

    tiktok = TIKTOK_URL_RE.search(text or "")
    if tiktok:
        url = _clean_url(tiktok.group(0))
        found.append((
            tiktok.start(),
            IncomingLink(url=url, video_id=extract_video_id(url), platform="tiktok"),
        ))

    youtube = YOUTUBE_URL_RE.search(text or "")
    if youtube:
        url = _clean_url(youtube.group(0))
        video_id = extract_youtube_id(url)
        if video_id:
            found.append((
                youtube.start(),
                IncomingLink(url=url, video_id=video_id, platform="youtube"),
            ))

    if not found:
        return None
    found.sort(key=lambda item: item[0])
    return found[0][1]


def _author_from_tikwm(data: dict) -> str | None:
    author = data.get("author")
    if isinstance(author, dict):
        name = author.get("unique_id") or author.get("nickname")
        return str(name) if name else None
    if isinstance(author, str) and author:
        return author
    return None


def _find_binary(name: str, extra: list[Path]) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for path in extra:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def _ffmpeg_path() -> str | None:
    return _find_binary("ffmpeg", [
        Path(__file__).resolve().parent / "bin" / "ffmpeg",
        Path.home() / ".local" / "bin" / "ffmpeg",
        Path("/usr/bin/ffmpeg"),
        Path("/usr/local/bin/ffmpeg"),
    ])


def _js_runtime() -> dict | None:
    deno = _find_binary("deno", [
        Path(__file__).resolve().parent / "bin" / "deno",
        Path.home() / ".local" / "bin" / "deno",
        Path.home() / ".deno" / "bin" / "deno",
        Path("/usr/local/bin/deno"),
    ])
    if deno:
        return {"deno": {"path": deno}}
    node = _find_binary("node", [
        Path.home() / ".local" / "bin" / "node",
        Path("/usr/bin/node"),
        Path("/usr/local/bin/node"),
    ])
    if node:
        return {"node": {"path": node}}
    return None


def youtube_setup_error() -> str | None:
    if _ffmpeg_path() is None:
        return "Для YouTube нужен ffmpeg."
    if _js_runtime() is None:
        return "Для YouTube нужен Deno или Node.js: без них yt-dlp не скачивает ролики."
    return None


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


def _youtube_ydl_opts(outtmpl: str) -> dict:
    # H.264 + AAC — то, что Telegram нормально проигрывает.
    opts = {
        "outtmpl": outtmpl,
        "format": (
            "bv*[vcodec^=avc1][height<=1080]+ba[acodec^=mp4a]/"
            "bv*[ext=mp4][height<=1080]+ba[ext=m4a]/"
            "b[ext=mp4][height<=1080]/b"
        ),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "ffmpeg_location": _ffmpeg_path(),
        "js_runtimes": _js_runtime(),
        "remote_components": ["ejs:github"],
    }
    proxy = os.getenv("YOUTUBE_PROXY", "").strip()
    if proxy:
        opts["proxy"] = proxy
    return opts


def _first_entry(info: dict) -> dict:
    entries = info.get("entries")
    if not entries:
        return info
    for entry in entries:
        if entry:
            return entry
    raise RuntimeError("В плейлисте нет видео")


def _take_file(ydl: yt_dlp.YoutubeDL, info: dict, tmp: Path) -> tuple[Path, dict]:
    info = _first_entry(info)
    filename = Path(ydl.prepare_filename(info))
    video_id = str(info.get("id") or "")
    candidates = [filename.with_suffix(".mp4"), filename]
    if video_id:
        candidates.append(tmp / f"{video_id}.mp4")
    src = next((path for path in candidates if path.is_file()), None)
    if src is None:
        files = [
            path for path in tmp.iterdir()
            if path.is_file() and not path.name.endswith(".part")
        ]
        if not files:
            raise RuntimeError("Файл после скачивания не найден")
        src = max(files, key=lambda path: path.stat().st_size)
    dest = DOWNLOAD_DIR / src.name
    dest.write_bytes(src.read_bytes())
    return dest, info


async def download_with_ytdlp(url: str) -> DownloadResult:
    """Скачивает через yt-dlp."""

    def _run() -> DownloadResult:
        with tempfile.TemporaryDirectory(dir=DOWNLOAD_DIR) as tmp:
            outtmpl = str(Path(tmp) / "%(id)s.%(ext)s")
            with yt_dlp.YoutubeDL(_ydl_opts(outtmpl)) as ydl:
                info = ydl.extract_info(url, download=True)
                if info is None:
                    raise RuntimeError("yt-dlp не вернул метаданные")

                dest, info = _take_file(ydl, info, Path(tmp))
                raw_id = info.get("id")
                author = info.get("uploader") or info.get("creator") or info.get("channel")
                return DownloadResult(
                    path=dest,
                    title=info.get("title") or info.get("description"),
                    video_id=str(raw_id) if raw_id else None,
                    author=str(author) if author else None,
                    source="ytdlp",
                )

    return await asyncio.to_thread(_run)


async def download_with_tikwm(url: str) -> DownloadResult:
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

        raw_id = data.get("id")
        video_id = str(raw_id) if raw_id else None
        dest = DOWNLOAD_DIR / f"{video_id or 'tiktok'}.mp4"

        async with session.get(video_url, timeout=aiohttp.ClientTimeout(total=120)) as video_resp:
            if video_resp.status != 200:
                raise RuntimeError(f"Не удалось скачать видео: HTTP {video_resp.status}")
            dest.write_bytes(await video_resp.read())

        return DownloadResult(
            path=dest,
            title=data.get("title"),
            video_id=video_id,
            author=_author_from_tikwm(data),
            source="tikwm",
        )


async def download_tiktok(url: str) -> DownloadResult:
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


def _youtube_network_hint(exc: BaseException) -> RuntimeError:
    text = str(exc)
    lowered = text.lower()
    markers = (
        "unable to download",
        "timed out",
        "timeout",
        "name resolution",
        "temporary failure in name resolution",
        "network is unreachable",
        "connection reset",
        "connection refused",
        "failed to resolve",
        "getaddrinfo",
        "urlopen error",
        "nodename nor servname",
    )
    if any(marker in lowered for marker in markers):
        text += (
            "\n\nYouTube с этого сервера недоступен. "
            "Shorts скачиваются, если бот запущен не из России "
            "или в .env указан YOUTUBE_PROXY."
        )
    return RuntimeError(text)


async def download_youtube(url: str) -> DownloadResult:
    """Скачивает YouTube Shorts (ролик не длиннее 3 минут)."""
    setup_error = youtube_setup_error()
    if setup_error:
        raise RuntimeError(setup_error)

    def _run() -> DownloadResult:
        with tempfile.TemporaryDirectory(dir=DOWNLOAD_DIR) as tmp:
            outtmpl = str(Path(tmp) / "%(id)s.%(ext)s")
            try:
                with yt_dlp.YoutubeDL(_youtube_ydl_opts(outtmpl)) as ydl:
                    info = ydl.extract_info(url, download=False)
                    if info is None:
                        raise RuntimeError("yt-dlp не вернул метаданные")
                    info = _first_entry(info)

                    if info.get("is_live") or info.get("live_status") == "is_live":
                        raise RuntimeError("Прямые эфиры не скачиваю. Пришли ссылку на Shorts.")

                    duration = info.get("duration")
                    if isinstance(duration, (int, float)) and duration > SHORTS_MAX_SECONDS:
                        total = int(duration)
                        raise RuntimeError(
                            "Это длиннее Shorts "
                            f"({total // 60}:{total % 60:02d}). "
                            "Пришли ролик до 3 минут."
                        )

                    downloaded = ydl.extract_info(url, download=True)
                    if downloaded is None:
                        raise RuntimeError("yt-dlp не вернул метаданные")
                    dest, info = _take_file(ydl, downloaded, Path(tmp))
            except RuntimeError:
                raise
            except Exception as exc:
                raise _youtube_network_hint(exc) from exc

            raw_id = info.get("id")
            author = info.get("uploader") or info.get("creator") or info.get("channel")
            return DownloadResult(
                path=dest,
                title=info.get("title") or info.get("description"),
                video_id=str(raw_id) if raw_id else extract_youtube_id(url),
                author=str(author) if author else None,
                source="youtube",
            )

    return await asyncio.to_thread(_run)


async def download_media(link: IncomingLink) -> DownloadResult:
    if link.platform == "youtube":
        return await download_youtube(link.url)
    return await download_tiktok(link.url)
