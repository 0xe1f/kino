import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"}
THUMBNAIL_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
DIRECTORY_THUMB_BASENAMES = ("cover", "folder", "thumbnail", "poster")
VIDEO_THUMB_BASENAMES = ("thumbnail", "cover", "poster")
THEME_OPTIONS = {"night", "day"}
BUILTIN_PLAYLISTS = {
    "favorites": {"name": "Favorites"},
    "watch_later": {"name": "Watch Later"},
}

media_root: str = os.getenv("MEDIA_ROOT", "/media/library")
scan_lock_ttl: int = int(os.getenv("SCAN_LOCK_TTL_SECONDS", "600"))
generated_thumb_dir: Path = Path(os.getenv("STATIC_DIR", "./static")) / "generated-thumbs"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def format_duration(value: float | int | None) -> str:
    if value is None:
        return "0:00"
    total_seconds = max(0, int(round(float(value))))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def sha1_text(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8"), usedforsecurity=False).hexdigest()


def normalize_username(value: str) -> str:
    return (value or "").strip()


def normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def normalize_video_id(raw: str) -> str:
    return raw if raw.startswith("video:") else f"video:{raw}"


def normalize_playlist_id(raw: str) -> str:
    return raw if raw.startswith("playlist:") else f"playlist:{raw}"


def is_valid_theme(theme: str | None) -> bool:
    return theme in THEME_OPTIONS


def parse_ffprobe_duration(path: str) -> float:
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json",
        path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=10)
        data = json.loads(proc.stdout or "{}")
        value = data.get("format", {}).get("duration")
        return float(value) if value else 0.0
    except Exception:
        return 0.0


def _first_existing_file(candidates: list[Path]) -> Path | None:
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def _to_media_relative(path: Path) -> str:
    return str(path.relative_to(Path(media_root))).replace("\\", "/")


def find_existing_video_thumbnail(relative_video_path: str) -> str | None:
    video_path = Path(media_root) / relative_video_path
    parent = video_path.parent
    stem = video_path.stem
    candidates = []
    for ext in THUMBNAIL_EXTENSIONS:
        candidates.append(parent / f"{stem}{ext}")
    for base in VIDEO_THUMB_BASENAMES:
        for ext in THUMBNAIL_EXTENSIONS:
            candidates.append(parent / f"{base}{ext}")
    found = _first_existing_file(candidates)
    return _to_media_relative(found) if found else None


def find_existing_directory_thumbnail(relative_dir_path: str) -> str | None:
    root = Path(media_root)
    target_dir = root / relative_dir_path if relative_dir_path else root
    candidates = []
    for base in DIRECTORY_THUMB_BASENAMES:
        for ext in THUMBNAIL_EXTENSIONS:
            candidates.append(target_dir / f"{base}{ext}")
    found = _first_existing_file(candidates)
    return _to_media_relative(found) if found else None


def choose_thumbnail_seek_seconds(duration_seconds: float | int | None) -> float:
    if not duration_seconds or duration_seconds <= 0:
        return 30.0
    duration = float(duration_seconds)
    seek = duration * 0.20
    seek = max(25.0, seek)
    seek = min(180.0, seek)
    return min(seek, max(5.0, duration - 15.0))


def ensure_generated_video_thumbnail(
    relative_video_path: str,
    absolute_video_path: str,
    duration_seconds: float,
) -> str | None:
    generated_thumb_dir.mkdir(parents=True, exist_ok=True)
    output_name = f"{sha1_text(relative_video_path)}.jpg"
    output_file = generated_thumb_dir / output_name
    if output_file.exists():
        return f"generated-thumbs/{output_name}"

    seek_seconds = choose_thumbnail_seek_seconds(duration_seconds)
    cmd = [
        "ffmpeg",
        "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{seek_seconds:.2f}",
        "-i", absolute_video_path,
        "-frames:v", "1",
        "-q:v", "3",
        str(output_file),
    ]
    try:
        subprocess.run(cmd, check=True, timeout=20)
        if output_file.exists():
            return f"generated-thumbs/{output_name}"
    except Exception:
        return None
    return None


def thumbnail_url_for(
    doc: dict[str, Any] | None,
    media_url_fn: Any,
    static_url_fn: Any,
) -> str | None:
    if not doc:
        return None
    kind = doc.get("thumbnail_kind")
    path = doc.get("thumbnail_path")
    if not path:
        return None
    if kind == "media":
        return media_url_fn(path)
    if kind == "static":
        return static_url_fn(path)
    return None
