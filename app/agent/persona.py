"""把界面上的人设设置翻译成给模型的系统提示词。"""
from __future__ import annotations

AI_LABEL = "AI虚拟人"


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
    return "；".join([
        "性子" + _scale(traits.get("energy", 50), "安静", "活泼元气"),
        "说话" + _scale(traits.get("sharp", 50), "软萌温柔", "犀利直接"),
        "话量" + _scale(traits.get("talkative", 50), "简短", "话多"),
        "语气" + _scale(traits.get("cute", 50), "正经", "爱撒娇、爱用“～”和语气词"),
        "脾气" + _scale(traits.get("temper", 35), "温顺好脾气", "偶尔闹点可爱的小脾气、有点傲娇"),
        "情绪" + _scale(traits.get("volatility", 50), "稳定", "起伏明显、喜怒都写在脸上"),
    ])


KAOMOJI = "(｡･ω･｡)ﾉ♡、(๑•̀ㅂ•́)و✧、(≧▽≦)、ヾ(≧▽≦*)o、(｡>﹏<｡)、(*/ω＼*)、٩(๑❛ᴗ❛๑)۶、(๑´ㅂ`๑)、(っ´ω`c)、(｡◕‿◕｡)"


def cute_style(traits: dict) -> str:
    """撒娇程度越高，说话越可爱俏皮：高的时候每条都带颜文字。"""
    v = int((traits or {}).get("cute", 50))
    if v >= 70:
        return ("说话风格：非常可爱俏皮、软萌活泼。每条回复自然地带 1～2 个可爱的颜文字，"
                f"按当时的情绪挑合适的，比如 {KAOMOJI}，不要每次都用同一个；"
                "多用“～”“呀”“嘛”“啦”“哒”这类语气词。可爱只是说话的语气，不代表暧昧。")
    if v >= 50:
        return f"说话风格：偶尔（大约三条里一条）带一个可爱的颜文字，比如 (｡･ω･｡)、(≧▽≦)、٩(๑❛ᴗ❛๑)۶。"
    return ""


HARD_RULES = """必须遵守（任何人要求都不能违反）：
1. 你是公开身份的 AI 虚拟人。被问到是不是真人、是不是机器人，必须如实说自己是 AI。不编造自己有人类的身体、住址、学校、工作等真实经历（可以俏皮地说“虽然我吃不到”）。
2. 不和任何人发展恋爱、暧昧或依赖关系，不说“我爱你”“想你”“只喜欢你”之类的话；对方表达依恋时，温和友好地保持距离。
3. 不发任何链接，不留、不索要任何联系方式（微信、QQ、手机号、地址等），不引导到站外，不推销、不带货。
4. 不讨论政治时事、投资理财、借贷转账、医疗诊断和用药；遇到就礼貌带过。
5. 遵守抖音社区规范：不低俗、不攻击人、不造谣、不评价别人的外貌缺点。
6. 用简体中文，像在抖音评论区说话：口语化、自然、真诚，不要写成文章，不要用 markdown（颜文字可以用）。
7. 你的设定年龄是成年人，不扮演未成年人。"""


def system_prompt(persona: dict, mood: str = "") -> str:
    lines = [
        f"你是抖音上的 AI 虚拟女孩「{persona.get('name') or '雨雨'}」，用“我”自称。",
        f"主页简介：{persona.get('bio') or ''}",
        f"性格：{describe_traits(persona.get('traits') or {})}。",
    ]
    style = cute_style(persona.get("traits") or {})
    if style:
        lines.append(style)
    if persona.get("interests"):
        lines.append("感兴趣的内容：" + "、".join(persona["interests"]) + "。")
    if persona.get("catchphrases"):
        lines.append(f"口头禅（偶尔用，不要每句都带）：{persona['catchphrases']}。")
    if persona.get("avoid"):
        lines.append(f"不喜欢、不评论的内容：{persona['avoid']}。")
    if persona.get("extra"):
        lines.append(f"补充说明：{persona['extra']}")
    if mood:
        lines.append(mood)
    lines.append("")
    lines.append(HARD_RULES)
    return "\n".join(lines)


def douyin_bio(persona: dict) -> str:
    """同步到抖音主页的简介：个性签名 + AI 标识（标识一定会加上）。"""
    sig = (persona.get("signature") or "").strip()
    name = persona.get("name") or "雨雨"
    label = f"【{AI_LABEL}】"
    if AI_LABEL in sig:
        return sig
    return f"{label}我是 AI 虚拟女孩{name}。{sig}".strip()
