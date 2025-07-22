import os
import json
import base64
import requests
from flask import Flask, request, make_response
from upstash_redis import Redis

app = Flask(__name__)

# === CONFIG ===
SLACK_TOKEN = os.environ.get("SLACK_TOKEN")
REDIS_URL = os.environ.get("UPSTASH_REDIS_REST_URL")
REDIS_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN")
TILITER_URL = "https://api.ai.vision.tiliter.com/api/v1/inference/receipt-processor"

redis = Redis(url=REDIS_URL, token=REDIS_TOKEN)

@app.route("/")
def health():
    return "Slack bot is running.", 200

@app.route("/events", methods=["POST"])
def slack_events():
    data = request.json
    print("📩 Incoming Slack event:")
    print(json.dumps(data, indent=2))

    # Handle Slack URL verification
    if data.get("type") == "url_verification":
        return make_response(data["challenge"], 200, {"Content-Type": "text/plain"})

    event_id = data.get("event_id")
    if event_id:
        if redis.get(f"seen:{event_id}"):
            return make_response("Duplicate event", 200)
        redis.set(f"seen:{event_id}", "1", ex=3600)

    event = data.get("event", {})
    user_id = event.get("user")
    event_type = event.get("type")

    if event_type != "message" or "bot_id" in event or not user_id:
        return make_response("Ignored event", 200)

    api_key = redis.get(f"key:{user_id}")
    if api_key is None:
        if not redis.get(f"warned:{user_id}"):
            redis.set(f"warned:{user_id}", "1", ex=3600)
            post_to_slack(
                event.get("channel"),
                event.get("ts"),
                ":warning: You haven’t set your Tiliter API key yet.\n\n"
                "Visit https://ai.vision.tiliter.com to buy credits and copy your API key. Then run:\n"
                "`/set-apikey YOUR_KEY`"
            )
        return make_response("No API key", 200)

    api_key = api_key.decode()

    if 'files' in event:
        for file in event['files']:
            if file.get('mimetype', '').startswith('image/'):
                image_url = file['url_private']
                channel = event['channel']
                thread_ts = event['ts']
                result = handle_image(image_url, api_key)
                post_to_slack(channel, thread_ts, result)

    return make_response("OK", 200)

@app.route("/set-apikey", methods=["POST"])
def set_api_key():
    user_id = request.form.get("user_id")
    text = request.form.get("text", "").strip()
    if not user_id or not text:
        return make_response("❌ Usage: /set-apikey YOUR_KEY", 200)
    redis.set(f"key:{user_id}", text)
    redis.delete(f"warned:{user_id}")
    return make_response("✅ Your Tiliter API key has been saved.", 200)

@app.route("/delete-apikey", methods=["POST"])
def delete_api_key():
    user_id = request.form.get("user_id")
    if not user_id:
        return make_response("Missing user ID", 200)
    redis.delete(f"key:{user_id}")
    redis.delete(f"warned:{user_id}")
    return make_response("🗑️ Your API key has been deleted.", 200)

@app.route("/get-apikey", methods=["POST"])
def get_api_key():
    user_id = request.form.get("user_id")
    key = redis.get(f"key:{user_id}")
    if not key:
        return make_response("ℹ️ No API key found.", 200)
    return make_response(f"🔐 Your API key is:\n```{key}```", 200)

def handle_image(image_url, api_key):
    print("⬇️ Downloading image from Slack...")
    img = requests.get(image_url, headers={"Authorization": f"Bearer {SLACK_TOKEN}"})
    if img.status_code != 200:
        return f":x: Failed to download image: {img.status_code}"

    image_b64 = base64.b64encode(img.content).decode("utf-8")
    payload = { "image_data": f"data:image/jpeg;base64,{image_b64}" }

    print("📤 Sending to Tiliter API...")
    res = requests.post(
        TILITER_URL,
        headers={"X-API-Key": api_key, "Content-Type": "application/json"},
        json=payload
    )

    if res.status_code != 200:
        return f":x: Tiliter API error {res.status_code}: {res.text}"

    try:
        result = res.json().get("result", {})
        merchant = result.get("merchant", "Unknown")
        total = result.get("total", "N/A")
        date = result.get("date", "N/A")
        address = result.get("address", "")
        currency = result.get("currency", "")
        items = result.get("items", [])
        item_lines = "\n".join([
            f"• {item.get('name', 'Unnamed')} — {item.get('price') or 'N/A'}{currency or ''}"
            for item in items
        ]) if items else "_No items found_"

        return (
            f"🧾 *Receipt Details:*\n"
            f"- Merchant: *{merchant}*\n"
            f"- Date: *{date}*\n"
            f"- Total: *{total}{currency}*\n"
            f"- Address: {address}\n\n"
            f"🛒 *Items:*\n{item_lines}"
        )
    except Exception as e:
        return f":x: Failed to parse response: {str(e)}"

def post_to_slack(channel, thread_ts, message):
    print("💬 Posting to Slack...")
    r = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {SLACK_TOKEN}", "Content-Type": "application/json"},
        json={"channel": channel, "thread_ts": thread_ts, "text": message}
    )
    print("🔁 Slack API response:", r.status_code, r.text)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
