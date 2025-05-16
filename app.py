import uuid
from flask import Flask, request, jsonify
import threading
from telethon import TelegramClient, events
from dotenv import load_dotenv
import os
import re
import asyncio
import time
from datetime import datetime, timedelta

load_dotenv()

# === Configuración ===
api_id = int(os.getenv("TELEGRAM_API"))
api_hash = os.getenv("TELEGRAM_API_HASH")
# target_channel = int(os.getenv("TELEGRAM_TARGET_CHANNEL"))  # Puede ser @micanal o ID tipo -100...

latest_signal_mrpip = None
latest_signal_forexpremim = None
latest_signal_btc = None
latest_signal_mrpip_sltp = None
signal_id_mrpip = None

# Canales que vamos a escuchar
TELEGRAM_CHANNEL_PIPS = int(os.getenv("TELEGRAM_CHANNEL_PIPS"))
TELEGRAM_CHANNEL_FOREX = int(os.getenv("TELEGRAM_CHANNEL_FOREX"))
TELEGRAM_CHANNEL_BTC = int(os.getenv("TELEGRAM_CHANNEL_BTC"))
TELEGRAM_CHANNEL_TARGET = int(os.getenv("TELEGRAM_TARGET_CHANNEL"))
WATCHED_CHANNELS = [TELEGRAM_CHANNEL_TARGET, TELEGRAM_CHANNEL_PIPS, TELEGRAM_CHANNEL_FOREX, TELEGRAM_CHANNEL_BTC]

# Inicializar cliente de Telethon
client_telegram = TelegramClient('local_session', api_id, api_hash)
telethon_event_loop = None

app = Flask(__name__)

# MR PIPS

def is_entry_signal_mr_pip(text):
    """
    Valida si el mensaje es una señal de entrada válida solo para US100 y XAUUSD.
    Retorna un diccionario con los datos si es válida, o None si no lo es.
    """
    if not text or not isinstance(text, str):
        return None

    text = text.strip().upper()

    # Solo aceptar estos dos símbolos
    allowed_symbols = {"US100", "XAUUSD"}

    # Expresión regular para validar la estructura del mensaje
    pattern = r'^([A-Z0-9]+)\s*\(([A-Z\s]+)\)\s+(BUY|SELL)\s+PUSH$'
    match = re.match(pattern, text)

    if match:
        symbol = match.group(1)
        market = match.group(2)
        direction = match.group(3)

        if symbol in allowed_symbols:
            return {
                'symbol': symbol,
                'market': market,
                'direction': direction
            }

    return None

def parse_entry_signal(text):
    """
    Parsea un mensaje de entrada y extrae símbolo, mercado y dirección si es válido.
    Solo acepta los símbolos US100 y XAUUSD.
    
    Retorna un diccionario con los datos extraídos o None si no coincide.
    """
    if not text or not isinstance(text, str):
        return None

    text = text.strip().upper()

    allowed_symbols = {"US100", "XAUUSD"}

    pattern = r'^([A-Z0-9]+)\s*\(([A-Z\s]+)\)\s+(BUY|SELL)\s+PUSH$'
    match = re.match(pattern, text)

    if match:
        symbol, market, direction = match.groups()
        if symbol in allowed_symbols:
            return {
                'symbol': symbol,
                'market': market.strip(),
                'side': direction
            }

    return None

def is_tp_sl_message_mr_pip(text):
    """
    Valida si el mensaje contiene al menos un TP y un SL en formato numérico.
    Retorna un diccionario con los datos si es válido, o None si no lo es.
    """
    if not text or not isinstance(text, str):
        return None

    text = text.strip().upper()

    # Buscar todos los TP: TP1=..., TP2=..., etc.
    tp_matches = re.findall(r'TP\d\s*=\s*([\d\.]+)', text)

    # Buscar SL
    sl_match = re.search(r'\bSL\s*=\s*([\d\.]+)', text)

    if tp_matches and sl_match:
        return {
            'tps': [float(tp) for tp in tp_matches],
            'sl': float(sl_match.group(1))
        }

    return None

