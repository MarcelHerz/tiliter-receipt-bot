import os
import json
import base64
import requests
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
    print("📩 Incoming Slack event:")
    print(json.dumps(data, indent=2))

    # === Deduplicate using event_id ===
    event_id = data.get("event_id")
    if event_id:
        seen_key = f"seen:{event_id}"
        if redis.get(seen_key):
            return make_response("Duplicate event", 200)
        redis.set(seen_key, "1", ex=3600)

    # === Handle URL verification ===
    if data.get("type") == "url_verification":
        return make_response(data["challenge"], 200, {"Content-Type": "text/plain"})

    event = data.get("event", {})
    user_id = event.get("user")
    event_type = event.get("type")

    if not user_id:
        return make_response("No user ID", 200)

    # Only handle user message events
    if event_type != "message":
        return make_response("Skipping non-message event", 200)

    if "bot_id" in event:
        return make_response("Ignore bot message", 200)

    api_key = redis.get(f"key:{user_id}")
    if api_key is None:
        warn_key = f"warned:{user_id}"
        if not redis.get(warn_key):
            redis.set(warn_key, "1", ex=3600)
            post_to_slack(
                event.get("channel"),
                event.get("ts"),
                ":warning: You haven’t set your Tiliter API key yet.\n\n"
                "Please visit https://ai.vision.tiliter.com to purchase credits and copy your API key. Then run:\n"
                "`/set-apikey YOUR_KEY`"
            )
        return make_response("No API key", 200)

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
        return make_response("❌ Please provide your API key like this:\n/set-apikey YOUR_KEY", 200)

    redis.set(f"key:{user_id}", text)
    return make_response("✅ Your Tiliter API key has been registered successfully.", 200)

@app.route("/delete-apikey", methods=["POST"])
def delete_api_key():
    user_id = request.form.get("user_id")

    if not user_id:
        return make_response("❌ Could not identify your user ID.", 200)

    redis.delete(f"key:{user_id}")
    redis.delete(f"warned:{user_id}")
    return make_response("🗑️ Your Tiliter API key has been deleted.", 200)

@app.route("/get-apikey", methods=["POST"])
def get_api_key():
    user_id = request.form.get("user_id")

    if not user_id:
        return make_response("❌ Could not identify your user ID.", 200)

    api_key = redis.get(f"key:{user_id}")
    if not api_key:
        return make_response("ℹ️ No API key found for your user.", 200)

    return make_response(f"🔐 Your current API key is:\n```{api_key}```", 200)

def handle_image(image_url, api_key):
    print("⬇️ Downloading image from Slack...")
    image_response = requests.get(image_url, headers={'Authorization': f'Bearer {SLACK_TOKEN}'})

    if image_response.status_code != 200:
        return f":x: Failed to download image. Status: {image_response.status_code}"

    image_b64 = base64.b64encode(image_response.content).decode('utf-8')
    payload = {
        "image_data": f"data:image/jpeg;base64,{image_b64}"
    }

    print("📤 Sending to Tili
