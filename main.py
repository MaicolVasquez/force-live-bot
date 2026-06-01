import os
import json
import re
import sqlite3
from flask import Flask, request
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
import gspread
from google.oauth2.service_account import Credentials
import anthropic
from fuzzywuzzy import fuzz
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ============= CONFIGURACIÓN =============
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
claude_client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

# ============= BASE DE DATOS (SQLite) =============
def init_db():
    """Inicializa la base de datos para los carritos"""
    conn = sqlite3.connect('carritos.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS carritos
                 (numero_cliente TEXT PRIMARY KEY, items TEXT, timestamp TEXT)''')
    conn.commit()
    conn.close()

init_db()

# ============= GOOGLE SHEETS SETUP =============
def conectar_google_sheets():
    """Conecta con Google Sheets usando variable de entorno o archivo"""
    scopes = [
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive'
    ]
    
    # Intenta leer desde variable de entorno primero (Seguro para producción)
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    
    if creds_json:
        creds_dict = json.loads(creds_json)
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    else:
        # Respaldo para entorno local
        creds = Credentials.from_service_account_file('credentials/credentials.json', scopes=scopes)
    
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(GOOGLE_SHEET_ID)
    sheet = spreadsheet.sheet1
    
    return sheet

# ============= CARRITO =============
def obtener_carrito(numero_cliente):
    """Obtiene el carrito del cliente desde la BD"""
    conn = sqlite3.connect('carritos.db')
    c = conn.cursor()
    c.execute("SELECT items FROM carritos WHERE numero_cliente=?", (numero_cliente,))
    row = c.fetchone()
    conn.close()
    
    if row:
        return {"items": json.loads(row[0]), "timestamp": datetime.now().isoformat()}
    
    return {"items": [], "timestamp": datetime.now().isoformat()}

def guardar_carrito(numero_cliente, carrito_data):
    """Guarda o actualiza el carrito en la BD"""
    conn = sqlite3.connect('carritos.db')
    c = conn.cursor()
    
    # Usamos REPLACE para insertar o actualizar según la llave primaria
    c.execute("REPLACE INTO carritos (numero_cliente, items, timestamp) VALUES (?, ?, ?)",
              (numero_cliente, json.dumps(carrito_data["items"]), datetime.now().isoformat()))
    conn.commit()
    conn.close()

def agregar_al_carrito(numero_cliente, medicamento, cantidad, precio):
    """Agrega item al carrito"""
    carrito = obtener_carrito(numero_cliente)
    encontrado = False
    
    for item in carrito["items"]:
        if item["medicamento"] == medicamento:
            item["cantidad"] += cantidad
            item["subtotal"] = item["cantidad"] * item["precio"]
            encontrado = True
            break
    
    if not encontrado:
        carrito["items"].append({
            "medicamento": medicamento,
            "cantidad": cantidad,
            "precio": precio,
            "subtotal": cantidad * precio
        })
        
    guardar_carrito(numero_cliente, carrito)

def obtener_resumen_carrito(numero_cliente):
    """Retorna resumen del carrito"""
    carrito = obtener_carrito(numero_cliente)
    
    if not carrito["items"]:
        return "📋 Tu carrito está vacío", 0
    
    resumen = "📋 **TU CARRITO:**\n"
    total = 0
    
    for i, item in enumerate(carrito["items"], 1):
        subtotal = item["cantidad"] * item["precio"]
        total += subtotal
        resumen += f"{i}. {item['medicamento']}\n"
        resumen += f"   Cantidad: {item['cantidad']} × S/ {item['precio']:.2f} = S/ {subtotal:.2f}\n"
    
    resumen += f"\n💰 **TOTAL: S/ {total:.2f}**\n"
    return resumen, total

# ============= BÚSQUEDA EN GOOGLE SHEETS =============
def buscar_medicamentos(query):
    """Búsqueda fuzzy en Google Sheets"""
    try:
        sheet = conectar_google_sheets()
        todos_registros = sheet.get_all_records()
        coincidencias = []
        
        for registro in todos_registros:
            medicamento = registro.get('Medicamento', '').lower()
            medida = registro.get('Medida', '')
            precio = float(registro.get('Precio', 0))
            stock = int(registro.get('Stock', 0))
            
            similitud = fuzz.token_set_ratio(query.lower(), medicamento)
            
            if similitud > 60:
                coincidencias.append({
                    'nombre': registro.get('Medicamento', ''),
                    'medida': medida,
                    'linea': registro.get('Línea', ''),
                    'precio': precio,
                    'stock': stock,
                    'similitud': similitud,
                    'completo': f"{registro.get('Medicamento', '')} ({medida})"
                })
        
        coincidencias.sort(key=lambda x: x['similitud'], reverse=True)
        return coincidencias[:5]
    
    except Exception as e:
        print(f"Error buscando medicamentos: {e}")
        return []

# ============= PROMPT PARA CLAUDE =============
PROMPT_SISTEMA = """Eres un vendedor profesional y MUY persuasivo de la farmacia Force Live.

INSTRUCCIONES:
1. Sé amable, profesional y enfocado en vender medicamentos
2. Si el cliente pregunta algo fuera de medicinas:
   Responde: "Entiendo, pero mi especialidad es solo asesorarte en medicamentos y salud. 
   ¿Qué medicina necesitas?"
3. Cuando el cliente quiera agregar algo:
   - Confirma: nombre, presentación (medida), cantidad y precio
   - Sugiere complementos relevantes
4. Usa técnicas de persuasión:
   - Urgencia: "Tenemos stock limitado..."
   - Relevancia: "Esto es perfecto para tu caso"
   - Complementariedad: "Estos dos van muy bien juntos"
5. Mantén mensajes cortos: MAX 3-4 líneas
6. Si dice "proceder", "pagar", "checkout", "confirmo compra":
   Responde: "Perfecto 👍 Un especialista nuestro te contactará en 1 minuto"
7. Cuando sugieras medicamentos:
   - Nombre y presentación
   - Precio
   - Por qué es relevante

NUNCA inventes medicamentos. Solo sugiere los que encontraste."""

# ============= WEBHOOK DE WHATSAPP =============
@app.route('/whatsapp', methods=['POST'])
def whatsapp_webhook():
    """Recibe mensajes de WhatsApp y responde"""
    
    incoming_msg = request.values.get('Body', '').strip()
    numero_cliente = request.values.get('From')
    
    print(f"📨 Mensaje de {numero_cliente}: {incoming_msg}")
    
    if not incoming_msg:
        return 'OK', 200
    
    try:
        # Búsqueda en Google Sheets
        medicamentos_encontrados = buscar_medicamentos(incoming_msg)
        
        # Preparar contexto
        contexto_medicamentos = ""
        if medicamentos_encontrados:
            contexto_medicamentos = "Medicamentos disponibles encontrados:\n"
            for med in medicamentos_encontrados:
                contexto_medicamentos += f"- {med['completo']} | Stock: {med['stock']} | Precio: S/ {med['precio']}\n"
        else:
            contexto_medicamentos = "No se encontraron medicamentos exactos."
        
        # Obtener carrito
        carrito_resumen, total = obtener_resumen_carrito(numero_cliente)
        carrito_texto = f"\n\nCarrito actual:\n{carrito_resumen}" if total > 0 else ""
        
        # Llamar a Claude
        mensaje_completo = f"""{contexto_medicamentos}

Mensaje del cliente: "{incoming_msg}"
{carrito_texto}

Responde naturalmente como vendedor."""
        
        response = claude_client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=400,
            system=PROMPT_SISTEMA,
            messages=[
                {"role": "user", "content": mensaje_completo}
            ]
        )
        
        respuesta_claude = response.content[0].text
        
        # Detectar si quiere agregar al carrito
        detectar_agregar_carrito(incoming_msg, medicamentos_encontrados, numero_cliente)
        
        # Detectar intención de pago
        palabras_pago = ['pagar', 'proceder', 'checkout', 'confirmo', 'adelante', 'comprar']
        si_pagar = any(palabra in incoming_msg.lower() for palabra in palabras_pago)
        
        if si_pagar:
            respuesta_claude += "\n\n✅ Un especialista te contactará en 1 minuto para coordinar el pago y envío."
        
        # Enviar respuesta
        enviar_whatsapp(numero_cliente, respuesta_claude)
        
        return 'OK', 200
    
    except Exception as e:
        print(f"❌ Error: {e}")
        enviar_whatsapp(numero_cliente, "Disculpa, tuve un problema técnico. ¿Puedes escribir de nuevo?")
        return 'ERROR', 500

def detectar_agregar_carrito(mensaje, medicamentos, numero_cliente):
    """Detecta si el cliente quiere agregar algo"""
    palabras_agregar = ['sí', 'si', 'quiero', 'dame', 'necesito', 'llevo', 'ok', 'dale', 'agrega']
    
    if any(palabra in mensaje.lower() for palabra in palabras_agregar) and medicamentos:
        med = medicamentos[0]
        cantidad = extraer_cantidad(mensaje)
        agregar_al_carrito(numero_cliente, med['completo'], cantidad, med['precio'])

def extraer_cantidad(mensaje):
    """Extrae cantidad evitando confundirse con mg, ml, gr"""
    # Busca números de 1 o 2 dígitos que NO estén seguidos por mg, ml, g, gr, kg, mcg
    matches = re.findall(r'\b(\d{1,2})\b(?!\s*(?:mg|ml|g|gr|kg|mcg))', mensaje, re.IGNORECASE)
    if matches:
        return int(matches[0])
    return 1 # Por defecto agrega 1 unidad si no encuentra un número aislado

def enviar_whatsapp(numero_cliente, mensaje):
    """Envía mensaje por WhatsApp"""
    twilio_client.messages.create(
        from_=TWILIO_WHATSAPP_FROM,
        to=numero_cliente,
        body=mensaje
    )

# ============= ENDPOINT DE PRUEBA =============
@app.route('/test', methods=['GET'])
def test():
    """Para verificar que el bot está activo"""
    return {
        'status': 'activo',
        'timestamp': datetime.now().isoformat()
    }, 200

# ============= MAIN =============
if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)