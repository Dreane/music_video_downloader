"""Скачивание TikTok и YouTube Shorts."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

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
YANDEX_URL_RE = re.compile(
    r"https?://music\.yandex\.(?:ru|kz|ua|by|com)/[^\s]+",
    re.IGNORECASE,
)
YANDEX_TRACK_RE = re.compile(r"/track/(\d+)")
VK_URL_RE = re.compile(
    r"https?://(?:(?:m|new)\.)?vk\.(?:com|ru)/(?:audio|music)[^\s]*",
    re.IGNORECASE,
)
VK_TRACK_RE = re.compile(r"(?:^|[^\w])audio(-?\d+_\d+)|/track/(-?\d+_\d+)", re.IGNORECASE)
_YANDEX_SIGN_PREFIX = "XGRlBW9FXlekgbPrRHuSiA"
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
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
    kind: str = "video"
    duration: int | None = None


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


def _yandex_track_id(url: str) -> str | None:
    match = YANDEX_TRACK_RE.search(url or "")
    return match.group(1) if match else None


def _vk_audio_id(url: str) -> str | None:
    match = VK_TRACK_RE.search(url or "")
    if not match:
        return None
    return match.group(1) or match.group(2)


def extract_link(text: str) -> IncomingLink | None:
    """Берёт первую ссылку TikTok, YouTube, Яндекс Музыки или ВК."""
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

    yandex = YANDEX_URL_RE.search(text or "")
    if yandex:
        url = _clean_url(yandex.group(0))
        track_id = _yandex_track_id(url)
        found.append((
            yandex.start(),
            IncomingLink(
                url=url,
                video_id=f"ym:{track_id}" if track_id else None,
                platform="yandex",
            ),
        ))

    vk = VK_URL_RE.search(text or "")
    if vk:
        url = _clean_url(vk.group(0))
        audio_id = _vk_audio_id(url)
        found.append((
            vk.start(),
            IncomingLink(
                url=url,
                video_id=f"vk:{audio_id}" if audio_id else None,
                platform="vk",
            ),
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


def _yandex_file_url(host: str, path: str, ts: str, signature: str, codec: str) -> str:
    digest = hashlib.md5(
        f"{_YANDEX_SIGN_PREFIX}{path[1:]}{signature}".encode()
    ).hexdigest()
    safe_codec = codec if codec in {"mp3", "aac"} else "mp3"
    return f"https://{host}/get-{safe_codec}/{digest}/{ts}{path}"


def _yandex_url_from_xml(payload: bytes, codec: str) -> str:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise RuntimeError("Яндекс Музыка вернула не адрес файла") from exc
    host = (root.findtext("host") or "").strip()
    path = (root.findtext("path") or "").strip()
    ts = (root.findtext("ts") or "").strip()
    signature = (root.findtext("s") or "").strip()
    if not all((host, path, ts, signature)):
        raise RuntimeError("Яндекс Музыка не вернула адрес файла")
    return _yandex_file_url(host, path, ts, signature, codec)


def _pick_yandex_variant(items: list[dict], *, has_token: bool) -> dict:
    full = [item for item in items if not item.get("preview") and item.get("downloadInfoUrl")]
    if not full:
        if has_token:
            raise RuntimeError("У этого аккаунта Яндекс Музыки нет полного файла трека.")
        raise RuntimeError(
            "Для полного трека добавь в .env YANDEX_MUSIC_TOKEN — "
            "токен своего аккаунта Яндекс Музыки."
        )

    def rank(item: dict) -> tuple[bool, int]:
        bitrate = item.get("bitrateInKbps") or 0
        return (item.get("codec") == "mp3", int(bitrate))

    return max(full, key=rank)


async def _download_to(session: aiohttp.ClientSession, url: str, dest: Path, headers: dict | None = None) -> None:
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=120)) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Не удалось скачать файл: HTTP {resp.status}")
            with dest.open("wb") as handle:
                async for chunk in resp.content.iter_chunked(65536):
                    if chunk:
                        handle.write(chunk)
    except RuntimeError:
        dest.unlink(missing_ok=True)
        raise
    except Exception as exc:
        dest.unlink(missing_ok=True)
        raise RuntimeError("Не удалось скачать файл") from exc

    if not dest.is_file() or dest.stat().st_size < 1024:
        dest.unlink(missing_ok=True)
        raise RuntimeError("Сервис вернул пустой файл")
    head = dest.read_bytes()[:32].lstrip()
    if head.startswith(b"<"):
        dest.unlink(missing_ok=True)
        raise RuntimeError("Сервис вернул страницу вместо аудио")


async def download_yandex(link: IncomingLink) -> DownloadResult:
    """Скачивает один трек Яндекс Музыки."""
    track_id = _yandex_track_id(link.url)
    if not track_id:
        raise RuntimeError(
            "Пришли ссылку на трек: music.yandex.ru/album/…/track/…"
        )

    token = os.getenv("YANDEX_MUSIC_TOKEN", "").strip()
    headers = {"User-Agent": _BROWSER_UA, "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"OAuth {token}"

    async with aiohttp.ClientSession(headers=headers) as session:
        try:
            async with session.get(
                f"https://api.music.yandex.net/tracks/{track_id}",
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Яндекс Музыка ответила HTTP {resp.status}")
                meta_payload = await resp.json(content_type=None)
            async with session.get(
                f"https://api.music.yandex.net/tracks/{track_id}/download-info",
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Яндекс Музыка ответила HTTP {resp.status}")
                download_payload = await resp.json(content_type=None)
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError("Яндекс Музыка недоступна") from exc

    if isinstance(meta_payload, dict) and meta_payload.get("error"):
        message = meta_payload["error"].get("message") if isinstance(meta_payload["error"], dict) else meta_payload["error"]
        raise RuntimeError(f"Яндекс Музыка: {message}")
    tracks = (meta_payload or {}).get("result") if isinstance(meta_payload, dict) else None
    if not tracks:
        raise RuntimeError("Яндекс Музыка не нашла трек")
    track = tracks[0]
    artists = [
        artist.get("name")
        for artist in track.get("artists") or []
        if isinstance(artist, dict) and artist.get("name")
    ]
    author = ", ".join(artists) or None
    title = track.get("title") or "Трек"
    duration_ms = track.get("durationMs")
    duration = int(duration_ms / 1000) if isinstance(duration_ms, (int, float)) else None

    variants = (download_payload or {}).get("result") if isinstance(download_payload, dict) else None
    if not isinstance(variants, list):
        raise RuntimeError("Яндекс Музыка не вернула файл трека")
    chosen = _pick_yandex_variant(variants, has_token=bool(token))
    codec = str(chosen.get("codec") or "mp3")
    info_url = str(chosen["downloadInfoUrl"])
    ext = "m4a" if codec == "aac" else "mp3"
    dest = DOWNLOAD_DIR / f"ym_{track_id}_{uuid4().hex[:8]}.{ext}"

    async with aiohttp.ClientSession(headers={"User-Agent": _BROWSER_UA}) as session:
        if chosen.get("direct"):
            file_url = info_url
        else:
            async with session.get(info_url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Яндекс Музыка ответила HTTP {resp.status}")
                file_url = _yandex_url_from_xml(await resp.read(), codec)
        await _download_to(session, file_url, dest)

    return DownloadResult(
        path=dest,
        title=f"{author} — {title}" if author else str(title),
        video_id=f"ym:{track_id}",
        author=author,
        source="yandex",
        kind="audio",
        duration=duration,
    )


async def download_vk(link: IncomingLink) -> DownloadResult:
    """Скачивает один трек ВК."""
    audio_id = _vk_audio_id(link.url)
    if not audio_id:
        raise RuntimeError("Пришли ссылку на один трек ВК: vk.com/audio…")

    token = os.getenv("VK_ACCESS_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "Для ВК добавь в .env VK_ACCESS_TOKEN — токен пользователя с доступом к аудио."
        )

    params = {"audios": audio_id, "access_token": token, "v": "5.131"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.vk.com/method/audio.getById",
                params=params,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"ВК ответил HTTP {resp.status}")
                payload = await resp.json(content_type=None)
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError("ВК недоступен") from exc

    if not isinstance(payload, dict):
        raise RuntimeError("ВК вернул пустой ответ")
    if payload.get("error"):
        message = payload["error"].get("error_msg") if isinstance(payload["error"], dict) else payload["error"]
        raise RuntimeError(f"ВК: {message}")
    items = payload.get("response") or []
    if not items or not isinstance(items, list):
        raise RuntimeError("ВК не нашёл трек")
    item = items[0]
    file_url = item.get("url")
    if not file_url:
        raise RuntimeError(
            "Этот токен ВК не отдаёт файл. Нужен токен пользователя с доступом к аудио."
        )

    artist = item.get("artist") or None
    title = item.get("title") or "Трек"
    duration = item.get("duration")
    duration = int(duration) if isinstance(duration, (int, float)) else None
    dest = DOWNLOAD_DIR / f"vk_{audio_id.replace('-', 'm')}_{uuid4().hex[:8]}.mp3"
    async with aiohttp.ClientSession() as session:
        await _download_to(
            session,
            str(file_url),
            dest,
            headers={"User-Agent": _BROWSER_UA, "Referer": "https://vk.com/"},
        )

    return DownloadResult(
        path=dest,
        title=f"{artist} — {title}" if artist else str(title),
        video_id=f"vk:{audio_id}",
        author=str(artist) if artist else None,
        source="vk",
        kind="audio",
        duration=duration,
    )


async def download_media(link: IncomingLink) -> DownloadResult:
    if link.platform == "youtube":
        return await download_youtube(link.url)
    if link.platform == "yandex":
        return await download_yandex(link)
    if link.platform == "vk":
        return await download_vk(link)
    return await download_tiktok(link.url)
