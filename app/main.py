from fastapi import FastAPI, Request, UploadFile, File
from fastapi.responses import JSONResponse, FileResponse
import psycopg2, psycopg2.extras, json, os, re, time, requests, base64, io
import openpyxl

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

def _nominatim_search(q: str, limit: int = 5):
    """地名文字列から候補地点のリスト（display_name/lat/lon）を返す。失敗時はNone。"""
    q = q.strip()
    if not q:
        return []

    cache_key = (q, limit)
    now = time.time()
    cached = _geocode_cache.get(cache_key)
    if cached and now - cached[0] < _GEOCODE_CACHE_TTL:
        return cached[1]

    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": q, "format": "json", "limit": limit},
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
        return None

    _geocode_cache[cache_key] = (now, results)
    return results

@app.get("/api/geocode")
def geocode(q: str):
    results = _nominatim_search(q, limit=5)
    if results is None:
        return JSONResponse({"ok": False, "error": "地名検索に失敗しました"}, status_code=502)
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

# ── AI連携（Gemini） ──────────────────────────────────────
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-3-flash-preview"
MAX_IMPORT_SIZE = 10 * 1024 * 1024  # 10MB

TRIP_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "title": {"type": "STRING"},
        "days": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "date": {"type": "STRING", "description": "YYYY-MM-DD形式"},
                    "items": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "time": {"type": "STRING", "description": "HH:MM形式。不明なら空文字"},
                                "title": {"type": "STRING"},
                                "place": {"type": "STRING", "description": "地図検索エンジン（OpenStreetMap）でヒットする、シンプルで一般に知られた施設名・地名のみ。番地までの詳細住所や説明的な言葉は含めない（良い例: 「清水寺」「伏見稲荷大社」「京都駅」。悪い例: 「京都府京都市東山区清水1-294」「清水寺 参拝」）"},
                                "notes": {"type": "STRING"},
                                "transport_car": {"type": "BOOLEAN"},
                                "transport_train": {"type": "BOOLEAN"},
                                "transport_plane": {"type": "BOOLEAN"},
                                "transport_other_label": {"type": "STRING", "description": "車・電車・飛行機以外の移動手段。なければ空文字"},
                                "cost": {"type": "NUMBER", "description": "金額。不明・該当なしなら0"},
                                "cost_currency": {"type": "STRING", "description": "通貨コード。不明ならJPY"},
                            },
                            "required": ["title"],
                        },
                    },
                },
                "required": ["date", "items"],
            },
        },
    },
    "required": ["title", "days"],
}

def _call_gemini(parts, response_schema):
    if not GEMINI_API_KEY:
        return None, "GEMINI_API_KEYが設定されていません"
    try:
        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
            params={"key": GEMINI_API_KEY},
            json={
                "contents": [{"parts": parts}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "responseSchema": response_schema,
                },
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text), None
    except requests.RequestException:
        return None, "AIとの通信に失敗しました。しばらく待って再度お試しください"
    except (KeyError, IndexError, json.JSONDecodeError):
        return None, "AIの応答を解析できませんでした"

def _build_days_from_ai(raw_days):
    days = []
    for raw_day in raw_days or []:
        items = []
        for raw_item in raw_day.get("items") or []:
            place = (raw_item.get("place") or "").strip()
            lat = lng = None
            if place:
                results = _nominatim_search(place, limit=1)
                if results:
                    lat = results[0]["lat"]
                    lng = results[0]["lon"]
                time.sleep(0.5)  # Nominatimの利用ポリシー（1req/秒）に配慮
            other_label = (raw_item.get("transport_other_label") or "").strip()
            items.append({
                "time": raw_item.get("time") or "",
                "title": raw_item.get("title") or "",
                "place": place,
                "lat": lat,
                "lng": lng,
                "notes": raw_item.get("notes") or "",
                "transport": {
                    "car": bool(raw_item.get("transport_car")),
                    "train": bool(raw_item.get("transport_train")),
                    "plane": bool(raw_item.get("transport_plane")),
                    "other": bool(other_label),
                    "otherLabel": other_label,
                },
                "cost": raw_item.get("cost") or None,
                "costCurrency": raw_item.get("cost_currency") or "JPY",
            })
        days.append({"date": raw_day.get("date") or "", "items": items})
    return days

