from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..prompts.rainbow_six_siege import (
    R6_CLUTCH_SITUATIONS,
    R6_EVENT_TYPES,
    R6_OBJECTIVE_OUTCOMES,
    R6_PERSPECTIVES,
    R6_PLAYER_ATTRIBUTIONS,
    R6_ROUND_OUTCOMES,
    R6_SCORE_LIMITS,
    RAINBOW_SIX_OUTPUT_EXTENSION,
    RAINBOW_SIX_SIEGE_PROFILE,
)
from ..settings import QWEN_CREDENTIALS_FILE, settings


log = logging.getLogger(__name__)


CONTENT_TYPES = {
    "gameplay_first_person",
    "gameplay_spectator",
    "replay",
    "operator_select",
    "menu",
    "round_result",
    "mixed",
    "unknown",
}
NON_ACTION_CONTENT_TYPES = {"replay", "operator_select", "menu", "round_result"}


HIGHLIGHT_LABELS = {
    "kill": "关键击杀",
    "multi_kill": "连续击杀",
    "clutch": "逆风翻盘或关键局势逆转",
    "escape": "残血逃生或极限生存",
    "teamfight": "团战、补枪或团队配合",
    "boss": "Boss或精英敌人战斗",
    "victory": "目标完成、获胜或庆祝",
    "reaction": "玩家或队友的激烈语音反应",
    "skill": "高水平操作、精准枪法或战术执行",
    "precision": "远距离、爆头或高难度精准击杀",
    "utility": "技能、投掷物或战术道具配合",
    "rescue": "关键救援、复活或保护队友",
    "funny": "有趣、搞笑但不是普通失误",
    "unexpected": "意外、罕见或难以预料的事件",
}


GAME_LABELS = {
    "auto": "自动识别具体FPS游戏",
    "rainbow_six_siege": "彩虹六号：围攻",
    "valorant": "无畏契约",
    "counter_strike_2": "Counter-Strike 2（CS2）",
    "counter_strike_global_offensive": "Counter-Strike: Global Offensive（CS:GO）",
    "call_of_duty": "使命召唤系列",
    "battlefield": "战地系列",
    "delta_force": "三角洲行动",
    "apex_legends": "Apex英雄",
    "pubg": "绝地求生（PUBG）",
    "overwatch_2": "守望先锋2",
    "escape_from_tarkov": "逃离塔科夫",
    "the_finals": "THE FINALS",
    "crossfire": "穿越火线",
    "halo_infinite": "光环：无限",
    "destiny_2": "命运2",
    "other_fps": "其他第一人称射击游戏",
}


GAME_MODE_LABELS = {
    "auto": "自动判断游戏模式",
    "round_based": "回合制竞技",
    "battle_royale": "大逃杀",
    "team_respawn": "团队重生或死亡竞赛",
    "extraction": "战术撤离",
    "objective": "占点、推车或任务目标",
    "hostage": "人质救援或防守",
    "bomb": "炸弹模式（下包与拆弹）",
    "campaign": "单人战役",
}


