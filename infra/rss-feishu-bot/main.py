import os
import re
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))

FEISHU_WEBHOOK = os.getenv("FEISHU_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/51d4824e-6b5a-4d35-b3f6-3bc14ef4f0e8")
DATA_DIR = os.getenv("DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)

SUBSCRIPTIONS = [
    {
        "id": "bilibili",
        "name": "B站动态",
        "url": os.getenv("BILI_RSS_URL", "http://rsshub:1200/bilibili/user/dynamic/3707010679835372"),
        "cache_file": os.path.join(DATA_DIR, "last_guid_bilibili.txt"),
        "default_author": "B站UP主",
        "card_header": "📢 【B站动态提醒】{author} 发布了新动态",
        "card_color": "blue"
    },
    {
        "id": "twitter",
        "name": "𝕏/Twitter",
        "url": os.getenv("X_RSS_URL", "http://rsshub:1200/twitter/user/Sanchesssmith"),
        "cache_file": os.path.join(DATA_DIR, "last_guid_twitter.txt"),
        "default_author": "Sanchesssmith",
        "card_header": "📢 【𝕏/Twitter 动态提醒】{author} 发布了新推文",
        "card_color": "blue"
    }
]

def log(msg, tag="系统"):
    now_str = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_str}] [{tag}] {msg}", flush=True)

def get_check_interval():
    """
    判断当前时间是否处于交易时间段（工作日 09:00~11:30, 13:00~15:00）
    工作日交易时间返回 60 秒（1 分钟），其他非交易时间返回 3600 秒（1 小时）
    """
    now = datetime.now(CST)
    is_weekday = now.weekday() < 5  # 0=Mon, 4=Fri, 5=Sat, 6=Sun
    t = now.time()

    in_morning = (t.hour == 9 and t.minute >= 0) or (t.hour == 10) or (t.hour == 11 and t.minute <= 30)
    in_afternoon = (t.hour >= 13 and t.hour < 15) or (t.hour == 15 and t.minute == 0)

    if is_weekday and (in_morning or in_afternoon):
        return 60, "高频模式 (1分钟/次)"
    else:
        return 3600, "低频模式 (1小时/次)"

def load_last_guid(cache_file):
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception as e:
            log(f"⚠️ 读取历史记录失败: {e}", tag="文件缓存")
    return None

def save_last_guid(cache_file, guid):
    try:
        with open(cache_file, "w", encoding="utf-8") as f:
            f.write(guid)
    except Exception as e:
        log(f"⚠️ 保存历史记录失败: {e}", tag="文件缓存")

def clean_html(raw_html):
    if not raw_html:
        return ""
    text = re.sub(r'<br\s*/?>', '\n', raw_html, flags=re.IGNORECASE)
    text = re.sub(r'</p>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    return text.strip()

def send_feishu_card(sub, title, author, link, pub_date, description=""):
    clean_desc = clean_html(description)
    content_text = clean_desc if clean_desc else title
    if len(content_text) > 500:
        content_text = content_text[:497] + "..."

    header_text = sub["card_header"].format(author=author)

    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": header_text
                },
                "template": sub.get("card_color", "blue")
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**动态内容**：\n{content_text}\n\n**发布时间**：{pub_date}"
                    }
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {
                                "tag": "plain_text",
                                "content": "🔗 点击查看原文"
                            },
                            "type": "primary",
                            "url": link
                        }
                    ]
                }
            ]
        }
    }
    try:
        res = requests.post(FEISHU_WEBHOOK, json=payload, timeout=10)
        if res.status_code == 200:
            log("✉️ 飞书卡片推送成功!", tag=sub["name"])
        else:
            log(f"❌ 飞书卡片推送失败! Status: {res.status_code}, Body: {res.text}", tag=sub["name"])
    except Exception as e:
        log(f"❌ 推送飞书发生网络异常: {e}", tag=sub["name"])

def check_subscription(sub):
    last_guid = load_last_guid(sub["cache_file"])
    tag = sub["name"]
    try:
        res = requests.get(sub["url"], timeout=15)
        if res.status_code != 200:
            log(f"❌ 获取 RSS 失败，HTTP 状态码: {res.status_code}, 响应: {res.text[:100]}", tag=tag)
            return
        
        root = ET.fromstring(res.text)
        channel = root.find("channel")
        if channel is None:
            log("⚠️ RSS 格式错误: 未找到 <channel>", tag=tag)
            return
            
        items = channel.findall("item")
        if not items:
            log("ℹ️ RSS 无内容条目", tag=tag)
            return
            
        latest_item = items[0]
        title = latest_item.find("title").text if latest_item.find("title") is not None else "新动态"
        link = latest_item.find("link").text if latest_item.find("link") is not None else ""
        guid = latest_item.find("guid").text if latest_item.find("guid") is not None else link
        pub_date = latest_item.find("pubDate").text if latest_item.find("pubDate") is not None else ""
        author = latest_item.find("author").text if latest_item.find("author") is not None else sub["default_author"]
        description = latest_item.find("description").text if latest_item.find("description") is not None else ""

        short_title = title.replace('\n', ' ')
        if len(short_title) > 30:
            short_title = short_title[:27] + "..."

        if not last_guid:
            log(f"ℹ️ 初始化记录最新: 『{short_title}』", tag=tag)
            save_last_guid(sub["cache_file"], guid)
            return

        if guid != last_guid:
            log(f"🎉 检测到更新！作者: {author} | 内容: 『{short_title}』", tag=tag)
            send_feishu_card(sub, title, author, link, pub_date, description)
            save_last_guid(sub["cache_file"], guid)
        else:
            log(f"💤 暂无更新 (最新: 『{short_title}』)", tag=tag)
            
    except Exception as e:
        log(f"❌ 检查更新发生异常: {e}", tag=tag)

if __name__ == "__main__":
    log("🚀 聚合版 RSS 飞书智能轮询服务已启动！")
    for sub in SUBSCRIPTIONS:
        log(f"📌 加载订阅源: {sub['name']} -> {sub['url']}")
    
    while True:
        for sub in SUBSCRIPTIONS:
            check_subscription(sub)
        
        interval_seconds, mode_desc = get_check_interval()
        log(f"⏰ 本轮检查完成，当前模式: {mode_desc}，将在 {interval_seconds} 秒后下一轮")
        time.sleep(interval_seconds)
