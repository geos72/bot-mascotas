from flask import Flask, request, jsonify
import os, json, re, time
import requests
from collections import deque

# ---------- Config ----------
PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")   # token de la página
VERIFY_TOKEN      = os.getenv("VERIFY_TOKEN")
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY")

# OpenAI (SDK v1.x)
try:
    from openai import OpenAI
    oai = OpenAI(api_key=OPENAI_API_KEY)
except Exception as e:
    oai = None
    print("⚠️ OpenAI SDK no disponible:", e)

app = Flask(__name__)

# ---------- Carga de catálogos ----------
def load_json(filename, default):
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ No se pudo leer {filename}: {e}")
        return default

PRODUCTS = load_json("products.json", [])
SHIPPING = load_json("shipping_rules.json", {
    "city_25_zones": [],
    "city_30_zones": [],
    "departments": [],
    "prices": {"city_25": 25, "city_30": 30, "department": 35}
})

# Índice simple para búsqueda por keywords
def normalize(t): 
    return re.sub(r"[^a-z0-9áéíóúñ ]","", t.lower())

INDEX = []
for p in PRODUCTS:
    keys = set()
    if "nombre" in p:   keys |= set(normalize(p["nombre"]).split())
    if "keywords" in p: 
        for kw in p["keywords"]:
            keys |= set(normalize(kw).split())
    INDEX.append({"ref": p, "keys": keys})

# ---------- Memoria corta por usuario ----------
# Guarda últimas N interacciones para saludo y contexto simple
MEMORY = {}  # user_id -> deque([{"role":"user/assistant","content":"..."}])
MAX_MEMORY = 6
GREETED   = {}  # user_id -> timestamp de último saludo

def remember(user_id, role, content):
    q = MEMORY.setdefault(user_id, deque(maxlen=MAX_MEMORY))
    q.append({"role": role, "content": content})

# ---------- Utilidades envío FB/IG ----------
FB_MSG_URL = "https://graph.facebook.com/v17.0/me/messages"

def send_text(psid, text, messaging_product="facebook"):
    payload = {
        "recipient": {"id": psid},
        "message": {"text": text},
        "messaging_type": "RESPONSE"
    }
    # Para IG se debe incluir el campo messaging_product
    if messaging_product == "instagram":
        payload["messaging_product"] = "instagram"
    r = requests.post(
        FB_MSG_URL,
        params={"access_token": PAGE_ACCESS_TOKEN},
        json=payload,
        timeout=15
    )
    if r.status_code >= 400:
        print("❌ Error FB text:", r.status_code, r.text)

def send_image(psid, image_url, messaging_product="facebook"):
    payload = {
        "recipient": {"id": psid},
        "message": {
            "attachment": {
                "type": "image",
                "payload": {"url": image_url, "is_reusable": True}
            }
        },
        "messaging_type": "RESPONSE"
    }
    if messaging_product == "instagram":
        payload["messaging_product"] = "instagram"
    r = requests.post(
        FB_MSG_URL,
        params={"access_token": PAGE_ACCESS_TOKEN},
        json=payload,
        timeout=20
    )
    if r.status_code >= 400:
        print("❌ Error FB image:", r.status_code, r.text)

# ---------- Lógica de dominio ----------
def find_products_by_text(text):
    txt = normalize(text)
    words = set(txt.split())
    # coincidencias por intersección de keywords
    scored = []
    for item in INDEX:
        score = len(words & item["keys"])
        if score:
            scored.append((score, item["ref"]))
    scored.sort(reverse=True, key=lambda x: x[0])
    results = [ref for _, ref in scored]
    # también chequeo por nombre completo incluido
    for p in PRODUCTS:
        if normalize(p.get("nombre","")) in txt:
            if p not in results:
                results.insert(0, p)
    return results[:5]

ZONA_RE = re.compile(r"\bzona\s*(\d{1,2})\b", re.IGNORECASE)

def shipping_quote(text):
    """Devuelve (monto, motivo) o (None,None) si no encontró."""
    prices = SHIPPING.get("prices", {})
    # detectar zona
    m = ZONA_RE.search(text)
    if m:
        z = int(m.group(1))
        if z in SHIPPING.get("city_30_zones", []):
            return prices.get("city_30", 30), f"Zona {z} (tarifa urbana especial)"
        if z in SHIPPING.get("city_25_zones", []):
            return prices.get("city_25", 25), f"Zona {z} (tarifa urbana estándar)"
        # si es una zona 1..25 no listada, asumir estándar
        if 1 <= z <= 25:
            return prices.get("city_25", 25), f"Zona {z} (tarifa urbana por defecto)"
    # detectar departamento
    txt = normalize(text)
    for dep in SHIPPING.get("departments", []):
        if normalize(dep) in txt:
            return prices.get("department", 35), dep
    return None, None

def wants_image(text):
    return any(w in normalize(text) for w in ["foto","imagen","foto del","ver imagen","ver foto","muestra","muéstrame"])

# ---------- OpenAI fallback ----------
SYSTEM_PROMPT = (
    "Eres el asistente de ventas de Pet Plus (accesorios y productos para mascotas en Guatemala). "
    "Sé claro, corto y útil. Si te preguntan por envíos, solicita zona o departamento. "
    "Si no estás seguro, pide un dato extra antes de inventar."
)

