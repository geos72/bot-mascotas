import os
import json
import sqlite3
import smtplib
import traceback
from email.mime.text import MIMEText
from email.utils import formataddr
from datetime import datetime, timedelta

from flask import Flask, request, jsonify
import requests

# ====== OpenAI (cliente moderno) ======
try:
    from openai import OpenAI
    oai = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
except Exception:
    oai = None

app = Flask(__name__)

PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")
VERIFY_TOKEN      = os.getenv("VERIFY_TOKEN")
BUSINESS_IG_ID    = os.getenv("BUSINESS_IG_ID")

SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
SUPPORT_EMAIL_TO = os.getenv("SUPPORT_EMAIL_TO")

GRAPH = "https://graph.facebook.com/v19.0"

# --------- Utilidades DB (memoria) ----------
DB_PATH = "state.db"

def db_init():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS memory (
        user_id TEXT PRIMARY KEY,
        last_updated TEXT,
        context TEXT
    );
    """)
    conn.commit()
    conn.close()

def get_context(user_id, max_minutes=120):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT last_updated, context FROM memory WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return []
    last, ctx = row
    try:
        last_dt = datetime.fromisoformat(last)
        if datetime.utcnow() - last_dt > timedelta(minutes=max_minutes):
            return []
    except Exception:
        return []
    try:
        return json.loads(ctx)
    except Exception:
        return []

def save_context(user_id, messages):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("REPLACE INTO memory(user_id,last_updated,context) VALUES (?,?,?)",
              (user_id, datetime.utcnow().isoformat(), json.dumps(messages)))
    conn.commit()
    conn.close()

db_init()

# --------- Carga catálogo ----------
def load_products():
    try:
        with open("products.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

PRODUCTS = load_products()

def search_products(query):
    q = query.lower()
    hits = []
    for p in PRODUCTS:
        texto = f"{p.get('nombre','')} {p.get('categoria','')} {p.get('descripcion','')}".lower()
        if q in texto:
            hits.append(p)
        if len(hits) >= 5:
            break
    return hits

# --------- Envío de mensajes Facebook/IG ----------
def send_fb(url, payload):
    headers = {"Content-Type": "application/json"}
    r = requests.post(url, json=payload, headers=headers, timeout=20)
    return r

def send_text_psid(psid, text):
    url = f"{GRAPH}/me/messages?access_token={PAGE_ACCESS_TOKEN}"
    payload = {
        "recipient": {"id": psid},
        "message": {"text": text}
    }
    return send_fb(url, payload)

def send_image_psid(psid, image_url):
    url = f"{GRAPH}/me/messages?access_token={PAGE_ACCESS_TOKEN}"
    payload = {
        "recipient": {"id": psid},
        "message": {
            "attachment": {
                "type": "image",
                "payload": {"url": image_url, "is_reusable": True}
            }
        }
    }
    return send_fb(url, payload)

def send_quick_replies(psid, text, replies):
    url = f"{GRAPH}/me/messages?access_token={PAGE_ACCESS_TOKEN}"
    payload = {
        "recipient": {"id": psid},
        "message": {
            "text": text,
            "quick_replies": [
                {"content_type":"text","title":t,"payload":p} for t,p in replies
            ]
        }
    }
    return send_fb(url, payload)

def send_buttons(psid, text, buttons):
    """buttons = [{'type':'postback','title':'Productos','payload':'MENU_PRODUCTOS'}, ...]"""
    url = f"{GRAPH}/me/messages?access_token={PAGE_ACCESS_TOKEN}"
    payload = {
        "recipient": {"id": psid},
        "message": {
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "button",
                    "text": text,
                    "buttons": buttons
                }
            }
        }
    }
    return send_fb(url, payload)

# --------- Menú / Get Started / Icebreakers ----------
def messenger_profile_setup():
    url = f"{GRAPH}/me/messenger_profile?access_token={PAGE_ACCESS_TOKEN}"
    data = {
        "get_started": {"payload": "GET_STARTED"},
        "greeting": [{"locale": "default", "text": "¡Hola! Soy tu asistente de Pet Plus 🐾"}],
        "persistent_menu": [{
            "locale": "default",
            "composer_input_disabled": False,
            "call_to_actions": [
                {"type":"postback","title":"🛍 Productos","payload":"MENU_PRODUCTOS"},
                {"type":"postback","title":"🕒 Horarios","payload":"MENU_HORARIOS"},
                {"type":"postback","title":"📍 Ubicación","payload":"MENU_UBICACION"},
                {"type":"postback","title":"👤 Humano","payload":"MENU_HUMANO"}
            ]
        }]
    }
    r = requests.post(url, json=data, timeout=20)
    return r.status_code, r.text

def instagram_icebreakers_setup():
    # Preguntas frecuentes que aparecen antes del primer mensaje en IG
    url = f"{GRAPH}/{BUSINESS_IG_ID}/icebreakers?access_token={PAGE_ACCESS_TOKEN}"
    data = {
        "ice_breakers": json.dumps([
            {"question":"Ver productos","payload":"MENU_PRODUCTOS"},
            {"question":"Horarios","payload":"MENU_HORARIOS"},
            {"question":"Ubicación","payload":"MENU_UBICACION"},
            {"question":"Hablar con humano","payload":"MENU_HUMANO"}
        ])
    }
    r = requests.post(url, data=data, timeout=20)
    return r.status_code, r.text

@app.route("/setup", methods=["POST","GET"])
def setup():
    try:
        s1 = messenger_profile_setup()
        s2 = instagram_icebreakers_setup()
        return jsonify({"messenger_profile": s1, "ig_icebreakers": s2})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --------- Fallback a humano por email ----------
def send_support_email(subject, body):
    if not all([SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SUPPORT_EMAIL_TO]):
        return False, "SMTP no configurado"
    msg = MIMEText(body, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr(("Bot Pet Plus", SMTP_USER))
    msg["To"] = SUPPORT_EMAIL_TO
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, [SUPPORT_EMAIL_TO], msg.as_string())
        return True, "OK"
    except Exception as e:
        return False, str(e)

# --------- Lógica del bot ----------
HELP_TEXT = (
    "Puedo ayudarte con:\n"
    "• Productos y precios\n"
    "• Horarios y ubicación\n"
    "• Recomendaciones para tu mascota\n\n"
    "Usa el menú o escribe tu consulta 🐶🐱"
)

def handle_postback(psid, payload):
    if payload == "GET_STARTED":
        send_buttons(psid, "¡Bienvenido a Pet Plus! ¿Qué necesitas hoy?",
                     [
                        {"type":"postback","title":"🛍 Productos","payload":"MENU_PRODUCTOS"},
                        {"type":"postback","title":"🕒 Horarios","payload":"MENU_HORARIOS"},
                        {"type":"postback","title":"📍 Ubicación","payload":"MENU_UBICACION"}
                     ])
    elif payload == "MENU_PRODUCTOS":
        send_quick_replies(psid, "¿Qué buscas? (ej. alimento, juguete, shampoo)",
                           [("Alimento","BUSCAR:alimento"),("Juguetes","BUSCAR:juguetes"),("Shampoo","BUSCAR:shampoo")])
    elif payload == "MENU_HORARIOS":
        send_text_psid(psid, "Abrimos Lun-Dom 9:00–19:00.")
    elif payload == "MENU_UBICACION":
        send_text_psid(psid, "Estamos en Av. Principal 123, Col. Centro. https://maps.google.com/?q=Av+Principal+123")
    elif payload == "MENU_HUMANO":
        ok, msg = send_support_email(
            "Escalado manual desde bot (Instagram/Messenger)",
            f"<b>PSID:</b> {psid}<br/>El usuario pidió hablar con humano."
        )
        send_text_psid(psid, "Listo. Un asesor te contactará pronto. 🙌")
    elif payload.startswith("BUSCAR:"):
        term = payload.split(":",1)[1]
        results = search_products(term)
        if results:
            for p in results:
                line = f"• {p['nombre']} - ${p['precio']}\n{p.get('descripcion','')}"
                send_text_psid(psid, line)
                if p.get("imagen"):
                    send_image_psid(psid, p["imagen"])
        else:
            send_text_psid(psid, "No encontré productos con esa búsqueda. Prueba con otra palabra. 😉")
    else:
        send_text_psid(psid, HELP_TEXT)

def generate_ai_reply(user_id, text):
    # recupera memoria
    context = get_context(user_id)
    messages = context[-10:]  # limitamos contexto
    messages.append({"role": "system", "content": "Eres un asistente de tienda de mascotas llamado Pet Plus. Responde breve y útil."})
    messages.append({"role": "user", "content": text})
    try:
        if oai is None:
            return "Estoy teniendo un detalle con el motor de IA. ¿Puedes intentar de nuevo en un momento?"
        resp = oai.chat.completions.create(
            model="gpt-4o-mini",  # cambia aquí el modelo si lo deseas
            messages=messages,
            temperature=0.3
        )
        reply = resp.choices[0].message.content.strip()
        # actualiza memoria
        messages.append({"role":"assistant","content":reply})
        save_context(user_id, messages[-20:])
        return reply
    except Exception:
        traceback.print_exc()
        return "Ahora mismo no puedo pensar 😅. Intento de nuevo enseguida."

def handle_text(psid, text):
    # Comandos rápidos
    low = text.lower().strip()
    if low in ("menu","menú"):
        handle_postback(psid, "GET_STARTED")
        return
    if any(k in low for k in ["humano","asesor","agente"]):
        handle_postback(psid, "MENU_HUMANO")
        return
    # búsqueda de productos por palabra clave
    if any(k in low for k in ["alimento","juguete","shampoo","shampú","arnés","collar"]):
        res = search_products(low)
        if res:
            send_text_psid(psid, "Esto podría interesarte:")
            for p in res:
                send_text_psid(psid, f"• {p['nombre']} - ${p['precio']}")
                if p.get("imagen"): send_image_psid(psid, p["imagen"])
            return
    # IA general
    reply = generate_ai_reply(psid, text)
    send_text_psid(psid, reply)

def handle_image(psid, image_url):
    # Si quieres describir la imagen con visión (opcional, según tu plan):
    # reply = describe_image(image_url)   # función opcional
    # Por ahora, solo confirmamos recepción
    send_text_psid(psid, "¡Gracias por la imagen! ¿Deseas que te recomiende un producto relacionado?")

# --------- Rutas ----------
@app.route("/")
def home():
    return "Bot de Mascotas activo"

@app.route("/webhook", methods=["GET","POST"])
def webhook():
    if request.method == "GET":
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if token == VERIFY_TOKEN:
            return challenge
        return "Token inválido", 403

    data = request.get_json(silent=True) or {}
    try:
        # Estructura común para Messenger e Instagram
        for entry in data.get("entry", []):
            for msg in entry.get("messaging", []):
                psid = msg.get("sender", {}).get("id")
                if not psid: 
                    continue

                # Postbacks (botones / menú / icebreakers)
                if "postback" in msg:
                    payload = msg["postback"].get("payload", "")
                    handle_postback(psid, payload)
                    continue

                # Mensajes
                if "message" in msg:
                    m = msg["message"]
                    # Adjuntos (imágenes)
                    if "attachments" in m:
                        for att in m["attachments"]:
                            if att.get("type") == "image":
                                image_url = att.get("payload", {}).get("url")
                                if image_url:
                                    handle_image(psid, image_url)
                                    break
                        continue
                    # Texto
                    text = m.get("text")
                    if text:
                        handle_text(psid, text)
        return "EVENT_RECEIVED", 200
    except Exception as e:
        print("❌ Error procesando webhook:", e)
        traceback.print_exc()
        return "Error", 500

# ---------- Arranque ----------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
