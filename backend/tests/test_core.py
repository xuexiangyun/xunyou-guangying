from __future__ import annotations

import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from backend.app.database import Database
from backend.app.services import maintenance, pipeline, qwen
from backend.app.services import slices as local_slices
from backend.app.services.qwen import (
    GAME_LABELS,
    GAME_MODE_LABELS,
    GAME_MODE_PROFILES,
    GAME_PROFILES,
    build_analysis_prompt,
    build_verification_prompt,
    is_authentication_error,
    load_credentials,
    normalize_analysis_payload,
    normalize_api_host,
    qwen_error_message,
    save_credentials,
)
from backend.app.schemas import JobCreateRequest, JobOptions, LocalSliceRequest
from backend.app.services.subtitles import create_caption_images
from backend.app.services.pipeline import (
    SourceVideo,
    _apply_analysis_budget,
    _build_montage_plan,
    _candidate_bounds,
    _qualified_candidates,
    _should_generate_subtitles,
    _target_duration,
    analysis_budget_for,
)
from backend.app.services.video import (
    candidate_count_for_duration,
    estimate_cost,
    probe_video,
    render_analysis_proxy,
    render_montage,
)
from backend.app.settings import settings


def test_candidate_count_is_bounded() -> None:
    assert 6 <= candidate_count_for_duration(60, "strict") <= 24
    assert candidate_count_for_duration(7200, "broad") == 24


def test_cost_estimate_has_range() -> None:
    estimate = estimate_cost(1800, "balanced")
    assert estimate["candidate_count"] > 0
    assert estimate["image_count"] > estimate["candidate_count"]
    assert estimate["estimated_cny_high"] > estimate["estimated_cny_low"] >= 0


def test_invalid_qwen_key_has_specific_guidance() -> None:
    error = RuntimeError("Error code: 401 - unauthorized")
    message = qwen_error_message(error)
    assert is_authentication_error(error)
    assert "百炼 API Key" in message
    assert "sk-" in message


def test_only_official_qwen_api_hosts_are_allowed() -> None:
    value = "https://ws-123.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/"
    assert normalize_api_host(value).endswith("/compatible-mode/v1")
    with pytest.raises(ValueError):
        normalize_api_host("http://127.0.0.1:8000/compatible-mode/v1")


def test_qwen_credentials_are_saved_owner_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    credential_file = tmp_path / "config" / "qwen_credentials.json"
    monkeypatch.setattr(qwen, "QWEN_CREDENTIALS_FILE", credential_file)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_HOST", raising=False)
    host = "https://ws-test.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    save_credentials("sk-test-only", host)
    assert load_credentials() == ("sk-test-only", host)
    assert credential_file.stat().st_mode & 0o777 == 0o600


def test_general_fps_prompt_contains_dynamic_context() -> None:
    prompt = build_analysis_prompt(
        ["multi_kill", "victory", "utility"],
        0.82,
        30.0,
        "apex_legends",
        "battle_royale",
    )
    assert "Apex英雄" in prompt
    assert "大逃杀" in prompt
    assert "category必须为\"none\"" in prompt
    assert "secondary_categories" in prompt
    assert "speech_value" in prompt
    assert "当前玩家本人被击杀" in prompt
    assert "enemy_kill_confirmed" in prompt
    assert "player_death_in_clip" in prompt
    assert "彩虹六号游戏视频的专业" not in prompt


def test_rainbow_six_prompt_requires_game_specific_evidence() -> None:
    prompt = build_analysis_prompt(
        ["kill", "multi_kill", "clutch", "victory", "precision", "utility", "skill"],
        0.9,
        30.0,
        "rainbow_six_siege",
        "bomb",
    )
    for term in (
        "视角归属",
        "击倒当确认击杀",
        "失败的1vX叫clutch",
        "player_attribution",
        "clutch_situation",
        "score_breakdown",
        "operation_difficulty",
        "拆弹器",
    ):
        assert term in prompt


def test_auto_game_prompt_conditionally_enables_rainbow_six_rules() -> None:
    prompt = build_analysis_prompt(["multi_kill", "clutch", "utility"], 0.8, 30.0, "auto", "auto")
    assert "彩六优先条件分支" in prompt
    assert "确认这是《彩虹六号：围攻》" in prompt
    assert "若确认是其他游戏" in prompt
    assert '"rainbow_six"' in prompt


def test_auto_detected_rainbow_six_preserves_structured_context() -> None:
    result = normalize_analysis_payload(
        {
            "score": 78,
            "category": "utility",
            "detected_game": "彩虹六号：围攻",
            "rainbow_six": {
                "perspective": "first_person",
                "player_attribution": "confirmed",
                "event_type": "vertical_play",
                "kill_count": 1,
                "clutch_situation": "none",
                "round_outcome": "win",
                "objective_outcome": "none",
            },
        },
        ["utility", "skill"],
        30.0,
        "auto",
        "auto",
    )
    assert result["rainbow_six"]["event_type"] == "vertical_play"
    assert result["rainbow_six"]["round_outcome"] == "win"