def llm_reply(history, user_msg):
    if not oai or not OPENAI_API_KEY:
        # Respuesta básica si no hay OpenAI disponible
        return "Gracias por tu mensaje. ¿Podrías darme un poco más de detalle para ayudarte mejor?"
    msgs = [{"role":"system","content": SYSTEM_PROMPT}]
    msgs += history[-(MAX_MEMORY-2):]
    msgs.append({"role":"user","content": user_msg})
    try:
        resp = oai.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=msgs,
            temperature=0.3
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print("⚠️ OpenAI error:", e)
        return "Entendido. Déjame confirmar un detalle para ayudarte mejor."

# ---------- Web ----------
@app.route("/")
def root():
    return "Bot de Mascotas activo"

@app.route("/webhook", methods=["GET","POST"])
def webhook():
    if request.method == "GET":
        # verificación
        if request.args.get("hub.verify_token") == VERIFY_TOKEN:
            return request.args.get("hub.challenge","")
        return "Token inválido", 403

    # POST: eventos
    data = request.get_json(force=True, silent=True) or {}
    # print(json.dumps(data, indent=2))  # útil para depurar
    for entry in data.get("entry", []):
        for event in entry.get("messaging", []):
            psid = event.get("sender",{}).get("id")
            if not psid:
                continue

            # Detectar si viene de IG o Messenger
            messaging_product = event.get("message",{}).get("messaging_product") \
                                or event.get("messaging_product") \
                                or "facebook"
            if messaging_product not in ("facebook","instagram"):
                messaging_product = "facebook"

            # Ignorar echos
            if event.get("message",{}).get("is_echo"):
                continue

            text = ""
            attachments = event.get("message",{}).get("attachments",[])
            if "message" in event:
                text = event["message"].get("text","").strip()

            # Saludo automático si no hemos saludado en los últimos 12h
            now = time.time()
            if psid not in GREETED or (now - GREETED[psid] > 12*3600):
                send_text(psid, "¡Hola! Bienvenid@ a Pet Plus ¿Cómo podemos ayudarle?",
                          messaging_product=messaging_product)
                GREETED[psid] = now

            if text:
                handle_text(psid, text, messaging_product)
            elif attachments:
                # Por ahora no hacemos visión, guiamos al cliente
                send_text(psid,
                          "Recibí tu imagen 👌. Para ubicar el producto más rápido, "
                          "¿me dices el nombre o alguna palabra clave (ej. “rascador”, “cepillo”, “pingüino”)?",
                          messaging_product=messaging_product)

    return "EVENT_RECEIVED", 200

def handle_text(psid, text, mp):
    remember(psid, "user", text)

    # 1) Costo de envío si el texto trae zona/dep
    price, label = shipping_quote(text)
    if price is not None:
        send_text(
            psid,
            f"El envío a **{label}** es de **Q{price}**. "
            "Si me confirmas la dirección y el producto, preparo el total y la entrega. 🚚",
            messaging_product=mp
        )
        remember(psid, "assistant", f"Envío a {label}: Q{price}")
        return

    # 2) Búsqueda de producto
    matches = find_products_by_text(text)
    if matches:
        # ¿pidió foto?
        if wants_image(text):
            prod = matches[0]
            if prod.get("imagen"):
                send_image(psid, prod["imagen"], messaging_product=mp)
            send_text(psid,
                      f"{prod.get('nombre','Producto')} — {prod.get('precio','')}\n"
                      f"{prod.get('descripcion','')}".strip(),
                      messaging_product=mp)
            remember(psid, "assistant", f"Mostró {prod.get('nombre','')}")
            return

        # Si hay 1 match: detallo
        if len(matches) == 1:
            p = matches[0]
            lines = [f"**{p.get('nombre','Producto')}**",
                     p.get("precio",""),
                     p.get("descripcion","").strip()]
            msg = "\n".join([l for l in lines if l])
            send_text(psid, msg, messaging_product=mp)
            if p.get("imagen"):
                send_image(psid, p["imagen"], messaging_product=mp)
            send_text(psid,
                      "¿Te confirmo disponibilidad y envío? Puedes decirme tu *zona* o *departamento* para el costo de envío.",
                      messaging_product=mp)
            remember(psid, "assistant", f"Detalles de {p.get('nombre','')}")
            return
        else:
            # Varios matches: pedir que elija
            opciones = [f"- {p.get('nombre','(sin nombre)')}" for p in matches[:5]]
            send_text(psid,
                      "Encontré varias opciones similares:\n" + "\n".join(opciones) +
                      "\n\n¿Sobre cuál te gustaría más info o foto?",
                      messaging_product=mp)
            remember(psid, "assistant", "Ofreció lista de coincidencias")
            return

    # 3) Fallback con LLM (respuesta general)
    hist = list(MEMORY.get(psid, []))
    reply = llm_reply(hist, text)
    send_text(psid, reply, messaging_product=mp)
    remember(psid, "assistant", reply)

# ---------- Run ----------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
