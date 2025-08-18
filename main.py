# main.py
from flask import Flask, request, jsonify
import os, json, re, unicodedata, requests
from datetime import datetime, timedelta

app = Flask(__name__)

# ======= ENV =======
PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")

# ======= CARGA DE CATÁLOGO Y ENVÍOS =======
def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ No se pudo leer {path}: {e}")
        return default

PRODUCTS = load_json("products.json", [])
SHIPPING = load_json("shipping_rules.json", {
    "ciudad_zonas_validas": list(range(1,26)),
    "costo_zona_normal": 25,
    "costo_zona_premium": 30,
    "zonas_premium": [],
    "costo_departamento": 35,
    "departamentos_gt": []
})

# Pre-normaliza catálogo para matching rápido
def strip_accents(s):
    return ''.join(c for c in unicodedata.normalize('NFD', s)
                   if unicodedata.category(c) != 'Mn')

def norm(s):
    if not isinstance(s, str):
        return ""
    s = strip_accents(s.lower())
    s = re.sub(r"[^a-z0-9\s]", " ", s)  # quita puntuación
    s = re.sub(r"\s+", " ", s).strip()
    return s

CATALOG = []
for p in PRODUCTS:
    nombre = p.get("nombre","")
    kws = p.get("keywords",[])
    tokens = set(norm(nombre).split())
    for k in kws:
        tokens.update(norm(k).split())
    CATALOG.append({
        "sku": p.get("sku",""),
        "nombre": nombre,
        "nombre_norm": norm(nombre),
        "tokens": tokens,
        "precio": p.get("precio",{}),
        "descripcion": p.get("descripcion",""),
        "imagen": p.get("imagen","")
    })

# ======= SESIONES EN MEMORIA =======
SESSIONS = {}  # sender_id -> {"greeted": bool, "stage": str, "product": dict|None, "last_seen": datetime}

def get_session(user_id):
    s = SESSIONS.get(user_id)
    if not s:
        s = {"greeted": False, "stage": "start", "product": None, "last_seen": datetime.utcnow()}
        SESSIONS[user_id] = s
    else:
        s["last_seen"] = datetime.utcnow()
    return s

# Limpieza básica de sesiones viejas (opcional)
def cleanup_sessions(max_minutes=120):
    now = datetime.utcnow()
    to_del = []
    for uid, s in SESSIONS.items():
        if now - s.get("last_seen", now) > timedelta(minutes=max_minutes):
            to_del.append(uid)
    for uid in to_del:
        del SESSIONS[uid]

# ======= FB SEND =======
GRAPH_URL = "https://graph.facebook.com/v17.0/me/messages"

def send_text(recipient_id, text):
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": text}
    }
    r = requests.post(
        GRAPH_URL,
        params={"access_token": PAGE_ACCESS_TOKEN},
        json=payload,
        timeout=20
    )
    print("➡️ FB text:", r.status_code, r.text)

def send_image(recipient_id, image_url):
    if not image_url:
        return
    payload = {
        "recipient": {"id": recipient_id},
        "message": {
            "attachment": {
                "type": "image",
                "payload": {"url": image_url, "is_reusable": True}
            }
        }
    }
    r = requests.post(
        GRAPH_URL,
        params={"access_token": PAGE_ACCESS_TOKEN},
        json=payload,
        timeout=20
    )
    print("➡️ FB image:", r.status_code, r.text)

# ======= INTENCIÓN Y MATCH =======
PRICE_WORDS = {"precio","cuanto","vale","cuánto","costo","cuesta"}
IMAGE_WORDS = {"foto","imagen","muestra","ver","enséñame","mostrame"}
SHIP_WORDS  = {"envio","envío","entrega","mandan","reparto","enviar","llegan","envian"}
HELP_WORDS  = {"ayuda","informacion","información"}

def detect_intent(text):
    t = norm(text)
    words = set(t.split())
    if words & PRICE_WORDS:
        return "price"
    if words & IMAGE_WORDS:
        return "image"
    if words & SHIP_WORDS:
        return "shipping"
    if words & HELP_WORDS:
        return "help"
    return "unknown"

