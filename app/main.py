import os
import hashlib
import hmac
import json
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import httpx
from anthropic import Anthropic

app = FastAPI()
client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]

# ナレッジベースをサーバー起動時に一括読み込み
KNOWLEDGE_DIR = Path(__file__).parent.parent / "knowledge"

def load_knowledge() -> str:
    texts = []
    for path in sorted(KNOWLEDGE_DIR.glob("*.txt")):
        texts.append(f"=== {path.stem} ===\n{path.read_text(encoding='utf-8')}")
    return "\n\n".join(texts)

KNOWLEDGE = load_knowledge()

SYSTEM_PROMPT = f"""あなたは「浜松の家づくり相談AI」です。
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

## 持っている情報
以下のナレッジベースを参照して回答してください。

{KNOWLEDGE}
"""

def verify_signature(body: bytes, signature: str) -> bool:
    """LINE Webhookの署名検証"""
    hash = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256
    ).digest()
    import base64
    expected = base64.b64encode(hash).decode("utf-8")
    return hmac.compare_digest(expected, signature)

async def reply_to_line(reply_token: str, text: str):
    """LINEに返信を送る"""
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
        )

def ask_claude(user_message: str) -> str:
    """Claude APIに問い合わせ"""
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text

@app.post("/webhook")
async def webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("X-Line-Signature", "")

    if not verify_signature(body, signature):
        raise HTTPException(status_code=400, detail="Invalid signature")

    data = json.loads(body)

    for event in data.get("events", []):
        if event.get("type") != "message":
            continue
        if event["message"].get("type") != "text":
            continue

        user_message = event["message"]["text"]
        reply_token = event["replyToken"]

        # Claude APIで回答生成
        answer = ask_claude(user_message)
        await reply_to_line(reply_token, answer)

    return JSONResponse({"status": "ok"})

@app.get("/health")
async def health():
    return {"status": "ok", "knowledge_loaded": bool(KNOWLEDGE)}
