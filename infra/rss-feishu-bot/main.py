import os
import re
import time
import html
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))

FEISHU_WEBHOOK = os.getenv("FEISHU_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/51d4824e-6b5a-4d35-b3f6-3bc14ef4f0e8")
DATA_DIR = os.getenv("DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)
RSSHUB_BASE_URL = os.getenv("RSSHUB_BASE_URL", "http://rsshub:1200")

# 连续失败阈值：必须连续失败 3 次（约 3 分钟以上）才判定为真正故障并报警，避免偶发抖动频繁弹窗
ALERT_FAIL_THRESHOLD = int(os.getenv("ALERT_FAIL_THRESHOLD", "3"))
ALERT_COOLDOWN_SECONDS = int(os.getenv("ALERT_COOLDOWN_SECONDS", "1800"))  # 持续异常时每 30 分钟最多提醒一次

SUBSCRIPTIONS = [
    {
        "id": "bilibili",
        "name": "B站动态",
        "url": os.getenv("BILI_RSS_URL", f"{RSSHUB_BASE_URL}/bilibili/user/dynamic/1039576265"),
        "cache_file": os.path.join(DATA_DIR, "last_guid_bilibili.txt"),
        "default_author": "B站UP主",
        "card_header": "📢 【B站动态提醒】{author} 发布了新动态",
        "card_color": "blue"
    }
]

# 动态配置 X (Twitter) 订阅列表
x_users_env = os.getenv("X_USERS")
if not x_users_env:
    legacy_x = os.getenv("X_RSS_URL")
    if legacy_x:
        x_users_raw = f"{legacy_x},wr0789"
    else:
        x_users_raw = "Sanchesssmith,wr0789"
else:
    x_users_raw = x_users_env

x_user_items = [u.strip() for u in x_users_raw.split(",") if u.strip()]

for u_raw in x_user_items:
    m = re.search(r'(?:twitter\.com|x\.com)/([a-zA-Z0-9_]+)', u_raw)
    if m:
        username = m.group(1)
        rss_url = f"{RSSHUB_BASE_URL}/twitter/user/{username}"
    elif "/twitter/user/" in u_raw:
        m_rss = re.search(r'/twitter/user/([a-zA-Z0-9_]+)', u_raw)
        username = m_rss.group(1) if m_rss else u_raw
        rss_url = u_raw
    else:
        username = u_raw
        rss_url = f"{RSSHUB_BASE_URL}/twitter/user/{username}"

    if username.lower() == "sanchesssmith":
        cache_filename = "last_guid_twitter.txt"
    else:
        cache_filename = f"last_guid_twitter_{username.lower()}.txt"

    SUBSCRIPTIONS.append({
        "id": f"twitter_{username.lower()}",
        "name": f"𝕏/{username}",
        "url": rss_url,
        "cache_file": os.path.join(DATA_DIR, cache_filename),
        "default_author": username,
        "card_header": "📢 【𝕏/Twitter 动态提醒】{author} 发布了新推文",
        "card_color": "blue"
    })

# 异常状态追踪器，防止刷屏：{sub_id: {"fail_count": 0, "first_fail_time": float, "last_alert_time": float, "is_alerted": False}}
ERROR_TRACKER = {}

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
    text = html.unescape(raw_html)
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</p>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    return text.strip()

def format_duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}秒"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}分钟"
    hours = minutes // 60
    rem_min = minutes % 60
    return f"{hours}小时{rem_min}分钟" if rem_min else f"{hours}小时"

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