def parse_tp_sl_message(text):
    """
    Valida y extrae TP1, TP2... y SL desde un mensaje.
    Requiere al menos un TP y un SL.
    
    Retorna un diccionario con:
        - 'tps': lista de floats
        - 'sl': float
    O None si el mensaje no contiene una señal válida.
    """
    if not text or not isinstance(text, str):
        return None

    text = text.strip().upper()

    # Buscar TPs: TP1=..., TP2=...
    tp_matches = re.findall(r'TP\d\s*=\s*([\d\.]+)', text)

    # Buscar SL
    sl_match = re.search(r'\bSL\s*=\s*([\d\.]+)', text)

    if tp_matches and sl_match:
        try:
            return {
                'tps': [str(tp) for tp in tp_matches],
                'sl': str(sl_match.group(1))
            }
        except ValueError:
            return None  # Algún número no era válido
    return None

# VIP PREMIUM FOREX

def is_forex_premium_signal(text):
    """
    Valida si un texto contiene una señal válida para los activos permitidos:
    US30, GOLD, BTC, XAU

    Acepta encabezado con o sin la palabra NOW y con palabras previas:
    - GOLD SELL NOW 3280
    - GUYS GOLD SELL NOW 3280
    - US30 SELL 41030
    - BTC BUY NOW 67200

    Requiere al menos un TP y un SL.
    """
    if not text or not isinstance(text, str):
        print("Texto inválido:", text)
        return False

    text = text.strip().upper()

    # Símbolos válidos
    allowed_symbols = {"US30", "GOLD", "BTC", "XAU"}

    # Buscar encabezado flexible
    match = re.search(
        r'(?:\b\w+\b\s+)*([A-Z0-9]+)\s+(BUY|SELL)(?:\s+NOW)?(?:\s+\w+)*\s+([\d\.]+(?:\s*/\s*[\d\.]+)?)',
        text
    )

    if not match:
        return False

    symbol = match.group(1).strip()
    if symbol not in allowed_symbols:
        return False

    # Validar SL
    has_sl = re.search(r'\bSL\s*[:=]?\s*([\d\.]+)', text, re.IGNORECASE)

    # Validar al menos un TP
    tp_matches = re.findall(r'\bTP\d*\s*[:=]?\s*([\d\.]+)', text, re.IGNORECASE)

    return all([
        has_sl,
        len(tp_matches) >= 1
    ])

def parse_forex_premium_signal(text):
    """
    Parsea una señal del tipo GOLD SELL NOW 3412 o GUYS GOLD SELL NOW 3412.
    Convierte GOLD y XAU a XAUUSD, y BTC a BTCUSD.
    Acepta solo estos símbolos originales: US30, GOLD, BTC, XAU.

    Retorna:
        - symbol: string
        - direction: BUY / SELL
        - entry: list[float]
        - sl: float
        - tps: list[float]
    """
    if not text or not isinstance(text, str):
        return None

    text = text.strip().upper()

    # Lista permitida de símbolos originales
    allowed_symbols = {"US30", "GOLD", "BTC", "XAU"}

    # Encabezado flexible
    match = re.search(
        r'(?:\b\w+\b\s+)*([A-Z0-9]+)\s+(BUY|SELL)(?:\s+NOW)?(?:\s+\w+)*\s+([\d\.]+(?:\s*/\s*[\d\.]+)?)',
        text
    )
    if not match:
        return None

    raw_symbol = match.group(1).strip()
    if raw_symbol not in allowed_symbols:
        return None

    # Mapeo de conversión
    symbol_map = {
        "GOLD": "XAUUSD",
        "XAU": "XAUUSD",
        "BTC": "BTCUSD"
    }
    symbol = symbol_map.get(raw_symbol, raw_symbol)

    direction = match.group(2).strip()
    entry_raw = match.group(3).strip()

    try:
        entry_prices = [str(p.strip()) for p in entry_raw.split('/') if p.strip()]
    except ValueError:
        return None

    # SL
    sl_match = re.search(r'\bSL\s*[:=]?\s*([\d\.]+)', text, re.IGNORECASE)
    if not sl_match:
        return None
    try:
        sl = str(sl_match.group(1))
    except ValueError:
        return None

    # TPs
    tp_matches = re.findall(r'\bTP\d*\s*[:=]?\s*([\d\.]+)', text, re.IGNORECASE)
    try:
        tps = [str(tp) for tp in tp_matches]
    except ValueError:
        return None

    if not tps:
        return None

    return {
        'symbol': symbol,
        'side': direction,
        'entry': entry_prices,
        'sl': sl,
        'tps': tps
    }

