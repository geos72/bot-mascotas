from flask import Flask, request, jsonify
import os
import requests
import traceback
from datetime import datetime

# === OpenAI SDK (>=1.0) ===
try:
    from openai import OpenAI
except ImportError:
    # Si alguna vez ves error de import, asegúrate que en requirements.txt tengas: openai>=1.40.0
    raise

app = Flask(__name__)

# ==== Config ====
PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN", "")
VERIFY_TOKEN      = os.getenv("VERIFY_TOKEN", "")
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL      = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")  # cámbialo si quieres otro modelo
IG_BUSINESS_ID    = os.getenv("IG_BUSINESS_ID", "")            # opcional, ayuda a distinguir IG vs Messenger

client = OpenAI(api_key=OPENAI_API_KEY)

# ==== Utilidades ====

def log(*args):
    print(datetime.utcnow().isoformat(), *args, flush=True)

def detectar_plataforma(entry_id: str) -> str:
    """
    Heurística simple:
    - Si configuraste IG_BUSINESS_ID y coincide con entry['id'] => 'instagram'
    - En caso contrario => 'messenger'
    """
    if IG_BUSINESS_ID and entry_id == IG_BUSINESS_ID:
        return "instagram"
    return "messenger"

def generar_respuesta(user_text: str) -> str:
    """
    Llama al modelo de OpenAI y devuelve el texto.
    """
    # Prompts mínimos para mantener el costo bajo y respuestas cortas.
    messages = [
        {"role": "system", "content": "Eres un asistente de atención para una tienda de mascotas. Responde de forma breve, amable y útil."},
        {"role": "user", "content": user_text.strip()[:2000]}  # recorta por seguridad
    ]
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=messages,
        temperature=0.5,
        max_tokens=350,
    )
    return resp.choices[0].message.content.strip()

def enviar_mensaje(recipient_id: str, texto: str, plataforma: str):
    """
    Envía el mensaje via Graph API.
    Para Instagram hay que poner messaging_product='instagram'.
    Para Messenger, 'messenger'.
    """
    url = "https://graph.facebook.com/v18.0/me/messages"
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": texto},
        "messaging_type": "RESPONSE",
        "messaging_product": "instagram" if plataforma == "instagram" else "messenger",
    }
    headers = {"Content-Type": "application/json"}
    params = {"access_token": PAGE_ACCESS_TOKEN}

    r = requests.post(url, json=payload, headers=headers, params=params, timeout=20)
    log(f"➡️  Envío a {plataforma}: {r.status_code} {r.text}")

# ==== Rutas ====

@app.route("/")
def home():
    return "Bot de Mascotas activo", 200

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        # Verificación de Webhook (Messenger/Instagram usan el mismo formato)
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == VERIFY_TOKEN:
            log("✅ Webhook verificado")
            return challenge, 200
        log("❌ Webhook verificación fallida")
        return "Token inválido", 403

    # POST: eventos
    try:
        data = request.get_json(force=True, silent=True) or {}
        log("📩 Evento recibido:", data)

        for entry in data.get("entry", []):
            entry_id = entry.get("id", "")
            plataforma = detectar_plataforma(entry_id)

            # Para Messenger/Instagram, los eventos vienen en entry['messaging']
            for messaging_event in entry.get("messaging", []):
                # Ignora echos y otros tipos
                if "message" not in messaging_event:
                    continue
                if messaging_event["message"].get("is_echo"):
                    continue

                sender_id = messaging_event["sender"]["id"]
                text = messaging_event["message"].get("text", "")

                # Si no hay texto (puede ser adjunto), contesta algo genérico
                if not text and messaging_event["message"].get("attachments"):
                    text = "Recibí tu mensaje. ¿Podrías escribirme en texto lo que necesitas?"

                if text:
                    try:
                        respuesta = generar_respuesta(text)
                    except Exception as e:
                        log("❌ Error generando respuesta OpenAI:", e)
                        traceback.print_exc()
                        respuesta = "Ahora mismo tengo un problemita técnico. ¿Podrías intentar de nuevo en un momento, por favor?"

                    enviar_mensaje(sender_id, respuesta, plataforma)

        # Responder rápido 200 para que Meta no reintente
        return "EVENT_RECEIVED", 200

    except Exception as e:
        log("❌ Error procesando webhook:", e)
        traceback.print_exc()
        # Aun con error, devolver 200 evita reintentos agresivos; usa 500 si quieres que Meta reintente.
        return "OK", 200

# ==== Requisitos de Instagram: desautorización y eliminación de datos ====

@app.route("/deauthorize", methods=["POST"])
def deauthorize():
    """
    Meta llama aquí cuando el usuario desautoriza la app.
    """
    try:
        data = request.form.to_dict()
        log("🔴 Desautorización IG:", data)
        # TODO: elimina datos de ese usuario en tu BD si guardas algo
        return "Usuario desautorizado", 200
    except Exception as e:
        log("❌ Error en /deauthorize:", e)
        return "Error", 500

@app.route("/delete-data", methods=["GET", "POST"])
def delete_data():
    """
    Meta llama aquí cuando el usuario solicita eliminación de datos.
    """
    try:
        if request.method == "GET":
            data = request.args.to_dict()
        else:
            data = request.form.to_dict()

        log("🗑 Solicitud de eliminación de datos IG:", data)
        # TODO: elimina datos en tu BD si guardas algo

        # Respuesta con información de confirmación (formato sugerido por Meta)
        return jsonify({
            "url": "https://bot-mascotas.onrender.com",   # puedes poner una página tuya de confirmación
            "confirmation_code": "datos_eliminados"
        }), 200
    except Exception as e:
        log("❌ Error en /delete-data:", e)
        return "Error", 500

# ==== Arranque ====

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)




