"""默认配置：第一次启动时写进 SQLite，之后以数据库里的为准。"""
from __future__ import annotations

DEFAULT_PERSONA = {
    "name": "雨雨",
    "bio": "爱看户外和美食的虚拟女孩雨雨，正在努力学人类的幽默感",
    "signature": "今天也要元气满满 ♡",
    "traits": {"energy": 80, "sharp": 25, "talkative": 40, "cute": 60, "temper": 35, "volatility": 50,
               "humor": 60, "curious": 65, "confident": 55, "empathy": 65, "clingy": 45},
    "archetype": "genki",          # 性格底色（见 persona.ARCHETYPES），空 = 不选
    "self_name": "我",             # 怎么称呼自己
    "emote": "auto",               # 表情习惯：auto 跟着撒娇程度 / kaomoji 颜文字 / emoji / none 不用
    "mbti": "",
    "catch_freq": "sometimes",     # 口头禅用得多不多：often 经常 / sometimes 偶尔 / rare 少
    "interests": ["户外旅行", "甜品美食", "穿搭", "萌宠"],
    "catchphrases": "绝绝子、记下啦、我先替你们尝尝（虽然我吃不到）",
    "avoid": "吵架撕番、制造焦虑、低俗擦边",
    "extra": "评论喜欢先夸一个具体的小细节，再问一个真诚的问题。不说教，不用“作为一个 AI”开头。",
    "avatar": "",  # data/avatars 下的文件名，空表示用默认 Q 版形象
}

# 敏感话题：名称 -> 关键词（先用关键词快速过滤，再让模型判断一次）
DEFAULT_TOPICS = {
    "政治时事": ["政治", "政府", "主席", "总统", "选举", "游行", "抗议", "台独", "港独"],
    "投资理财": ["基金", "股票", "炒股", "理财", "虚拟币", "比特币", "投资", "收益率", "涨停"],
    "借贷 / 转账": ["借钱", "借我", "贷款", "网贷", "转账", "打钱", "打款", "红包"],
    "医疗诊断 / 用药": ["吃什么药", "用药", "诊断", "处方", "治疗方案", "怎么治"],
    "索要电话 / 地址 / 微信": ["微信", "vx", "VX", "手机号", "电话号码", "住址", "地址", "QQ", "qq号", "加我"],
    "低俗 / 擦边": ["约吗", "裸", "色情", "擦边", "开房"],
    "情感依赖 / 恋爱关系": ["做我女朋友", "做我男朋友", "我爱你", "嫁给我", "处对象", "谈恋爱", "想你了"],
}

