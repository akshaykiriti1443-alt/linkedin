import os
import hmac
import hashlib
import httpx
from fastapi import FastAPI, Request, HTTPException, Query
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Instagram Auto-Reply Bot")

VERIFY_TOKEN = os.environ["VERIFY_TOKEN"]
APP_SECRET = os.environ["APP_SECRET"]
PAGE_ACCESS_TOKEN = os.environ["PAGE_ACCESS_TOKEN"]
REPLY_TEMPLATE = os.environ.get(
    "REPLY_TEMPLATE",
    "Thanks for your comment! 🙌 DM me to get the full guide.",
)

GRAPH_API = "https://graph.facebook.com/v19.0"


def _verify_signature(body: bytes, sig_header: str) -> bool:
    expected = "sha256=" + hmac.new(
        APP_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, sig_header or "")


@app.get("/webhook")
async def webhook_verify(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    """Instagram webhook verification handshake."""
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        return int(hub_challenge)
    raise HTTPException(status_code=403, detail="Verification failed")


@app.post("/webhook")
async def webhook_receive(request: Request):
    """Handle incoming Instagram webhook events."""
    body = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_signature(body, sig):
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = await request.json()

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") != "comments":
                continue
            value = change.get("value", {})
            comment_id = value.get("id")
            # Skip replies to avoid reply loops
            if value.get("parent_id") or not comment_id:
                continue
            await _reply_to_comment(comment_id)

    return {"status": "ok"}


async def _reply_to_comment(comment_id: str):
    url = f"{GRAPH_API}/{comment_id}/replies"
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url,
            params={"access_token": PAGE_ACCESS_TOKEN},
            json={"message": REPLY_TEMPLATE},
        )
        resp.raise_for_status()
