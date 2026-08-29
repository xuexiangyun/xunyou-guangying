from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
TEMP_DIR = DATA_DIR / "temp"
DB_DIR = DATA_DIR / "db"
CONFIG_DIR = DATA_DIR / "config"
QWEN_CREDENTIALS_FILE = CONFIG_DIR / "qwen_credentials.json"
FRONTEND_DIST = ROOT_DIR / "frontend" / "dist"


def _find_binary(name: str) -> str:
    explicit = os.getenv(name.upper())
    if explicit and Path(explicit).exists():
        return explicit
    found = shutil.which(name)
    if found:
        return found
    for candidate in (Path("/opt/homebrew/bin") / name, Path("/usr/local/bin") / name):
        if candidate.exists():
            return str(candidate)
    return name


@dataclass(frozen=True)
class Settings:
    app_name: str = "瞬游光影"
    host: str = "127.0.0.1"
    port: int = 8503
    ffmpeg: str = _find_binary("ffmpeg")
    ffprobe: str = _find_binary("ffprobe")
    qwen_model: str = os.getenv("QWEN_MODEL", "qwen3-omni-flash")
    qwen_candidate_seconds: float = 30.0
    qwen_proxy_max_bytes: int = 7 * 1024 * 1024
    qwen_parallel_workers: int = 2
    transition_seconds: float = 0.35
    max_upload_bytes: int = 20 * 1024 * 1024 * 1024
    max_duration_seconds: float = 2 * 60 * 60
    max_width: int = 1920
    max_height: int = 1080
    chunk_size: int = 32 * 1024 * 1024
    qwen_text_input_cny_per_million: float = 1.80
    qwen_audio_input_cny_per_million: float = 15.80
    qwen_video_input_cny_per_million: float = 3.30
    qwen_text_output_cny_per_million: float = 12.70
    usd_to_cny: float = 6.75


settings = Settings()


def ensure_directories() -> None:
    for directory in (UPLOAD_DIR, OUTPUT_DIR, TEMP_DIR, DB_DIR, CONFIG_DIR):
        directory.mkdir(parents=True, exist_ok=True)
