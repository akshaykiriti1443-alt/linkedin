# Instagram Auto-Reply Bot

Automatically replies to comments on your Instagram posts using the Instagram Graph API.

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure environment
```bash
cp .env.example .env
# Fill in all values in .env
```

### 3. Get your credentials (Meta Developer Console)

1. Go to [developers.facebook.com](https://developers.facebook.com) and create an app (type: **Business**).
2. Add the **Instagram Graph API** product.
3. Connect your Instagram Professional account (Creator or Business).
4. Generate a **Page Access Token** with these permissions:
   - `instagram_basic`
   - `instagram_manage_comments`
5. Copy your **App Secret** from Settings → Basic.

### 4. Run the server
```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

Expose it publicly (e.g. via [ngrok](https://ngrok.com) for testing):
```bash
ngrok http 8000
```

### 5. Register the webhook in Meta Developer Console

1. Go to your app → **Webhooks** → **Instagram**.
2. Set:
   - **Callback URL**: `https://your-domain.com/webhook`
   - **Verify Token**: same value as `VERIFY_TOKEN` in your `.env`
3. Subscribe to the **comments** field.

## Customizing the reply

Edit `REPLY_TEMPLATE` in your `.env`:
```
REPLY_TEMPLATE=Hey! Drop me a DM and I'll send you the free resource 🎁
```

## Deployment

Any platform that can run a Python ASGI server works: Railway, Render, Fly.io, etc.
Set the four env vars (`VERIFY_TOKEN`, `APP_SECRET`, `PAGE_ACCESS_TOKEN`, `REPLY_TEMPLATE`) in the platform's settings.