GAME_PROFILES = {
    "auto": "先根据HUD布局、击杀信息、角色技能、武器、目标提示和胜利界面识别具体游戏。无法可靠确认时不要猜测游戏专属机制，改用通用FPS证据判断。",
    "rainbow_six_siege": RAINBOW_SIX_SIEGE_PROFILE,
    "valorant": "重点识别特工技能、爆能器安装/拆除、回合比分、击杀播报、首杀、补枪、Ace、多杀和残局。技能连招、经济局反杀、守包/回防和在时间压力下完成目标属于高价值证据。",
    "counter_strike_2": "重点识别T/CT阵营、C4安装与拆除、A/B包点、击杀播报、经济局、首杀、补枪、Ace、残局、AWP或高难度投掷物。必须区分击杀、助攻和回合真正获胜，人质目标的结果高于无结果刷枪。",
    "counter_strike_global_offensive": "按CS:GO经典竞技规则理解T/CT阵营、C4、人质救援、经济、护甲与拆弹器；重点识别Ace、残局、首杀、补枪、AWP、投掷物和回合胜负。必须结合击杀播报、计时器和目标提示确认结果。",
    "call_of_duty": "根据画面区分多人竞技、搜索与摧毁、战区和僵尸等玩法；重点识别连续击杀、击杀连杀奖励、快速多杀、目标占领、下包/拆包、倒地与彻底淘汰。重生模式中单次普通击杀价值较低，连杀、目标逆转和决胜操作价值更高。",
    "battlefield": "重点识别征服、突破、突袭等大战场目标，区分步兵、坦克、飞机和直升机战斗；关注占点/守点、推进或守住区域、摧毁载具、远距离击杀、多人连杀、救起队友和小队协作。不要只按击杀数评分，改变据点、兵力值或进攻扇区结果的行动更重要。",
    "delta_force": "先区分大战场Warfare、烽火地带/Operations撤离和黑鹰坠落战役。大战场重点看攻防区域、载具、兵种技能、救援和团队推进；撤离模式重点看高价值物资、击倒与淘汰、队友救援、生存和成功撤离；战役重点看任务节点和高难度战斗。",
    "apex_legends": "重点区分击倒、彻底淘汰和整队团灭，结合传奇技能、护甲破裂、倒地状态、复活/重生、缩圈、排名与冠军界面。1v多、残血反杀、救起队友、技能联动和最终吃鸡价值高于普通远距离消耗。",
    "pubg": "重点区分击倒与最终淘汰，识别小队团灭、救援、载具、投掷物、毒圈压力、剩余人数、排名和大吉大利界面。远距离精准击杀、1v多、决赛圈生存与最终获胜是核心高光，普通搜物资和无结果对枪不得高分。",
    "overwatch_2": "重点识别英雄技能、终极技能、击杀播报、团队团灭、关键救援、占点/推车/机器人进度和加时。多杀只是证据之一，终极技能联动、保护队友、阻止占点和改变团战胜负的操作更重要。",
    "escape_from_tarkov": "重点识别PMC/Scav、贵重物资、任务物品、受伤状态、治疗、Boss、队友配合和撤离倒计时。成功带出高价值物资、以弱胜强、极限治疗生存和安全撤离高于普通遭遇战；死亡前的无结果击杀不应视为完整胜利。",
    "the_finals": "重点识别Cashout保险箱、存款/窃取进度、团队团灭、复活雕像、建筑破坏、职业技能和最终比分。围绕Cashout的抢夺、最后一秒窃取、守点和利用破坏改变战局，价值高于远离目标的普通击杀。",
    "crossfire": "重点识别潜伏者/保卫者、幽灵模式、团队竞技、击杀播报、连杀和残局。幽灵模式中的潜行或关键发现，应结合回合结果评分。",
    "halo_infinite": "重点识别护盾破裂、能量武器、载具、击杀奖章、连续多杀、夺旗/抢球/据点目标和最终比分。高价值操作包括连杀奖章、夺取强力武器、护送目标和扭转团队比分。",
    "destiny_2": "先区分熔炉竞技与PVE活动；竞技重点看守护者击杀、连续击杀、超能、复活和占点，PVE重点看Boss阶段、机制执行、濒死救援、稀有掉落和任务完成。普通清怪不能等同于Boss击杀或关键机制成功。",
    "other_fps": "使用通用FPS规则，结合击杀播报、准星反馈、血量、弹药、队伍状态、目标进度、计时器、声音和胜利界面确认事件。无法确认专属机制时应降低置信度，并在evidence中说明实际观察到的证据。",
}


GAME_MODE_PROFILES = {
    "auto": "先判断是回合制、大逃杀、重生、撤离、目标、炸弹、人质还是战役。没有足够HUD证据时不要强行套用某种规则。",
    "round_based": "死亡通常会影响整回合且可能不能立即重生。重点结合存活人数、回合计时、比分、经济、残局和回合结束界面；普通单杀低于首杀、补枪、Ace、1vX翻盘和决胜回合。",
    "battle_royale": "严格区分击倒与彻底淘汰，关注小队团灭、复活/重生、缩圈、剩余人数、排名和最终冠军。前期普通击倒价值较低，决赛圈、1v多和吃鸡结果价值最高。",
    "team_respawn": "玩家可反复重生，单次普通击杀通常不是高光。重点寻找快速连杀、连续存活、击杀连杀奖励、关键防守、比分反超和与团队目标直接相关的战斗。",
    "extraction": "结果必须结合物资价值、任务物品、队伍存活和是否成功撤离判断。高价值搜刮后的安全撤离、队友救援和撤离点极限战斗高于中途无结果击杀。",
    "objective": "重点读取占领、争夺、推车、机器人、Cashout、扇区或任务进度。只有直接推进、守住、抢回或在加时完成目标的击杀与配合才应显著加分。",
    "hostage": "识别人质位置、拾取/护送/救援进度、救援区、攻守阵营和回合结果。成功接触并带出人质、阻止救援、掩护队友和最后时刻完成目标属于高光；误伤人质、远离目标刷击杀和未完成救援不得作为胜利证据。",
    "bomb": "这是《彩虹六号：围攻》的炸弹局。高光评测以目标结果为准：进攻方以成功下包（安装拆弹器）为核心，防守方以成功拆包（拆除拆弹器）或阻止下包为核心。进攻方成功下包、下包后的守包与回防、计时压力下守住拆弹器直到引爆属于高价值；防守方成功拆包、阻止下包、极限拆弹、假拆和关键回防属于高价值。必须用HUD、拆弹器状态、倒计时、播报或回合胜利画面确认下包、拆包或阻止下包真正成功，不能只因靠近包点或看到拆弹器就判定成功，失败的尝试不得算作高光。",
    "campaign": "重点寻找任务目标完成、Boss/精英敌人击败、困难遭遇、濒死生存、关键剧情动作和大型演出场面。普通赶路、重复清怪和过场等待不得高分。",
}