# BTC

def is_enfoque_signal(text):
    """
    Valida señales con formato estilo cripto:
    - BUY/SELL <symbol>
    - Entry price <number>
    - SL : <number> (con o sin emoji)
    - TP1 / TP2 : <number> (con o sin emoji)

    Requiere al menos un TP y un SL.
    """
    if not text or not isinstance(text, str):
        print("Texto inválido:", text)
        return False

    text = text.strip().upper()

    # Validar encabezado tipo: BUY BTCUSD o SELL XAUUSD
    header_match = re.search(r'^(BUY|SELL)\s+([A-Z0-9]+)', text)
    if not header_match:
        return False

    # Entry price
    has_entry = re.search(r'ENTRY\s+PRICE\s+([\d\.]+)', text)

    # Stop Loss
    has_sl = re.search(r'SL\s*[:=]?\s*([\d\.]+)', text)

    # Take Profits (TP1, TP2...)
    tp_matches = re.findall(r'TP\d*\s*[:=]?\s*([\d\.]+)', text)

    return all([
        has_entry,
        has_sl,
        len(tp_matches) >= 1
    ])

def parse_enfoque_signal(text):
    """
    Parsea señales tipo cripto con estructura:
    BUY BTCUSD
    Entry price 97100
    SL : 96300
    TP1 : 97250
    ...
    
    Retorna un diccionario estructurado o None si falta algo esencial.
    """
    if not text or not isinstance(text, str):
        return None

    text = text.strip().upper()

    # Encabezado: BUY BTCUSD
    header_match = re.search(r'^(BUY|SELL)\s+([A-Z0-9]+)', text)
    if not header_match:
        return None

    direction = header_match.group(1).strip()
    symbol = header_match.group(2).strip()

    # Entry price
    entry_match = re.search(r'ENTRY\s+PRICE\s+([\d\.]+)', text)
    if not entry_match:
        return None
    try:
        entry = str(entry_match.group(1))
    except ValueError:
        return None

    # SL
    sl_match = re.search(r'\bSL\s*[:=]?\s*([\d\.]+)', text)
    if not sl_match:
        return None
    try:
        sl = str(sl_match.group(1))
    except ValueError:
        return None

    # TPs
    tp_matches = re.findall(r'TP\d*\s*[:=]?\s*([\d\.]+)', text)
    try:
        tps = [str(tp) for tp in tp_matches]
    except ValueError:
        return None

    if not tps:
        return None

    return {
        'symbol': symbol,
        'side': direction,
        'entry': entry,
        'sl': sl,
        'tps': tps
    }

def send_order_to_mt5(order_data):
    global latest_signal_mrpip, latest_signal_mrpip_sltp, latest_signal_forexpremim, latest_signal_btc

    vendor = order_data.get("vendor", "").lower()

    if vendor == "pip":
        latest_signal_mrpip = order_data
        print(f"📤 Señal de Mr Pips almacenada: {order_data['symbol']} [{order_data['side']}]")
    
    if vendor == "pipsltp":
        latest_signal_mrpip_sltp = order_data
        print(f"📤 Señal con SL y TPs de Mr Pips almacenada")

    elif vendor == "premiun_forex":
        latest_signal_forexpremim = order_data
        print(f"📤 Señal de Forex Premium almacenada: {order_data['symbol']} [{order_data['side']}]")

    elif vendor == "enfoque_btc":
        latest_signal_btc = order_data
        print(f"📤 Señal de Enfoque BTC almacenada: {order_data['symbol']} [{order_data['side']}]")

    else:
        print("❌ Vendor desconocido en la señal:", vendor)

