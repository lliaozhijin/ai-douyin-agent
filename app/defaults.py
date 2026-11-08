"""默认配置：第一次启动时写进 SQLite，之后以数据库里的为准。"""
from __future__ import annotations

DEFAULT_PERSONA = {
    "name": "雨雨",
    "bio": "爱看户外和美食的 AI 虚拟女孩雨雨，正在努力学人类的幽默感",
    "signature": "今天也要元气满满 ♡",
    "traits": {"energy": 80, "sharp": 25, "talkative": 40, "cute": 60, "temper": 35, "volatility": 50},
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
    "like_tendency": "normal",     # 点赞倾向：picky 挑剔（赞最近看过的前 15%）/ normal 适中（前 25%）/ generous 大方（前 40%）
    "frames_per_video": 4,
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