def test_rainbow_six_ace_uses_structured_score_and_context() -> None:
    result = normalize_analysis_payload(
        {
            "score": 40,
            "confidence": 0.96,
            "category": "multi_kill",
            "secondary_categories": ["clutch", "victory"],
            "evidence_timestamps": [
                {"time": 8.0, "claim": "击杀栏确认第五次击杀"},
                {"time": 10.0, "claim": "回合胜利画面"},
            ],
            "rainbow_six": {
                "perspective": "first_person",
                "player_attribution": "confirmed",
                "event_type": "ace",
                "kill_count": 5,
                "clutch_situation": "1v3",
                "round_outcome": "win",
                "objective_outcome": "none",
                "score_breakdown": {
                    "operation_difficulty": 19,
                    "tactical_value": 18,
                    "round_importance": 14,
                    "rarity": 15,
                    "visual_clarity": 10,
                    "emotion": 9,
                    "narrative_completeness": 10,
                },
            },
        },
        ["multi_kill", "clutch", "victory"],
        30.0,
        "rainbow_six_siege",
        "bomb",
    )
    assert result["score"] == 95
    assert result["category"] == "multi_kill"
    assert result["rainbow_six"]["event_type"] == "ace"
    assert result["rainbow_six"]["kill_count"] == 5


def test_rainbow_six_failed_clutch_is_not_labeled_as_clutch() -> None:
    result = normalize_analysis_payload(
        {
            "score": 96,
            "category": "clutch",
            "secondary_categories": ["multi_kill", "skill"],
            "evidence_timestamps": [
                {"time": 7.0, "claim": "第三次击杀"},
                {"time": 11.0, "claim": "回合失败画面"},
            ],
            "rainbow_six": {
                "perspective": "first_person",
                "player_attribution": "confirmed",
                "event_type": "clutch",
                "kill_count": 3,
                "clutch_situation": "1v3",
                "round_outcome": "loss",
                "objective_outcome": "none",
                "score_breakdown": {
                    "operation_difficulty": 20,
                    "tactical_value": 19,
                    "round_importance": 15,
                    "rarity": 14,
                    "visual_clarity": 10,
                    "emotion": 10,
                    "narrative_completeness": 10,
                },
            },
        },
        ["multi_kill", "clutch", "skill"],
        30.0,
        "rainbow_six_siege",
        "round_based",
    )
    assert result["category"] == "multi_kill"
    assert result["score"] == 84


def test_rainbow_six_failed_clutch_without_independent_value_is_rejected() -> None:
    result = normalize_analysis_payload(
        {
            "score": 90,
            "category": "clutch",
            "rainbow_six": {
                "perspective": "first_person",
                "player_attribution": "confirmed",
                "event_type": "clutch",
                "kill_count": 0,
                "clutch_situation": "1v3",
                "round_outcome": "loss",
                "objective_outcome": "none",
            },
        },
        ["clutch", "skill"],
        30.0,
        "rainbow_six_siege",
        "round_based",
    )
    assert result["category"] == "none"
    assert result["score"] == 35


def test_rainbow_six_unclear_kill_attribution_cannot_qualify() -> None:
    result = normalize_analysis_payload(
        {
            "score": 92,
            "category": "multi_kill",
            "rainbow_six": {
                "perspective": "spectator",
                "player_attribution": "unclear",
                "event_type": "triple_kill",
                "kill_count": 3,
                "clutch_situation": "none",
                "round_outcome": "win",
                "objective_outcome": "none",
            },
        },
        ["multi_kill", "skill"],
        30.0,
        "rainbow_six_siege",
        "round_based",
    )
    assert result["category"] == "none"
    assert result["score"] == 35


def test_rainbow_six_high_score_requires_timestamped_evidence() -> None:
    result = normalize_analysis_payload(
        {
            "score": 95,
            "category": "multi_kill",
            "rainbow_six": {
                "perspective": "first_person",
                "player_attribution": "confirmed",
                "event_type": "triple_kill",
                "kill_count": 3,
                "clutch_situation": "none",
                "round_outcome": "win",
                "objective_outcome": "none",
                "score_breakdown": {
                    "operation_difficulty": 19,
                    "tactical_value": 17,
                    "round_importance": 14,
                    "rarity": 13,
                    "visual_clarity": 10,
                    "emotion": 9,
                    "narrative_completeness": 10,
                },
            },
        },
        ["multi_kill", "skill"],
        30.0,
        "rainbow_six_siege",
        "round_based",
    )
    assert result["score"] == 79


