from flask import Flask, request
import os
import requests

app = Flask(__name__)

PAGE_ACCESS_TOKEN = os.environ.get("PAGE_ACCESS_TOKEN")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN")

@app.route('/')
def home():
    return "Bot de Mascotas activo"

@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        token_sent = request.args.get("hub.verify_token")
        return request.args.get("hub.challenge") if token_sent == VERIFY_TOKEN else 'Invalid verification token'
    else:
        data = request.get_json()
        if data.get("object") == "page":
            for entry in data.get("entry"):
                messaging = entry.get("messaging")
                for message in messaging:
                    if message.get("message"):
                        sender_id = message["sender"]["id"]
                        response = {"text": "¡Hola! Gracias por tu mensaje 😺"}
                        requests.post(
                            f"https://graph.facebook.com/v12.0/me/messages?access_token={PAGE_ACCESS_TOKEN}",
                            json={"recipient": {"id": sender_id}, "message": response},
                            headers={"Content-Type": "application/json"}
                        )
        return "OK", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
