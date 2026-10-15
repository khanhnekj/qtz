import httpx
import json
import base64
import time
import os
import random
import re
import asyncio
from datetime import datetime

API_BASE = "https://discord.com/api/v9"
HEARTBEAT_INTERVAL = 20
POLL_INTERVAL = 30

SUPPORTED_TASKS = [
    "WATCH_VIDEO",
    "PLAY_ON_DESKTOP",
    "STREAM_ON_DESKTOP",
    "PLAY_ACTIVITY",
    "WATCH_VIDEO_ON_MOBILE"
]

def get_val(d, *keys):
    if not d: return None
    for k in keys:
        if k in d: return d[k]
    return None

def get_task_config(quest):
    cfg = quest.get("config", {})
    return get_val(cfg, "taskConfig", "task_config", "taskConfigV2", "task_config_v2")

def get_quest_name(quest):
    cfg = quest.get("config", {})
    msgs = cfg.get("messages", {})
    name = get_val(msgs, "questName", "quest_name") or get_val(msgs, "gameTitle", "game_title")
    if name: return name.strip()
    return cfg.get("application", {}).get("name") or f"Quest#{quest.get('id')}"

def get_expires_at(quest):
    return get_val(quest.get("config", {}), "expiresAt", "expires_at")

def get_user_status(quest):
    us = get_val(quest, "userStatus", "user_status")
    return us if isinstance(us, dict) else {}

def is_completable(quest):
    expires = get_expires_at(quest)
    if expires:
        try:
            if datetime.fromisoformat(expires.replace("Z", "+00:00")).timestamp() <= time.time():
                return False
        except: pass
    tc = get_task_config(quest)
    if not tc or "tasks" not in tc: return False
    return any(t in tc["tasks"] for t in SUPPORTED_TASKS)

def is_enrolled(quest):
    return bool(get_val(get_user_status(quest), "enrolledAt", "enrolled_at"))

def is_completed(quest):
    return bool(get_val(get_user_status(quest), "completedAt", "completed_at"))

def get_task_type(quest):
    tc = get_task_config(quest)
    if not tc or "tasks" not in tc: return None
    for t in SUPPORTED_TASKS:
        if t in tc["tasks"]: return t
    return None

def get_seconds_needed(quest):
    tc = get_task_config(quest)
    tt = get_task_type(quest)
    return tc["tasks"][tt].get("target", 0) if tc and tt else 0

def get_seconds_done(quest):
    tt = get_task_type(quest)
    if not tt: return 0
    return get_user_status(quest).get("progress", {}).get(tt, {}).get("value", 0)

async def fetch_latest_build_number():
    fallback = 504649
    try:
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get("https://discord.com/app", headers={"User-Agent": ua})
            if res.status_code != 200: return fallback
            text = res.text
            env_match = re.search(r'GLOBAL_ENV\s*=\s*({.*?});', text)
            if env_match:
                try:
                    env = json.loads(env_match.group(1))
                    if "buildNumber" in env: return int(env["buildNumber"])
                except: pass
            scripts = re.findall(r'src="/assets/([^"]+\.js)"', text)
            if not scripts: return fallback
            for asset_path in reversed(scripts[-5:]):
                try:
                    asset_url = f"https://discord.com/assets/{asset_path}"
                    asset_res = await client.get(asset_url, headers={"User-Agent": ua})
                    m = re.search(r'buildNumber["\s:]+["\s]*(\d{5,7})', asset_res.text)
                    if m: return int(m.group(1))
                except: pass
            return fallback
    except:
        return fallback

def make_super_properties(build_number):
    obj = {
        "os": "Windows", "browser": "Discord Client", "release_channel": "stable",
        "client_version": "1.0.9175", "os_version": "10.0.26100", "os_arch": "x64",
        "app_arch": "x64", "system_locale": "en-US",
        "browser_user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) discord/1.0.9175 Chrome/128.0.6613.186 Electron/32.2.7 Safari/537.36",
        "browser_version": "32.2.7", "client_build_number": build_number,
        "native_build_number": 59498, "client_event_source": None
    }
    return base64.b64encode(json.dumps(obj).encode()).decode()

class DiscordApi:
    def __init__(self, token, build_number):
        self.token = token
        self.username = "Unknown"
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) discord/1.0.9175 Chrome/128.0.6613.186 Electron/32.2.7 Safari/537.36"
        sp = make_super_properties(build_number)
        self.headers = {
            "Authorization": token, "Content-Type": "application/json",
            "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9", "User-Agent": ua,
            "X-Super-Properties": sp, "X-Discord-Locale": "en-US",
            "X-Discord-Timezone": "Asia/Ho_Chi_Minh", "Origin": "https://discord.com",
            "Referer": "https://discord.com/channels/@me"
        }
        self.client = httpx.AsyncClient(headers=self.headers, timeout=20, follow_redirects=True)

    async def get(self, path):
        return await self.client.get(f"{API_BASE}{path}")

    async def post(self, path, payload=None):
        return await self.client.post(f"{API_BASE}{path}", json=payload)

    async def validate_token(self):
        try:
            res = await self.get("/users/@me")
            if res.status_code == 200:
                user = res.json()
                self.username = user.get('username', 'Unknown')
                return True
            return False
        except:
            return False

    async def close(self):
        await self.client.aclose()

