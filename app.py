import os
import json
import base64
import requests
import threading
from flask import Flask, request, make_response
from upstash_redis import Redis

app = Flask(__name__)

# === CONFIGURATION ===
SLACK_TOKEN = os.environ.get("SLACK_TOKEN")
REDIS_URL = os.environ.get("UPSTASH_REDIS_REST_URL")
REDIS_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN")
TILITER_URL = "https://api.ai.vision.tiliter.com/api/v1/inference/receipt-processor"

# Redis via Upstash HTTP client
redis = Redis(url=REDIS_URL, token=REDIS_TOKEN)

@app.route("/")
def health():
    return "Slack bot is running.", 200

@app.route("/events", methods=["POST"])
def slack_events():
    data = request.json

    # Immediately respond to Slack to avoid retries
    response = make_response("OK", 200)
    response.headers["Content-Type"] = "text/plain"

    threading.Thread(target=handle_slack_event, args=(data,)).start()
    return response

def handle_slack_event(data):
    event = data.get("event", {})
    event_type = event.get("type")
    subtype = event.get("subtype")

    # Ignore bot messages and system events
    if "bot_id" in event or subtype in ["bot_message", "message_changed", "message_deleted"]:
        return

    user_id = event.get("user")
    channel = event.get("channel")
    event_ts = event.get("ts")

    if not user_id or not channel or not event_ts:
        return

    # Deduplication logic
    dedup_key = (
        event.get("client_msg_id") or
        (event.get("files")[0].get("id") if event.get("files") else None) or
        f"{channel}:{event_ts}"
    )
    dedup_key = f"processed:{dedup_key}"
    if redis.get(dedup_key):
        return
    redis.set(dedup_key, "1", ex=300)

    # Check API key
    api_key = redis.get(f"key:{user_id}")
    if api_key is None:
        warn_key = f"warned:{user_id}"
        if not redis.get(warn_key):
            redis.set(warn_key, "1", ex=3600)  # Warn once per hour
            post_to_slack(
                channel,
                event_ts,
                ":warning: You haven’t set your Tiliter API key yet.\n\nPlease use `/register-key YOUR_KEY` to set it."
            )
        return

    api_key = api_key.decode()

    # Process image files
    if event_type == "message" and 'files' in event:
        for file in event['files']:
            if file.get('mimetype', '').startswith('image/'):
                image_url = file['url_private']
                result = handle_image(image_url, api_key)
                post_to_slack(channel, event_ts, result)

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
        headers={
            'X-API-Key': api_key,
            'Content-Type': 'application/json'
        },
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
        item_lines = "\n".join([
            f"• {item.get('name', 'Unnamed')} — {item.get('price', 'N/A')}{currency}"
            for item in items
        ])

        return (
            f"🧾 *Receipt Details:*\n"
            f"- Merchant: *{merchant}*\n"
            f"- Date: *{date}*\n"
            f"- Total: *{total}{currency}*\n"
            f"- Address: {address}\n\n"
            f"🛒 *Items:*\n{item_lines}"
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
