import os
import hashlib
import hmac
import json
import base64
import logging
import re
from pathlib import Path
from datetime import datetime, date
from collections import defaultdict

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import httpx
from anthropic import Anthropic, APIStatusError, APIConnectionError, RateLimitError

# ──────────────────────────────────────────
# ログ設定
# ──────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────
# 環境変数
# ──────────────────────────────────────────
app = FastAPI()
client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

LINE_CHANNEL_SECRET       = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
ADMIN_LINE_USER_ID        = os.environ.get("ADMIN_LINE_USER_ID", "")  # 管理者のLINEユーザーID

# レート制限（1ユーザー/1日あたりの最大メッセージ数）
RATE_LIMIT_PER_DAY   = int(os.environ.get("RATE_LIMIT_PER_DAY", "20"))
# 会話履歴の最大保持メッセージ数
MAX_HISTORY_MESSAGES = int(os.environ.get("MAX_HISTORY_MESSAGES", "10"))

# ──────────────────────────────────────────
# パス設定
# ──────────────────────────────────────────
KNOWLEDGE_DIR     = Path(__file__).parent.parent / "knowledge"
DATA_DIR          = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)

QUERY_LOG_FILE    = DATA_DIR / "query_log.jsonl"
USER_PROFILE_FILE = DATA_DIR / "user_profiles.json"

# ──────────────────────────────────────────
# インメモリストア
# ──────────────────────────────────────────
conversation_history: dict[str, list] = defaultdict(list)  # user_id -> messages
rate_counter:         dict[str, dict] = {}                  # user_id -> {date, count}

