# TripPlanner — デプロイ手順

## 全体の流れ

```
Supabase (PostgreSQL) ← データ保存
       ↑
    Render (FastAPI) ← アプリサーバー（無料）
       ↑
    GitHub ← コード管理・自動デプロイ
       ↑
UptimeRobot ← Renderのスリープ防止（無料）
```

---

## Step 1: Supabase でデータベースを作る

1. https://supabase.com にアクセス → **Start your project**（無料）
2. 新しいプロジェクトを作成（名前: `tripplanner` など、パスワードは控えておく）
3. 左メニュー **SQL Editor** を開き、以下を実行：

```sql
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rooms (
    name       TEXT PRIMARY KEY,
    created_at TIMESTAMP DEFAULT NOW()
);
```

（アプリ起動時に `init_db()` が自動作成するため、このステップは省略しても問題ありません）

4. 左メニュー **Settings → Database** を開く
5. **Connection string → URI** をコピー（`postgresql://postgres:...` の形式）
   - **必ず「Transaction pooler」ではなく「Session pooler」か「Direct connection」を使うこと**

→ これが `DATABASE_URL` になる（後でRenderに貼り付ける）

---

## Step 1.5: Gemini APIキーを取得する（AI機能用）

「AIに旅程を考えてもらう」「しおりファイルから取り込む」機能で使用。無料・クレジットカード登録不要。

1. https://aistudio.google.com/apikey にアクセス（Googleアカウントでログイン）
2. **Create API key** をクリック
3. 表示されたキーをコピー

→ これが `GEMINI_API_KEY` になる（後でRenderに貼り付ける）

---

## Step 2: GitHubにコードをプッシュ

### リポジトリ構成（最終形）

```
tripplanner/
├── app/
│   ├── main.py
│   ├── requirements.txt
│   ├── index.html       ← ルームログイン画面
│   └── trip.html         ← 旅程・地図・予算アプリ本体
└── Dockerfile
```

### 手順

```bash
git init
git add .
git commit -m "initial commit"
git branch -M main
git remote add origin https://github.com/あなたのユーザー名/tripplanner.git
git push -u origin main
```

---

## Step 3: Render にデプロイ

1. https://render.com にアクセス → **New → Web Service**
2. GitHubリポジトリを接続
3. 設定：
   - **Environment**: `Docker`
   - **Instance Type**: `Free`
4. **Environment Variables** に以下を追加：
   | Key | Value |
   |-----|-------|
   | `DATABASE_URL` | Supabaseでコピーしたpostgresql://... の文字列 |
   | `GEMINI_API_KEY` | Google AI Studioで発行したAPIキー |
5. **Deploy** をクリック

→ デプロイ完了後、`https://あなたのサービス名.onrender.com` でアクセス可能

---

## Step 4: UptimeRobot でスリープ防止

Renderの無料プランは**15分アクセスがないとスリープ**する（起動に30秒かかる）。
UptimeRobotで定期的にアクセスさせてスリープを防ぐ。

1. https://uptimerobot.com → 無料登録
2. **Add New Monitor**：
   - **Monitor Type**: `HTTP(s)`
   - **Friendly Name**: `TripPlanner`
   - **URL**: `https://あなたのサービス名.onrender.com/`
   - **Monitoring Interval**: `5 minutes`
3. **Create Monitor** をクリック

---

## ローカル開発

```bash
cd app
pip install -r requirements.txt
export DATABASE_URL=postgresql://postgres:...   # SupabaseのURLをそのまま使ってOK
uvicorn main:app --reload --port 8000
# → http://localhost:8000 でアクセス
```

ローカルもSupabase（本番と同じDB）に接続する構成。個人利用のため、ローカル用DBを別途用意する必要はない。

---

## 使い方

1. `/` でユーザー名（旅行グループ名）を入力するだけで入室（パスワードは不要）
2. 同じユーザー名を知っている人は誰でも同じ旅行データを閲覧・編集できる
3. 旅程・地図・予算の変更は自動保存される（他の人の変更を見るには「更新」ボタンを押す）
