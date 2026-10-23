from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class DurationMode(StrEnum):
    SECONDS_30 = "seconds_30"
    SECONDS_60 = "seconds_60"
    SECONDS_90 = "seconds_90"
    SECONDS_120 = "seconds_120"
    # Keep legacy values readable for tasks created by older app versions.
    AUTO = "auto"
    SHORT = "short"
    LONG = "long"


class Sensitivity(StrEnum):
    STRICT = "strict"
    BALANCED = "balanced"
    BROAD = "broad"


class SubtitleStyle(StrEnum):
    GAMING = "gaming"
    MINIMAL = "minimal"
    NEON = "neon"


class SubtitleMode(StrEnum):
    SMART = "smart"
    OFF = "off"
    ALWAYS = "always"


class Game(StrEnum):
    AUTO = "auto"
    RAINBOW_SIX_SIEGE = "rainbow_six_siege"
    VALORANT = "valorant"
    COUNTER_STRIKE_2 = "counter_strike_2"
    COUNTER_STRIKE_GLOBAL_OFFENSIVE = "counter_strike_global_offensive"
    CALL_OF_DUTY = "call_of_duty"
    BATTLEFIELD = "battlefield"
    DELTA_FORCE = "delta_force"
    APEX_LEGENDS = "apex_legends"
    PUBG = "pubg"
    OVERWATCH_2 = "overwatch_2"
    ESCAPE_FROM_TARKOV = "escape_from_tarkov"
    THE_FINALS = "the_finals"
    CROSSFIRE = "crossfire"
    HALO_INFINITE = "halo_infinite"
    DESTINY_2 = "destiny_2"
    OTHER_FPS = "other_fps"


class GameMode(StrEnum):
    AUTO = "auto"
    ROUND_BASED = "round_based"
    BATTLE_ROYALE = "battle_royale"
    TEAM_RESPAWN = "team_respawn"
    EXTRACTION = "extraction"
    OBJECTIVE = "objective"
    HOSTAGE = "hostage"
    BOMB = "bomb"
    CAMPAIGN = "campaign"


HIGHLIGHT_TYPES = {
    "kill",
    "multi_kill",
    "clutch",
    "escape",
    "teamfight",
    "boss",
    "victory",
    "reaction",
    "skill",
    "precision",
    "utility",
    "rescue",
    "funny",
    "unexpected",
}

MAX_JOB_FILES = 8


class UploadInitRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0)
    content_type: str = "application/octet-stream"

    @field_validator("filename")
    @classmethod
    def filename_only(cls, value: str) -> str:
        if value != value.replace("\\", "/").split("/")[-1]:
            raise ValueError("文件名不能包含路径")
        return value


class UploadInitResponse(BaseModel):
    upload_id: str
    chunk_size: int
    received_bytes: int = 0


class UploadStatus(BaseModel):
    upload_id: str
    filename: str
    size: int
    received_bytes: int
    state: str
    metadata: dict | None = None


class JobOptions(BaseModel):
    game: Game = Game.AUTO
    game_mode: GameMode = GameMode.AUTO
    highlight_types: list[str] = Field(
        default_factory=lambda: [
            "kill",
            "multi_kill",
            "clutch",
            "escape",
            "teamfight",
            "victory",
            "reaction",
            "skill",
            "precision",
            "utility",
            "rescue",
            "boss",
            "funny",
            "unexpected",
        ]
    )
    duration_mode: DurationMode = DurationMode.SECONDS_60
    subtitle_mode: SubtitleMode = SubtitleMode.SMART
    subtitles: bool | None = None  # Legacy option kept for tasks created by older versions.
    subtitle_style: SubtitleStyle = SubtitleStyle.GAMING
    sensitivity: Sensitivity = Sensitivity.BALANCED

    @field_validator("highlight_types")
    @classmethod
    def validate_highlight_types(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(values))
        invalid = set(normalized) - HIGHLIGHT_TYPES
        if invalid:
            raise ValueError(f"未知高光类型: {', '.join(sorted(invalid))}")
        if not normalized:
            raise ValueError("至少选择一种高光类型")
        return normalized


class JobCreateRequest(BaseModel):
    upload_id: str | None = None  # Legacy single-file field kept for older clients.
    upload_ids: list[str] = Field(default_factory=list)
    options: JobOptions = Field(default_factory=JobOptions)

    @field_validator("upload_ids")
    @classmethod
    def normalize_upload_ids(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values if value and value.strip()]
        return list(dict.fromkeys(normalized))

    def resolved_upload_ids(self) -> list[str]:
        ids = list(self.upload_ids)
        if not ids and self.upload_id:
            ids = [self.upload_id]
        if not ids:
            raise ValueError("请至少提供一个上传文件。")
        if len(ids) > MAX_JOB_FILES:
            raise ValueError(f"一次最多支持{MAX_JOB_FILES}个录像文件。")
        return ids


class QwenCredentialRequest(BaseModel):
    qwen_api_key: str = Field(min_length=1, max_length=512)
    qwen_api_host: str = Field(min_length=1, max_length=1024)


class JobStatus(BaseModel):
    job_id: str
    upload_id: str
    upload_ids: list[str] = Field(default_factory=list)
    state: Literal["queued", "running", "completed", "failed", "cancelled"]
    stage: str
    progress: float
    message: str
    options: JobOptions
    filename: str | None = None
    filenames: list[str] = Field(default_factory=list)
    source_size: int | None = None
    files_metadata: list[dict] = Field(default_factory=list)
    metadata: dict | None = None
    result: dict | None = None
    error: str | None = None
    created_at: str
    updated_at: str


class CostEstimate(BaseModel):
    candidate_count: int
    image_count: int
    estimated_usd_low: float
    estimated_usd_high: float
    estimated_cny_low: float
    estimated_cny_high: float
    free_tier_may_be_zero: bool = True
    model: str