def format_signal_for_telegram(order_data):
    global latest_signal_mrpip
    """
    Formatea una señal de trading para enviar como mensaje de Telegram (Markdown),
    soportando distintos formatos de `order_data`.
    """
    # Extraer campos con respaldo alternativo
    symbol = order_data.get("symbol", "🆔 ACTIVO NO DEFINIDO")
    direction = order_data.get("direction") or order_data.get("side") or "🧐"
    sl = order_data.get("sl")
    tps = order_data.get("tps")
    entry = order_data.get("entry", "⏳ Esperando ejecución")
    vendor = order_data.get("vendor")

    # Armar líneas condicionalmente
    if vendor == "pip":
        lines = ["📢 Nueva Señal de Mr Pips\n"]
    if vendor == "pipsltp":
        lines = ["📢 TP y SL de Mr Pips\n"]
    elif vendor == "premiun_forex":
        lines = ["📢 Nueva Señal de Premiun Forex\n"]
    elif vendor == "enfoque_btc":
        lines = ["📢 Nueva Señal de Enfoque BTC\n"]

    if vendor == "pipsltp":
        symbol = latest_signal_mrpip['symbol']
        direction = latest_signal_mrpip['side']

    if direction and symbol:
        lines.append(f"📈 {direction} - `{symbol}`\n")
    
    # lines.append(f"🎯 Entry: `{entry}`")

    if isinstance(tps, list) and len(tps) > 0:
        for i, tp in enumerate(tps):
            lines.append(f"🎯 TP{i+1}: `{tp}`")

    if sl:
        lines.append(f"🛑 SL: `{sl}`")

    return "\n".join(lines)


# === Handler principal ===

@client_telegram.on(events.NewMessage(chats=WATCHED_CHANNELS))
async def handler(event):
    global signal_id_mrpip
    sender_id = int(event.chat_id)
    message = event.message.message

    print(f"sender: {sender_id}")
    print(f"message: {message}")

    #CHANNEL_CRYPTO
    if sender_id in [TELEGRAM_CHANNEL_TARGET, TELEGRAM_CHANNEL_PIPS] and is_entry_signal_mr_pip(message):
        header = "📡 Señal de Mr Pips Recibida con Punto de Entrada"

        print(f"\n🪙 Señal de MR Pip detectada:\n{message}\n{'='*60}")

        signal_data = parse_entry_signal(message)
        if signal_data:
            order_data = {
                "symbol": signal_data['symbol'],         # Ej: "CRASH 1000 INDEX"
                "side": signal_data['side'],         # "BUY" o "SELL"
                "vendor": "pip"
            }
            signal_id_mrpip = str(uuid.uuid4())
            order_data['signal_id'] = signal_id_mrpip

            send_order_to_mt5(order_data)
            print(order_data)
            await client_telegram.send_message(entity=TELEGRAM_CHANNEL_TARGET, message=f"{format_signal_for_telegram(order_data)}")
            return
        
    elif sender_id in [TELEGRAM_CHANNEL_TARGET, TELEGRAM_CHANNEL_PIPS] and is_tp_sl_message_mr_pip(message):
        header = "📡 Señal de Mr Pips Recibida con SL y TP"

        print(f"\n🪙 Señal MR Pip detectada:\n{message}\n{'='*60}")

        signal_data = parse_tp_sl_message(message)
        if signal_data:
            order_data = {
                "tps": signal_data['tps'],         # Ej: "CRASH 1000 INDEX"
                "sl": signal_data['sl'],            # "BUY" o "SELL"
                "vendor": "pipsltp"
            }

            if signal_id_mrpip:
                order_data['signal_id'] = signal_id_mrpip

            send_order_to_mt5(order_data)
            print(signal_data)
            await client_telegram.send_message(entity=TELEGRAM_CHANNEL_TARGET, message=f"{format_signal_for_telegram(order_data)}")
            return

    elif sender_id in [TELEGRAM_CHANNEL_TARGET, TELEGRAM_CHANNEL_FOREX] and is_forex_premium_signal(message):
        header = "📡 Señal de Premiun Forex Recibida con SL y TP"

        print(f"\n🪙 Señal Premiun Forex detectada:\n{message}\n{'='*60}")

        signal_data = parse_forex_premium_signal(message)
        if signal_data:
            order_data = {
                "symbol": signal_data['symbol'],         # Ej: "CRASH 1000 INDEX"
                "side": signal_data['side'],   # "BUY" o "SELL"
                "sl": signal_data['sl'],
                "tps": signal_data['tps'],
                "vendor": "premiun_forex"
            }
            send_order_to_mt5(order_data)
            print(signal_data)
            await client_telegram.send_message(entity=TELEGRAM_CHANNEL_TARGET, message=f"{format_signal_for_telegram(order_data)}")
            return
        
    elif sender_id in [TELEGRAM_CHANNEL_TARGET, TELEGRAM_CHANNEL_BTC] and is_enfoque_signal(message):
        header = "📡 Señal de Enfoque BTC Recibida con SL y TP"

        print(f"\n🪙 Señal Enfoque BTC detectada:\n{message}\n{'='*60}")

        signal_data = parse_enfoque_signal(message)
        if signal_data:
            order_data = {
                "symbol": signal_data['symbol'],         # Ej: "CRASH 1000 INDEX"
                "side": signal_data['side'],   # "BUY" o "SELL"
                "sl": signal_data['sl'],
                "tps": signal_data['tps'],
                "vendor": "enfoque_btc"
            }
            send_order_to_mt5(order_data)
            print(signal_data)
            await client_telegram.send_message(entity=TELEGRAM_CHANNEL_TARGET, message=f"{format_signal_for_telegram(order_data)}")
            return
        
    else:
        if sender_id  == TELEGRAM_CHANNEL_PIPS:
            header = "⚠️ Se recibió un mensaje de Mr Pips, pero no es una señal"
        elif sender_id == TELEGRAM_CHANNEL_FOREX:
            header = "⚠️ Se recibió un mensaje de VIP Premium Forex, pero no es una señal"
        elif sender_id == TELEGRAM_CHANNEL_BTC:
            header = "⚠️ Se recibió un mensaje El Enfoque, pero no es una señal"
        elif sender_id  == TELEGRAM_CHANNEL_TARGET:
            header = "⚠️ Se recibió un mensaje del grupo The Billions, pero no es una señal"
        else:
            header = "⚠️ Se recibió un mensaje, pero no es de otro canal"
        
        print(f"\n📭 Mensaje ignorado de canal {sender_id}.\n{'='*60}")
        
    # Enviar mensaje al canal
    try:
        # await client_telegram.send_message(entity=target_channel, message=f"{header}\n\n{message}")
        await client_telegram.send_message(entity=TELEGRAM_CHANNEL_TARGET, message=f"{header}")
        print("✅ Mensaje enviado al canal destino.")
    except Exception as e:
        print(f"❌ Error al enviar mensaje al canal: {e}")

