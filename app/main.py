from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse
import psycopg2, psycopg2.extras, json, os, re, time, requests

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
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("ALTER TABLE rooms DROP COLUMN IF EXISTS password")
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

# ── ルームID バリデーション ──────────────────────────────
ROOM_RE = re.compile(r'^[^\s:/\\]{1,32}$')

def room_key(room: str, kind: str) -> str:
    if not ROOM_RE.match(room):
        raise ValueError(f"Invalid room id: {room!r}")
    return f"{room}:{kind}"

# ── 認証API（パスワードなし・ユーザー名のみ） ──────────────
@app.post("/api/auth/enter")
async def enter_room(request: Request):
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name or not ROOM_RE.match(name):
        return JSONResponse({"ok": False, "error": "IDが無効です（1〜32文字、スペース・コロン・スラッシュ不可）"}, status_code=400)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO rooms(name) VALUES(%s) ON CONFLICT (name) DO NOTHING",
                (name,)
            )
        conn.commit()
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

# ── ルート検索 Proxy（OSRM / 実道路ルート・車専用） ────────
_route_cache = {}
_ROUTE_CACHE_TTL = 86400  # 24時間（同じ2地点間のルートは基本変わらないので長め）

@app.get("/api/route")
def route(from_lat: float, from_lng: float, to_lat: float, to_lng: float):
    cache_key = (round(from_lat, 5), round(from_lng, 5), round(to_lat, 5), round(to_lng, 5))
    now = time.time()
    cached = _route_cache.get(cache_key)
    if cached and now - cached[0] < _ROUTE_CACHE_TTL:
        return JSONResponse(cached[1])

    try:
        resp = requests.get(
            f"https://router.project-osrm.org/route/v1/driving/{from_lng},{from_lat};{to_lng},{to_lat}",
            params={"overview": "full", "geometries": "geojson"},
            headers={"User-Agent": "TripPlanner/1.0 (personal travel planning app)"},
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != "Ok" or not data.get("routes"):
            return JSONResponse({"ok": False, "error": "ルートが見つかりませんでした"}, status_code=404)
        r = data["routes"][0]
        result = {
            "ok": True,
            "distance_m": r["distance"],
            "duration_s": r["duration"],
            "geometry": [[lat, lng] for lng, lat in r["geometry"]["coordinates"]],
        }
    except requests.RequestException:
        return JSONResponse({"ok": False, "error": "ルート検索に失敗しました"}, status_code=502)

    _route_cache[cache_key] = (now, result)
    return JSONResponse(result)

# ── 静的ファイル配信 ──────────────────────────────────────
@app.get("/")
def index_page():
    return FileResponse(os.path.join(BASE_DIR, "index.html"))

@app.get("/trip")
def trip_page():
    return FileResponse(os.path.join(BASE_DIR, "trip.html"))