def send_feishu_error_card(sub, error_type, detail, fail_count, duration_str):
    now_str = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    sub_name = sub.get("name", sub.get("id", "未知源"))
    sub_url = sub.get("url", "")
    
    clean_detail = clean_html(detail)
    if len(clean_detail) > 300:
        clean_detail = clean_detail[:297] + "..."
    if not clean_detail:
        clean_detail = "无详细错误响应"

    elements = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**📌 订阅源**：{sub_name}\n"
                    f"**⚠️ 异常类型**：<font color='red'>**{error_type}**</font>\n"
                    f"**🔢 失败统计**：连续失败 **{fail_count}** 次 (已持续 {duration_str})\n"
                    f"**⏰ 告警时间**：{now_str}\n\n"
                    f"**📝 错误信息**：\n```text\n{clean_detail}\n```"
                )
            }
        }
    ]

    if sub_url.startswith("http"):
        elements.append({
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {
                        "tag": "plain_text",
                        "content": "🔍 诊断 RSS 源地址"
                    },
                    "type": "danger",
                    "url": sub_url
                }
            ]
        })

    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"🚨 【RSS 订阅异常告警】{sub_name}"
                },
                "template": "red"
            },
            "elements": elements
        }
    }

    try:
        res = requests.post(FEISHU_WEBHOOK, json=payload, timeout=10)
        if res.status_code == 200:
            log(f"🚨 飞书异常告警卡片发送成功 (已连续失败 {fail_count} 次)", tag=sub_name)
        else:
            log(f"❌ 飞书异常告警卡片发送失败: {res.status_code} {res.text}", tag=sub_name)
    except Exception as e:
        log(f"❌ 发送异常告警网络出错: {e}", tag=sub_name)

def send_feishu_recovery_card(sub, fail_count, duration_str):
    now_str = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    sub_name = sub.get("name", sub.get("id", "未知源"))
    sub_url = sub.get("url", "")

    elements = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**📌 订阅源**：{sub_name}\n"
                    f"**🎉 状态**：<font color='green'>**已恢复正常**</font>\n"
                    f"**📊 故障回顾**：此前连续失败 **{fail_count}** 次，历时约 **{duration_str}**\n"
                    f"**⏰ 恢复时间**：{now_str}\n\n"
                    f"机器人已重新接入最新内容流，后续将继续正常监控更新。"
                )
            }
        }
    ]

    if sub_url.startswith("http"):
        elements.append({
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {
                        "tag": "plain_text",
                        "content": "🔗 打开订阅源"
                    },
                    "type": "primary",
                    "url": sub_url
                }
            ]
        })

    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"✅ 【RSS 订阅源已恢复】{sub_name}"
                },
                "template": "green"
            },
            "elements": elements
        }
    }

    try:
        res = requests.post(FEISHU_WEBHOOK, json=payload, timeout=10)
        if res.status_code == 200:
            log("✅ 飞书恢复通知卡片发送成功!", tag=sub_name)
        else:
            log(f"❌ 飞书恢复通知卡片发送失败: {res.status_code} {res.text}", tag=sub_name)
    except Exception as e:
        log(f"❌ 发送恢复通知网络出错: {e}", tag=sub_name)

def handle_subscription_error(sub, error_type, detail):
    sub_id = sub["id"]
    tag = sub["name"]
    now = time.time()

    if sub_id not in ERROR_TRACKER:
        ERROR_TRACKER[sub_id] = {
            "fail_count": 0,
            "first_fail_time": now,
            "last_alert_time": 0,
            "is_alerted": False
        }

    tracker = ERROR_TRACKER[sub_id]
    tracker["fail_count"] += 1
    duration_str = format_duration(now - tracker["first_fail_time"])

    # 关键防抖逻辑：
    # 偶发 1~2 次失败（可能是源站轻微抖动、空数据缓存等）不告警，仅后台打日志。
    # 只有连续失败达到 ALERT_FAIL_THRESHOLD (默认 3 次) 时，才首次向飞书发送报警。
    should_alert = False
    if tracker["fail_count"] >= ALERT_FAIL_THRESHOLD:
        if not tracker["is_alerted"]:
            should_alert = True
        elif now - tracker["last_alert_time"] >= ALERT_COOLDOWN_SECONDS:
            should_alert = True

    if should_alert:
        send_feishu_error_card(sub, error_type, detail, tracker["fail_count"], duration_str)
        tracker["last_alert_time"] = now
        tracker["is_alerted"] = True
    else:
        if tracker["fail_count"] < ALERT_FAIL_THRESHOLD:
            log(f"⚠️ 遇到偶发抖动 (连续失败 {tracker['fail_count']}/{ALERT_FAIL_THRESHOLD} 次，暂不触发飞书告警): {error_type}", tag=tag)
        else:
            log(f"⚠️ 订阅持续异常中 (第 {tracker['fail_count']} 次失败，告警冷却中): {error_type} - {detail[:80]}", tag=tag)

