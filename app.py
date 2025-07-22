import os
import json
import base64
import requests
from flask import Flask, request, make_response

app = Flask(__name__)

# === CONFIGURATION ===
SLACK_TOKEN = os.environ.get("SLACK_TOKEN")
DEFAULT_TILITER_API_KEY = os.environ.get("TILITER_API_KEY")
TILITER_URL = 'https://api.ai.vision.tiliter.com/api/v1/inference/object-counter'

# In-memory storage (temporary until Redis is plugged in)
user_api_keys = {}

@app.route("/")
def health():
    return "Slack bot is running.", 200

@app.route("/events", methods=["POST"])
def slack_events():
    data = request.json
    print("📩 Incoming Slack event:")
    print(json.dumps(data, indent=2))

    # Slack URL verification
    if data.get("type") == "url_verification":
        return make_response(data["challenge"], 200, {"Content-Type": "text/plain"})

    if data.get("type") == "event_callback":
        event = data.get("event", {})
        user_id = event.get("user")

        # Handle "register" command
        if event.get("type") == "message" and 'text' in event:
            text = event["text"].strip().lower()
            if text.startswith("register sk-"):
                api_key = text.split("register", 1)[1].strip()
                user_api_keys[user_id] = api_key
                post_to_slack(event["channel"], event["ts"], ":white_check_mark: API key registered successfully.")
                return make_response("Registered", 200)

        # Process file uploads
        if event.get("type") == "message" and 'files' in event:
            image_url = next((f['url_private'] for f in event['files'] if f.get('mimetype', '').startswith('image/')), None)
            if image_url:
                api_key = user_api_keys.get(user_id)
                if not api_key:
                    post_to_slack(event["channel"], event["ts"], ":warning: Please register your API key first using `register sk-...`.")
                    return make_response("No key", 200)

                user_text = event.get("text", "").lower()
                object_name = user_text.replace("count", "").strip() if user_text.startswith("count") else None
                result = handle_image(image_url, object_name, api_key)
                post_to_slack(event["channel"], event["ts"], result)

    return make_response("OK", 200)

def handle_image(image_url, object_name, api_key):
    print("⬇️ Downloading image from Slack...")
    image_response = requests.get(image_url, headers={'Authorization': f'Bearer {SLACK_TOKEN}'})
    if image_response.status_code != 200:
        return f":x: Failed to download image. Status: {image_response.status_code}"

    image_b64 = base64.b64encode(image_response.content).decode('utf-8')
    image_data_uri = f"data:image/jpeg;base64,{image_b64}"
    payload = {
        "image_data": image_data_uri,
        "parameter": f"count {object_name}" if object_name else ""
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
        counts = result.get("object_counts", {})
        total = result.get("total_objects", 0)

        if not counts:
            return ":x: No objects found."

        lines = "\n".join([f"• {obj}: {count}" for obj, count in counts.items()])
        return (
            f":brain: *Tiliter Result:*\n"
            f":white_check_mark: Total objects found: {total}\n"
            f":1234: Breakdown:\n{lines}"
        )
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
