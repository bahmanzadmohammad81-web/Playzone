import os, json, hmac, hashlib, sqlite3, secrets, time, html, urllib.parse
from collections import defaultdict, deque
import requests
from flask import Flask, request, jsonify, g, send_from_directory
from werkzeug.exceptions import HTTPException

BASE = os.path.dirname(os.path.abspath(__file__))
FRONT = os.path.join(BASE, "..", "frontend")
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE, "data", "playzone.db"))
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
APP_URL = os.environ.get("APP_URL", "")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

app = Flask(__name__, static_folder=None)

# lo/hi: valid value range. best: "max" or "min" (lower is better, e.g. reaction time)
GAMES = {
    "snake": {"lo": 0, "hi": 5000, "best": "max"},
    "reaction": {"lo": 80, "hi": 2000, "best": "min"},
    "math": {"lo": 0, "hi": 200, "best": "max"},
}
PERIODS = {"all": None, "week": 7, "month": 30}
MIN_PLAY_SECONDS = 3
SESSION_TTL = 600
LEVEL_XP = 500


class AppError(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, first_name TEXT, username TEXT, created REAL, last_seen REAL);
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER, game TEXT, created REAL, used INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS scores(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, game TEXT,
  value INTEGER, points INTEGER, created REAL);
CREATE INDEX IF NOT EXISTS idx_scores_game ON scores(game, created);
CREATE INDEX IF NOT EXISTS idx_scores_user ON scores(user_id);
"""


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def _close(_):
    d = g.pop("db", None)
    if d is not None:
        d.close()


def init_db():
    c = sqlite3.connect(DB_PATH)
    c.executescript(SCHEMA)
    c.commit()
    c.close()


init_db()


# ---------- Telegram auth ----------
def verify_init_data(raw):
    if not raw or not BOT_TOKEN:
        raise AppError("این برنامه باید از داخل تلگرام باز شود.", 401)
    data = dict(urllib.parse.parse_qsl(raw, keep_blank_values=True))
    received = data.pop("hash", "")
    check = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise AppError("اعتبارسنجی تلگرام ناموفق بود.", 401)
    if time.time() - int(data.get("auth_date", 0)) > 86400:
        raise AppError("نشست تلگرام منقضی شده. برنامه را دوباره باز کن.", 401)
    user = json.loads(data.get("user", "{}"))
    if "id" not in user:
        raise AppError("اطلاعات کاربر ناقص است.", 401)
    return user


def upsert_user(u):
    uid, now = int(u["id"]), time.time()
    db().execute("""INSERT INTO users(id,first_name,username,created,last_seen) VALUES(?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET first_name=excluded.first_name,
        username=excluded.username, last_seen=excluded.last_seen""",
                 (uid, u.get("first_name", ""), u.get("username") or "", now, now))
    db().commit()
    return uid


_hits = defaultdict(deque)


def current_user():
    uid = upsert_user(verify_init_data(request.headers.get("X-Init-Data", "")))
    q, now = _hits[uid], time.time()
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= 60:
        raise AppError("درخواست‌ها زیاد است. کمی صبر کن.", 429)
    q.append(now)
    return uid


# ---------- data helpers ----------
def points_for(game, value):
    return {"snake": value, "math": value * 10, "reaction": max(0, 1000 - value)}[game]


def profile_of(uid):
    u = db().execute("SELECT first_name, username FROM users WHERE id=?", (uid,)).fetchone()
    per = {r["game"]: {"plays": r["plays"], "points": r["pts"]} for r in db().execute(
        "SELECT game, COUNT(*) AS plays, SUM(points) AS pts FROM scores WHERE user_id=? GROUP BY game", (uid,))}
    total = sum(v["points"] for v in per.values())
    history = [dict(r) for r in db().execute(
        "SELECT game, value, points, created FROM scores WHERE user_id=? ORDER BY id DESC LIMIT 20", (uid,))]
    return {"name": (u["first_name"] or u["username"] or "بازیکن") if u else "بازیکن",
            "username": u["username"] if u else "", "points": total,
            "level": 1 + total // LEVEL_XP, "progress": total % LEVEL_XP, "xp_needed": LEVEL_XP,
            "plays": sum(v["plays"] for v in per.values()), "per_game": per, "history": history}


def board(game, period):
    since = 0 if PERIODS[period] is None else time.time() - PERIODS[period] * 86400
    name = "COALESCE(NULLIF(u.first_name,''), u.username, 'بازیکن') AS name"
    if game == "overall":
        sql = (f"SELECT u.id, {name}, SUM(s.points) AS value FROM scores s JOIN users u ON u.id=s.user_id "
               "WHERE s.created>=? GROUP BY s.user_id ORDER BY value DESC")
        args = (since,)
    else:
        agg, order = ("MIN", "ASC") if GAMES[game]["best"] == "min" else ("MAX", "DESC")
        sql = (f"SELECT u.id, {name}, {agg}(s.value) AS value FROM scores s JOIN users u ON u.id=s.user_id "
               f"WHERE s.game=? AND s.created>=? GROUP BY s.user_id ORDER BY value {order}")
        args = (game, since)
    return [dict(r) for r in db().execute(sql, args)]


# ---------- routes ----------
@app.errorhandler(AppError)
def _app_error(e):
    return jsonify(error=str(e)), e.status


@app.errorhandler(Exception)
def _any_error(e):
    if isinstance(e, HTTPException):
        return jsonify(error=e.description), e.code
    return jsonify(error="خطای داخلی سرور."), 500


@app.get("/")
def index():
    return send_from_directory(FRONT, "index.html")


@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/api/home")
def home():
    uid = current_user()
    popular = [dict(r) for r in db().execute(
        "SELECT game, COUNT(*) AS plays FROM scores GROUP BY game ORDER BY plays DESC")]
    recent = [dict(r) for r in db().execute("""SELECT s.game, s.value, s.created,
        COALESCE(NULLIF(u.first_name,''), u.username, 'بازیکن') AS name
        FROM scores s JOIN users u ON u.id=s.user_id ORDER BY s.id DESC LIMIT 8""")]
    return jsonify(me=profile_of(uid), popular=popular, recent=recent)


@app.get("/api/profile")
def profile():
    return jsonify(profile_of(current_user()))


@app.get("/api/ranking")
def ranking():
    uid = current_user()
    game = request.args.get("game", "overall")
    period = request.args.get("period", "all")
    if (game != "overall" and game not in GAMES) or period not in PERIODS:
        raise AppError("پارامتر نامعتبر است.", 400)
    rows = board(game, period)
    me = next(({"rank": i + 1, "value": r["value"]} for i, r in enumerate(rows) if r["id"] == uid), None)
    top = [{"rank": i + 1, "name": r["name"], "value": r["value"], "is_me": r["id"] == uid}
           for i, r in enumerate(rows[:50])]
    return jsonify(top=top, me=me)


@app.post("/api/session")
def new_session():
    uid = current_user()
    game = (request.get_json(silent=True) or {}).get("game")
    if game not in GAMES:
        raise AppError("بازی نامعتبر است.", 400)
    now = time.time()
    db().execute("DELETE FROM sessions WHERE created<?", (now - SESSION_TTL,))
    token = secrets.token_urlsafe(24)
    db().execute("INSERT INTO sessions(token,user_id,game,created) VALUES(?,?,?,?)", (token, uid, game, now))
    db().commit()
    return jsonify(token=token)


@app.post("/api/score")
def submit_score():
    uid = current_user()
    b = request.get_json(silent=True) or {}
    token = str(b.get("token", ""))
    try:
        value = int(b.get("value"))
    except (TypeError, ValueError):
        raise AppError("مقدار امتیاز نامعتبر است.", 400)

    # Consume the session first: one token = one attempt, so retries cannot cheat.
    cur = db().execute("UPDATE sessions SET used=1 WHERE token=? AND user_id=? AND used=0", (token, uid))
    db().commit()
    if cur.rowcount != 1:
        raise AppError("نشست بازی معتبر نیست یا قبلاً ثبت شده است.", 403)
    s = db().execute("SELECT game, created FROM sessions WHERE token=?", (token,)).fetchone()
    game, elapsed = s["game"], time.time() - s["created"]
    if elapsed > SESSION_TTL:
        raise AppError("نشست بازی منقضی شده است.", 422)
    min_time = 6 if game == "reaction" else MIN_PLAY_SECONDS
    if elapsed < min_time:
        raise AppError("زمان بازی خیلی کوتاه است.", 422)
    if game == "math" and elapsed > 70:
        raise AppError("زمان بازی تمام شده است.", 422)
    cap = {"snake": elapsed * 7 + 5, "math": elapsed + 1, "reaction": GAMES["reaction"]["hi"]}[game]
    rule = GAMES[game]
    if not (rule["lo"] <= value <= min(rule["hi"], cap)):
        raise AppError("امتیاز خارج از محدوده ممکن است.", 422)

    pts = points_for(game, value)
    db().execute("INSERT INTO scores(user_id,game,value,points,created) VALUES(?,?,?,?,?)",
                 (uid, game, value, pts, time.time()))
    db().commit()
    return jsonify(value=value, points=pts)


# ---------- Telegram bot webhook ----------
def tg_send(chat_id, text, kb=None):
    if not BOT_TOKEN:
        return
    body = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if kb:
        body["reply_markup"] = kb
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=body, timeout=10)


@app.post("/webhook/<secret>")
def webhook(secret):
    if not WEBHOOK_SECRET or not hmac.compare_digest(secret, WEBHOOK_SECRET):
        return "forbidden", 403
    msg = (request.get_json(silent=True) or {}).get("message") or {}
    frm, chat = msg.get("from") or {}, (msg.get("chat") or {}).get("id")
    if not chat or "id" not in frm:
        return "ok"
    uid = upsert_user(frm)
    cmd = (msg.get("text") or "").strip().split(" ")[0].split("@")[0].lower()
    if cmd == "/start":
        kb = {"inline_keyboard": [[{"text": "🎮 Play Game", "web_app": {"url": APP_URL}}]]} if APP_URL else None
        tg_send(chat, "🎮 <b>PlayZone</b>\nبه PlayZone خوش آمدی! روی دکمه زیر بزن و بازی کن.", kb)
    elif cmd == "/help":
        tg_send(chat, "/start - شروع\n/help - راهنما\n/profile - پروفایل من\n/top - ۱۰ بازیکن برتر")
    elif cmd == "/profile":
        p = profile_of(uid)
        tg_send(chat, f"👤 {html.escape(p['name'])}\n🏅 سطح {p['level']} · ⭐ {p['points']} امتیاز\n🎮 {p['plays']} بازی")
    elif cmd == "/top":
        rows = board("overall", "all")[:10]
        lines = [f"{i + 1}. {html.escape(r['name'])} — {r['value']}" for i, r in enumerate(rows)]
        tg_send(chat, "🏆 <b>برترین بازیکنان</b>\n" + "\n".join(lines) if lines else "هنوز امتیازی ثبت نشده.")
    return "ok"


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
