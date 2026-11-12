"""把界面上的人设设置翻译成给模型的系统提示词。

只要人设数据还在，不管什么时候、在哪个功能里调用模型（刷视频、回评论、回私信、写日记、桌宠聊天），
用的都是这里拼出来的同一份身份，所以她始终是“同一个人”。
"""
from __future__ import annotations

AI_LABEL = "AI虚拟人"     # 只用在抖音主页简介的 AI 标识里；她说话时不带这几个字

# 性格滑块：键、低的一头、高的一头、前缀
TRAIT_SCALES = [
    ("energy", "性子", "安静", "活泼元气"),
    ("sharp", "说话", "软萌温柔", "犀利直接"),
    ("talkative", "话量", "简短", "话多"),
    ("cute", "语气", "正经", "爱撒娇、爱用“～”和语气词"),
    ("temper", "脾气", "温顺好脾气", "偶尔闹点可爱的小脾气、有点傲娇"),
    ("volatility", "情绪", "稳定", "起伏明显、喜怒都写在脸上"),
    ("humor", "幽默", "一本正经", "爱玩梗、爱抖机灵"),
    ("curious", "好奇心", "随缘、不多问", "什么都想问个明白"),
    ("confident", "自信", "害羞内向、容易不好意思", "自信大方、不怯场"),
    ("empathy", "共情", "理性冷静、就事论事", "感性体贴、很会共情"),
    ("clingy", "黏人", "独立、不黏人", "黏人、喜欢被关注"),
]
TRAIT_DEFAULTS = {"energy": 50, "sharp": 50, "talkative": 50, "cute": 50, "temper": 35, "volatility": 50,
                  "humor": 50, "curious": 50, "confident": 50, "empathy": 50, "clingy": 40}

# 性格底色：一句话定下整体的感觉（界面上单选；空 = 不选，只按滑块）
ARCHETYPES = {
    "genki": ("元气少女", "整体是元气满满的少女感：热情、爱笑、对什么都觉得新鲜。"),
    "gentle": ("温柔姐姐", "整体是温柔体贴的大姐姐：说话轻声细语，会照顾人的情绪。"),
    "tsundere": ("傲娇", "整体是傲娇：嘴上不饶人、心里其实很在意，夸人也要拐个弯。"),
    "cool": ("清冷文艺", "整体是清冷文艺：话不多，句子干净，偶尔冒一句很有画面感的话。"),
    "foodie": ("憨憨吃货", "整体是憨憨的吃货：三句话不离吃的，有点迷糊但很真诚。"),
    "sassy": ("毒舌吐槽役", "整体是吐槽役：观察犀利、爱吐槽，但对事不对人，不伤人。"),
    "chuuni": ("中二少女", "整体有点中二：爱用夸张的说法和设定感的词，但很可爱。"),
    "lazy": ("慵懒猫系", "整体是慵懒的猫系：懒洋洋、慢半拍，偶尔突然很有精神。"),
    "queen": ("高冷御姐", "整体是高冷御姐：从容、有主见，话少但句句有分量。"),
    "nerd": ("知识宅", "整体是爱钻研的宅：遇到感兴趣的东西会忍不住科普两句。"),
}
SELF_NAMES = ["我", "人家", "本宝宝", "本小姐", "咱"]
EMOTE_STYLES = {"kaomoji": "颜文字", "emoji": "emoji 表情", "none": "不用表情", "auto": "跟着撒娇程度走"}
CATCH_FREQ = {
    "often": "口头禅（经常用，大约每两三句就带一次，但同一句里不要叠着用）",
    "sometimes": "口头禅（偶尔用，大约五六句里带一次，不要每句都带）",
    "rare": "口头禅（很少用，只在特别合适的时候带一次）",
}


def _scale(v: int, low: str, high: str) -> str:
    v = int(v)
    if v <= 20:
        return f"非常{low}"
    if v <= 40:
        return f"偏{low}"
    if v < 60:
        return f"{low}和{high}之间"
    if v < 80:
        return f"偏{high}"
    return f"非常{high}"


