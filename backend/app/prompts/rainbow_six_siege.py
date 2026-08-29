from __future__ import annotations


RAINBOW_SIX_SIEGE_PROFILE = """你现在必须按《彩虹六号：围攻》的真实回合逻辑判断高光，而不是把它当成普通重生制FPS。

判读顺序（必须依次完成）：
1. 视角归属：先判断这是玩家第一视角、观战视角、回放/击杀镜头还是无法确认。只有准星动作、第一视角开火与击杀栏/得分/回合反馈能够互相印证时，才能把击杀归属于当前视角；队友击杀、助攻、击倒和确认击杀不可混为一谈。
2. 回合局势：读取进攻/防守方、存活人数、回合计时、比分、血量、拆弹器状态和目标提示。识别“1vX”必须看到局面形成；称为clutch必须继续看到当前玩家赢下回合，失败的残局不能叫翻盘。
3. 动作链：识别首杀/突破（entry）、补枪（refrag/trade）、连续击杀、转点反杀、拉枪、提前枪、穿墙/穿地板、垂直进攻、绕后、听声定位、无人机/摄像头信息和干员技能联动。连续击杀必须属于同一回合、同一玩家，且具有紧密的时间或战术联系。
4. 目标结果：区分拾取、掉落、安装和已安装拆弹器，以及守包、回防、假拆、成功解除、阻止安装/解除；必须用HUD、倒计时、播报或回合结束画面确认成功。人质和区域模式按实际目标提示判断，不得强套炸弹模式。
5. 最终价值：先确认发生了什么和结果，再评价难度与观赏性。击杀数不是唯一标准，改变回合胜负的道具、信息、救援、守包或极限目标操作可高于无结果刷枪。

彩六高光定义与分类映射：
- Ace：当前玩家在同一回合确认击杀全部5名敌人；映射multi_kill，可辅以clutch/victory。不能仅凭主播喊“Ace”认定。
- 4K/三杀/双杀：必须数清当前玩家的确认击杀；映射multi_kill。普通双杀只有在速度、难度或回合影响明确时才超过70分。
- 1v2至1v5翻盘：必须同时确认人数劣势形成与最终回合胜利；映射clutch。1v1本身不是高光，除非有赛点、极限时间、残血或高难度目标操作。
- 首杀、突破、补枪和交叉火力：根据回合影响映射kill、skill或teamfight；普通无压力单杀通常低于60分。
- 爆头、甩枪、提前枪、盲穿、穿墙/地板和远距离击杀：映射precision或skill，但必须能从画面确认难度，不能只看击杀栏猜测。
- C4、手雷、阔剑、干员技能、无人机/摄像头信息、垂直破坏和反制道具：映射utility；仅放置道具但没有产生信息、空间或结果不算高光。
- 下包、守包、回防、极限拆弹、阻止下包/拆弹：映射victory、clutch或utility；目标是否成功必须有结果证据。
- 强烈语音反应或节目效果映射reaction、funny或unexpected，但语音不能代替游戏事件证据。

证据优先级：
- 强证据：第一视角完整动作链、右上击杀栏、顶部存活人数变化、拆弹器状态、回合胜负画面、明确游戏播报。
- 辅助证据：血量、计时器、准星命中反馈、得分提示、队内语音和主播反应。
- 单独不足以定案：枪声很密、画面抖动、音量峰值、模糊文字、队友欢呼、单个加分数字或没有结果的交火。

评分必须按以下7项相加，不能先凭感觉给总分：
- operation_difficulty 操作难度0-20：枪法、反应、身法、压枪、目标切换、生存压力。
- tactical_value 战术价值0-20：信息、道具、地图破坏、站位、转点、团队贡献。
- round_importance 回合重要性0-15：人数劣势、赛点、时间、血量、目标压力。
- rarity 稀有程度0-15：Ace、大人数残局、极限目标、罕见技巧。
- visual_clarity 视觉清晰度0-10：动作与HUD是否清楚、归属是否可验证。
- emotion 情绪/节目效果0-10：真实反应、紧张、惊喜或笑点。
- narrative_completeness 叙事完整性0-10：是否看得到局面、行动和结果。

分数校准：
- 92-100：确认Ace、1v4/1v5翻盘或同级别决定性目标操作，且证据完整。
- 82-91：确认4K、1v2/1v3翻盘、极限拆弹/阻止目标或罕见高难度连贯操作。
- 70-81：清晰三杀、高质量双杀、关键首杀+存活、穿墙/垂直/道具配合并影响回合。
- 50-69：普通但明确的单杀/双杀、局部战术价值或结果不完整的精彩尝试。
- 0-49：归属不清、普通交火、失败且无独立价值、菜单/等待/跑图或没有结果证据。

剪辑边界：event_time优先放在当前玩家决定性击杀敌方的中心；recommended_duration应包含理解高光所需的最短前因和击杀结果。普通枪战建议6-12秒，战术道具8-16秒，1vX/守包/拆弹建议12-20秒。不要用过长跑图填充，也不要截掉人数劣势形成、最后击杀、目标结果或关键语音反应。初判范围包含当前玩家本人死亡时不要直接淘汰，必须把clip_end改为player_death_time减2.0秒并向前调整clip_start；即使不足6秒也不得向后延伸到阵亡提示、死亡视角或击杀回放。只按修剪后仍保留的击杀与战术证据评分。

严格避免：把击倒当确认击杀；把队友击杀算给当前视角；用最终总击杀数推导单回合Ace；把失败的1vX叫clutch；未见结果就声称下包/拆弹成功；因枪声或欢呼很响就给高分；无法看清HUD时编造干员、地图、点位、血量或精确剩余时间。无法确认时使用unknown/unclear并降低confidence。"""