def normalize_api_host(api_host: str) -> str:
    value = api_host.strip().rstrip("/")
    if not value:
        raise ValueError("请粘贴创建 API Key 时显示的 API Host。")
    parsed = urlparse(value)
    hostname = (parsed.hostname or "").lower()
    official_host = hostname == "dashscope.aliyuncs.com" or hostname.endswith(".maas.aliyuncs.com")
    if parsed.scheme != "https" or not official_host:
        raise ValueError("API Host 必须是阿里云百炼提供的 https 地址。")
    if not parsed.path.endswith("/compatible-mode/v1"):
        raise ValueError("API Host 不完整，应以 /compatible-mode/v1 结尾。")
    return value


def normalize_api_key(api_key: str) -> str:
    key = api_key.strip()
    if not key:
        raise ValueError("请先填写阿里云百炼生成的 API Key。")
    if not key.startswith("sk-"):
        raise ValueError("百炼 API Key 通常以 sk- 开头，请重新复制。")
    if key.startswith("sk-sp-"):
        raise ValueError("这里需要按量付费 API Key，不能使用 Token Plan 或 Coding Plan 的 sk-sp- Key。")
    return key


def save_credentials(api_key: str, api_host: str) -> None:
    """Persist credentials locally with owner-only file permissions."""
    key = normalize_api_key(api_key)
    host = normalize_api_host(api_host)
    QWEN_CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = QWEN_CREDENTIALS_FILE.with_suffix(".tmp")
    payload = json.dumps({"api_key": key, "api_host": host}, ensure_ascii=False)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    os.replace(temporary, QWEN_CREDENTIALS_FILE)
    os.chmod(QWEN_CREDENTIALS_FILE, 0o600)