# ──────────────────────────────────────────
# ユーザープロファイル（ファイル永続化）
# ──────────────────────────────────────────
def load_user_profiles() -> dict:
    if USER_PROFILE_FILE.exists():
        try:
            return json.loads(USER_PROFILE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def save_user_profiles(profiles: dict):
    try:
        USER_PROFILE_FILE.write_text(
            json.dumps(profiles, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception as e:
        logger.error(f"プロファイル保存エラー: {e}")

user_profiles: dict = load_user_profiles()

# ──────────────────────────────────────────
# ナレッジベース読み込み
# ──────────────────────────────────────────
def load_knowledge() -> str:
    texts = []
    for path in sorted(KNOWLEDGE_DIR.glob("*.txt")):
        texts.append(f"=== {path.stem} ===\n{path.read_text(encoding='utf-8')}")
    return "\n\n".join(texts)

KNOWLEDGE = load_knowledge()

# ──────────────────────────────────────────
# システムプロンプト
# ──────────────────────────────────────────
_BASE_PROMPT = """あなたは「浜松の家づくり相談AI」です。
浜松市とその周辺エリア（磐田・袋井など）で注文住宅・新築を検討している施主の相談に答えます。

## あなたの役割
- 浜松エリアの工務店情報・地域特性・家づくりの基礎知識を元に、施主が「どの工務店に相談すべきか」「何を確認すべきか」を具体的にアドバイスする
- 中立的な立場で情報を提供する（特定工務店の過度な宣伝はしない）
- 「専門家に相談すべき内容」は明確にそう伝え、AIが断定しない

## 回答のルール
- LINEで読みやすいよう、短い段落・改行を意識する
- 一度に詰め込みすぎず、追加質問を促す
- 工務店を紹介する際は、得意分野とAI引用データを根拠として示す
- 「わかりません」より「〜については工務店に直接確認することをおすすめします」と案内する
- 回答は日本語のみ

## ユーザー情報の収集
会話の流れで以下の情報をまだ把握できていない場合、自然に1〜2項目ずつ確認してください。
収集したい情報: 年齢・家族構成（人数・子供の有無）・世帯年収・建築予定エリア・予算・現在の住まい状況

## プロフィール情報の自動記録（重要なルール）
ユーザーが自分の情報（年齢・家族構成・世帯年収・建築エリア・予算・住まい状況）を話したとき、
回答文の末尾に以下の形式でJSONブロックを追加してください。新情報がない場合は不要です。

__PROFILE_UPDATE__
{{"age": "35歳", "family": "夫婦＋子2人"}}
__END_PROFILE__

JSONのキー（わかるものだけ）:
- age           : 年齢
- family        : 家族構成
- income        : 世帯年収
- area          : 建築予定エリア
- budget        : 予算
- housing_status: 現在の住まい

## 持っている情報
{knowledge}

{profile_section}"""

_PROFILE_LABELS = {
    "age":            "年齢",
    "family":         "家族構成",
    "income":         "世帯年収",
    "area":           "建築予定エリア",
    "budget":         "予算",
    "housing_status": "現在の住まい",
}

def build_system_prompt(user_id: str) -> str:
    profile = user_profiles.get(user_id, {})

    known   = [f"- {_PROFILE_LABELS[k]}: {profile[k]}" for k in _PROFILE_LABELS if k in profile]
    missing = [_PROFILE_LABELS[k] for k in _PROFILE_LABELS if k not in profile]

    profile_section = ""
    if known:
        profile_section += "## 把握済みのユーザー情報\n" + "\n".join(known) + "\n"
    if missing:
        profile_section += f"\n## まだ確認できていない情報\n{', '.join(missing)}\n（会話の中で自然に確認してください）\n"

    return _BASE_PROMPT.format(knowledge=KNOWLEDGE, profile_section=profile_section)

# ──────────────────────────────────────────
# プロファイル更新の抽出
# ──────────────────────────────────────────
_PROFILE_RE = re.compile(
    r"\n*__PROFILE_UPDATE__\s*(.*?)\s*__END_PROFILE__",
    re.DOTALL
)

def extract_and_update_profile(user_id: str, raw_answer: str) -> str:
    """回答からプロファイルJSONを抽出・保存し、クリーンな回答テキストを返す"""
    match = _PROFILE_RE.search(raw_answer)
    if match:
        try:
            updates = json.loads(match.group(1))
            if user_id not in user_profiles:
                user_profiles[user_id] = {}
            user_profiles[user_id].update(updates)
            save_user_profiles(user_profiles)
            logger.info(f"プロファイル更新: user_id={user_id} updates={updates}")
        except json.JSONDecodeError as e:
            logger.warning(f"プロファイルJSON解析エラー: {e}")
        return _PROFILE_RE.sub("", raw_answer).strip()
    return raw_answer

# ──────────────────────────────────────────
# クエリログ
# ──────────────────────────────────────────
def log_query(user_id: str, user_message: str, response: str):
    entry = {
        "timestamp": datetime.now().isoformat(),
        "user_id":   user_id,
        "message":   user_message,
        "response":  response,
    }
    try:
        with open(QUERY_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.error(f"クエリログ書き込みエラー: {e}")

# ──────────────────────────────────────────
# レート制限
# ──────────────────────────────────────────
def check_rate_limit(user_id: str) -> bool:
    """制限超過の場合 False を返す。制限内なら True を返しカウントを加算する"""
    today = str(date.today())
    if user_id not in rate_counter or rate_counter[user_id]["date"] != today:
        rate_counter[user_id] = {"date": today, "count": 0}
    if rate_counter[user_id]["count"] >= RATE_LIMIT_PER_DAY:
        return False
    rate_counter[user_id]["count"] += 1
    return True

# ──────────────────────────────────────────
# 管理者通知（LINE Push）
# ──────────────────────────────────────────
async def notify_admin(message: str):
    if not ADMIN_LINE_USER_ID:
        logger.warning("ADMIN_LINE_USER_ID 未設定 - 管理者通知をスキップ")
        return
    try:
        async with httpx.AsyncClient() as http:
            res = await http.post(
                "https://api.line.me/v2/bot/message/push",
                headers={
                    "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
                    "Content-Type": "application/json",
                },
                json={
                    "to": ADMIN_LINE_USER_ID,
                    "messages": [{"type": "text", "text": f"⚠️【管理者通知】\n{message}"}],
                },
                timeout=10.0,
            )
            logger.info(f"管理者通知送信: status={res.status_code}")
    except Exception as e:
        logger.error(f"管理者通知エラー: {e}")

# ──────────────────────────────────────────
# LINE 署名検証
# ──────────────────────────────────────────
def verify_signature(body: bytes, signature: str) -> bool:
    digest = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)

# ──────────────────────────────────────────
# LINE 返信
# ──────────────────────────────────────────
async def reply_to_line(reply_token: str, text: str):
    async with httpx.AsyncClient() as http:
        await http.post(
            "https://api.line.me/v2/bot/message/reply",
            headers={
                "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
                "Content-Type": "application/json",
            },
            json={
                "replyToken": reply_token,
                "messages": [{"type": "text", "text": text}],
            },
            timeout=30.0,
        )

# ──────────────────────────────────────────
# Claude API 呼び出し（会話履歴付き）
# ──────────────────────────────────────────
async def ask_claude(user_id: str, user_message: str) -> str:
    history = conversation_history[user_id]
    history.append({"role": "user", "content": user_message})

    # 古い履歴を削除（最新 MAX_HISTORY_MESSAGES 件のみ保持）
    if len(history) > MAX_HISTORY_MESSAGES:
        conversation_history[user_id] = history[-MAX_HISTORY_MESSAGES:]
        history = conversation_history[user_id]

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1200,
            system=build_system_prompt(user_id),
            messages=history,
        )
        raw_answer = response.content[0].text

        # プロファイル更新を抽出してクリーンな回答を生成
        answer = extract_and_update_profile(user_id, raw_answer)

        # 会話履歴にアシスタント返答を追加（クリーン版）
        history.append({"role": "assistant", "content": answer})
        return answer

    except RateLimitError as e:
        logger.error(f"RateLimitError: {e}")
        await notify_admin(
            f"🚨 APIのレート制限に到達しました！\n"
            f"しばらく返信できない状態です。\n詳細: {e}"
        )
        return "申し訳ありません、現在アクセスが集中しています。しばらく待ってから再度お試しください🙏"

    except APIStatusError as e:
        logger.error(f"APIStatusError: status={e.status_code}, message={e.message}")
        if e.status_code == 402:
            await notify_admin(
                f"🚨【緊急】API利用料金の残高が不足しています！\n"
                f"Anthropic Console で請求情報を確認してください。\nステータス: {e.status_code}"
            )
        elif e.status_code == 529:
            await notify_admin(
                f"⚠️ AnthropicのAPIが過負荷状態です（529 Overloaded）。\n"
                f"しばらくすると自動回復する見込みです。"
            )
        else:
            await notify_admin(
                f"⚠️ APIエラーが発生しました。\nステータス: {e.status_code}\n{e.message}"
            )
        return "申し訳ありません、現在AIに接続できない状態です。しばらく時間をおいてから再度お試しください🙏"

    except APIConnectionError as e:
        logger.error(f"APIConnectionError: {e}")
        await notify_admin(f"⚠️ API接続エラーが発生しました。\nネットワーク状態を確認してください。\n{e}")
        return "申し訳ありません、AIへの接続に失敗しました。しばらくお待ちください🙏"

    except Exception as e:
        logger.error(f"予期しないエラー: {type(e).__name__}: {e}")
        await notify_admin(f"⚠️ 予期しないエラーが発生しました。\n種類: {type(e).__name__}\n{e}")
        return "申し訳ありません、エラーが発生しました。しばらく時間をおいてから再度お試しください🙏"

# ──────────────────────────────────────────
# エンドポイント
# ──────────────────────────────────────────
@app.post("/webhook")
async def webhook(request: Request):
    body      = await request.body()
    signature = request.headers.get("X-Line-Signature", "")

    if not verify_signature(body, signature):
        raise HTTPException(status_code=400, detail="Invalid signature")

    data = json.loads(body)

    for event in data.get("events", []):
        if event.get("type") != "message":
            continue
        if event["message"].get("type") != "text":
            continue

        user_id      = event["source"].get("userId", "unknown")
        user_message = event["message"]["text"]
        reply_token  = event["replyToken"]

        # ① レート制限チェック
        if not check_rate_limit(user_id):
            await reply_to_line(
                reply_token,
                f"本日のご利用回数の上限（{RATE_LIMIT_PER_DAY}回）に達しました。\n"
                "明日またお気軽にご相談ください😊"
            )
            logger.info(f"レート制限超過: user_id={user_id}")
            continue

        # ② Claude API で回答生成
        answer = await ask_claude(user_id, user_message)

        # ③ クエリログ保存
        log_query(user_id, user_message, answer)

        # ④ LINE 返信
        await reply_to_line(reply_token, answer)

    return JSONResponse({"status": "ok"})


@app.get("/health")
async def health():
    return {
        "status":           "ok",
        "knowledge_loaded": bool(KNOWLEDGE),
        "users_in_memory":  len(conversation_history),
        "profiles_stored":  len(user_profiles),
        "query_log":        str(QUERY_LOG_FILE),
    }


@app.get("/logs")
async def get_logs(limit: int = 50):
    """最新のクエリログを返す（管理用）"""
    if not QUERY_LOG_FILE.exists():
        return {"logs": [], "total": 0}
    lines = [l for l in QUERY_LOG_FILE.read_text(encoding="utf-8").strip().split("\n") if l]
    recent = lines[-limit:]
    return {
        "logs":  [json.loads(l) for l in recent],
        "total": len(lines),
    }


@app.get("/profiles")
async def get_profiles():
    """収集済みユーザープロファイル一覧（管理用）"""
    return {
        "profiles": user_profiles,
        "count":    len(user_profiles),
    }
