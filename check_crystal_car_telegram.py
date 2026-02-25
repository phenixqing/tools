import json
import os
from datetime import date, timedelta
from calendar import monthrange
import requests

BASE = "https://parking.crystalmountainresort.com"
EVENTS_API = BASE + "/events/"

HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": BASE + "/",
    "User-Agent": "Mozilla/5.0",
}

STATE_FILE = os.getenv("CRYSTAL_STATE_FILE", "crystal_state.json")
ALERT_ON_FIRST_SEEN_AVAILABLE = os.getenv("ALERT_ON_FIRST_SEEN_AVAILABLE", "1") == "1"

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")


def next_saturday(d: date) -> date:
    return d + timedelta(days=(5 - d.weekday()) % 7)

def check_saturday_car():
    target = next_saturday(date.today())
    ms, me = month_range(target)
    events = fetch_events(ms, me)
    ev = find_event(events, target)
    if not ev:
        return target, None, None
    return target, car_available(ev), ev

def next_sunday(d: date) -> date:
    return d + timedelta(days=(6 - d.weekday()) % 7)


def month_range(d: date):
    start = d.replace(day=1)
    last = monthrange(d.year, d.month)[1]
    end = d.replace(day=last)
    return start, end


def fetch_events(start: date, end: date):
    params = {
        "rettype": "collective",
        "start": start.isoformat(),
        "end": (end + timedelta(days=1)).isoformat(),  # end 多给一天更稳
    }
    r = requests.get(EVENTS_API, params=params, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()


def find_event(events, target: date):
    t = target.isoformat()
    for ev in events:
        if (ev.get("start") or "")[:10] == t:
            return ev
    return None


def car_available(ev: dict) -> bool:
    title = (ev.get("title") or "").lower()
    cls = (ev.get("className") or "").lower()
    if "sold out" in title:
        return False
    if "unavailable" in cls:
        return False
    return True


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def tg_send(text: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        raise RuntimeError("请先设置环境变量 TG_BOT_TOKEN 和 TG_CHAT_ID")

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    r = requests.post(url, json=payload, timeout=20)
    r.raise_for_status()


def check_sunday_car():
    target = next_sunday(date.today())
    ms, me = month_range(target)
    events = fetch_events(ms, me)
    ev = find_event(events, target)
    if not ev:
        return target, None, None
    return target, car_available(ev), ev


if __name__ == "__main__":
    target, now_ok, ev = check_saturday_car()
    key = target.isoformat()

    state = load_state()
    prev = state.get(key)  # True/False/"missing"/None

    if now_ok is None:
        state[key] = "missing"
        save_state(state)
        print(f"⚠️ {key} 没找到 event（可能还没放票）")
        raise SystemExit(0)

    # 触发：不可订 -> 可订
    if prev is False and now_ok is True:
        msg = (
            f"✅ Crystal Parking 可订：{key}（本周日）\n"
            f"Car Parking 由不可订变为可订！\n"
            f"{BASE}/\n\n"
            f"title={ev.get('title')}\n"
            f"className={ev.get('className')}"
        )
        tg_send(msg)
        print("✅ 已发 Telegram：状态变化 不可订->可订")

    # 可选：首次看到就是可订，也提醒一次
    if ALERT_ON_FIRST_SEEN_AVAILABLE and prev in (None, "missing") and now_ok is True:
        msg = (
            f"✅ Crystal Parking 可订：{key}（本周日）\n"
            f"当前显示可订（首次发现）。\n"
            f"{BASE}/\n\n"
            f"title={ev.get('title')}\n"
            f"className={ev.get('className')}"
        )
        tg_send(msg)
        print("✅ 已发 Telegram：首次发现可订")

    state[key] = now_ok
    save_state(state)

    print(f"{key} Car={'OK' if now_ok else 'NO'} (prev={prev})")