class QuestAutocompleter:
    def __init__(self, api, manager):
        self.api = api
        self.manager = manager
        self.completed_ids = set()
        self.completed_count = 0
        self._running = True

    async def fetch_quests(self):
        try:
            res = await self.api.get("/quests/@me")
            if res.status_code == 200:
                data = res.json()
                return data.get("quests", []) if isinstance(data, dict) else data
            elif res.status_code == 429:
                await asyncio.sleep(res.json().get("retry_after", 5))
                return await self.fetch_quests()
            return []
        except:
            return []

    async def enroll_quest(self, quest):
        qid = quest["id"]
        name = get_quest_name(quest)
        res = await self.api.post(f"/quests/{qid}/enroll", {
            "location": 11, "is_targeted": False,
            "traffic_metadata_raw": quest.get("traffic_metadata_raw"),
            "traffic_metadata_sealed": quest.get("traffic_metadata_sealed")
        })
        if res.status_code in [200, 201, 204]:
            self.manager.add_log(f"Nhận quest: {name}", "ok", self.api.username)
            return True
        return False

    async def complete_video(self, quest):
        qid, name = quest["id"], get_quest_name(quest)
        sec_needed = get_seconds_needed(quest)
        sec_done = get_seconds_done(quest)

        while sec_done < sec_needed and self._running:
            timestamp = sec_done + 7
            try:
                res = await self.api.post(f"/quests/{qid}/video-progress",
                                          {"timestamp": min(sec_needed, timestamp + random.random())})
                if res.status_code == 200:
                    if res.json().get("completed_at"): break
                    sec_done = min(sec_needed, timestamp)
                elif res.status_code == 429:
                    await asyncio.sleep(res.json().get("retry_after", 5) + 1)
                    continue
            except: pass

            self.manager.update_task(self.api.username, qid, name, sec_done, sec_needed)
            await asyncio.sleep(7)

        await self.api.post(f"/quests/{qid}/video-progress", {"timestamp": sec_needed})
        self.completed_count += 1
        self.manager.remove_task(self.api.username, qid)
        self.manager.add_log(f"Xong Video: {name}", "ok", self.api.username)
        self.manager.add_completed_quest(self.api.username, name) # Ghi nhận đã xong

    async def complete_heartbeat(self, quest, is_activity=False):
        qid, name = quest["id"], get_quest_name(quest)
        tt = get_task_type(quest)
        sec_needed = get_seconds_needed(quest)
        sec_done = get_seconds_done(quest)
        pid = random.randint(1000, 30000)

        while sec_done < sec_needed and self._running:
            try:
                res = await self.api.post(f"/quests/{qid}/heartbeat", {
                    "stream_key": f"call:0:{pid}" if not is_activity else "call:0:1",
                    "terminal": False
                })
                if res.status_code == 200:
                    body = res.json()
                    prog = body.get("progress", {})
                    if tt in prog: sec_done = prog[tt].get("value", sec_done)
                    if body.get("completed_at") or sec_done >= sec_needed: break
                elif res.status_code == 429:
                    await asyncio.sleep(res.json().get("retry_after", 10) + 1)
                    continue
            except: pass

            self.manager.update_task(self.api.username, qid, name, sec_done, sec_needed)
            await asyncio.sleep(HEARTBEAT_INTERVAL)

        await self.api.post(f"/quests/{qid}/heartbeat", {
            "stream_key": f"call:0:{pid}" if not is_activity else "call:0:1",
            "terminal": True
        })
        self.completed_count += 1
        self.manager.remove_task(self.api.username, qid)
        self.manager.add_log(f"Xong Quest: {name}", "ok", self.api.username)
        self.manager.add_completed_quest(self.api.username, name) # Ghi nhận đã xong

    async def process_quest(self, quest):
        qid, tt = quest["id"], get_task_type(quest)
        if not tt or qid in self.completed_ids: return
        self.completed_ids.add(qid)
        if tt in ["WATCH_VIDEO", "WATCH_VIDEO_ON_MOBILE"]:
            await self.complete_video(quest)
        elif tt in ["PLAY_ON_DESKTOP", "STREAM_ON_DESKTOP"]:
            await self.complete_heartbeat(quest)
        elif tt == "PLAY_ACTIVITY":
            await self.complete_heartbeat(quest, True)

    async def run_forever(self):
        self.manager.add_log(f"Bot đã khởi động...", "info", self.api.username)
        while self._running:
            try:
                quests = await self.fetch_quests()
                if quests:
                    actionable = [q for q in quests if is_completable(q) and not is_completed(q)]
                    for q in actionable:
                        if not is_enrolled(q):
                            await self.enroll_quest(q)
                    
                    to_process = [q for q in actionable if is_enrolled(q) and q["id"] not in self.completed_ids]
                    for q in to_process:
                        asyncio.create_task(self.process_quest(q))
            except Exception as e:
                self.manager.add_log(f"Lỗi: {e}", "error", self.api.username)
            await asyncio.sleep(POLL_INTERVAL)

    def stop(self):
        self._running = False

