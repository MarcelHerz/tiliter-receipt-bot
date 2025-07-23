import os
import json
import base64
import requests
from flask import Flask, request, make_response, redirect
from upstash_redis import Redis

app = Flask(__name__)

# === CONFIGURATION ===
SLACK_TOKEN = os.environ.get("SLACK_TOKEN")
SLACK_CLIENT_ID = os.environ.get("SLACK_CLIENT_ID")
SLACK_CLIENT_SECRET = os.environ.get("SLACK_CLIENT_SECRET")
REDIS_URL = os.environ.get("UPSTASH_REDIS_REST_URL")
REDIS_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN")
TILITER_URL = "https://api.ai.vision.tiliter.com/api/v1/inference/receipt-processor"

# Redis via Upstash HTTP client
redis = Redis(url=REDIS_URL, token=REDIS_TOKEN)

# To avoid duplicate handling
processed_event_ids = set()

@app.route("/")
def health():
    return "Slack bot is running.", 200

@app.route("/install")
def install():
    slack_url = (
        "https://slack.com/oauth/v2/authorize"
        f"?client_id={SLACK_CLIENT_ID}"
        "&scope=commands,files:read,chat:write"
        "&user_scope="
    )
    return redirect(slack_url)

@app.route("/oauth/callback")
def oauth_callback():
    code = request.args.get("code")
    if not code:
        return "Missing code", 400

    response = requests.post("https://slack.com/api/oauth.v2.access", data={
        "client_id": SLACK_CLIENT_ID,
        "client_secret": SLACK_CLIENT_SECRET,
        "code": code
    })

    if response.status_code != 200 or not response.json().get("ok"):
        print("❌ OAuth error:", response.text)
        return "OAuth failed", 400

    print("✅ App installed:", response.json())
    return "App installed successfully! You can now use the Tiliter bot in your Slack workspace."

@app.route("/events", methods=["POST"])
def slack_events():
    data = request.json
    print("📩 Incoming Slack event:")
    print(json.dumps(data, indent=2))

    if data.get("type") == "url_verification":
        return make_response(data["challenge"], 200, {"Content-Type": "text/plain"})

    event = data.get("event", {})
    event_id = data.get("event_id")
    user_id = event.get("user")
    event_type = event.get("type")
    subtype = event.get("subtype")

    if event_id in processed_event_ids:
        return make_response("Duplicate", 200)
    processed_event_ids.add(event_id)

    if event_type == "message" and subtype == "file_share":
        if "bot_id" in event:
            return make_response("Ignore bot", 200)

        api_key = redis.get(f"key:{user_id}")
        if api_key is None:
            warn_key = f"warned:{user_id}:{event.get('ts')}"
            if not redis.get(warn_key):
                redis.set(warn_key, "1", ex=3600)
                post_to_slack(
                    event.get("channel"),
                    event.get("ts"),
                    ":warning: You haven’t set your Tiliter API key yet.\n\nVisit https://ai.vision.tiliter.com to purchase credits, then use `/setapikey YOUR_KEY` to activate."
                )
            return make_response("No API key", 200)

        if isinstance(api_key, bytes):
            api_key = api_key.decode()

        for file in event.get("files", []):
            if file.get("mimetype", "").startswith("image/"):
                image_url = file["url_private"]
                result = handle_image(image_url, api_key)
                post_to_slack(event["channel"], event["ts"], result)

    return make_response("OK", 200)

@app.route("/set-apikey", methods=["POST"])
def set_api_key():
    payload = request.form
    user_id = payload.get("user_id")
    text = payload.get("text", "").strip()

    if not text:
        return make_response("Usage: /set-apikey YOUR_KEY", 200)

    redis.set(f"key:{user_id}", text)
    return make_response("✅ Tiliter API key saved successfully.", 200)

@app.route("/get-apikey", methods=["POST"])
def get_api_key():
    user_id = request.form.get("user_id")
    api_key = redis.get(f"key:{user_id}")
    if api_key:
        if isinstance(api_key, bytes):
            api_key = api_key.decode()
        return make_response(f"🔐 Your current API key is:\n```{api_key}```", 200)
    return make_response("❌ No API key set.", 200)

@app.route("/delete-apikey", methods=["POST"])
def delete_api_key():
    user_id = request.form.get("user_id")
    redis.delete(f"key:{user_id}")
    return make_response("🗑️ Tiliter API key removed.", 200)

def handle_image(image_url, api_key):
    print("⬇️ Downloading image from Slack...")
    image_response = requests.get(image_url, headers={'Authorization': f'Bearer {SLACK_TOKEN}'})
    if image_response.status_code != 200:
        return f":x: Failed to download image. Status: {image_response.status_code}"

    image_b64 = base64.b64encode(image_response.content).decode('utf-8')
    payload = {
        "image_data": f"data:image/jpeg;base64,{image_b64}"
    }

    print("📤 Sending to Tiliter API...")
    response = requests.post(
        TILITER_URL,
        headers={'X-API-Key': api_key, 'Content-Type': 'application/json'},
        json=payload
    )

    if response.status_code != 200:
        return f":x: Tiliter API error {response.status_code}: {response.text}"

    try:
        result = response.json().get("result", {})
        print("✅ Tiliter API response:")
        print(json.dumps(result, indent=2))

        merchant = result.get("merchant", "Unknown")
        total = result.get("total", "N/A")
        date = result.get("date", "N/A")
        address = result.get("address", "")
        currency = result.get("currency", "")

        items = result.get("items", [])
        if not items:
            item_lines = "_No items detected._"
        else:
            item_lines = "\n".join([f"• {item.get('name', 'Unnamed')} — {item.get('price', 'N/A')}{currency}" for item in items])

        return (
            f":receipt: *Receipt Details:*\n"
            f"- Merchant: *{merchant}*\n"
            f"- Date: *{date}*\n"
            f"- Total: *{total}{currency}*\n"
            f"- Address: {address}\n\n"
            f":shopping_trolley: *Items:*\n{item_lines}"
        )
    except Exception as e:
        return f":x: Could not parse Tiliter response:\n{str(e)}"

def post_to_slack(channel, thread_ts, message):
    print("💬 Posting result back to Slack...")
    res = requests.post(
        'https://slack.com/api/chat.postMessage',
        headers={
            'Authorization': f'Bearer {SLACK_TOKEN}',
            'Content-Type': 'application/json'
        },
        json={
            'channel': channel,
            'thread_ts': thread_ts,
            'text': message
        }
    )
    print("🔁 Slack API response:", res.status_code, res.text)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