def test_replay_content_is_rejected_even_with_high_score() -> None:
    result = normalize_analysis_payload(
        {
            "score": 96,
            "confidence": 0.99,
            "content_type": "replay",
            "category": "multi_kill",
            "evidence_timestamps": [
                {"time": 4.0, "claim": "回放击杀"},
                {"time": 9.0, "claim": "胜利结算"},
            ],
        },
        ["multi_kill", "skill"],
        30.0,
        "rainbow_six_siege",
        "round_based",
    )
    assert result["category"] == "none"
    assert result["score"] == 35


def test_verification_prompt_treats_preliminary_result_as_hypothesis() -> None:
    prompt = build_verification_prompt(
        {"score": 92, "category": "clutch", "title": "1v2翻盘"},
        30.0,
        "rainbow_six_siege",
        "bomb",
    )
    assert "只是假设" in prompt
    assert "赛事回放" in prompt
    assert "evidence_timestamps" in prompt
    assert "player_death_in_clip" in prompt
    assert "player_death_time减2.0秒" in prompt
    assert "不要因此直接淘汰" in prompt


def test_candidate_with_player_death_inside_clip_is_trimmed_not_rejected() -> None:
    result = normalize_analysis_payload(
        {
            "score": 96,
            "confidence": 0.98,
            "content_type": "gameplay_first_person",
            "category": "multi_kill",
            "enemy_kill_confirmed": True,
            "player_death_detected": True,
            "player_death_time": 14.0,
            "player_death_in_clip": False,
            "clip_start": 7.0,
            "event_time": 11.0,
            "clip_end": 15.0,
            "recommended_duration": 8.0,
            "evidence_timestamps": [
                {"time": 10.0, "claim": "当前玩家确认击杀敌方"},
                {"time": 14.0, "claim": "当前玩家阵亡"},
            ],
        },
        ["multi_kill", "skill"],
        30.0,
        "call_of_duty",
        "team_respawn",
    )
    assert result["category"] == "multi_kill"
    assert result["score"] == 96
    assert result["clip_start"] == 6.0
    assert result["clip_end"] == 12.0
    assert result["player_death_in_clip"] is False
    assert result["death_safe_trimmed"] is True


def test_death_safe_short_clip_is_not_expanded_back_into_death() -> None:
    start, duration, event = _candidate_bounds(
        {
            "context_start": 0.0,
            "context_duration": 30.0,
            "event_time": 3.0,
            "clip_start": 0.0,
            "clip_end": 3.0,
            "recommended_duration": 12.0,
            "death_safe_trimmed": True,
        },
        30.0,
    )
    assert start == 0.0
    assert duration == 3.0
    assert event == 3.0


def test_candidate_ending_before_player_death_can_keep_enemy_kill() -> None:
    result = normalize_analysis_payload(
        {
            "score": 88,
            "confidence": 0.94,
            "content_type": "gameplay_first_person",
            "category": "kill",
            "enemy_kill_confirmed": True,
            "player_death_detected": True,
            "player_death_time": 18.0,
            "player_death_in_clip": False,
            "clip_start": 7.0,
            "event_time": 11.0,
            "clip_end": 16.5,
            "recommended_duration": 9.5,
            "evidence_timestamps": [
                {"time": 10.8, "claim": "当前玩家确认击杀敌方"},
                {"time": 12.0, "claim": "击杀结果完整"},
            ],
        },
        ["kill", "skill"],
        30.0,
        "call_of_duty",
        "team_respawn",
    )
    assert result["category"] == "kill"
    assert result["score"] == 88
    assert result["player_death_in_clip"] is False


def test_every_game_and_mode_has_a_prompt_profile() -> None:
    assert set(GAME_LABELS) == set(GAME_PROFILES)
    assert set(GAME_MODE_LABELS) == set(GAME_MODE_PROFILES)


@pytest.mark.parametrize(
    ("game", "mode", "required_terms"),
    [
        ("battlefield", "objective", ["载具", "据点", "占领"]),
        ("delta_force", "extraction", ["Warfare", "高价值物资", "成功撤离"]),
        ("counter_strike_global_offensive", "hostage", ["CS:GO", "人质救援", "误伤人质"]),
        ("counter_strike_2", "bomb", ["C4", "T/CT阵营", "A/B包点"]),
        ("rainbow_six_siege", "bomb", ["成功下包", "成功拆包", "阻止下包"]),
    ],
)
def test_selected_game_and_mode_rules_reach_qwen_prompt(game: str, mode: str, required_terms: list[str]) -> None:
    prompt = build_analysis_prompt(["kill", "clutch", "victory"], 0.75, 30.0, game, mode)
    for term in required_terms:
        assert term in prompt


