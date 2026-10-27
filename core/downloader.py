"""yt-dlp integration: playlist parsing, queue population and media download.

All downloads happen through the yt-dlp Python API.  No shell or PowerShell is
used.  A minibattle always consists of exactly one video file plus one audio
file, both created inside a dedicated temporary directory.
"""
from __future__ import annotations

import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any, Iterable

import config
from database import Database

log = logging.getLogger("goal3.downloader")

_VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".ts"}
_AUDIO_EXTENSIONS = {".m4a", ".opus", ".webm", ".mp3", ".aac", ".wav", ".ogg", ".mp4", ".mkv"}


class DownloadError(RuntimeError):
    pass


def _import_ytdlp():
    try:
        import yt_dlp  # type: ignore
    except ImportError as exc:  # pragma: no cover - dependency error
        raise DownloadError(
            "yt-dlp is not installed. Run: pip install -r requirements.txt"
        ) from exc
    return yt_dlp


def _safe_stem(value: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value)).strip("._-")
    return stem[:120] or "video"


def _watch_url(entry: dict) -> str | None:
    url = entry.get("webpage_url") or entry.get("original_url")
    if url and str(url).startswith("http"):
        return str(url)
    video_id = entry.get("id")
    if video_id and re.fullmatch(r"[A-Za-z0-9_-]{6,}", str(video_id)):
        return f"https://www.youtube.com/watch?v={video_id}"
    raw = entry.get("url")
    if raw and str(raw).startswith("http"):
        return str(raw)
    return None


def parse_source(url: str, db: Database, initial_voice_side: str = config.SIDE_RIGHT) -> dict:
    """Extract playlist/video metadata and add every item to the queue."""
    yt_dlp = _import_ytdlp()
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "ignoreerrors": True,
        "noplaylist": False,
    }
    log.info("Parsing source: %s", url)
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        raise DownloadError(f"Could not parse {url}: {exc}") from exc
    if not info:
        raise DownloadError(f"yt-dlp returned no metadata for {url}")

    entries: list[dict] = []
    if info.get("entries") is not None:
        entries = [entry for entry in info.get("entries") or [] if entry]
    else:
        entries = [info]

    added: list[dict] = []
    for entry in entries:
        video_id = str(entry.get("id") or "").strip()
        watch_url = _watch_url(entry)
        if not video_id or not watch_url:
            log.warning("Skipping playlist entry without a usable id/url: %s", entry.get("title"))
            continue
        title = entry.get("title") or video_id
        try:
            duration = float(entry.get("duration") or 0.0)
        except (TypeError, ValueError):
            duration = 0.0
        created = db.queue_video(
            video_id,
            watch_url,
            title,
            initial_voice_side=initial_voice_side,
            duration_sec=duration,
        )
        added.append(
            {
                "video_id": video_id,
                "url": watch_url,
                "title": title,
                "duration_sec": duration,
                "created": created,
            }
        )
    if not added:
        raise DownloadError("No downloadable videos were found at this URL")
    log.info("Queued %d video(s), %d new", len(added), sum(1 for x in added if x["created"]))
    return {
        "source_title": info.get("title"),
        "playlist": info.get("entries") is not None,
        "items": added,
    }


def add_urls(urls: Iterable[str], db: Database, initial_voice_side: str = config.SIDE_RIGHT) -> list[dict]:
    results = []
    for url in urls:
        url = (url or "").strip()
        if not url:
            continue
        results.append(parse_source(url, db, initial_voice_side=initial_voice_side))
    return results


def _new_temp_dir(video_id: str) -> Path:
    config.ensure_directories()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = config.TEMP_DIR / f"{_safe_stem(video_id)}_{stamp}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _base_options(outtmpl: str, fmt: str) -> dict:
    options: dict[str, Any] = {
        "format": fmt,
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": config.YTDLP_RETRIES,
        "fragment_retries": config.YTDLP_RETRIES,
        "socket_timeout": config.YTDLP_SOCKET_TIMEOUT,
        "concurrent_fragment_downloads": 3,
        "overwrites": False,
        "windowsfilenames": True,
    }
    if config.FFMPEG_LOCATION:
        options["ffmpeg_location"] = config.FFMPEG_LOCATION
    return options