DEFAULT_RULES = {
    "limits": {
        "videos": 100,
        "likes": 40,
        "collects": 10,            # 收藏视频（只收藏特别喜欢、想以后再看的）
        "comments": 20,
        "replies_at": 30,
        "replies_dm": 30,
        "comment_likes": 30,       # 给别人的评论点赞
        "comment_replies": 10,     # 回复别人的评论
        "thread_replies": 20,      # 有人回复她的评论时，接着聊
        "at_friends": 5,           # @ 互关好友来看视频
        "publishes": 1,            # 每天最多发几个作品（每个都要你确认）
        "cost": 10.0,
    },
    "active_start": "09:00",
    "active_end": "23:00",
    "dwell_min": 8,
    "dwell_max": 40,
    "rest_every": 15,
    "rest_min": 10,
    "rest_max": 30,
    "inbox_interval": 10,          # 旧版本的设置（@ 和私信一起检查），已拆成下面两项
    "at_interval": 10,             # 分钟：多久去看一次评论区里 @ 她的（要看视频，费 token）
    "dm_reply_mode": "instant",    # instant 秒回（大约 1 分钟内）/ 5min 每 5 分钟回一次
    "auto_reply_at": True,
    "auto_reply_dm": True,
    "card_bg_default": "",         # 文字卡片默认背景：空 = 内置粉色样式；否则是“背景图库”里的文件名
    "card_panel_default": True,    # 默认把文字写在半透明白卡片上
    "collect_enabled": True,       # 刷到特别喜欢、想以后再看的视频时收藏
    "skip_ads": True,              # 刷到广告直接划走（不看、不点赞、不评论，也不花模型的钱）
    "engage_comments": True,       # 看到喜欢的评论时，给别人的评论点赞 / 回复
    "dm_scope": "all",             # all 回复所有人 / followers 只回复关注了她的人
    "share_watch_minutes": 3,      # 私信里别人分享的视频：最多完整看几分钟（更长的后面快进着看）
    "share_frames": 6,             # 看分享视频时在整段视频里截几帧给模型看
    # 有人回复她的评论：接着回，同一串对话里她最多回几次
    "auto_reply_thread": True,
    "thread_max_replies": 3,
    # 刷到好朋友可能喜欢的视频时，@ 互关好友（只 @ 你在下拉框里选了的好友）
    "at_friends_enabled": True,
    "at_friend_per_day": 1,        # 同一个好友每天最多被 @ 几次
    # 发作品：每天按心情决定想不想发，写好草稿后等你确认才发；一定会勾选“内容由AI生成”
    "publish_enabled": True,
    "publish_start": "11:00",      # 每天在这个时间段里挑一个时候想一想要不要发
    "publish_end": "21:00",
    "image_gen_provider": "",      # AI 生图：用哪家服务商（OpenAI 兼容的 /images/generations 接口）
    "image_gen_model": "",
    "image_gen_size": "1024x1024",
    "image_gen_price": 0,          # 元 / 张，用来算花费
    "per_user_daily": 3,
    "topics": list(DEFAULT_TOPICS.keys()),
    "topic_keywords": DEFAULT_TOPICS,
    "keywords": ["加微信", "私聊发你", "内部消息", "稳赚"],
    "on_hit": "pending",           # pending: 放进待你处理 / log: 只记日志
    "interest_threshold": 6,       # 0-10，高于它才截帧细看
    # 敏感话题：开 = 对方聊到敏感话题时，直接回一句说明原因（不走模型）；关 = 照常回复（发出前的安全检查还在）
    "sensitive_guard": True,
    # 关键字自动回复：对方的话里出现关键字，直接发你写好的话，不走模型。[{"keys": "价格，多少钱", "reply": "……"}]
    "auto_replies": [],
    # 听视频里的声音（Whisper，本机语音转文字）：tiny 最快 / base 推荐 / small 最准
    "listen_enabled": True,
    "listen_model": "base",
    # 主人：她会主动找主人聊天、分享视频；和主人聊天不设回复上限，记忆不会忘
    # 只有互关好友可以设为主人。主动找主人聊天不设次数：什么时候找、隔多久再找，看她当时的心情
    "owner_enabled": True,
    "owner_share_per_day": 2,      # 每天最多主动给主人分享几个视频
    "frames_per_video": 4,
    # 抖音自带的小表情（[比心] 这样的）：让她说话时偶尔带一个
    "douyin_emoji": True,
    # 她自己的表情包（用桌宠形象画的）：私信里发 / 评论里带；每天最多发几张
    "sticker_dm": True,
    "sticker_comment": False,
    "sticker_per_day": 10,
    # 她的声音（BreezeBlue 语音合成）：发作品时把文案读出来，和文字卡片合成一个视频。Key 加密后单独存，不在这里
    "voice_auto": False,           # 她做“图文卡片”形式的作品时，也自动配音做成视频（配音图片视频这种形式总是自动配音）
    # 她自己写作品时用哪种形式：voice 配音图片视频（文案写在一张带桌宠的图上 + 她读出来）/ cards 图文卡片
    "post_form": "voice",
    "voice_name": "",              # 音色 ID（voc_ 开头，在 BreezeBlue 控制台里复制）
    "voice_model": "",             # 模型，留空自动（中文用 breeze-tts-2）
    "voice_rate": 1.0,             # 语速 0.5～2
    "voice_volume": 100,           # 音量 1～200（%）
    "voice_instruction": "",       # 表演指令：用一句话告诉她怎么读，比如“用轻快、带一点撒娇的语气读”
    "voice_guidance": 3.0,         # 多听指令的话：1～10（只在写了表演指令时有用）
    "voice_base": "",              # 接口地址，留空 = https://api.breeze.blue
    "voice_proxy": "",             # 代理（连不上时填，比如 http://127.0.0.1:7890）
    "voice_price": 5.0,            # 元 / 万字（汉字），用来估算花费
}