@app.post("/api/ai/suggest")
async def ai_suggest(request: Request):
    body = await request.json()
    prompt = (body.get("prompt") or "").strip()
    start_date = (body.get("start_date") or "").strip()
    end_date = (body.get("end_date") or "").strip()
    if not prompt:
        return JSONResponse({"ok": False, "error": "行き先ややりたいことを入力してください"}, status_code=400)

    instruction = (
        "あなたは旅行プランナーです。以下の希望をもとに、日本語で具体的な旅行のしおりをJSON形式で作成してください。"
        "各予定のplaceには、番地までの詳細住所ではなく、地図検索でヒットするシンプルな施設名・地名のみを入れてください（例:「清水寺」）。"
        f"\n\n希望: {prompt}"
    )
    if start_date:
        instruction += f"\n開始日: {start_date}"
    if end_date:
        instruction += f"\n終了日: {end_date}"

    data, error = _call_gemini([{"text": instruction}], TRIP_SCHEMA)
    if error:
        return JSONResponse({"ok": False, "error": error}, status_code=502)

    days = _build_days_from_ai(data.get("days"))
    return JSONResponse({"ok": True, "title": data.get("title") or "AI旅行プラン", "days": days})

@app.post("/api/ai/import")
async def ai_import(file: UploadFile = File(...)):
    content = await file.read()
    if len(content) > MAX_IMPORT_SIZE:
        return JSONResponse({"ok": False, "error": "ファイルサイズが大きすぎます（10MBまで）"}, status_code=400)

    filename = (file.filename or "").lower()
    instruction = (
        "この旅のしおりの内容を、日本語で旅行データとしてJSON形式で抽出してください。"
        "各予定のplaceには、番地までの詳細住所ではなく、地図検索でヒットするシンプルな施設名・地名のみを入れてください（例:「清水寺」）。"
        "移動を表す項目は、実際に使われている移動手段（車・電車・飛行機・その他）をtransport_*で表してください。"
    )

    if filename.endswith(".xlsx"):
        try:
            wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
        except Exception:
            return JSONResponse({"ok": False, "error": "Excelファイルを読み込めませんでした"}, status_code=400)
        lines = []
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) for c in row if c is not None and str(c).strip()]
                if cells:
                    lines.append(" / ".join(cells))
        parts = [{"text": instruction + "\n\n--- しおりの内容 ---\n" + "\n".join(lines)}]
    elif filename.endswith((".png", ".jpg", ".jpeg", ".pdf")):
        if filename.endswith(".pdf"):
            mime = "application/pdf"
        elif filename.endswith(".png"):
            mime = "image/png"
        else:
            mime = "image/jpeg"
        parts = [
            {"text": instruction},
            {"inline_data": {"mime_type": mime, "data": base64.b64encode(content).decode("ascii")}},
        ]
    else:
        return JSONResponse({"ok": False, "error": "対応していないファイル形式です（xlsx/png/jpg/pdfのみ）"}, status_code=400)

    data, error = _call_gemini(parts, TRIP_SCHEMA)
    if error:
        return JSONResponse({"ok": False, "error": error}, status_code=502)

    days = _build_days_from_ai(data.get("days"))
    return JSONResponse({"ok": True, "title": data.get("title") or "取り込んだ旅行", "days": days})

# ── 静的ファイル配信 ──────────────────────────────────────
@app.get("/")
def index_page():
    return FileResponse(os.path.join(BASE_DIR, "index.html"))

@app.get("/trip")
def trip_page():
    return FileResponse(os.path.join(BASE_DIR, "trip.html"))