# === Ejecutar cliente ===
def start_flask():
    port = int(os.getenv("PORT", 3000))
    print(f"🌐 Flask escuchando en puerto {port}")
    app.run(host="0.0.0.0", port=port)

def main():
    print("🚀 Bot y backend MT5 iniciando...")
    flask_thread = threading.Thread(target=start_flask)
    flask_thread.start()
    with client_telegram:
        telethon_event_loop = client_telegram.loop  # 🔥 capturamos el loop real
        client_telegram.run_until_disconnected()

@app.route("/")
def index():
    return {"status": "ok", "message": "API activa!"}

@app.route("/ping")
def ping():
    return {"status": "ok", "message": "bot activo!"}

@app.route("/mt5/mrpip/execute", methods=["GET"])
def get_mrpip_signal():
    global latest_signal_mrpip
    if not latest_signal_mrpip:
        return "", 204
    signal = latest_signal_mrpip
    latest_signal_mrpip = None
    return jsonify(signal)

@app.route("/mt5/mrpip/sltp", methods=["GET"])
def get_mrpip_sltp_signal():
    global latest_signal_mrpip_sltp
    if not latest_signal_mrpip_sltp:
        return "", 204
    signal = latest_signal_mrpip_sltp
    latest_signal_mrpip_sltp = None
    return jsonify(signal)

@app.route("/mt5/forexpremium/execute", methods=["GET"])
def get_forexpremium_signal():
    global latest_signal_forexpremim
    if not latest_signal_forexpremim:
        return "", 204
    signal = latest_signal_forexpremim
    latest_signal_forexpremim = None
    return jsonify(signal)

@app.route("/mt5/btc/execute", methods=["GET"])
def get_btc_signal():
    global latest_signal_btc
    if not latest_signal_btc:
        return "", 204
    signal = latest_signal_btc
    latest_signal_btc = None
    return jsonify(signal)

if __name__ == "__main__":
    main()