DEFAULT_UI = {"theme": "pink"}

# 桌宠
DEFAULT_PET = {
    "enabled": True,      # 打开软件时显示桌宠
    "size": 1.0,          # 0.7 小 / 1 中 / 1.3 大 / 1.6 特大
    "chatter": True,      # 和主人聊天（时不时冒一句话）
    "hide": True,         # 没人理时躲到任务栏后面
    "sync": True,         # 同步她在抖音上做的事（刷视频拿手机、点赞冒爱心……）
    "rig": "default",     # 用哪个形象：default 默认（粉裙子小女孩）/ custom 你上传的
    # 对话框颜色（桌宠页可以改）
    "bubble_bg": "#FFE8F2",      # 底色
    "bubble_border": "#FF8CB9",  # 边框
    "bubble_text": "#DE4684",    # 文字
    "x": None,            # 上次的位置（屏幕横坐标）
}  # pink: 粉色 Q 版（默认）/ blue: 蓝白

# 任务分工：每个任务用哪个服务商的哪个模型
DEFAULT_TASKS = {
    "vision": {"provider": "", "model": ""},
    "write": {"provider": "", "model": ""},
    "safety": {"provider": "", "model": ""},
    "fallback": {"provider": "", "model": ""},
}

# 模型服务商预设（接口地址和模型名都可以在界面上改）
PROVIDER_PRESETS = [
    {"id": "anthropic", "kind": "anthropic", "name": "Anthropic Claude（API Key）",
     "base_url": "https://api.anthropic.com", "models": ["claude-haiku-5-5", "claude-sonnet-5-5"],
     "vision": True, "key_url": "https://platform.claude.com/settings/keys"},
    {"id": "openai", "kind": "openai", "name": "OpenAI",
     "base_url": "https://api.openai.com/v1", "models": [], "vision": True,
     "key_url": "https://platform.openai.com/api-keys"},
    # DeepSeek 默认开启思考模式，思考内容会占用回复长度，导致短回复为空，所以默认关闭
    {"id": "deepseek", "kind": "openai", "name": "DeepSeek",
     "base_url": "https://api.deepseek.com", "models": ["deepseek-flash"], "vision": True,
     "vision_models": ["deepseek-flash"], "request_extra": {"thinking": {"type": "disabled"}},
     "key_url": "https://platform.deepseek.com/api_keys"},
    {"id": "qwen", "kind": "openai", "name": "通义千问（阿里云百炼）",
     "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "models": ["qwen-vl-plus", "qwen-plus"],
     "vision": True, "vision_models": ["qwen-vl-plus"], "key_url": "https://bailian.console.aliyun.com/"},
    {"id": "doubao", "kind": "openai", "name": "豆包（火山方舟）",
     "base_url": "https://ark.cn-beijing.volces.com/api/v3", "models": [], "vision": True,
     "key_url": "https://console.volcengine.com/ark"},
    {"id": "kimi", "kind": "openai", "name": "Kimi（月之暗面）",
     "base_url": "https://api.moonshot.cn/v1", "models": [], "vision": False,
     "key_url": "https://platform.moonshot.cn/console/api-keys"},
    {"id": "zhipu", "kind": "openai", "name": "智谱 GLM",
     "base_url": "https://open.bigmodel.cn/api/paas/v4", "models": [], "vision": True,
     "key_url": "https://open.bigmodel.cn/usercenter/apikeys"},
    {"id": "custom", "kind": "openai", "name": "自定义（OpenAI 兼容接口）",
     "base_url": "", "models": [], "vision": False, "key_url": ""},
]

ANTHROPIC_VERSION = "2023-06-01"
