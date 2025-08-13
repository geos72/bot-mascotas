# main.py
import os
import logging
from flask import Flask, request, jsonify
import requests

# --- OpenAI SDK moderno (>=1.0) ---
from openai import OpenAI
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")

GRAPH_URL = "https://graph.facebook.com/v17.0/me/messages"

@app.route("/")
def home():
    return "Bot activo para Messenger + Instagram", 200

# ---------- Webhook (verificación + recepción) ----------
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        # Verificación de Meta
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")

        if mode == "subscribe" and token == VERIFY_TOKEN:
            logging.info("✅ Webhook verificado correctamente")
            return challenge, 200
        logging.warning("❌ Verificación fallida")
        return "Token inválido", 403

    # POST: eventos
    try:
        data = request.get_json(silent=True, force=True) or {}
        # Estructura estándar: { "object": "...", "entry": [ { "messaging": [...] } ] }
        for entry in data.get("entry", []):
            for event in entry.get("messaging", []):
                handle_event(event)
    except Exception as e:
        logging.exception(f"❌ Error procesando POST /webhook: {e}")
        # Siempre devolver 200 para que Meta no reintente infinitamente en caso de errores puntuales
        return "EVENT_RECEIVED_WITH_ERRORS", 200

    return "EVENT_RECEIVED", 200


# ---------- Procesar cada evento ----------
def handle_event(event: dict):
    sender_id = event.get("sender", {}).get("id")
    if not sender_id:
        return

    # Platform puede venir en el propio evento
    platform = event.get("messaging_product") or infer_platform(event)
    # Texto del usuario (o postback title como fallback)
    user_text = extract_text(event)

    logging.info(f"📩 From {platform} | sender: {sender_id} | text: {user_text!r}")

    if not user_text:
        # Si no hay texto (puede ser adjunto/imagen), responde algo simple
        send_message(sender_id, "Gracias por tu mensaje 😊 ¿Cómo puedo ayudarte?", platform)
        return

    # ---- Llama a OpenAI ----
    try:
        ai = client.chat.completions.create(
            model="gpt-3.5-turbo",  # puedes cambiar a "gpt-4o-mini" si lo prefieres
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Eres un asistente amable para una tienda de mascotas. "
                        "Responde breve, claro y útil. Si preguntan por servicios, "
                        "productos, horarios o ubicación, ofrece detalles concretos."
                    ),
                },
                {"role": "user", "content": user_text},
            ],
            temperature=0.4,
            max_tokens=300,
        )
        reply_text = ai.choices[0].message.content.strip()
    except Exception as e:
        logging.exception(f"❌ Error llamando a OpenAI: {e}")
        reply_text = "Perdón, tuve un problema generando la respuesta. ¿Podrías repetirlo?"

    # ---- Responder por la plataforma correcta ----
    send_message(sender_id, reply_text, platform)


def extract_text(event: dict) -> str:
    """Obtiene texto del evento (mensaje o postback)."""
    msg = event.get("message", {})
    if "text" in msg:
        return msg["text"]
    # Quick replies
    qr = msg.get("quick_reply", {})
    if "payload" in qr:
        return str(qr["payload"])
    # Postbacks (botones)
    postback = event.get("postback", {})
    if "title" in postback:
        return str(postback["title"])
    if "payload" in postback:
        return str(postback["payload"])
    return ""


def infer_platform(event: dict) -> str:
    """
    Si Meta no manda 'messaging_product', inferimos:
    - Instagram: suele mandarlo, pero por si acaso… comprobamos algunos indicios.
    Por defecto devolvemos 'messenger'.
    """
    # Meta suele incluir 'messaging_product' = 'instagram'/'messenger'.
    # Si no está, asumimos messenger para mantener compatibilidad.
    return "messenger"


def send_message(psid: str, text: str, platform: str):
    """
    Envía el mensaje usando Graph API.
    Para Instagram es OBLIGATORIO incluir "messaging_product": "instagram".
    Para Messenger también lo ponemos (es válido y explícito).
    """
    payload = {
        "messaging_product": "instagram" if platform == "instagram" else "messenger",
        "recipient": {"id": psid},
        "message": {"text": text},
    }
    headers = {"Content-Type": "application/json"}

    try:
        r = requests.post(
            GRAPH_URL,
            params={"access_token": PAGE_ACCESS_TOKEN},
            json=payload,
            headers=headers,
            timeout=15,
        )
        logging.info(f"➡️  Enviado ({platform}) {r.status_code}: {r.text}")
    except Exception as e:
        logging.exception(f"❌ Error enviando a {platform}: {e}")


# ---------- Run ----------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)


