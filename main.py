from flask import Flask, request
import requests
import openai
import os
import traceback

app = Flask(__name__)

PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = openai.OpenAI(api_key=OPENAI_API_KEY)

@app.route('/')
def home():
    return "Bot de Mascotas activo"

@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')
        if token == VERIFY_TOKEN:
            return challenge
        return 'Token inválido', 403

    elif request.method == 'POST':
        try:
            data = request.get_json()
            for entry in data.get('entry', []):
                for messaging_event in entry.get('messaging', []):
                    sender_id = messaging_event['sender']['id']
                    if 'message' in messaging_event and 'text' in messaging_event['message']:
                        user_message = messaging_event['message']['text']
                        respuesta = generar_respuesta(user_message)
                        enviar_mensaje(sender_id, respuesta)
            return "EVENT_RECEIVED", 200
        except Exception as e:
            print("❌ Error procesando el webhook:", e)
            traceback.print_exc()
            return "Error interno", 500

def generar_respuesta(mensaje):
    response = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": mensaje}]
    )
    return response.choices[0].message.content

def enviar_mensaje(recipient_id, mensaje):
    url = f"https://graph.facebook.com/v17.0/me/messages?access_token={PAGE_ACCESS_TOKEN}"
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": mensaje}
    }
    headers = {"Content-Type": "application/json"}
    response = requests.post(url, json=payload, headers=headers)
    print("🟢 Respuesta enviada a Facebook:", response.status_code, response.text)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