def test_qwen_result_can_reject_non_highlight() -> None:
    result = normalize_analysis_payload(
        {
            "score": 91,
            "confidence": 1.4,
            "category": "unsupported_type",
            "secondary_categories": ["multi_kill", "utility", "bad_type"],
            "evidence": ["只有普通移动"],
        },
        ["multi_kill", "utility"],
        30.0,
        "auto",
        "auto",
    )
    assert result["category"] == "none"
    assert result["score"] == 35
    assert result["confidence"] == 1.0
    assert result["secondary_categories"] == ["multi_kill", "utility"]


def test_smart_subtitles_only_run_for_valuable_speech() -> None:
    pure_gameplay = [{"category": "precision", "secondary_categories": [], "speech_value": 18}]
    reaction = [
        {
            "category": "reaction",
            "secondary_categories": ["funny"],
            "speech_value": 78,
            "speech_reason": "队友持续欢呼",
        }
    ]
    assert _should_generate_subtitles({"subtitle_mode": "smart"}, pure_gameplay, True)[0] is False
    assert _should_generate_subtitles({"subtitle_mode": "smart"}, reaction, True)[0] is True
    assert _should_generate_subtitles({"subtitle_mode": "always"}, pure_gameplay, True)[0] is True
    assert _should_generate_subtitles({"subtitle_mode": "off"}, reaction, True)[0] is False
    assert _should_generate_subtitles({"subtitles": False}, reaction, True)[0] is False


def test_job_options_support_general_fps_context() -> None:
    options = JobOptions(game="valorant", game_mode="round_based", highlight_types=["kill", "precision"])
    assert options.game == "valorant"
    assert options.game_mode == "round_based"
    assert options.subtitle_mode == "smart"
    expanded = JobOptions(game="delta_force", game_mode="bomb", highlight_types=["clutch", "victory"])
    assert expanded.game == "delta_force"
    assert expanded.game_mode == "bomb"


def test_montage_duration_controls_segment_count() -> None:
    analyzed = [
        {
            "score": 100 - index,
            "context_start": index * 30.0,
            "event_time": 12.0,
            "recommended_duration": 10.0,
            "title": f"高光{index}",
            "category": "skill",
            "reason": "精彩操作",
            "provider": "local",
        }
        for index in range(10)
    ]
    short_plan = _build_montage_plan(analyzed, _target_duration("seconds_30", 600), 600)
    long_plan = _build_montage_plan(analyzed, _target_duration("seconds_120", 600), 600)
    assert len(short_plan) == 3
    assert len(long_plan) == 10
    assert sum(item["duration"] for item in short_plan) == pytest.approx(30, abs=0.01)
    assert sum(item["duration"] for item in long_plan) == pytest.approx(100, abs=0.01)

    transitioned_plan = _build_montage_plan(analyzed, 60, 600, transition_duration=0.35)
    effective_duration = sum(item["duration"] for item in transitioned_plan) - 0.35 * (len(transitioned_plan) - 1)
    assert effective_duration <= 60.25


def test_montage_uses_model_clip_boundaries() -> None:
    plan = _build_montage_plan(
        [
            {
                "score": 90,
                "context_start": 100.0,
                "context_duration": 30.0,
                "clip_start": 2.0,
                "event_time": 5.0,
                "clip_end": 10.0,
                "recommended_duration": 8.0,
                "title": "完整动作链",
                "category": "clutch",
                "reason": "包含前因与结果",
                "provider": "qwen",
            }
        ],
        30.0,
        300.0,
    )
    assert plan[0]["start"] == pytest.approx(102.0)
    assert plan[0]["duration"] == pytest.approx(8.0)


def test_qwen_failures_and_low_confidence_are_not_qualified() -> None:
    analyzed = [
        {"provider": "local", "category": "skill", "score": 60, "confidence": 0},
        {"provider": "qwen", "category": "multi_kill", "score": 82, "confidence": 0.5},
        {"provider": "qwen", "category": "clutch", "score": 84, "confidence": 0.9},
    ]
    strict = _qualified_candidates(analyzed, "strict", qwen_enabled=True)
    broad = _qualified_candidates(analyzed, "broad", qwen_enabled=True)
    assert [item["category"] for item in strict] == ["clutch"]
    assert [item["category"] for item in broad] == ["multi_kill", "clutch"]


def test_rainbow_six_candidate_requires_second_review() -> None:
    base = {
        "provider": "qwen",
        "category": "clutch",
        "score": 88,
        "confidence": 0.95,
        "rainbow_six": {"perspective": "first_person"},
    }
    assert _qualified_candidates([base], "strict", qwen_enabled=True) == []
    reviewed = {**base, "verification": {"reviewed": True}}
    assert _qualified_candidates([reviewed], "strict", qwen_enabled=True) == [reviewed]


