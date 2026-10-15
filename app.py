import os
import json
import asyncio
import threading
import secrets
from functools import wraps
from flask import (
    Flask, render_template, request, jsonify,
    session, redirect, url_for, flash
)
from werkzeug.security import generate_password_hash, check_password_hash
from bot import QuestBotManager

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

CONFIG_FILE = "config.json"
USERS_FILE = "users.json"
MUSIC_FILE = "music.json"

# ─── Config helpers ───────────────────────────────────────────
def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"tokens": []}

def save_config(config):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

def load_users():
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    # Default admin: admin / admin123  (đổi ngay sau khi deploy!)
    default = {
        "admin": {
            "password": generate_password_hash("admin123"),
            "role": "admin",
            "display_name": "Admin"
        }
    }
    save_users(default)
    return default

def save_users(users):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2)

def load_music():
    if os.path.exists(MUSIC_FILE):
        with open(MUSIC_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"tracks": []}

def save_music(data):
    with open(MUSIC_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

# ─── Auth helpers ─────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized – vui lòng đăng nhập"}), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect(url_for("login_page"))
        users = load_users()
        user = users.get(session["user"])
        if not user or user.get("role") != "admin":
            if request.path.startswith("/api/"):
                return jsonify({"error": "Chỉ Admin mới được thực hiện thao tác này"}), 403
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return decorated

def current_user():
    if "user" not in session:
        return None
    users = load_users()
    u = users.get(session["user"])
    if not u:
        return None
    return {
        "username": session["user"],
        "display_name": u.get("display_name", session["user"]),
        "role": u.get("role", "user")
    }

# ─── Bot ──────────────────────────────────────────────────────
bot_manager = QuestBotManager()

def start_bot_loop():
    config = load_config()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(bot_manager.start(config))

bot_thread = threading.Thread(target=start_bot_loop, daemon=True)
bot_thread.start()

# ─── Pages ────────────────────────────────────────────────────
@app.route("/")
@login_required
def index():
    return render_template("index.html", user=current_user())

@app.route("/login", methods=["GET", "POST"])
def login_page():
    if "user" in session:
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        users = load_users()
        user = users.get(username)
        if user and check_password_hash(user["password"], password):
            session["user"] = username
            session.permanent = True
            return redirect(url_for("index"))
        error = "Sai tên đăng nhập hoặc mật khẩu"
    return render_template("login.html", error=error, mode="login")

@app.route("/register", methods=["GET", "POST"])
def register_page():
    if "user" in session:
        return redirect(url_for("index"))
    error = None
    success = None
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        display = request.form.get("display_name", "").strip() or username

        if not username or not password:
            error = "Vui lòng điền đầy đủ thông tin"
        elif len(username) < 3:
            error = "Tên đăng nhập phải từ 3 ký tự trở lên"
        elif len(password) < 6:
            error = "Mật khẩu phải từ 6 ký tự trở lên"
        elif password != confirm:
            error = "Mật khẩu xác nhận không khớp"
        elif not username.isalnum() and "_" not in username:
            error = "Tên đăng nhập chỉ được chứa chữ, số và gạch dưới"
        else:
            users = load_users()
            if username in users:
                error = "Tên đăng nhập đã tồn tại"
            else:
                users[username] = {
                    "password": generate_password_hash(password),
                    "role": "user",
                    "display_name": display
                }
                save_users(users)
                success = "Đăng ký thành công! Hãy đăng nhập."
    return render_template("login.html", error=error, success=success, mode="register")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))

# ─── Protected APIs ───────────────────────────────────────────
@app.route("/api/me")
@login_required
def api_me():
    return jsonify(current_user())

@app.route("/api/tokens", methods=["GET"])
@login_required
def get_tokens():
    config = load_config()
    tokens = []
    for t in config.get("tokens", []):
        masked = t[:10] + "..." + t[-5:] if len(t) > 15 else t[:5] + "***"
        username = bot_manager.accounts.get(t, {}).get("username", "Unknown")
        tokens.append({"masked": masked, "full": t, "username": username})
    return jsonify(tokens)

@app.route("/api/tokens", methods=["POST"])
@login_required
def add_token():
    # Chỉ admin mới được thêm token
    user = current_user()
    if user["role"] != "admin":
        return jsonify({"error": "Chỉ Admin mới được thêm token!"}), 403

    token = request.json.get("token", "").strip()
    if not token:
        return jsonify({"error": "Token trống!"}), 400

    config = load_config()
    if token in config["tokens"]:
        return jsonify({"error": "Token đã tồn tại!"}), 400

    result = bot_manager.add_token(token)
    if result:
        config["tokens"].append(token)
        save_config(config)
        return jsonify({"success": True, "message": "Thêm thành công!"})
    else:
        return jsonify({"error": "Token không hợp lệ hoặc đã tồn tại!"}), 400

@app.route("/api/tokens", methods=["DELETE"])
@login_required
def remove_token():
    user = current_user()
    if user["role"] != "admin":
        return jsonify({"error": "Chỉ Admin mới được xóa token!"}), 403

    token = request.json.get("token", "").strip()
    config = load_config()
    if token in config["tokens"]:
        config["tokens"].remove(token)
        save_config(config)
        bot_manager.remove_token(token)
        return jsonify({"success": True, "message": "Đã xóa token"})
    return jsonify({"error": "Không tìm thấy token"}), 404

@app.route("/api/status")
@login_required
def status():
    return jsonify(bot_manager.get_status())

@app.route("/api/logs")
@login_required
def logs():
    return jsonify(bot_manager.get_logs())

# ─── Music APIs ───────────────────────────────────────────────
@app.route("/api/music", methods=["GET"])
@login_required
def get_music():
    return jsonify(load_music())

@app.route("/api/music", methods=["POST"])
@admin_required
def add_music():
    data = request.json or {}
    url = (data.get("url") or "").strip()
    title = (data.get("title") or "").strip() or "SoundCloud Track"

    if not url:
        return jsonify({"error": "Link trống!"}), 400
    if "soundcloud.com" not in url.lower():
        return jsonify({"error": "Chỉ chấp nhận link SoundCloud!"}), 400

    music = load_music()
    # Tránh trùng
    if any(t["url"] == url for t in music["tracks"]):
        return jsonify({"error": "Link này đã có trong playlist!"}), 400

    track = {
        "id": secrets.token_hex(6),
        "url": url,
        "title": title,
        "added_by": session["user"]
    }
    music["tracks"].append(track)
    save_music(music)
    return jsonify({"success": True, "track": track})

@app.route("/api/music/<track_id>", methods=["DELETE"])
@admin_required
def delete_music(track_id):
    music = load_music()
    before = len(music["tracks"])
    music["tracks"] = [t for t in music["tracks"] if t["id"] != track_id]
    if len(music["tracks"]) == before:
        return jsonify({"error": "Không tìm thấy bài hát"}), 404
    save_music(music)
    return jsonify({"success": True})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
