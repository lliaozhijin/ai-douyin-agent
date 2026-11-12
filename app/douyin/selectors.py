"""抖音网页版的页面元素定位。

抖音网页会改版，所以这些定位写在 data/selectors.json 里，可以不改代码直接调整。
第一次运行时会把下面的默认值写进那个文件；之后以文件为准（文件里没有的键用默认值）。
每个键可以是一个字符串，也可以是一个列表（按顺序尝试，第一个找到的生效）。
"""
from __future__ import annotations

import json
import logging

from ..paths import SELECTORS_FILE

log = logging.getLogger(__name__)

SELECTORS_VERSION = 23  # 改了默认定位就加 1，旧的 selectors.json 会自动备份并更新

DEFAULT_SELECTORS: dict = {
    "_说明": "抖音网页改版后，如果某个动作失效，修改这里对应的定位即可。修改后重启软件生效。",
    "_version": SELECTORS_VERSION,
    "home_url": "https://www.douyin.com/?recommend=1",
    "login_url": "https://www.douyin.com/",
    "profile_url": "https://www.douyin.com/user/self",
    "login_cookie_names": ["sessionid", "sessionid_ss"],
    # 退出登录时清掉名字里带这些词的 cookie（登录凭证）；其余设备相关的 cookie 保留
    "login_cookie_words": ["session", "sid", "uid_tt", "passport", "odin_tt", "n_mh", "login", "token"],

    # 推荐页（打开后被跳到“精选”时，点左侧导航的“推荐”）
    "nav_recommend": ["a[href*='recommend=1']", "[data-e2e='douyin-navigation'] >> text=推荐"],
    "feed_active": ["[data-e2e='feed-active-video']"],
    "video_element": ["[data-e2e='feed-active-video'] video", "video"],
    "video_desc": ["[data-e2e='feed-active-video'] [data-e2e='video-desc']", "[data-e2e='video-desc']"],
    "video_author": ["[data-e2e='feed-active-video'] [data-e2e='feed-video-nickname']",
                     "[data-e2e='feed-video-nickname']"],
    "like_button": ["[data-e2e='feed-active-video'] [data-e2e='video-player-digg']",
                    "[data-e2e='video-player-digg']"],
    "collect_button": ["[data-e2e='feed-active-video'] [data-e2e='video-player-collect']",
                       "[data-e2e='video-player-collect']"],
    "comment_button": ["[data-e2e='feed-active-video'] [data-e2e='feed-comment-icon']",
                       "[data-e2e='feed-comment-icon']"],
    # 单个视频的详情页（www.douyin.com/video/<id>，私信分享、@ 通知里点进去的都是这种页面）
    # 图文作品（www.douyin.com/note/<id>）的详情页是 note-detail，文案在作者那一块下面
    "detail_container": ["[data-e2e='video-detail']", "[data-e2e='note-detail']"],
    "detail_desc": ["[data-e2e='detail-video-info'] h1", "[data-e2e='detail-video-info']",
                    "[data-e2e='user-info'] + div"],
    "detail_comment_tab": ["text=/^评论\\s*[(（]/", "[role='tab']:has-text('评论')"],
    "detail_author": ["[data-e2e='user-info'] a[href*='/user/']"],
    # 用户主页（www.douyin.com/user/<id>）：名字、抖音号、头像、关注按钮（“回关”“互相关注”说明对方关注了她）
    "profile_info": ["[data-e2e='user-info']"],
    "profile_name": ["[data-e2e='user-info'] h1"],
    "profile_avatar": ["[data-e2e='live-avatar'] img", "[data-e2e='user-detail'] img"],
    "profile_follow_btn": ["[data-e2e='user-info-follow-btn']"],
    "profile_follows_me_words": ["回关", "互相关注", "相互关注"],
    "next_video_key": "ArrowDown",
    "like_key": "z",

    # 评论区
    "comment_list": ["[data-e2e='comment-list']"],
    # 广告视频：作者名后面的“广告”小标签（一个 30×16 的图标），播放器里还有一句“广告视频不支持倍速功能”
    "ad_marker": ["[data-e2e='feed-active-video'] .xgTips:has-text('广告视频')",
                  "[data-e2e='feed-active-video'] .account .video-create-time + svg[width='30'][height='16']"],
    # 带商品的视频点开评论时，右边可能先弹商品卡（#videoSideBar，有“立即购买”），把评论卡（#videoSideCard）挡住
    "product_card": ["#videoSideBar:has-text('立即购买')"],
    "product_card_close": ["#videoSideBar #svg_icon_circle_close", "#videoSideBar [class*='close']"],
    "comment_item": ["[data-e2e='comment-item']"],
    "comment_input": [".comment-input-inner-container [contenteditable='true']",
                      ".comment-input-container [contenteditable='true']",
                      "[data-e2e='comment-input'] [contenteditable='true']",
                      ".public-DraftEditor-content"],
    # 评论输入框没点开时只是一行“留下你的精彩评论吧”的占位文字，要先点一下它，可以打字的输入框才会出现
    "comment_input_open": [".comment-input-inner-container", ".comment-input-container",
                           "text=留下你的精彩评论吧"],
    # 输入框右边红色的发送按钮（输入了内容 / 配了图以后才出现）。平时按回车发送；回车没发出去时点它
    "comment_send": [".commentInput-right-ct span:has(path[fill='#FE2C55'])",
                     ".comment-input-container span:has(path[fill='#FE2C55'])",
                     ".commentInput-right-ct span:has(svg):nth-of-type(4)"],
    "comment_reply_text": "回复",
    # 每条评论里的各个部分（2026-11 实测网页结构）
    "comment_user": [".comment-item-info-wrap a[href*='/user/']"],
    "comment_text": [".comment-item-info-wrap + div", "[class*='comment-item-content']"],
    "comment_id": ["[data-e2e='video-comment-more'] [id^='tooltip_']"],
    "comment_like": [".comment-item-stats-container > div:first-child > p:first-child",
                     ".comment-item-stats-container p:has(svg)"],
    "comment_reply_button": [".comment-item-stats-container span:text-is('回复')",
                             ".comment-item-stats-container :text-is('回复')"],
    "comment_reply_input": ["[contenteditable='true']:focus",
                            ".comment-input-inner-container [contenteditable='true']",
                            ".public-DraftEditor-content"],
    "comment_close": ["[data-e2e='comment-close']", ".comment-close"],
    # 评论区底部的“加载更多 / 展开更多”（只在评论列表里找）；找 @ 她的那条评论时最多往下翻几屏
    "comment_more": ["button:has-text('加载更多')", ":text-is('展开更多')", "button:has-text('查看更多评论')",
                     ":text-matches('^查看更多评论')"],
    "comment_search_rounds": 40,

    # 通知（@ 我的）
    # 2026-11 实测：右上角“通知”打开“互动消息”面板，每条通知一行：头像、名字、评论内容、“在评论中提到了你 刚刚”、视频缩略图
    "notice_entry": ["[data-e2e='notice-entry']", "[data-e2e='something-button']:has-text('通知')"],
    "notice_panel": ["[data-e2e='listDlgTest-container']"],
    "notice_item": ["[data-e2e='listDlgTest-container'] div[style*='height']:has([data-e2e='user-name-card'])"],
    "notice_user": ["[data-e2e='user-name-card']"],
    "notice_empty_text": ["暂无消息", "暂无通知", "暂无互动消息"],
    "notice_content": ["pre"],
    "notice_at_keywords": ["@了你", "提到了你", "在评论中@了你"],
    "notice_reply_keywords": ["回复了你的评论", "回复了你"],
    # 评论下面折叠的回复（“展开 3 条回复”）
    # 2026-11 实测：“展开 N 条回复”是评论里的一个按钮（class 带 comment-reply-expand-btn）；
    # Playwright 的 :text-matches 在这里匹配不到，改用 has-text
    "comment_expand": ["button.comment-reply-expand-btn", "button:has-text('条回复')", "button:has-text('展开更多')",
                       ":text-matches('展开\\d*条回复')", "[class*='expand']"],
    # 在评论框里输入 @ 和名字后弹出的好友候选（点一下才是真正的 @，对方才会收到提醒）
    "mention_item": ["[class*='mention'] [class*='item']:visible", "[class*='Mention'] [class*='item']:visible",
                     "[class*='atUser']:visible", "[class*='at-user']:visible", "[role='option']:visible",
                     "[class*='suggest'] li:visible", ".semi-popover-content li:visible", "[class*='userItem']:visible"],

    # 自己主页的“关注”列表（读互关好友）
    "following_entry": ["[data-e2e='user-info-follow']", "[data-e2e='user-info'] div:has-text('关注'):not(:has-text('粉丝'))"],
    "following_panel": ["[data-e2e='user-fans-container']", "[role='dialog']:visible", ".semi-modal-content:visible"],
    "following_item": ["[data-e2e='user-fans-container'] li", "[role='dialog'] li", ".semi-modal-content li"],
    "mutual_words": ["互相关注", "相互关注", "互关"],

    # 抖音创作者中心：发布作品（图文 / 视频）
    "creator_upload_video": "https://creator.douyin.com/creator-micro/content/upload",
    "creator_upload_image": "https://creator.douyin.com/creator-micro/content/upload?default-tab=3",
    "creator_file_input": ["input[type='file']"],
    "creator_tab_image": ["text=发布图文", "text=上传图文"],
    "creator_title": ["input[placeholder*='标题']", "input[placeholder*='作品标题']"],
    "creator_desc": ["[data-placeholder*='简介'][contenteditable='true']", ".zone-container[contenteditable='true']",
                     "[contenteditable='true']"],
    # 作品声明：高级设置 → 自主声明 → 内容由AI生成（必须勾上，否则不发布）
    "creator_declare_entry": ["text=自主声明", "text=添加声明", "text=作品声明", "text=声明"],
    "creator_declare_ai": ["label.semi-radio:has-text('内容由AI生成')", "label:has-text('内容由AI生成')",
                           "text=内容由AI生成"],
    "creator_declare_ok": ["button:has-text('确定')", "button:has-text('确认')", "button:has-text('完成')"],
    "creator_advanced": ["text=高级设置", "text=更多设置"],
    "creator_uploading": ["text=上传中", "text=正在上传", "text=处理中"],
    "creator_publish": ["button:text-is('发布')", "button:has-text('发布'):not(:has-text('定时'))"],
    "creator_done_words": ["发布成功", "作品管理", "content/manage"],
    "creator_login_words": ["扫码登录", "验证码登录", "密码登录"],

    # 私信（2026-11 实测：右侧面板，左边会话列表，点开某个会话后聊天窗口盖住列表）
    "dm_entry": ["[data-e2e='im-entry']", "[data-e2e='something-button']:has-text('消息')"],
    "dm_panel": ["[class*='ConversationListwrapper']", "[data-e2e='msg-input']", "[data-e2e='conversation-item']"],
    "dm_conversation_item": ["[data-e2e='conversation-item']:visible", "[data-e2e='conversation-item']"],
    "dm_conv_title": ["[class*='ConversationItemtitle']:not([class*='Wrapper'])"],
    "dm_unread_badge": ["[class*='unread']", "[class*='badge']", "[class*='Badge']"],
    "dm_chat_title": ["[class*='StackChatHeadertitle']:not([class*='Container']):visible"],
    "dm_chat_header": ["[class*='StackChatHeaderuser']:visible", "[data-e2e='chat-header']:visible"],
    "dm_back": ["[class*='StackTitleBarleftArea']:visible svg", "[class*='StackTitleBarleftArea']:visible"],
    "dm_close": ["svg[class*='closeImPage']:visible"],
    "dm_message_item": ["[data-e2e='msg-item-content']", "[data-e2e='msg-item']"],
    "dm_from_me": "[class*='isFromMe']",
    "dm_newest_first": True,
    "dm_conv_avatar": ["img"],
    "dm_chat_avatar": ["[class*='StackChatHeader'] [data-apm-action='个人页卡片']:visible",
                       "[class*='StackChatHeaderuser'] img:visible"],
    # 只在私信面板和它弹出的资料卡片里找主页链接（不能在整个页面找，会找到后面视频作者的主页）
    "dm_profile_link": ["#imSaasContainerId a[href*='/user/']", "[id*='im-entry'] a[href*='/user/']",
                        ".semi-portal a[href*='/user/']"],
    # 视频分享卡片（2026-11 实测：封面 + 作者头像 + 作者名 + 播放图标，网页上没有链接）
    "dm_share_card": ["[class*='MessageItemShareAweme']"],
    "dm_text_bubble": ["[class*='MessageItemText']"],
    "dm_share_link": ["a[href*='/video/']", "a[href*='modal_id=']", "a[href*='/note/']"],
    "dm_unsupported_text": "暂不支持该消息类型",
    # 私信输入框旁边“选择文件”的隐藏上传框（可以发 jpg / png / gif / mp4）；选完文件后如果弹出确认框，点里面的“发送”
    "dm_file_input": ["[data-e2e='msg-input'] input.semi-upload-hidden-input[type='file']",
                      "[data-e2e='msg-input'] input[type='file']"],
    "dm_file_confirm": ["[id='pc-im-popup-container'] button:has-text('发送')", ".semi-modal button:has-text('发送')",
                        "[class*='im-saas'] button:has-text('发送')"],
    # 聊天里的图片消息（2026-11 实测：class 带 MessageItemImage）。分享的视频卡片也有封面图，不算图片消息
    "dm_image_bubble": ["[class*='MessageItemImage']", "[class*='MessageItemEmoji']", "[class*='MessageItemSticker']"],
    # 评论输入框右边“图片”按钮背后的隐藏上传框（png / jpg / webp）
    "comment_file_input": [".comment-input-container input[type='file']", ".commentInput-right-ct input[type='file']"],
    "comment_image_preview": [".comment-input-container img[src^='blob:']", ".comment-input-container img[src*='douyinpic']",
                              ".comment-input-container [class*='image'] img"],
    "dm_input": ["[data-e2e='msg-input'] [contenteditable='true']", "[data-e2e='msg-input'] textarea"],

    # 编辑资料（2026-11 实测：入口是主页名字旁边的铅笔图标；简介是富文本编辑器，不是 textarea）
    "edit_profile_button": ["[data-e2e='user-info'] h1 ~ span[role='img']",
                            "[data-e2e='user-info'] .semi-icon", "text=编辑资料"],
    "bio_editor": ["div:has(> div:text-is('简介')) [contenteditable='true']",
                   "div:has(> div:text-is('简介')) textarea", ".public-DraftEditor-content"],
    "save_profile_button": ["button:has-text('保存')"],

    # 盖在页面上的新手引导 / 提示层：看到就点“我知道了”关掉（不是验证码）
    "tip_close": ["[data-e2e='recommend-guide-mask'] button",
                  "button:has-text('我知道了')", "button:has-text('知道了')"],
    "comment_key": "x",

    # 验证码（检测到就暂停，交给你手动处理）
    # 手机短信验证（登录或抖音风控时要求填手机收到的验证码）：检测到就弹窗提醒你去浏览器里填写
    # 创作者中心点“发布”后的二次验证弹窗（“接收短信验证码”）：.second-verify-panel / .uc-ui-verify_sms-verify
    "sms_verify": [".second-verify-panel", ".uc-ui-verify_sms-verify", "input[placeholder*='验证码']:visible",
                   "text=接收短信验证码", "text=获取验证码", "text=发送验证码", "text=短信验证",
                   "text=验证手机", "text=手机号验证", "text=身份验证"],
    # 刷视频 / 点私信时抖音弹的“二次验证”（短信验证码）面板和它的遮罩：开着的时候页面上什么都点不了
    "second_verify": ["#uc-second-verify .second-verify-mask", ".second-verify-panel", ".uc-ui-verify_sms-verify"],
    # 不要放“text=验证码”这种宽泛的文字：评论、文案里出现“验证码”三个字也会被当成弹了验证
    "captcha": ["#captcha_container", ".captcha_verify_container", "iframe[src*='verifycenter']",
                "iframe[src*='captcha']", "text=请完成下列验证"],
}


def _write_defaults() -> None:
    try:
        SELECTORS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SELECTORS_FILE.write_text(json.dumps(DEFAULT_SELECTORS, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        log.error("无法写入 selectors.json：%s", e)


def load_selectors() -> dict:
    data = dict(DEFAULT_SELECTORS)
    if not SELECTORS_FILE.exists():
        _write_defaults()
        return data
    try:
        user = json.loads(SELECTORS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.error("selectors.json 读取失败，使用默认值：%s", e)
        return data
    if int(user.get("_version") or 1) < SELECTORS_VERSION:
        # 软件更新带来了新的定位：旧文件备份成 selectors.json.bak，再写入新版
        try:
            SELECTORS_FILE.with_suffix(".json.bak").write_text(
                json.dumps(user, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
        log.info("selectors.json 已从第 %s 版更新到第 %s 版（旧文件备份为 selectors.json.bak）",
                 user.get("_version") or 1, SELECTORS_VERSION)
        _write_defaults()
        return data
    data.update({k: v for k, v in user.items() if not k.startswith("_")})
    return data


def as_list(v) -> list[str]:
    if v is None:
        return []
    return [v] if isinstance(v, str) else list(v)