def test_database_roundtrip(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    source = tmp_path / "video.mp4"
    source.touch()
    database.create_upload("upload", "video.mp4", source, 0, "video/mp4")
    database.update_upload("upload", state="ready", metadata_json={"duration": 20})
    upload = database.get_upload("upload")
    assert upload and upload["metadata"]["duration"] == 20
    database.create_job("job", "upload", {"duration_mode": "auto"})
    database.update_job("job", state="completed", result_json={"filename": "result.mp4"})
    job = database.get_job("job")
    assert job and job["result"]["filename"] == "result.mp4"


def test_cleanup_matches_uploads_by_name_not_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = Database(tmp_path / "maintenance.sqlite3")
    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir()
    monkeypatch.setattr(maintenance, "UPLOAD_DIR", uploads_dir)
    monkeypatch.setattr(maintenance, "OUTPUT_DIR", tmp_path / "outputs")
    monkeypatch.setattr(maintenance, "TEMP_DIR", tmp_path / "temp")
    (tmp_path / "outputs").mkdir()
    (tmp_path / "temp").mkdir()

    # Stale absolute path (simulates a moved project directory) must not cause deletion.
    database.create_upload("up1", "a.mp4", Path("/somewhere/else/up1.mp4"), 1, "video/mp4")
    real_file = uploads_dir / "up1.mp4"
    real_file.write_bytes(b"x")
    orphan = uploads_dir / "ghost.mp4"
    orphan.write_bytes(b"x")

    result = maintenance.cleanup_storage(database)
    assert real_file.exists()
    assert not orphan.exists()
    assert result["removed_upload_files"] == 1


def test_repair_stored_paths_after_move(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = Database(tmp_path / "repair.sqlite3")
    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir()
    monkeypatch.setattr(maintenance, "UPLOAD_DIR", uploads_dir)
    monkeypatch.setattr(maintenance, "OUTPUT_DIR", tmp_path / "outputs")
    monkeypatch.setattr(maintenance, "TEMP_DIR", tmp_path / "temp")
    (tmp_path / "outputs").mkdir()
    (tmp_path / "temp").mkdir()

    database.create_upload("up1", "a.mp4", Path("/old/uploads/up1.mp4"), 1, "video/mp4")
    (uploads_dir / "up1.mp4").write_bytes(b"x")

    assert maintenance.repair_stored_paths(database) == 1
    assert database.get_upload("up1")["stored_path"] == str(uploads_dir / "up1.mp4")


def test_caption_image_is_created(tmp_path: Path) -> None:
    images = create_caption_images(
        [{"start": 1.0, "end": 3.0, "text": "漂亮的残局胜利"}],
        tmp_path,
        "gaming",
        1280,
        720,
    )
    assert len(images) == 1
    assert images[0][0].exists()


@pytest.mark.integration
def test_local_pipeline_with_synthetic_video(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "fps_test.mp4"
    subprocess.run(
        [
            settings.ffmpeg,
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1280x720:rate=30:duration=20",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=20",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(source),
        ],
        check=True,
    )
    metadata = probe_video(source)
    database = Database(tmp_path / "pipeline.sqlite3")
    database.create_upload("upload", source.name, source, source.stat().st_size, "video/mp4")
    database.update_upload("upload", state="ready", received_bytes=source.stat().st_size, metadata_json=metadata)
    options = {
        "game": "auto",
        "game_mode": "auto",
        "highlight_types": ["kill", "clutch", "skill"],
        "duration_mode": "auto",
        "subtitles": False,
        "subtitle_style": "gaming",
        "sensitivity": "strict",
    }
    database.create_job("job", "upload", options)
    monkeypatch.setattr(pipeline, "TEMP_DIR", tmp_path / "temp")
    monkeypatch.setattr(pipeline, "OUTPUT_DIR", tmp_path / "output")
    pipeline.run_pipeline(
        database=database,
        job_id="job",
        sources=[SourceVideo(path=source, metadata=metadata)],
        options=options,
        qwen_api_key="",
        qwen_api_host="",
    )
    job = database.get_job("job")
    assert job and job["state"] == "completed", job.get("error") if job else None
    assert Path(job["result"]["output_path"]).exists()

    proxy = render_analysis_proxy(source, tmp_path / "qwen_proxy.mp4", 0.0, 20.0)
    assert proxy.exists()
    assert proxy.stat().st_size <= settings.qwen_proxy_max_bytes
    proxy_metadata = probe_video(proxy)
    assert proxy_metadata["width"] == 1280
    assert proxy_metadata["height"] == 720

    montage = tmp_path / "two-part-montage.mp4"
    render_montage(
        montage,
        [
            {"start": 0.0, "duration": 4.0, "source": str(source)},
            {"start": 10.0, "duration": 4.0, "source": str(source)},
        ],
    )
    montage_metadata = probe_video(montage)
    assert montage_metadata["duration"] == pytest.approx(8.0, abs=0.2)

    transitioned_montage = tmp_path / "two-part-transitioned-montage.mp4"
    transition_result = render_montage(
        transitioned_montage,
        [
            {"start": 0.0, "duration": 4.0, "source": str(source)},
            {"start": 10.0, "duration": 4.0, "source": str(source)},
        ],
        transition_duration=1.5,
    )
    transitioned_metadata = probe_video(transitioned_montage)
    assert transitioned_metadata["duration"] == pytest.approx(6.5, abs=0.25)
    assert transition_result["transition_count"] == 1
    assert transition_result["transition_style"] == "diagonal_stripes"


def test_job_create_request_resolves_upload_ids_with_legacy_support() -> None:
    legacy = JobCreateRequest(upload_id="u1")
    assert legacy.resolved_upload_ids() == ["u1"]

    normalized = JobCreateRequest(upload_ids=[" u1 ", "u2", "u1", " ", "u2"])
    assert normalized.resolved_upload_ids() == ["u1", "u2"]

    both = JobCreateRequest(upload_id="legacy", upload_ids=["u1", "u2"])
    assert both.resolved_upload_ids() == ["u1", "u2"]

    with pytest.raises(ValueError):
        JobCreateRequest().resolved_upload_ids()

    with pytest.raises(ValueError):
        JobCreateRequest(upload_ids=[f"u{index}" for index in range(9)]).resolved_upload_ids()


def test_analysis_budget_keeps_every_file_represented() -> None:
    candidates = [{"file_index": 0, "rank": rank} for rank in range(10)] + [
        {"file_index": 1, "rank": rank} for rank in range(3)
    ]
    trimmed = _apply_analysis_budget(candidates, 8)
    assert len(trimmed) == 8
    # The shorter recording keeps all of its candidates instead of being
    # crowded out by the longer one.
    assert sum(1 for item in trimmed if item["file_index"] == 1) == 3
    assert sum(1 for item in trimmed if item["file_index"] == 0) == 5
    assert _apply_analysis_budget(candidates, 20) == candidates


def test_montage_plan_dedups_events_per_file_and_orders_by_file() -> None:
    def candidate(score: float, file_index: int, path: str, event_time: float) -> dict:
        return {
            "score": score,
            "category": "skill",
            "title": f"片段{file_index}",
            "confidence": 0.8,
            "reason": "本地测试候选",
            "provider": "local",
            "file_index": file_index,
            "source": path,
            "filename": Path(path).name,
            "context_start": 0.0,
            "context_duration": 30.0,
            "event_time": event_time,
            "clip_start": 10.0,
            "clip_end": 22.0,
            "death_safe_trimmed": True,
        }

    analyzed = [
        candidate(90, 1, "/tmp/round-2.mp4", 15.0),
        candidate(80, 0, "/tmp/round-1.mp4", 15.0),
        # Same file as the 80-score event and nearly overlapping: deduped.
        candidate(70, 0, "/tmp/round-1.mp4", 16.0),
    ]
    plan = _build_montage_plan(analyzed, 60.0, [600.0, 600.0], 0.0)
    assert len(plan) == 2
    assert [segment["file_index"] for segment in plan] == [0, 1]
    assert plan[0]["source"] == "/tmp/round-1.mp4"
    assert plan[0]["filename"] == "round-1.mp4"
    assert plan[1]["source"] == "/tmp/round-2.mp4"
    assert plan[1]["filename"] == "round-2.mp4"


def test_database_stores_multi_file_jobs_and_migrates_legacy_schema(tmp_path: Path) -> None:
    legacy_path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(legacy_path)
    connection.executescript(
        """
        CREATE TABLE uploads (
            id TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            stored_path TEXT NOT NULL,
            size INTEGER NOT NULL,
            received_bytes INTEGER NOT NULL DEFAULT 0,
            content_type TEXT NOT NULL,
            state TEXT NOT NULL,
            metadata_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY,
            upload_id TEXT NOT NULL REFERENCES uploads(id) ON DELETE CASCADE,
            state TEXT NOT NULL,
            stage TEXT NOT NULL,
            progress REAL NOT NULL DEFAULT 0,
            message TEXT NOT NULL,
            options_json TEXT NOT NULL,
            metadata_json TEXT,
            result_json TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        PRAGMA user_version = 1;
        """
    )
    connection.commit()
    connection.close()

    database = Database(legacy_path)
    with sqlite3.connect(legacy_path) as migrated:
        columns = {row[1] for row in migrated.execute("PRAGMA table_info(jobs)").fetchall()}
        assert "upload_ids_json" in columns
        assert "files_metadata_json" in columns
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == 2
    migrated.close()

    metadata_by_upload = {
        "u1": {"width": 1280, "duration": 60.0},
        "u2": {"width": 960, "duration": 45.0},
    }
    for upload_id, metadata in metadata_by_upload.items():
        path = tmp_path / f"{upload_id}.mp4"
        path.write_bytes(b"placeholder")
        database.create_upload(upload_id, path.name, path, path.stat().st_size, "video/mp4")
        database.update_upload(
            upload_id,
            state="ready",
            received_bytes=path.stat().st_size,
            metadata_json=metadata,
        )

    database.create_job(
        "job",
        "u1",
        {"game": "auto", "sensitivity": "balanced"},
        upload_ids=["u1", "u2"],
        files_metadata=[metadata_by_upload["u1"], metadata_by_upload["u2"]],
    )
    job = database.get_job("job")
    assert job is not None
    assert job["upload_id"] == "u1"
    assert job["upload_ids"] == ["u1", "u2"]
    assert job["files_metadata"] == [metadata_by_upload["u1"], metadata_by_upload["u2"]]

    database.update_job("job", upload_ids_json=["u1"], files_metadata_json=[metadata_by_upload["u1"]])
    updated = database.get_job("job")
    assert updated is not None
    assert updated["upload_ids"] == ["u1"]
    assert updated["files_metadata"] == [metadata_by_upload["u1"]]


@pytest.mark.integration
def test_multi_file_pipeline_produces_cross_file_montage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "match-1.mp4"
    subprocess.run(
        [
            settings.ffmpeg,
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1280x720:rate=30:duration=20",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=20",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(first),
        ],
        check=True,
    )
    second = tmp_path / "match-2.mp4"
    subprocess.run(
        [
            settings.ffmpeg,
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=960x540:rate=24:duration=20",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(second),
        ],
        check=True,
    )

    database = Database(tmp_path / "multi.sqlite3")
    sources: list[pipeline.SourceVideo] = []
    for index, path in enumerate((first, second), start=1):
        metadata = probe_video(path)
        upload_id = f"upload-{index}"
        database.create_upload(upload_id, path.name, path, path.stat().st_size, "video/mp4")
        database.update_upload(
            upload_id,
            state="ready",
            received_bytes=path.stat().st_size,
            metadata_json=metadata,
        )
        sources.append(pipeline.SourceVideo(path=path, metadata=metadata, filename=path.name))

    options = {
        "game": "auto",
        "game_mode": "auto",
        "highlight_types": ["kill", "clutch", "skill"],
        "duration_mode": "seconds_30",
        "subtitles": False,
        "subtitle_style": "gaming",
        "sensitivity": "balanced",
    }
    database.create_job(
        "job",
        "upload-1",
        options,
        upload_ids=["upload-1", "upload-2"],
        files_metadata=[sources[0].metadata, sources[1].metadata],
    )
    monkeypatch.setattr(pipeline, "TEMP_DIR", tmp_path / "temp")
    monkeypatch.setattr(pipeline, "OUTPUT_DIR", tmp_path / "output")
    pipeline.run_pipeline(
        database=database,
        job_id="job",
        sources=sources,
        options=options,
        qwen_api_key="",
        qwen_api_host="",
    )

    job = database.get_job("job")
    assert job and job["state"] == "completed", job.get("error") if job else None
    result = job["result"]
    assert result["file_count"] == 2
    assert [item["filename"] for item in result["files"]] == ["match-1.mp4", "match-2.mp4"]
    assert {segment["source"] for segment in result["segments"]} == {str(first), str(second)}

    output = Path(result["output_path"])
    assert output.exists()
    output_metadata = probe_video(output)
    # Normalized to the smallest shared resolution, and the audio-less source
    # received an injected silent track.
    assert output_metadata["width"] == 960
    assert output_metadata["height"] == 540
    assert output_metadata["has_audio"] is True


def test_candidate_proxies_survive_until_next_cleanup(tmp_path: Path) -> None:
    base = tmp_path / "temp"
    job_dir = base / "job-1"
    candidate_dir = job_dir / "candidate_00"
    candidate_dir.mkdir(parents=True)
    (candidate_dir / "qwen_candidate.mp4").write_bytes(b"proxy")
    raw_clip = job_dir / "highlight_raw.mp4"
    raw_clip.write_bytes(b"raw")

    assert maintenance.clear_candidate_dirs(base) == 1
    assert not candidate_dir.exists()
    # The job directory and non-candidate files are left for inspection.
    assert job_dir.is_dir()
    assert raw_clip.exists()
    assert maintenance.clear_candidate_dirs(tmp_path / "missing") == 0


def test_local_slice_request_shares_upload_selection_rules() -> None:
    assert LocalSliceRequest(upload_id="u1").resolved_upload_ids() == ["u1"]
    assert LocalSliceRequest(upload_ids=["a", "a", "b"]).resolved_upload_ids() == ["a", "b"]
    with pytest.raises(ValueError):
        LocalSliceRequest().resolved_upload_ids()


def test_analysis_budget_matches_job_and_slice_paths() -> None:
    assert analysis_budget_for(30.0, "balanced", 1) == 12
    assert analysis_budget_for(60.0, "balanced", 1) == 12
    assert analysis_budget_for(90.0, "balanced", 1) == 16
    assert analysis_budget_for(120.0, "balanced", 1) == 20
    assert analysis_budget_for(60.0, "broad", 1) == 14
    assert analysis_budget_for(60.0, "balanced", 3) == 16


def test_slice_task_renders_exactly_the_qwen_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local_slices, "TEMP_DIR", tmp_path / "temp")
    monkeypatch.setattr(
        local_slices,
        "rank_local_candidates",
        lambda path, duration, sensitivity, callback=None, token=None: [
            {"center": float(index * 25 + 20), "local_score": round(1 - index * 0.02, 4)}
            for index in range(20)
        ],
    )

    def fake_render(source, output, start, duration, token=None):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"proxy")
        return output

    monkeypatch.setattr(local_slices, "render_analysis_proxy", fake_render)

    metadata = {"duration": 600.0, "width": 1920, "height": 1080, "has_audio": True}
    source = SourceVideo(path=tmp_path / "a.mp4", metadata=metadata)
    manager = local_slices.LocalSliceManager()
    task_id = manager.submit([source], sensitivity="balanced", duration_mode="seconds_30")

    deadline = time.time() + 30
    task = manager.status(task_id)
    while task and task["state"] in {"queued", "running"} and time.time() < deadline:
        time.sleep(0.05)
        task = manager.status(task_id)

    assert task and task["state"] == "completed", task
    # 20 local candidates are trimmed to the same 12-slot budget a real job uses.
    assert len(task["slices"]) == analysis_budget_for(30.0, "balanced", 1) == 12
    assert all(item["filename"].startswith("candidate_") for item in task["slices"])


def test_orphan_uploads_are_removed_but_active_ones_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir()
    monkeypatch.setattr(maintenance, "UPLOAD_DIR", uploads_dir)
    monkeypatch.setattr(maintenance, "OUTPUT_DIR", tmp_path / "outputs")
    monkeypatch.setattr(maintenance, "TEMP_DIR", tmp_path / "temp")

    database = Database(tmp_path / "db.sqlite3")
    for upload_id in ("u1", "u2", "u3"):
        path = uploads_dir / f"{upload_id}.mp4"
        path.write_bytes(b"x")
        database.create_upload(upload_id, f"{upload_id}.mp4", path, 1, "video/mp4")
        database.update_upload(upload_id, state="ready", received_bytes=1)
    database.create_job("job", "u1", {"game": "auto", "sensitivity": "balanced"}, upload_ids=["u1"])
    # u3 is an upload still in progress and must survive.
    database.update_upload("u3", state="uploading")

    assert maintenance.discard_orphan_uploads(database) == 1
    assert database.get_upload("u1") is not None
    assert database.get_upload("u2") is None
    assert database.get_upload("u3") is not None
    assert (uploads_dir / "u1.mp4").exists()
    assert not (uploads_dir / "u2.mp4").exists()
    assert (uploads_dir / "u3.mp4").exists()

    # An upload held by a running local-slice task is protected too.
    path = uploads_dir / "u4.mp4"
    path.write_bytes(b"x")
    database.create_upload("u4", "u4.mp4", path, 1, "video/mp4")
    database.update_upload("u4", state="ready", received_bytes=1)
    assert maintenance.discard_orphan_uploads(database, keep_upload_ids={"u4"}) == 0
    assert database.get_upload("u4") is not None


@pytest.mark.integration
def test_local_slice_manager_writes_candidate_clips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local_slices, "TEMP_DIR", tmp_path / "temp")
    video = tmp_path / "match.mp4"
    subprocess.run(
        [
            settings.ffmpeg,
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=15:duration=8",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(video),
        ],
        check=True,
    )
    metadata = probe_video(video)

    manager = local_slices.LocalSliceManager()
    task_id = manager.submit([SourceVideo(path=video, metadata=metadata)])

    deadline = time.time() + 120
    task = manager.status(task_id)
    while task and task["state"] in {"queued", "running"} and time.time() < deadline:
        time.sleep(0.2)
        task = manager.status(task_id)

    assert task and task["state"] == "completed", task
    assert task["slices"], task
    for item in task["slices"]:
        assert Path(item["path"]).exists()
        assert item["source"] == "match.mp4"
        assert item["duration"] > 0
    assert (Path(task["directory"]) / "slices.json").exists()
