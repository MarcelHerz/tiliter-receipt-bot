import os
import json
import base64
import requests
import redis
from flask import Flask, request, make_response

app = Flask(__name__)

# === CONFIGURATION ===
SLACK_TOKEN = os.environ.get("SLACK_TOKEN")
REDIS_URL = os.environ.get("REDIS_URL")
TILITER_AGENT = "receipt-processor"
TILITER_URL = f'https://api.ai.vision.tiliter.com/api/v1/inference/{TILITER_AGENT}'

# Redis client setup
r = redis.from_url(REDIS_URL)

# In-memory tracking of processed events
processed_events = set()

@app.route("/")
def health():
    return "Slack bot is running.", 200

@app.route("/events", methods=["POST"])
def slack_events():
    data = request.json
    print("📩 Incoming Slack event:")
    print(json.dumps(data, indent=2))

    if data.get("type") == "url_verification":
        return make_response(data["challenge"], 200, {"Content-Type": "text/plain"})

    event_id = data.get("event_id")
    if event_id in processed_events:
        print("⏩ Duplicate event ignored.")
        return make_response("Duplicate", 200)
    processed_events.add(event_id)

    if data.get("type") == "event_callback":
        event = data.get("event", {})
        user_id = event.get("user")

        # Handle API key setup command
        if event.get("type") == "message" and not 'files' in event:
            text = event.get("text", "").strip().lower()
            if text.startswith("/setapikey") or text.startswith("my key is"):
                token = text.replace("/setapikey", "").replace("my key is", "").strip()
                if token:
                    r.set(f"key:{user_id}", token)
                    post_to_slack(event["channel"], event["ts"], "✅ Your API key has been saved.")
                    return make_response("OK", 200)
                else:
                    post_to_slack(event["channel"], event["ts"], "❌ No API key detected. Please try again.")
                    return make_response("OK", 200)

        if event.get("type") == "message" and 'files' in event:
            for file in event['files']:
                if file.get('mimetype', '').startswith('image/'):
                    image_url = file['url_private']
                    channel = event['channel']
                    thread_ts = event['ts']

                    api_key = r.get(f"key:{user_id}")
                    if not api_key:
                        post_to_slack(channel, thread_ts, "❌ You haven't set your API key yet. Please send `/setapikey YOUR_KEY`")
                        return make_response("OK", 200)

                    result = handle_image(image_url, api_key.decode())
                    post_to_slack(channel, thread_ts, result)

        return make_response("OK", 200)

    return make_response("Ignored", 200)

def handle_image(image_url, api_key):
    print("⬇️ Downloading image from Slack...")
    image_response = requests.get(
        image_url,
        headers={'Authorization': f'Bearer {SLACK_TOKEN}'}
    )

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
        merchant = result.get("merchant", "Unknown")
        total = result.get("total", "N/A")
        date = result.get("date", "N/A")
        address = result.get("address", "N/A")
        items = result.get("items", [])

        message = (
            f"🧾 *Receipt Details:*
"
            f"• *Merchant:* {merchant}
"
            f"• *Date:* {date}
"
            f"• *Total:* {total} €
"
            f"• *Address:* {address}
"
        )

        if items:
            message += ":shopping_trolley: *Items:*
"
            for item in items:
                name = item.get("name", "Unnamed")
                price = item.get("price", "?")
                message += f"• {name} — {price} €
"

        return message

    except Exception as e:
        return f":x: Could not parse Tiliter response:\n{str(e)}"

def post_to_slack(channel, thread_ts, message):
    print("💬 Posting result back to Slack...")
    requests.post(
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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