def handle_subscription_success(sub):
    sub_id = sub["id"]
    tag = sub["name"]
    if sub_id in ERROR_TRACKER:
        tracker = ERROR_TRACKER.pop(sub_id)
        # 只有在之前确实触发过报警（连续失败 >= 3 次）的情况下，恢复时才推送恢复通知
        if tracker.get("is_alerted"):
            duration_str = format_duration(time.time() - tracker["first_fail_time"])
            log(f"🎉 订阅源故障已恢复！共失败 {tracker['fail_count']} 次，持续 {duration_str}", tag=tag)
            send_feishu_recovery_card(sub, tracker["fail_count"], duration_str)

def check_subscription(sub):
    last_guid = load_last_guid(sub["cache_file"])
    tag = sub["name"]
    
    # 增加即时轻量重试（遇到 503/412 时停顿 1 秒再试一次，缓解源站瞬间频控）
    max_retries = 2
    res = None
    for attempt in range(max_retries):
        try:
            res = requests.get(sub["url"], timeout=15)
            if res.status_code == 200:
                break
            if attempt < max_retries - 1:
                time.sleep(1.5)
        except Exception:
            if attempt < max_retries - 1:
                time.sleep(1.5)
            else:
                pass

    try:
        if res is None:
            handle_subscription_error(sub, "网络连接失败", "请求未收到任何响应")
            return

        if res.status_code != 200:
            error_msg = f"HTTP {res.status_code}"
            log(f"❌ 获取 RSS 失败: {error_msg}, 响应: {res.text[:100]}", tag=tag)
            handle_subscription_error(sub, f"HTTP {res.status_code} 状态异常", res.text[:300])
            return
        
        root = ET.fromstring(res.text)
        channel = root.find("channel")
        if channel is None:
            err_detail = "返回 XML 中未包含 <channel> 根节点"
            log(f"⚠️ RSS 格式错误: {err_detail}", tag=tag)
            handle_subscription_error(sub, "RSS 内容格式解析失败", err_detail)
            return
            
        items = channel.findall("item")
        if not items:
            log("ℹ️ RSS 无内容条目", tag=tag)
            handle_subscription_success(sub)
            return
            
        latest_item = items[0]
        title = latest_item.find("title").text if latest_item.find("title") is not None else "新动态"
        link = latest_item.find("link").text if latest_item.find("link") is not None else ""
        guid = latest_item.find("guid").text if latest_item.find("guid") is not None else link
        pub_date = latest_item.find("pubDate").text if latest_item.find("pubDate") is not None else ""
        author = latest_item.find("author").text if latest_item.find("author") is not None else sub["default_author"]
        description = latest_item.find("description").text if latest_item.find("description") is not None else ""

        # 走到这里代表 RSS 请求和解析完全成功，检查并触发恢复逻辑
        handle_subscription_success(sub)

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
            
    except requests.exceptions.RequestException as e:
        log(f"❌ 请求 RSS 发生网络异常: {e}", tag=tag)
        handle_subscription_error(sub, f"网络请求异常 ({type(e).__name__})", str(e))
    except ET.ParseError as e:
        log(f"❌ RSS XML 解析失败: {e}", tag=tag)
        handle_subscription_error(sub, "XML 语法解析失败", str(e))
    except Exception as e:
        log(f"❌ 检查更新发生未知异常: {e}", tag=tag)
        handle_subscription_error(sub, f"系统未知异常 ({type(e).__name__})", str(e))

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
