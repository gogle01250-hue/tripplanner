from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse
import psycopg2, psycopg2.extras, json, os, re, secrets, string, time, requests

DATABASE_URL = os.environ["DATABASE_URL"]
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI()

# ── DB接続 ────────────────────────────────────────────────
def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    return conn

def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS kv (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS rooms (
                    name       TEXT PRIMARY KEY,
                    password   TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
        conn.commit()

init_db()

# ── KV helpers ───────────────────────────────────────────
def kv_get(key: str, default):
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT value FROM kv WHERE key=%s", (key,))
            row = cur.fetchone()
            return json.loads(row["value"]) if row else default

def kv_set(key: str, value):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO kv(key,value) VALUES(%s,%s)
                ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value
                """,
                (key, json.dumps(value, ensure_ascii=False))
            )
        conn.commit()

# ── パスワード生成 ────────────────────────────────────────
def generate_password() -> str:
    """ランダム12文字英数字（XXXX-XXXX-XXXX形式）"""
    chars = string.ascii_uppercase + string.digits
    groups = [''.join(secrets.choice(chars) for _ in range(4)) for _ in range(3)]
    return '-'.join(groups)

# ── ルームID バリデーション ──────────────────────────────
ROOM_RE = re.compile(r'^[^\s:/\\]{1,32}$')

def room_key(room: str, kind: str) -> str:
    if not ROOM_RE.match(room):
        raise ValueError(f"Invalid room id: {room!r}")
    return f"{room}:{kind}"

# ── 認証API ──────────────────────────────────────────────
@app.post("/api/auth/register")
async def register(request: Request):
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name or not ROOM_RE.match(name):
        return JSONResponse({"ok": False, "error": "IDが無効です（1〜32文字、スペース・コロン・スラッシュ不可）"}, status_code=400)
    password = generate_password()
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO rooms(name, password) VALUES(%s, %s)",
                    (name, password)
                )
            conn.commit()
        return JSONResponse({"ok": True, "password": password})
    except psycopg2.errors.UniqueViolation:
        return JSONResponse({"ok": False, "error": "このIDは既に使用されています"}, status_code=409)

@app.post("/api/auth/login")
async def login(request: Request):
    body = await request.json()
    name = (body.get("name") or "").strip()
    password = (body.get("password") or "").strip()
    if not name or not password:
        return JSONResponse({"ok": False, "error": "IDとパスワードを入力してください"}, status_code=400)
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT password FROM rooms WHERE name=%s", (name,))
            row = cur.fetchone()
    if not row or row["password"] != password:
        return JSONResponse({"ok": False, "error": "IDまたはパスワードが違います"}, status_code=401)
    return JSONResponse({"ok": True})

@app.post("/api/auth/change-password")
async def change_password(request: Request):
    body = await request.json()
    name         = (body.get("name") or "").strip()
    new_password = (body.get("new_password") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "ルームIDが不明です"}, status_code=400)
    if len(new_password) < 5:
        return JSONResponse({"ok": False, "error": "パスワードは5文字以上で入力してください"}, status_code=400)
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT name FROM rooms WHERE name=%s", (name,))
            if not cur.fetchone():
                return JSONResponse({"ok": False, "error": "ルームが見つかりません"}, status_code=404)
            cur.execute("UPDATE rooms SET password=%s WHERE name=%s", (new_password, name))
    return JSONResponse({"ok": True})

# ── 旅行データAPI（ルームスコープ） ────────────────────────
@app.get("/api/{room}/trips")
def get_trips(room: str):
    return JSONResponse(kv_get(room_key(room, "trips"), []))

@app.put("/api/{room}/trips")
async def put_trips(room: str, request: Request):
    body = await request.json()
    kv_set(room_key(room, "trips"), body)
    return {"ok": True}

# ── ジオコーディング Proxy（Nominatim / OpenStreetMap） ───
_geocode_cache = {}
_GEOCODE_CACHE_TTL = 3600  # 1時間

@app.get("/api/geocode")
def geocode(q: str):
    q = q.strip()
    if not q:
        return JSONResponse([])

    now = time.time()
    cached = _geocode_cache.get(q)
    if cached and now - cached[0] < _GEOCODE_CACHE_TTL:
        return JSONResponse(cached[1])

    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": q, "format": "json", "limit": 5},
            headers={"User-Agent": "TripPlanner/1.0 (personal travel planning app)"},
            timeout=5,
        )
        resp.raise_for_status()
        results = [
            {
                "display_name": item["display_name"],
                "lat": float(item["lat"]),
                "lon": float(item["lon"]),
            }
            for item in resp.json()
        ]
    except requests.RequestException:
        return JSONResponse({"ok": False, "error": "地名検索に失敗しました"}, status_code=502)

    _geocode_cache[q] = (now, results)
    return JSONResponse(results)

# ── 静的ファイル配信 ──────────────────────────────────────
@app.get("/")
def index_page():
    return FileResponse(os.path.join(BASE_DIR, "index.html"))

@app.get("/trip")
def trip_page():
    return FileResponse(os.path.join(BASE_DIR, "trip.html"))