def load_credentials() -> tuple[str, str] | None:
    """Load an environment override or the owner-only local credential file."""
    environment_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    environment_host = os.getenv("DASHSCOPE_API_HOST", "").strip()
    if environment_key or environment_host:
        if not environment_key or not environment_host:
            raise ValueError("DASHSCOPE_API_KEY 和 DASHSCOPE_API_HOST 必须同时配置。")
        return normalize_api_key(environment_key), normalize_api_host(environment_host)
    if not QWEN_CREDENTIALS_FILE.exists():
        return None
    try:
        payload = json.loads(QWEN_CREDENTIALS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("本机千问配置文件损坏，请重新配置。") from exc
    return normalize_api_key(str(payload.get("api_key", ""))), normalize_api_host(str(payload.get("api_host", "")))


def qwen_error_message(exc: Exception) -> str:
    message = str(exc)
    lowered = message.lower()
    if "401" in lowered or "invalidapikey" in lowered or "invalid_api_key" in lowered or "invalid api key" in lowered or "unauthorized" in lowered:
        return "百炼 API Key 无效。请复制按量付费的 sk- 开头 Key，不要使用 Token Plan 或 Coding Plan 的 sk-sp- Key。"
    if "402" in lowered or "insufficient" in lowered or "arrear" in lowered or "balance" in lowered or "arrearage" in lowered:
        return "阿里云账户余额不足或存在欠费，请在费用与成本中充值或结清欠费。"
    if "403" in lowered or "permission" in lowered or "forbidden" in lowered:
        return "当前 Key 没有模型权限，或 API Key 与 API Host 地域不一致。请都选择华北2（北京）。"
    if "404" in lowered or "not_found" in lowered or "not found" in lowered:
        return f"没有找到 {settings.qwen_model}。请检查 API Host 是否完整，并确认地域为华北2（北京）。"
    if "429" in lowered or "rate" in lowered or "quota" in lowered:
        return "Qwen3-Omni-Flash 调用额度或频率已达到上限，请检查免费额度、余额或稍后重试。"
    if "timeout" in lowered or "timed out" in lowered:
        return "连接阿里云百炼超时，请检查网络后重试。"
    return f"Qwen3-Omni-Flash 暂时无法连接：{message[:180]}"


def is_authentication_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(
        token in message
        for token in ("401", "403", "invalidapikey", "invalid_api_key", "api key", "api_key", "unauthorized", "permission", "forbidden")
    )


def _collect_stream(stream: Any) -> tuple[str, dict[str, Any]]:
    parts: list[str] = []
    usage_payload: dict[str, Any] = {}
    for chunk in stream:
        choices = getattr(chunk, "choices", None) or []
        if choices:
            content = getattr(choices[0].delta, "content", None)
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for item in content:
                    text = item.get("text") if isinstance(item, dict) else getattr(item, "text", None)
                    if text:
                        parts.append(str(text))
        usage = getattr(chunk, "usage", None)
        if usage:
            usage_payload = {
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "output_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
            }
    return "".join(parts).strip(), usage_payload


def validate_api_key(api_key: str, api_host: str) -> dict[str, str]:
    from openai import OpenAI

    key = normalize_api_key(api_key)
    host = normalize_api_host(api_host)
    client = OpenAI(api_key=key, base_url=host, timeout=45.0, max_retries=0)
    try:
        stream = client.chat.completions.create(
            model=settings.qwen_model,
            messages=[{"role": "user", "content": "只回复：可用"}],
            modalities=["text"],
            stream=True,
            stream_options={"include_usage": True},
            max_completion_tokens=16,
            extra_body={"enable_thinking": False},
        )
        text, _ = _collect_stream(stream)
        if not text:
            raise RuntimeError("模型没有返回验证结果")
        return {"model": settings.qwen_model, "display_name": "Qwen3-Omni-Flash"}
    finally:
        client.close()


def _safe_json(text: str) -> dict[str, Any]:
    value = text.strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        start = value.find("{")
        end = value.rfind("}")
        if start >= 0 and end > start:
            return json.loads(value[start : end + 1])
        raise


def _video_data_url(video_path: Path) -> str:
    if video_path.stat().st_size > settings.qwen_proxy_max_bytes:
        raise ValueError("候选视频压缩后仍然过大，无法安全上传给千问。")
    encoded = base64.b64encode(video_path.read_bytes()).decode("ascii")
    return f"data:video/mp4;base64,{encoded}"


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1"}:
            return True
        if normalized in {"false", "no", "0"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _string_list(value: Any, *, limit: int, item_limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    output: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in output:
            output.append(text[:item_limit])
        if len(output) >= limit:
            break
    return output


def _timestamped_evidence(value: Any, context_duration: float) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    output: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        claim = str(item.get("claim") or "").strip()
        if not claim:
            continue
        raw_time = _safe_float(item.get("time"), -1.0)
        if raw_time < 0:
            continue
        time = max(0.0, min(context_duration, raw_time))
        output.append({"time": round(time, 3), "claim": claim[:100]})
        if len(output) >= 6:
            break
    return output


def _clip_bounds(
    payload: dict[str, Any],
    context_duration: float,
    event_time: float,
    recommended_duration: float,
) -> tuple[float, float]:
    fallback_start = event_time - recommended_duration * 0.55
    fallback_end = event_time + recommended_duration * 0.45
    start = _safe_float(payload.get("clip_start"), fallback_start)
    end = _safe_float(payload.get("clip_end"), fallback_end)
    start = max(0.0, min(context_duration, start))
    end = max(0.0, min(context_duration, end))
    if end <= start or event_time < start or event_time > end:
        start = max(0.0, fallback_start)
        end = min(context_duration, fallback_end)

    duration = end - start
    if duration < 6.0:
        duration = min(6.0, context_duration)
        start = event_time - duration * 0.55
        start = max(0.0, min(start, max(0.0, context_duration - duration)))
        end = start + duration
    elif duration > 20.0:
        duration = 20.0
        start = event_time - duration * 0.55
        start = max(0.0, min(start, max(0.0, context_duration - duration)))
        end = start + duration
    return round(start, 3), round(end, 3)


def _enum_value(value: Any, allowed: set[str], default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else default


def _looks_like_rainbow_six(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return any(alias in text for alias in ("彩虹六号", "彩六", "rainbow six", "rainbow 6", "r6 siege", "r6s"))


def _optional_int(value: Any, minimum: int, maximum: int) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return max(minimum, min(maximum, number))


def _normalize_rainbow_six_context(value: Any) -> tuple[dict[str, Any], bool, bool]:
    raw = value if isinstance(value, dict) else {}
    breakdown_raw = raw.get("score_breakdown")
    breakdown_source = breakdown_raw if isinstance(breakdown_raw, dict) else {}
    breakdown: dict[str, int] = {}
    for key, maximum in R6_SCORE_LIMITS.items():
        number = _optional_int(breakdown_source.get(key), 0, maximum)
        breakdown[key] = number if number is not None else 0

    context = {
        "perspective": _enum_value(raw.get("perspective"), R6_PERSPECTIVES, "unknown"),
        "player_attribution": _enum_value(
            raw.get("player_attribution"), R6_PLAYER_ATTRIBUTIONS, "unclear"
        ),
        "event_type": _enum_value(raw.get("event_type"), R6_EVENT_TYPES, "unknown"),
        "kill_count": _optional_int(raw.get("kill_count"), 0, 5),
        "clutch_situation": _enum_value(
            raw.get("clutch_situation"), R6_CLUTCH_SITUATIONS, "unknown"
        ),
        "round_outcome": _enum_value(raw.get("round_outcome"), R6_ROUND_OUTCOMES, "unknown"),
        "objective_outcome": _enum_value(
            raw.get("objective_outcome"), R6_OBJECTIVE_OUTCOMES, "unknown"
        ),
        "score_breakdown": breakdown,
    }
    complete_breakdown = bool(breakdown_source) and all(key in breakdown_source for key in R6_SCORE_LIMITS)
    return context, bool(raw), complete_breakdown


def _first_allowed(allowed_types: list[str], *candidates: str) -> str:
    return next((candidate for candidate in candidates if candidate in allowed_types), "none")


def _enforce_rainbow_six_consistency(
    category: str,
    secondary_categories: list[str],
    score: int,
    context: dict[str, Any],
    allowed_types: list[str],
) -> tuple[str, list[str], int]:
    attribution = context["player_attribution"]
    kill_count = context["kill_count"]
    clutch_situation = context["clutch_situation"]
    round_outcome = context["round_outcome"]
    event_type = context["event_type"]

    combat_categories = {"kill", "multi_kill", "clutch", "precision"}
    if category in combat_categories and attribution == "unclear":
        score = min(score, 49)
    elif category in combat_categories and attribution == "likely":
        score = min(score, 79)

    if category == "multi_kill" and kill_count is not None and kill_count < 2:
        category = _first_allowed(allowed_types, "kill", "skill") if kill_count == 1 else "none"
        score = min(score, 69)

    valid_clutch = clutch_situation in {"1v2", "1v3", "1v4", "1v5"} and round_outcome == "win"
    if category == "clutch" and not valid_clutch:
        if kill_count is not None and kill_count >= 2:
            category = _first_allowed(allowed_types, "multi_kill", "skill")
            score = min(score, 79 if round_outcome == "unknown" else 84)
        elif kill_count == 1:
            category = _first_allowed(allowed_types, "kill", "skill")
            score = min(score, 69)
        else:
            category = "none"
            score = min(score, 35)

    if event_type == "ace" and (kill_count != 5 or round_outcome != "win"):
        context["event_type"] = "unknown"
        score = min(score, 89)
    if event_type == "four_k" and kill_count is not None and kill_count != 4:
        context["event_type"] = "unknown"
        score = min(score, 81)
    if event_type == "triple_kill" and kill_count is not None and kill_count != 3:
        context["event_type"] = "unknown"
        score = min(score, 79)
    if event_type == "double_kill" and kill_count is not None and kill_count != 2:
        context["event_type"] = "unknown"
        score = min(score, 69)
    if event_type == "ordinary":
        score = min(score, 69)

    secondary_categories = [item for item in secondary_categories if item != category]
    return category, secondary_categories, score


def _category_key(value: Any, allowed_types: list[str], *, allow_none: bool = True) -> str:
    text = str(value or "").strip()
    if allow_none and text == "none":
        return "none"
    if text in allowed_types:
        return text
    for key in allowed_types:
        if text == HIGHLIGHT_LABELS.get(key):
            return key
    return "none" if allow_none else ""


def build_analysis_prompt(
    allowed_types: list[str],
    local_score: float,
    context_duration: float,
    game: str,
    game_mode: str,
) -> str:
    categories = "\n".join(f"- {key}: {HIGHLIGHT_LABELS.get(key, key)}" for key in allowed_types)
    game_label = GAME_LABELS.get(game, GAME_LABELS["other_fps"])
    mode_label = GAME_MODE_LABELS.get(game_mode, GAME_MODE_LABELS["auto"])
    game_profile = GAME_PROFILES.get(game, GAME_PROFILES["other_fps"])
    mode_profile = GAME_MODE_PROFILES.get(game_mode, GAME_MODE_PROFILES["auto"])
    auto_rainbow_six_instruction = ""
    if game == "auto":
        auto_rainbow_six_instruction = f"""

彩六优先条件分支：只有先从HUD、可破坏环境、干员/道具和回合目标确认这是《彩虹六号：围攻》，才应用下面的彩六规则；若确认是其他游戏，忽略整段规则且rainbow_six填null。
{RAINBOW_SIX_SIEGE_PROFILE}"""
    output_extension = RAINBOW_SIX_OUTPUT_EXTENSION if game in {"auto", "rainbow_six_siege"} else ""
    return f"""你是一名专业的第一人称射击游戏高光剪辑师。

游戏名称：{game_label}
游戏模式：{mode_label}
候选视频时长：约{context_duration:.1f}秒
本地声音和画面强度得分：{local_score:.3f}（仅作为激烈程度参考，不能代替事件证据）

游戏专属判断规则：
{game_profile}{auto_rainbow_six_instruction}

模式专属判断规则：
{mode_profile}

如果游戏规则与通用模式用词不同，以游戏实际HUD和上述游戏专属规则为准；必须观察到画面、声音或HUD证据后才能认定目标成功。

本项目的核心选片政策（优先级高于其他分类与评分规则）：
1. 重点寻找并优先选择“当前玩家亲自击杀敌方”的第一视角画面。快速连续击杀、关键反杀、爆头、远距离击杀、以少打多和直接改变目标结果的击杀优先级最高。
2. 必须区分当前玩家击杀敌方、队友击杀、助攻、击倒、敌方击杀队友以及当前玩家本人被击杀；只有画面动作与击杀反馈/HUD能够互相印证时，enemy_kill_confirmed才可为true。
3. 初判时不要因为候选视频中出现当前玩家本人被击杀就直接淘汰。先正常判断此前是否存在当前玩家击杀敌方的有效高光，再修剪边界。
4. 如果当前玩家本人死亡落入初步[clip_start, clip_end]范围，必须把最终clip_end设为player_death_time减2.0秒，并尽量向前移动clip_start以保留完整击杀过程；不得保留倒地死亡、死亡提示、阵亡视角或击杀回放。敌方被当前玩家击杀的画面不属于此规则。
5. 修剪导致区间不足6秒时允许输出更短片段，不得为了凑时长把clip_end重新延伸到死亡画面。只有修剪后的区间本身没有任何有效击杀或其他高光证据时，才按普通证据不足规则降低分数或设为none，不能把“后来本人死亡”本身当作淘汰理由。
6. player_death_detected表示候选视频中是否看到了当前玩家死亡；player_death_time填写死亡开始的最早秒数；player_death_in_clip必须根据修剪后的最终[clip_start, clip_end]区间重新判断，严禁误把敌方死亡填为当前玩家死亡。

请同时分析以下维度：
1. 游戏画面、HUD、击杀提示和比分变化
2. 击杀数量、连续性以及是否产生明确战斗结果
3. 枪声、爆炸、技能、胜利音效和语音反应
4. 血量、人数、局势压力和是否实现逆转
5. 目标点、炸弹、撤离、复活、Boss或任务进度
6. 瞄准、身法、技能、投掷物、战术和团队配合难度
7. 事件是否罕见、有趣、意外，且适合进入高光成片
8. 是否存在清晰人声，以及语音、交流或反应对理解这段高光是否重要

如果游戏名称或模式设为自动，请根据画面、HUD、规则和声音进行判断。

允许的主要高光类型：
{categories}

分类规则：
- 只把最能代表片段价值的类型放入category，其他符合类型放入secondary_categories。
- 普通移动、菜单、等待、重复交火、无明确结果的战斗和普通失误不能获得高分。
- 先判断content_type。赛事回放、干员选择/禁用、菜单或只有回合结算的片段不是战斗高光，category必须为none。
- 赛事回放中展示的击杀不得归属为当前玩家的实时第一视角击杀。纯摄像头/无人机视角和纯胜利界面不得声称看到连杀。
- 有当前玩家确认击杀敌方的候选应优先于没有击杀结果的普通操作；未确认敌方击杀的候选，除非存在决定性目标胜利或极罕见战术价值，否则score不得高于69。
- 初步区间包含本人死亡时不要直接淘汰；先将clip_end修剪到player_death_time前2.0秒，再根据修剪后保留下来的高光证据评分。
- 如果没有清晰可见或可听的高光证据，category必须为"none"，score不得高于35。
- 50分以下代表不建议入选；75分以上必须有明确结果或明显的高难度证据。
- evidence_timestamps必须是候选视频内的真实秒数；不能给出时间点的HUD、击杀或胜利声称不得用来支撑82分以上的结论。
- clip_start和clip_end要包含最短必要前因、完整动作和结果，不要用跑图、回放或结算填充时长。
- speech_value只评价字幕价值：没有人声为0，背景闲聊不高于30，有用交流或反应不低于60，语音是核心看点时不低于85。

只输出一个JSON对象，不要输出Markdown或解释：
{{
  "score": 0到100的整数,
  "confidence": 0到1之间的判断置信度,
  "content_type": "gameplay_first_person、gameplay_spectator、replay、operator_select、menu、round_result、mixed或unknown",
  "category": "允许类型之一或none",
  "secondary_categories": ["最多3个其他允许类型"],
  "title": "不超过18个汉字的中文标题",
  "reason": "一句中文入选或淘汰理由",
  "evidence": ["最多4条来自画面或声音的具体证据"],
  "evidence_timestamps": [{{"time": "候选视频内秒数", "claim": "该时刻可直接核对的证据"}}],
  "enemy_kill_confirmed": true或false，是否确认当前玩家亲自击杀至少一名敌方,
  "player_death_detected": true或false，候选视频中是否出现当前玩家本人死亡,
  "player_death_time": "当前玩家本人死亡开始的最早秒数；未发生或无法确认填null",
  "player_death_in_clip": true或false，修剪后的最终clip_start到clip_end内是否仍包含当前玩家本人死亡画面；正确修剪后应为false,
  "detected_game": "识别到的游戏名称或通用FPS",
  "detected_mode": "识别到的游戏模式",
  "speech_value": 0到100之间、语音字幕对这段高光的价值,
  "speech_reason": "一句话说明是否值得生成字幕",
  "clip_start": "建议成片起点，候选视频内秒数",
  "event_time": 高光中心在候选视频中的秒数,
  "clip_end": "建议成片终点，候选视频内秒数",
  "recommended_duration": 6到20之间、覆盖高光事件的秒数{output_extension}
}}
"""


def normalize_analysis_payload(
    payload: dict[str, Any],
    allowed_types: list[str],
    context_duration: float,
    game: str,
    game_mode: str,
) -> dict[str, Any]:
    score = max(0, min(100, int(_safe_float(payload.get("score"), 0))))
    category = _category_key(payload.get("category"), allowed_types)
    content_type = _enum_value(payload.get("content_type"), CONTENT_TYPES, "unknown")
    evidence_timestamps = _timestamped_evidence(payload.get("evidence_timestamps"), context_duration)

    secondary_categories: list[str] = []
    for item in _string_list(payload.get("secondary_categories"), limit=6, item_limit=40):
        key = _category_key(item, allowed_types, allow_none=False)
        if key and key != category and key not in secondary_categories:
            secondary_categories.append(key)
        if len(secondary_categories) >= 3:
            break

    rainbow_six_context: dict[str, Any] | None = None
    is_rainbow_six = game == "rainbow_six_siege" or (
        game == "auto" and _looks_like_rainbow_six(payload.get("detected_game"))
    )
    if is_rainbow_six:
        rainbow_six_context, has_context, complete_breakdown = _normalize_rainbow_six_context(
            payload.get("rainbow_six")
        )
        if complete_breakdown:
            score = sum(rainbow_six_context["score_breakdown"].values())
        if has_context:
            category, secondary_categories, score = _enforce_rainbow_six_consistency(
                category,
                secondary_categories,
                score,
                rainbow_six_context,
                allowed_types,
            )

        if category in {"kill", "multi_kill", "clutch", "precision"} and (
            rainbow_six_context["perspective"] != "first_person"
            or rainbow_six_context["player_attribution"] != "confirmed"
        ):
            category = "none"
            score = min(score, 35)

    if content_type in NON_ACTION_CONTENT_TYPES:
        category = "none"
        score = min(score, 35)
    if score >= 82 and len(evidence_timestamps) < 2:
        score = 79

    if category == "none":
        score = min(score, 35)
    confidence = max(0.0, min(1.0, _safe_float(payload.get("confidence"), 0.5)))
    speech_value = max(0, min(100, int(_safe_float(payload.get("speech_value"), 0))))
    event_time = max(0.0, min(context_duration, _safe_float(payload.get("event_time"), context_duration / 2)))
    recommended_duration = max(6.0, min(20.0, _safe_float(payload.get("recommended_duration"), 12)))
    clip_start, clip_end = _clip_bounds(
        payload,
        context_duration,
        event_time,
        recommended_duration,
    )
    enemy_kill_confirmed = _safe_bool(payload.get("enemy_kill_confirmed"))
    player_death_detected = _safe_bool(payload.get("player_death_detected"))
    player_death_time: float | None = None
    if payload.get("player_death_time") is not None:
        raw_death_time = _safe_float(payload.get("player_death_time"), -1.0)
        if raw_death_time >= 0:
            player_death_time = round(max(0.0, min(context_duration, raw_death_time)), 3)
    player_death_in_clip = _safe_bool(payload.get("player_death_in_clip"))
    death_safe_trimmed = False
    death_falls_in_initial_clip = (
        player_death_detected
        and player_death_time is not None
        and clip_start <= player_death_time <= clip_end
    )
    if death_falls_in_initial_clip:
        safe_end = round(max(0.0, player_death_time - 2.0), 3)
        clip_end = min(clip_end, safe_end)
        if clip_end > 0 and clip_end - clip_start < min(6.0, clip_end):
            clip_start = round(max(0.0, clip_end - 6.0), 3)
        if clip_end > clip_start:
            event_time = round(max(clip_start, min(event_time, clip_end)), 3)
        else:
            clip_start = clip_end
            event_time = clip_end
        player_death_in_clip = False
        death_safe_trimmed = True
    elif player_death_detected and player_death_time is not None:
        player_death_in_clip = clip_start <= player_death_time <= clip_end

    default_game = "FPS游戏" if game == "auto" else GAME_LABELS.get(game, "FPS游戏")
    default_mode = "未知模式" if game_mode == "auto" else GAME_MODE_LABELS.get(game_mode, "未知模式")
    title_fallback = "未发现明确高光" if category == "none" else HIGHLIGHT_LABELS.get(category, "FPS高光")
    reason_fallback = "没有发现足够的高光证据" if category == "none" else "千问识别到明确高光事件"
    result = {
        "score": score,
        "confidence": round(confidence, 3),
        "content_type": content_type,
        "category": category,
        "category_label": HIGHLIGHT_LABELS.get(category, "无明确高光"),
        "secondary_categories": secondary_categories,
        "title": str(payload.get("title") or title_fallback)[:18],
        "reason": str(payload.get("reason") or reason_fallback)[:160],
        "evidence": _string_list(payload.get("evidence"), limit=4, item_limit=100),
        "evidence_timestamps": evidence_timestamps,
        "enemy_kill_confirmed": enemy_kill_confirmed,
        "player_death_detected": player_death_detected,
        "player_death_time": player_death_time,
        "player_death_in_clip": player_death_in_clip,
        "death_safe_trimmed": death_safe_trimmed,
        "detected_game": str(payload.get("detected_game") or default_game)[:40],
        "detected_mode": str(payload.get("detected_mode") or default_mode)[:40],
        "speech_value": speech_value,
        "speech_reason": str(payload.get("speech_reason") or "没有检测到对高光有价值的语音")[:120],
        "event_time": event_time,
        "clip_start": clip_start,
        "clip_end": clip_end,
        "recommended_duration": recommended_duration,
    }
    if rainbow_six_context is not None:
        result["rainbow_six"] = rainbow_six_context
    return result


def analyze_candidate(
    api_key: str,
    api_host: str,
    video_path: Path,
    allowed_types: list[str],
    local_score: float,
    context_duration: float,
    game: str = "auto",
    game_mode: str = "auto",
) -> dict[str, Any]:
    from openai import OpenAI

    prompt = build_analysis_prompt(allowed_types, local_score, context_duration, game, game_mode)
    host = normalize_api_host(api_host)
    client = OpenAI(api_key=api_key.strip(), base_url=host, timeout=180.0, max_retries=1)
    try:
        stream = client.chat.completions.create(
            model=settings.qwen_model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "video_url", "video_url": {"url": _video_data_url(video_path)}},
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            modalities=["text"],
            stream=True,
            stream_options={"include_usage": True},
            max_completion_tokens=1100,
            temperature=0.1,
            extra_body={"enable_thinking": False},
        )
        text, usage = _collect_stream(stream)
        payload = _safe_json(text)
        result = normalize_analysis_payload(payload, allowed_types, context_duration, game, game_mode)
        result["usage"] = usage
        return result
    finally:
        client.close()


def build_verification_prompt(
    preliminary: dict[str, Any],
    context_duration: float,
    game: str,
    game_mode: str,
) -> str:
    game_label = GAME_LABELS.get(game, GAME_LABELS["other_fps"])
    mode_label = GAME_MODE_LABELS.get(game_mode, GAME_MODE_LABELS["auto"])
    review_payload = {key: value for key, value in preliminary.items() if key != "usage"}
    hypothesis = json.dumps(review_payload, ensure_ascii=False, separators=(",", ":"))
    return f"""你是FPS高光的二次审核员。请重新观看这段约{context_duration:.1f}秒视频，不要相信初判结果中的任何声称，必须用视频内的可见时刻独立核对。

游戏：{game_label}
模式：{mode_label}
待审核的初判JSON（只是假设，不是证据）：
{hypothesis}

审核要求：
1. 先确认content_type。如果主要是赛事回放、选人/禁用、菜单、摄像头/无人机或纯结算，不得把其中的画面当成当前玩家实时击杀。
2. 每条关键证据必须在evidence_timestamps中给出可回看的秒数。找不到时间点就删除该声称。
3. 彩六击杀归属必须同时有第一视角动作、击杀反馈与HUD变化；clutch必须看到1v2及以上局面形成并看到回合胜利。
4. 初判的标题、分数、击杀数、干员、目标结果都可能是错的。若证据不足，category必须为none、score不高于35。
5. 重新确认enemy_kill_confirmed、player_death_detected、player_death_time和player_death_in_clip。当前玩家本人死亡、阵亡提示、死亡视角或击杀回放不得出现在最终区间内；敌方死亡不能误判为当前玩家死亡。
6. 初判范围包含本人死亡时不要因此直接淘汰。clip_end必须改为player_death_time减2.0秒，并向前调整clip_start以尽量保留完整击杀；不足6秒时允许短片段，不能向后延伸到死亡画面。
7. 只根据修剪后仍保留的高光证据修正category和score；若修剪后没有有效事件，才按普通证据不足处理，不能把后续本人死亡本身当作淘汰理由。

只返回修正后的JSON对象，字段结构与初判JSON一致。不要返回Markdown或解释。
""".strip()


def verify_candidate(
    api_key: str,
    api_host: str,
    video_path: Path,
    preliminary: dict[str, Any],
    allowed_types: list[str],
    context_duration: float,
    game: str = "auto",
    game_mode: str = "auto",
) -> dict[str, Any]:
    from openai import OpenAI

    prompt = build_verification_prompt(preliminary, context_duration, game, game_mode)
    host = normalize_api_host(api_host)
    client = OpenAI(api_key=api_key.strip(), base_url=host, timeout=180.0, max_retries=1)
    try:
        stream = client.chat.completions.create(
            model=settings.qwen_model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "video_url", "video_url": {"url": _video_data_url(video_path)}},
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            modalities=["text"],
            stream=True,
            stream_options={"include_usage": True},
            max_completion_tokens=1100,
            temperature=0.0,
            extra_body={"enable_thinking": False},
        )
        text, usage = _collect_stream(stream)
        payload = _safe_json(text)
        result = normalize_analysis_payload(payload, allowed_types, context_duration, game, game_mode)
        result["usage"] = usage
        result["verification"] = {
            "reviewed": True,
            "preliminary_score": preliminary.get("score", 0),
            "preliminary_category": preliminary.get("category", "none"),
        }
        return result
    finally:
        client.close()
