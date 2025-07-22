import os
import json
import base64
import requests
from flask import Flask, request, make_response

app = Flask(__name__)

# === CONFIGURATION ===
SLACK_TOKEN = os.environ.get("SLACK_TOKEN")
TILITER_API_KEY = os.environ.get("TILITER_API_KEY")
TILITER_URL = 'https://api.ai.vision.tiliter.com/api/v1/inference/receipt-processor'

processed_events = set()

@app.route("/")
def health():
    return "Receipt Slack bot is running.", 200

@app.route("/events", methods=["POST"])
def slack_events():
    data = request.json
    print("📩 Incoming Slack event:")
    print(json.dumps(data, indent=2))

    # Slack challenge verification
    if data.get("type") == "url_verification":
        return make_response(data["challenge"], 200, {"Content-Type": "text/plain"})

    # Ignore duplicate events
    event_id = data.get("event_id")
    if event_id in processed_events:
        print("⏩ Duplicate event ignored.")
        return make_response("Duplicate", 200)
    processed_events.add(event_id)

    if data.get("type") == "event_callback":
        event = data.get("event", {})
        if event.get("type") == "message" and 'files' in event:
            for file in event['files']:
                if file.get('mimetype', '').startswith('image/'):
                    image_url = file['url_private']
                    channel = event['channel']
                    thread_ts = event['ts']
                    result = handle_image(image_url)
                    post_to_slack(channel, thread_ts, result)

        return make_response("OK", 200)

    return make_response("Ignored", 200)

def handle_image(image_url):
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
            'X-API-Key': TILITER_API_KEY,
            'Content-Type': 'application/json'
        },
        json=payload
    )

    if response.status_code != 200:
        return f":x: Tiliter API error {response.status_code}: {response.text}"

    try:
        result = response.json().get("result", {})

        merchant = result.get("merchant", "Unknown")
        date = result.get("date", "N/A")
        total = result.get("total_amount", "N/A")
        tax = result.get("tax", "N/A")
        address = result.get("address", "N/A")
        items = result.get("items", [])

        lines = [f"🧾 *Receipt Details:*"]
        lines.append(f"- Merchant: *{merchant}*")
        lines.append(f"- Date: *{date}*")
        lines.append(f"- Total: *{total}*")

        if tax and tax != "N/A":
            lines.append(f"- Tax: {tax}")
        if address and address != "N/A":
            lines.append(f"- Address: {address}")

        if items:
            lines.append(f"\n*🛒 Items:*")
            for item in items:
                desc = item.get("description", "Unnamed")
                price = item.get("price", "€0.00")
                lines.append(f"• {desc} — {price}")

        return "\n".join(lines)

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
            'text': f"{message}"
        }
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