class QuestBotManager:
    def __init__(self):
        self.accounts = {}
        self.logs = []
        self.tasks_info = {}
        self.completed_history = [] # Danh sách nhiệm vụ đã hoàn thành
        self.loop = None
        self.build_num = None
        self.start_time = time.time()

    def add_log(self, msg, level="info", username=None):
        ts = datetime.now().strftime("%H:%M:%S")
        self.logs.append({"time": ts, "level": level, "msg": msg, "username": username})
        if len(self.logs) > 500:
            self.logs = self.logs[-500:]

    def update_task(self, username, qid, name, current, total):
        key = f"{username}:{qid}"
        self.tasks_info[key] = {
            "username": username, "name": name,
            "current": int(current), "total": int(total),
            "percent": round(min(100, (current / total) * 100), 1) if total > 0 else 0
        }

    def remove_task(self, username, qid):
        key = f"{username}:{qid}"
        self.tasks_info.pop(key, None)

    def add_completed_quest(self, username, name):
        ts = datetime.now().strftime("%H:%M:%S")
        self.completed_history.append({"time": ts, "username": username, "name": name})
        if len(self.completed_history) > 50: # Giữ lại 50 quest gần nhất
            self.completed_history = self.completed_history[-50:]

    async def start(self, config):
        self.loop = asyncio.get_event_loop()
        self.build_num = await fetch_latest_build_number()
        self.add_log(f"Hệ thống sẵn sàng. Build: {self.build_num}", "ok")

        tokens = config.get("tokens", [])
        env_tokens = os.environ.get("DISCORD_TOKENS", "")
        if env_tokens:
            tokens = [t.strip() for t in env_tokens.split(",") if t.strip()]

        for token in tokens:
            await self.add_token_async(token)

        while True:
            await asyncio.sleep(1)

    async def add_token_async(self, token):
        token = token.strip()
        if not token or token in self.accounts:
            return False
        api = DiscordApi(token, self.build_num or await fetch_latest_build_number())
        if await api.validate_token():
            bot = QuestAutocompleter(api, self)
            self.accounts[token] = {
                "api": api, "bot": bot,
                "username": api.username, "status": "online"
            }
            self.add_log(f"Thêm token thành công", "ok", api.username)
            asyncio.create_task(bot.run_forever())
            return True
        else:
            self.add_log(f"Token không hợp lệ", "error")
            await api.close()
            return False

    def add_token(self, token):
        if self.loop:
            future = asyncio.run_coroutine_threadsafe(self.add_token_async(token), self.loop)
            return future.result(timeout=30)
        return False

    def remove_token(self, token):
        if token in self.accounts:
            async def _remove():
                self.accounts[token]["bot"].stop()
                await self.accounts[token]["api"].close()
                username = self.accounts[token]["username"]
                del self.accounts[token]
                to_remove = [k for k in self.tasks_info if k.startswith(f"{username}:")]
                for k in to_remove:
                    self.tasks_info.pop(k, None)
                self.add_log(f"Đã xóa token khỏi hệ thống", "info", username)
            if self.loop:
                future = asyncio.run_coroutine_threadsafe(_remove(), self.loop)
                future.result(timeout=10)

    def get_status(self):
        accounts = []
        for token, info in self.accounts.items():
            accounts.append({
                "username": info["username"],
                "masked_token": token[:10] + "..." + token[-5:],
                "status": info["status"],
                "completed": info["bot"].completed_count,
                "active_tasks": sum(1 for k in self.tasks_info if k.startswith(f"{info['username']}:"))
            })
        return {
            "accounts": accounts,
            "total_completed": sum(a["completed"] for a in accounts),
            "total_active": len(self.tasks_info),
            "uptime": int(time.time() - self.start_time),
            "tasks": list(self.tasks_info.values()),
            "completed_history": self.completed_history
        }

    def get_logs(self):
        return self.logs[-150:]
