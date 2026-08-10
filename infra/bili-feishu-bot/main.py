import os
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))

RSS_URL = os.getenv("RSS_URL", "http://rsshub:1200/bilibili/user/dynamic/1039576265")
FEISHU_WEBHOOK = os.getenv("FEISHU_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/51d4824e-6b5a-4d35-b3f6-3bc14ef4f0e8")
DATA_DIR = os.getenv("DATA_DIR", "/data")
CACHE_FILE = os.path.join(DATA_DIR, "last_guid.txt")

os.makedirs(DATA_DIR, exist_ok=True)

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

def load_last_guid():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception as e:
            print(f"读取历史记录失败: {e}")
    return None

def save_last_guid(guid):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            f.write(guid)
    except Exception as e:
        print(f"保存历史记录失败: {e}")

def send_feishu_card(title, author, link, pub_date):
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"📢 【B站动态提醒】{author} 发布了新动态"
                },
                "template": "blue"
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**动态摘要**：\n{title}\n\n**发布时间**：{pub_date}"
                    }
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {
                                "tag": "plain_text",
                                "content": "🔗 点击查看原动态"
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
        print(f"飞书推送结果: status={res.status_code}, body={res.text}")
    except Exception as e:
        print(f"推送飞书失败: {e}")

def check_update():
    last_guid = load_last_guid()
    try:
        res = requests.get(RSS_URL, timeout=15)
        if res.status_code != 200:
            print(f"获取 RSS 失败，HTTP 状态码: {res.status_code}")
            return
        
        root = ET.fromstring(res.text)
        channel = root.find("channel")
        if channel is None:
            return
            
        items = channel.findall("item")
        if not items:
            return
            
        latest_item = items[0]
        title = latest_item.find("title").text if latest_item.find("title") is not None else "无标题"
        link = latest_item.find("link").text if latest_item.find("link") is not None else ""
        guid = latest_item.find("guid").text if latest_item.find("guid") is not None else link
        pub_date = latest_item.find("pubDate").text if latest_item.find("pubDate") is not None else ""
        author = latest_item.find("author").text if latest_item.find("author") is not None else "UP主"

        if not last_guid:
            print(f"服务初始化记录，当前最新动态 GUID: {guid}")
            save_last_guid(guid)
            return

        if guid != last_guid:
            print(f"🎉 检测到新动态: {title}")
            send_feishu_card(title, author, link, pub_date)
            save_last_guid(guid)
        else:
            now_str = datetime.now(CST).strftime("%H:%M:%S")
            print(f"[{now_str}] 暂无新动态更新 (最新 GUID: {guid[-15:]})")
            
    except Exception as e:
        print(f"检查动态时发生异常: {e}")

if __name__ == "__main__":
    print("🚀 B站动态飞书智能轮询服务已启动！")
    while True:
        check_update()
        interval_seconds, mode_desc = get_check_interval()
        now_str = datetime.now(CST).strftime("%H:%M:%S")
        print(f"[{now_str}] 当前监控时段: {mode_desc}，下次轮询将在 {interval_seconds} 秒后进行")
        time.sleep(interval_seconds)