def _find_output(directory: Path, prefix: str, extensions: set[str]) -> Path | None:
    candidates = []
    for path in directory.glob(f"{prefix}.*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in extensions:
            continue
        if path.name.endswith(".part"):
            continue
        if path.stat().st_size <= 0:
            continue
        candidates.append(path)
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_size)


def _download_one(
    yt_dlp,
    url: str,
    directory: Path,
    prefix: str,
    fmt: str,
    extensions: set[str],
) -> Path:
    outtmpl = str(directory / f"{prefix}.%(ext)s")
    options = _base_options(outtmpl, fmt)

    def hook(progress: dict) -> None:
        if progress.get("status") == "finished":
            log.info("Download finished: %s", progress.get("filename"))
        elif progress.get("status") == "error":
            log.error("Download error: %s", progress)

    options["progress_hooks"] = [hook]
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.extract_info(url, download=True)
    except Exception as exc:
        raise DownloadError(f"yt-dlp failed for {url}: {exc}") from exc
    output = _find_output(directory, prefix, extensions)
    if output is None:
        raise DownloadError(f"yt-dlp reported success but no {prefix} file was found in {directory}")
    log.info("Downloaded %s (%.1f MB)", output.name, output.stat().st_size / (1024 * 1024))
    return output


def download_video(url: str, video_id: str) -> dict:
    """Download one source video and its separate audio track into data/temp/.

    One download can contain one or several minibattles.  Returns paths; the
    caller is responsible for deleting them in a finally block via
    cleanup_media().  On any failure this function itself cleans up.
    """
    yt_dlp = _import_ytdlp()
    work_dir = _new_temp_dir(video_id)
    try:
        video = _download_one(
            yt_dlp,
            url,
            work_dir,
            "video",
            config.VIDEO_FORMAT,
            _VIDEO_EXTENSIONS,
        )
        audio = _download_one(
            yt_dlp,
            url,
            work_dir,
            "audio",
            config.AUDIO_FORMAT,
            _AUDIO_EXTENSIONS,
        )
        return {
            "video_path": str(video.resolve()),
            "audio_path": str(audio.resolve()),
            "temp_dir": str(work_dir.resolve()),
        }
    except BaseException:
        # Includes KeyboardInterrupt: never leave a partially downloaded
        # .mp4/.m4a behind, even if the operator interrupts the download.
        cleanup_temp_dir(work_dir)
        raise


# Backward-compatible name used by older integrations.
def download_minibattle(url: str, video_id: str) -> dict:
    return download_video(url, video_id)


def cleanup_media(video_path: str | Path | None, audio_path: str | Path | None) -> None:
    """Delete the source .mp4/.m4a files.  Never raises."""
    for value in (video_path, audio_path):
        if not value:
            continue
        path = Path(value)
        try:
            if path.exists() and path.is_file():
                path.unlink()
                log.info("Deleted temporary media: %s", path)
        except OSError as exc:
            log.error("Could not delete temporary media %s: %s", path, exc)


def cleanup_temp_dir(directory: str | Path | None) -> None:
    """Remove a temporary directory left after a download/processing failure."""
    if not directory:
        return
    path = Path(directory)
    if not path.exists():
        return
    try:
        resolved = path.resolve()
        temp_root = config.TEMP_DIR.resolve()
        if temp_root not in resolved.parents and resolved != temp_root:
            log.error("Refusing to remove directory outside TEMP_DIR: %s", resolved)
            return
        shutil.rmtree(resolved, ignore_errors=True)
        log.info("Removed temporary directory: %s", resolved)
    except OSError as exc:
        log.error("Could not remove temporary directory %s: %s", path, exc)


def cleanup_stale_temp(max_age_hours: float = 24.0) -> int:
    """Remove stale leftover temp directories; returns count removed."""
    if not config.TEMP_DIR.exists():
        return 0
    removed = 0
    cutoff = time.time() - max_age_hours * 3600.0
    for child in config.TEMP_DIR.iterdir():
        if not child.is_dir():
            continue
        try:
            if child.stat().st_mtime < cutoff:
                cleanup_temp_dir(child)
                removed += 1
        except OSError:
            continue
    return removed
