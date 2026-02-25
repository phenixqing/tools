# tools


1) 先拿 Telegram Bot Token 和 Chat ID

在 Telegram 搜索 @BotFather → /newbot → 拿到 BOT_TOKEN（形如 123456:ABC...）

获取 CHAT_ID（最简单做法）：

给你的 bot 发一条消息（比如“hi”）

浏览器打开（把 <TOKEN> 换成你的）：
https://api.telegram.org/bot<TOKEN>/getUpdates
    {
    "ok": true,
    "result": [
        {
        "update_id": 123456789,
        "message": {
            "chat": { "id": 123456789, "type": "private" },
            "text": "hi"
        }
        }
    ]
    }
在返回 JSON 里找到 message.chat.id（私聊一般是正数；群里一般是负数）




2)

设置环境变量 macOS / Linux
export TG_BOT_TOKEN="123456:ABCDEF..."
export TG_CHAT_ID="123456789"



3) 定时跑（每10分钟）

cron：  */10 * * * * /usr/bin/python3 /path/check_crystal_car_telegram.py >> /tmp/crystal.log 2>&1