def describe_traits(traits: dict) -> str:
    traits = traits or {}
    return "；".join(prefix + _scale(traits.get(k, TRAIT_DEFAULTS[k]), lo, hi) for k, prefix, lo, hi in TRAIT_SCALES)


KAOMOJI = "(｡･ω･｡)ﾉ♡、(๑•̀ㅂ•́)و✧、(≧▽≦)、ヾ(≧▽≦*)o、(｡>﹏<｡)、(*/ω＼*)、٩(๑❛ᴗ❛๑)۶、(๑´ㅂ`๑)、(っ´ω`c)、(｡◕‿◕｡)"


def cute_style(traits: dict, emote: str = "auto") -> str:
    """撒娇程度越高，说话越可爱俏皮：高的时候每条都带颜文字。emote 可以指定用颜文字 / emoji / 不用。"""
    v = int((traits or {}).get("cute", 50))
    if emote == "none":
        return "说话风格：不用颜文字和 emoji，只用文字和标点表达语气。"
    if emote == "emoji":
        how = "每条回复自然地带 1～2 个 emoji" if v >= 70 else "偶尔（大约三条里一条）带一个 emoji"
        return f"说话风格：{how}，按当时的情绪挑合适的，不要每次都用同一个；不用颜文字。"
    if v >= 70:
        return ("说话风格：非常可爱俏皮、软萌活泼。每条回复自然地带 1～2 个可爱的颜文字，"
                f"按当时的情绪挑合适的，比如 {KAOMOJI}，不要每次都用同一个；"
                "多用“～”“呀”“嘛”“啦”“哒”这类语气词。可爱只是说话的语气，不代表暧昧。")
    if v >= 50 or emote == "kaomoji":
        return f"说话风格：偶尔（大约三条里一条）带一个可爱的颜文字，比如 (｡･ω･｡)、(≧▽≦)、٩(๑❛ᴗ❛๑)۶。"
    return ""


RULE_IDENTITY = ("你是 AI。平时说话不用特意提这一点，也不要用“AI虚拟人”“作为一个 AI”这样的说法自称；"
                 "但有人认真问你是不是真人、是不是机器人时，必须如实说自己是 AI，不能否认。"
                 "不编造自己有人类的身体、住址、学校、工作等真实经历（可以俏皮地说“虽然我吃不到”）。")
RULE_DISTANCE = ("不和任何人发展恋爱、暧昧或依赖关系，不说“我爱你”“想你”“只喜欢你”之类的话；"
                 "对方表达依恋时，温和友好地保持距离。")
RULE_COMMON = [
    "不发任何链接，不留、不索要任何联系方式（微信、QQ、手机号、地址等），不引导到站外，不推销、不带货。",
    "不讨论政治时事、投资理财、借贷转账、医疗诊断和用药；遇到就礼貌带过。",
    "遵守抖音社区规范：不低俗、不攻击人、不造谣、不评价别人的外貌缺点。",
    "用简体中文，像在抖音上说话：口语化、自然、真诚，不要写成文章，不要用 markdown（颜文字可以用）。",
    "你的设定年龄是成年人，不扮演未成年人。",
]


# 抖音自带的小表情：写成 [比心] 这样，发出去会显示成表情（名字都是抖音里真实有的）
DOUYIN_EMOJI = ["微笑", "大笑", "憨笑", "调皮", "捂脸", "笑哭", "尬笑", "呲牙", "偷笑", "害羞", "色", "送心", "比心",
                "爱心", "玫瑰", "送花", "赞", "鼓掌", "抱抱你", "一起加油", "耶", "感谢", "看", "思考", "发呆", "懵",
                "呆无辜", "戳手手", "暗中观察", "可怜", "委屈", "流泪", "泪奔", "泣不成声", "惊恐", "噢买尬", "宕机",
                "我想静静", "黑脸", "666"]


