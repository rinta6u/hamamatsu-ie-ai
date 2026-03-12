# 浜松の家づくり相談AI - Claude Code 作業指示書

## プロジェクト概要
浜松エリアで家を建てたい施主がLINEで相談できるAIチャットボット。
ナレッジベース（工務店情報・地域情報・家づくり知識）をもとにClaude APIが回答する。

## ディレクトリ構成
```
hamamatsu-ie-ai/
├── app/
│   └── main.py          # FastAPIサーバー（LINE Webhook + Claude API呼び出し）
├── knowledge/
│   ├── kb1_2_koumuten_master.txt   # 工務店統合マスター（AI引用データ＋Webスコア）
│   ├── kb3_hamamatsu_context.txt   # 浜松地域コンテキスト（地盤・気候・補助金等）
│   └── kb4_iezukuri_general.txt    # 家づくり一般知識（費用・工法・契約注意点等）
├── requirements.txt
├── Procfile             # Railway起動コマンド
├── .env.example         # 環境変数テンプレート
└── .gitignore
```

## デプロイ手順（Railwayへ）

### Step 1: GitHubリポジトリを作成してプッシュ
```bash
cd hamamatsu-ie-ai
git init
git add .
git commit -m "first commit"
git remote add origin https://github.com/YOUR_USERNAME/hamamatsu-ie-ai.git
git push -u origin main
```

### Step 2: Railwayで環境変数を設定
Railway Dashboard → プロジェクト → Variables に以下を追加：
```
ANTHROPIC_API_KEY=sk-ant-...（Anthropic Consoleで取得）
LINE_CHANNEL_SECRET=...（LINE Developersで取得）
LINE_CHANNEL_ACCESS_TOKEN=...（LINE Developersで取得）
```

### Step 3: RailwayにGitHubリポジトリを接続
Railway Dashboard → New Project → Deploy from GitHub → リポジトリ選択

### Step 4: LINE DevelopersでWebhook URLを設定
RailwayのデプロイURLを確認後、LINE Developers Console で：
```
Webhook URL: https://YOUR-APP.railway.app/webhook
```
「Webhookの利用」をONにする

---

## 開発・修正時のよくある作業

### ナレッジを更新したい
`knowledge/` 配下のtxtファイルを直接編集してgit pushするだけ。
サーバー再起動時に自動で再読み込みされる。

### AIの回答スタイルを変えたい
`app/main.py` の `SYSTEM_PROMPT` を編集する。

### 動作確認（ローカル）
```bash
pip install -r requirements.txt
cp .env.example .env
# .envに実際のキーを記入
uvicorn app.main:app --reload
# http://localhost:8000/health でナレッジ読み込み確認
```

---

## 今後の拡張予定タスク

- [ ] 会話履歴の保持（現状はステートレス・1問1答）
- [ ] 質問ログの保存（施主がどんな質問をしているか収集）
- [ ] ナレッジの月次自動更新（Perplexity API連携）
- [ ] 工務店カテゴリ別の絞り込み対応

---

## 注意事項
- `.env` はGitにコミットしない（.gitignoreで除外済み）
- `ANTHROPIC_API_KEY` は必ずRailwayの環境変数で管理する
- LINE署名検証（`verify_signature`）は必ず有効にしておく（セキュリティ）