def find_product(text):
    t = norm(text)
    tokens = set(t.split())
    best = None
    best_score = 0
    for p in CATALOG:
        score = len(tokens & p["tokens"])
        # Coincidencia fuerte si menciona el nombre normalizado
        if p["nombre_norm"] in t:
            score += 3
        if score > best_score:
            best = p
            best_score = score
    # umbral mínimo: 1 token/keyword o nombre
    return best if best_score >= 1 else None

# ======= SHIPPING =======
ZONAS_PREMIUM = {norm(z) for z in SHIPPING.get("zonas_premium", [])}
DEPTOS = {norm(d) for d in SHIPPING.get("departamentos_gt", [])}
ZONAS_VALIDAS = set(SHIPPING.get("ciudad_zonas_validas", []))
COSTO_NORMAL = SHIPPING.get("costo_zona_normal", 25)
COSTO_PREMIUM = SHIPPING.get("costo_zona_premium", 30)
COSTO_DEPTO = SHIPPING.get("costo_departamento", 35)

def compute_shipping(user_text):
    t = norm(user_text)
    # 1) zona N
    m = re.search(r"\bzona\s+(\d{1,2})\b", t)
    if m:
        z = int(m.group(1))
        if z in ZONAS_VALIDAS:
            # premium por nombre de colonia/municipio "complicado"
            for prem in ZONAS_PREMIUM:
                if prem in t:
                    return f"Zona {z} (premium) Q{COSTO_PREMIUM}", COSTO_PREMIUM
            return f"Zona {z} Q{COSTO_NORMAL}", COSTO_NORMAL

    # 2) Departamento
    for d in DEPTOS:
        if d in t:
            # “Guatemala” puede ser depto o ciudad; si dice "departamento de guatemala" aplicamos depto
            if "departamento" in t or d != "guatemala":
                return f"Departamento de {d.title()} Q{COSTO_DEPTO}", COSTO_DEPTO

    # 3) Zonas premium por mención explícita (sin zona)
    for prem in ZONAS_PREMIUM:
        if prem in t:
            return f"{prem.title()} (premium) Q{COSTO_PREMIUM}", COSTO_PREMIUM

    # 4) Si menciona “zona” pero no número, pedirlo
    if "zona" in t:
        return "zona_pendiente", None

    return None, None

# ======= FLUJO =======
WELCOME = "¡Hola! Bienvenid@ a Pet Plus ¿Cómo podemos ayudarle?"

def product_info_text(p):
    # Descripción y precio SOLO desde catálogo
    precio = p.get("precio", {})
    linea_precio = []
    if "unidad" in precio:
        linea_precio.append(f"1 x Q{precio['unidad']}")
    if "dos_unidades" in precio:
        linea_precio.append(f"2 x Q{precio['dos_unidades']}")
    precios = " | ".join(linea_precio) if linea_precio else "Precio disponible bajo consulta."
    desc = p.get("descripcion","")
    name = p.get("nombre","Producto")
    txt = f"{name}\n\n{desc}\n\nPrecios: {precios}"
    return txt.strip()