RAINBOW_SIX_OUTPUT_EXTENSION = """,
  "rainbow_six": {
    "perspective": "first_person、spectator、replay或unknown",
    "player_attribution": "confirmed、likely或unclear",
    "event_type": "ace、four_k、triple_kill、double_kill、single_kill、clutch、plant、defuse、plant_denial、defuse_denial、opening_pick、refrag、wallbang、vertical_play、utility_play、teamplay、escape、funny、ordinary或unknown",
    "kill_count": "能确认的当前玩家本回合击杀数0到5；无法确认填null",
    "clutch_situation": "1v1、1v2、1v3、1v4、1v5、none或unknown",
    "round_outcome": "win、loss或unknown",
    "objective_outcome": "plant、defuse、plant_denied、defuse_denied、hostage_rescued、area_secured、none或unknown",
    "score_breakdown": {
      "operation_difficulty": "0到20的整数",
      "tactical_value": "0到20的整数",
      "round_importance": "0到15的整数",
      "rarity": "0到15的整数",
      "visual_clarity": "0到10的整数",
      "emotion": "0到10的整数",
      "narrative_completeness": "0到10的整数"
    }
  }"""


R6_PERSPECTIVES = {"first_person", "spectator", "replay", "unknown"}
R6_PLAYER_ATTRIBUTIONS = {"confirmed", "likely", "unclear"}
R6_EVENT_TYPES = {
    "ace",
    "four_k",
    "triple_kill",
    "double_kill",
    "single_kill",
    "clutch",
    "plant",
    "defuse",
    "plant_denial",
    "defuse_denial",
    "opening_pick",
    "refrag",
    "wallbang",
    "vertical_play",
    "utility_play",
    "teamplay",
    "escape",
    "funny",
    "ordinary",
    "unknown",
}
R6_CLUTCH_SITUATIONS = {"1v1", "1v2", "1v3", "1v4", "1v5", "none", "unknown"}
R6_ROUND_OUTCOMES = {"win", "loss", "unknown"}
R6_OBJECTIVE_OUTCOMES = {
    "plant",
    "defuse",
    "plant_denied",
    "defuse_denied",
    "hostage_rescued",
    "area_secured",
    "none",
    "unknown",
}
R6_SCORE_LIMITS = {
    "operation_difficulty": 20,
    "tactical_value": 20,
    "round_importance": 15,
    "rarity": 15,
    "visual_clarity": 10,
    "emotion": 10,
    "narrative_completeness": 10,
}