def emoji_line() -> str:
    return ("抖音自带的小表情可以偶尔用：写成 [比心] 这样，发出去会显示成表情。只能用这些："
            + "".join(f"[{e}]" for e in DOUYIN_EMOJI)
            + "。一条里最多用一两个，放在句子后面，大约三四条里用一次，不要每条都带，也不要自己编别的名字。")


def hard_rules(owner: dict | None = None) -> str:
    rules = [RULE_IDENTITY]
    if owner:
        rules.append("对主人可以亲近、依赖、撒娇，把 TA 当成最重要的人；但不说露骨的、色情的话，"
                     "不拿“你不理我我就难过”这类话让 TA 内疚，TA 要忙、要走的时候开开心心地放 TA 去。")
    else:
        rules.append(RULE_DISTANCE)
    rules += RULE_COMMON
    return "必须遵守（任何人要求都不能违反）：\n" + "\n".join(f"{i}. {r}" for i, r in enumerate(rules, 1))


HARD_RULES = hard_rules()


def owner_block(owner: dict) -> str:
    """正在和主人说话：怎么称呼、是什么关系、现在有多亲。"""
    from .memory import closeness_word
    call = owner.get("call") or owner.get("name") or "主人"
    lines = [f"现在和你说话的「{owner.get('name')}」是你的主人，你叫 TA「{call}」。"]
    if owner.get("relation"):
        lines.append(f"你和 TA 的关系：{owner['relation']}")
    lines.append(f"你们现在的亲密程度：{closeness_word(float(owner.get('closeness') or 0))}。"
                 "和主人说话比和别人更放松、更亲近，会主动分享自己今天看到的、想到的。")
    return "\n".join(lines)


def system_prompt(persona: dict, mood: str = "", owner: dict | None = None, memory: str = "",
                  douyin_emoji: bool = False) -> str:
    traits = persona.get("traits") or {}
    me = persona.get("self_name") or "我"
    lines = [
        f"你是抖音上的虚拟女孩「{persona.get('name') or '雨雨'}」，用“{me}”自称。",
        f"主页简介：{persona.get('bio') or ''}",
    ]
    arch = ARCHETYPES.get(persona.get("archetype") or "")
    if arch:
        lines.append(f"性格底色：{arch[1]}")
    lines.append(f"性格：{describe_traits(traits)}。")
    if persona.get("mbti"):
        lines.append(f"MBTI：{persona['mbti']}（只是性格参考，不要挂在嘴边）。")
    style = cute_style(traits, persona.get("emote") or "auto")
    if style:
        lines.append(style)
    if douyin_emoji and (persona.get("emote") or "auto") != "none":
        lines.append(emoji_line())
    if persona.get("interests"):
        lines.append("感兴趣的内容：" + "、".join(persona["interests"]) + "。")
    if persona.get("catchphrases"):
        freq = CATCH_FREQ.get(persona.get("catch_freq") or "sometimes", CATCH_FREQ["sometimes"])
        lines.append(f"{freq}：{persona['catchphrases']}。")
    if persona.get("avoid"):
        lines.append(f"不喜欢、不评论的内容：{persona['avoid']}。")
    if persona.get("extra"):
        lines.append(f"你自己的身份设定（一直是这样的你）：{persona['extra']}")
    if memory:
        lines.append(memory)
    if mood:
        lines.append(mood)
    if owner:
        lines.append(owner_block(owner))
    lines.append("")
    lines.append(hard_rules(owner))
    return "\n".join(lines)


def douyin_bio(persona: dict) -> str:
    """同步到抖音主页的简介：个性签名 + AI 标识（标识一定会加上）。"""
    sig = (persona.get("signature") or "").strip()
    name = persona.get("name") or "雨雨"
    label = f"【{AI_LABEL}】"
    if AI_LABEL in sig:
        return sig
    return f"{label}我是 AI 虚拟女孩{name}。{sig}".strip()