def handle_message(user_id, text):
    s = get_session(user_id)

    # 1) Saludo solo 1 vez al inicio
    if not s["greeted"]:
        send_text(user_id, WELCOME)
        s["greeted"] = True
        # No retornamos; seguimos interpretando el primer mensaje

    intent = detect_intent(text)

    # 2) Si aún no hay producto seleccionado, intentamos detectar
    if s["product"] is None:
        p = find_product(text)
        if p:
            s["product"] = p
            s["stage"] = "product_selected"
            send_text(user_id, product_info_text(p))
            send_text(user_id, "¿Te muestro una foto, deseas el precio o prefieres ver los costos de envío?")
            return
        else:
            # sugerir por keywords
            # top 3 por coincidencia (reutiliza find_product lógica simple)
            suggestions = []
            t = norm(text)
            toks = set(t.split())
            scored = []
            for p in CATALOG:
                score = len(toks & p["tokens"])
                if p["nombre_norm"] in t:
                    score += 3
                if score > 0:
                    scored.append((score, p))
            scored.sort(reverse=True, key=lambda x:x[0])
            if scored:
                names = [x[1]["nombre"] for x in scored[:3]]
                send_text(user_id, "¿Te refieres a alguno de estos?: " + " / ".join(names))
            else:
                send_text(user_id, "Cuéntame qué producto buscas (por ejemplo: “rascador”, “guantes húmedos”, “pingüino rodador”).")
            return

    # 3) Con producto seleccionado, resolvemos intención
    p = s["product"]

    if intent == "image":
        if p.get("imagen"):
            send_image(user_id, p["imagen"])
        else:
            send_text(user_id, "Aún no tengo imagen cargada para este producto.")
        return

    if intent == "price":
        send_text(user_id, product_info_text(p))
        send_text(user_id, "Si me indicas tu zona o departamento, te digo el costo de envío y te preparo el total. 😊")
        s["stage"] = "awaiting_shipping"
        return

    if intent == "shipping" or s["stage"] == "awaiting_shipping":
        etiqueta, costo = compute_shipping(text)
        if etiqueta == "zona_pendiente":
            send_text(user_id, "¿De qué **zona** eres? (por ejemplo: “zona 2”).")
            s["stage"] = "awaiting_shipping"
            return
        if etiqueta and costo is not None:
            # calcular total si hay precio unitario
            precio = p.get("precio", {}).get("unidad")
            if isinstance(precio, (int, float)):
                total = precio + costo
                send_text(user_id, f"Envío a **{etiqueta}**. Producto Q{precio} + envío Q{costo} = **Total Q{total}**.\nSi te parece bien, dime tu dirección y nombre para coordinar la entrega. 🧾🚚")
            else:
                send_text(user_id, f"Envío a **{etiqueta}**. Si deseas, te confirmo el total cuando me indiques la cantidad que llevarás.")
            s["stage"] = "closing"
            return
        # si no detectó nada
        send_text(user_id, "Para calcular el envío, ¿podrías indicar **zona** (1–25) o **departamento**?")
        s["stage"] = "awaiting_shipping"
        return

    if intent == "help":
        send_text(user_id, "Puedo ayudarte con información de productos (descripción, foto, precio) y calcular el envío por zona o departamento. 😊")
        return

    # Si el usuario escribe algo más y ya hay producto:
    # reforzamos catálogo; no inventamos
    if "precio" in norm(text):
        # a veces “unknown” pero menciona precio
        send_text(user_id, product_info_text(p))
        send_text(user_id, "¿Te calculo el envío? Dime tu zona (1–25) o departamento.")
        s["stage"] = "awaiting_shipping"
        return

    # Pregunta abierta pero ya hay producto
    send_text(user_id, "¿Quieres que te envíe **foto**, **precio** o calcule **envío** para este producto?")
    return

# ======= WEB =======
@app.route("/")
def home():
    return "Bot de Mascotas activo"

@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    cleanup_sessions()

    if request.method == 'GET':
        token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')
        if token == VERIFY_TOKEN:
            return challenge
        return 'Token inválido', 403

    data = request.get_json()
    try:
        for entry in data.get('entry', []):
            for event in entry.get('messaging', []):
                sender = event['sender']['id']
                # Mensajes normales
                if 'message' in event:
                    msg = event['message']
                    # ignorar echos/entregas/adjuntos no texto (las imágenes las tratamos como intención "image")
                    if 'text' in msg:
                        handle_message(sender, msg['text'])
                    elif 'attachments' in msg:
                        # si llega una imagen del cliente, pedimos nombre del producto o adjuntamos flujo
                        send_text(sender, "Recibí tu imagen 👍. ¿Sobre qué producto necesitas información? (puedes escribir el nombre)")
                # Postbacks (opcional)
                if 'postback' in event:
                    payload = event['postback'].get('payload','')
                    # Evita que los postbacks generen nuevos saludos múltiples
                    if payload.lower() in {"get_started","start"}:
                        sess = get_session(sender)
                        if not sess["greeted"]:
                            send_text(sender, WELCOME)
                            sess["greeted"] = True
    except Exception as e:
        print("❌ Error webhook:", e)
    return "EVENT_RECEIVED", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
