import os
import json
import traceback
from flask import Flask, request, jsonify
import requests

# OpenAI SDK v1
from openai import OpenAI

app = Flask(__name__)

PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")  # puedes cambiarlo a gpt-3.5-turbo si prefieres

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

FB_GRAPH_BASE = "https://graph.facebook.com/v19.0/me/messages"


@app.route("/", methods=["GET"])
def home():
    return "Bot de Mascotas activo", 200


# --- Verificación del webhook de Meta ---
@app.route("/webhook", methods=["GET"])
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Error de verificación", 403


# --- Recepción de eventos ---
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True, silent=True) or {}
        # LOG opcional
        print("==> Webhook payload:")
        print(json.dumps(data, ensure_ascii=False, indent=2))

        # Messenger (object = "page")
        if data.get("object") == "page":
            for entry in data.get("entry", []):
                # Forma típica de Messenger
                if "messaging" in entry:
                    handle_messenger_entry(entry)

                # Por compatibilidad: algunos IG eventos también llegan aquí con changes
                if "changes" in entry:
                    handle_instagram_changes(entry)

        # Instagram (algunas integraciones pueden marcar object="instagram")
        elif data.get("object") == "instagram":
            for entry in data.get("entry", []):
                # IG puede venir como "messaging" (parecido a Messenger) o como "changes"
                if "messaging" in entry:
                    handle_instagram_messaging(entry)
                if "changes" in entry:
                    handle_instagram_changes(entry)

        return "EVENT_RECEIVED", 200

    except Exception as e:
        print("❌ Error procesando webhook:", e)
        traceback.print_exc()
        return "Error", 500


# -------------------------
#       HANDLERS
# -------------------------

def handle_messenger_entry(entry):
    """Procesa eventos tradicionales de Facebook Messenger."""
    for evt in entry.get("messaging", []):
        if "message" in evt and "text" in evt["message"]:
            sender_id = evt["sender"]["id"]
            user_text = evt["message"]["text"]
            reply_and_send(sender_id, user_text, product="messenger")


def handle_instagram_messaging(entry):
    """
    Algunos eventos de Instagram llegan en entry['messaging'] con
    'messaging_product': 'instagram'.
    """
    for evt in entry.get("messaging", []):
        product = evt.get("messaging_product")
        if product == "instagram" and "message" in evt and "text" in evt["message"]:
            sender_id = evt["sender"]["id"]
            user_text = evt["message"]["text"]
            reply_and_send(sender_id, user_text, product="instagram")


def handle_instagram_changes(entry):
    """
    Otros eventos de Instagram llegan en entry['changes'][*]['value'] con:
      value.messaging_product == 'instagram'
      value.messages -> lista de mensajes
      value.from.id -> remitente
    """
    for change in entry.get("changes", []):
        value = change.get("value", {})
        if value.get("messaging_product") == "instagram":
            msgs = value.get("messages", [])
            sender_id = None

            # intentamos detectar el sender en diferentes campos
            frm = value.get("from") or value.get("sender") or {}
            if isinstance(frm, dict):
                sender_id = frm.get("id")

            for m in msgs:
                # 'text' puede venir como dict {'body': '...'} o como str '...'
                text = (
                    (m.get("text") or {}).get("body")
                    if isinstance(m.get("text"), dict)
                    else m.get("text")
                )
                if not text:
                    text = m.get("message")  # fallback

                if sender_id and text:
                    reply_and_send(sender_id, text, product="instagram")


# -------------------------
#   CORE: OpenAI & FB/IG
# -------------------------

def reply_and_send(recipient_id: str, user_text: str, product: str):
    """
    Llama a OpenAI para generar respuesta y la envía por Graph.
    product: 'messenger' o 'instagram'
    """
    try:
        ai_text = generate_reply(user_text)
    except Exception as e:
        print("❌ Error con OpenAI:", e)
        ai_text = (
            "Lo siento, ahora mismo tengo mucha carga. ¿Puedes repetir tu pregunta "
            "en unos segundos?"
        )

    try:
        send_fb_message(recipient_id, ai_text, product=product)
    except Exception as e:
        print(f"❌ Error enviando mensaje a {product}:", e)


def generate_reply(prompt: str) -> str:
    """
    Usa el SDK v1 de OpenAI (chat.completions).
    """
    try:
        completion = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Eres un asistente amable para una tienda de mascotas. "
                        "Responde en español de manera breve y útil."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.6,
            max_tokens=300,
        )
        return completion.choices[0].message.content.strip()
    except Exception as e:
        # Manejo específico de cuota/429
        msg = str(e)
        if "insufficient_quota" in msg or "429" in msg:
            return (
                "Ahora mismo no puedo consultar el motor de IA por límite de uso. "
                "Intentémoslo de nuevo en un momento, por favor."
            )
        raise


def send_fb_message(recipient_id: str, text: str, product: str = "messenger"):
    """
    Envía el mensaje usando Graph. Para Instagram hay que incluir
    'messaging_product': 'instagram'.
    """
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": text},
        "messaging_type": "RESPONSE",
    }

    if product == "instagram":
        payload["messaging_product"] = "instagram"

    params = {"access_token": PAGE_ACCESS_TOKEN}
    headers = {"Content-Type": "application/json"}

    r = requests.post(FB_GRAPH_BASE, params=params, json=payload, headers=headers, timeout=15)
    print(f"➡️ Enviado a {product}: {r.status_code} {r.text}")
    r.raise_for_status()


# -------------------------
#        RUN (Render)
# -------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)




