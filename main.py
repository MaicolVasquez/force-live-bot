import os
import json
import re
from flask import Flask, request
from twilio.rest import Client
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

# Inicializar clientes
try:
    twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
except Exception as e:
    print(f"Error inicializando Twilio: {e}")
    twilio_client = None

try:
    claude_client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
except Exception as e:
    print(f"Error inicializando Claude: {e}")
    claude_client = None

# ============= GOOGLE SHEETS =============
def conectar_google_sheets():
    """Conecta con Google Sheets"""
    try:
        scopes = [
            'https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive'
        ]
        
        creds = Credentials.from_service_account_file(
            'credentials/credentials.json',
            scopes=scopes
        )
        
        gc = gspread.authorize(creds)
        spreadsheet = gc.open_by_key(GOOGLE_SHEET_ID)
        sheet = spreadsheet.sheet1
        
        return sheet
    except Exception as e:
        print(f"Error conectando Google Sheets: {e}")
        return None

# ============= CARRITO =============
carritos = {}

def obtener_carrito(numero_cliente):
    if numero_cliente not in carritos:
        carritos[numero_cliente] = {
            "items": [],
            "timestamp": datetime.now().isoformat()
        }
    return carritos[numero_cliente]

def agregar_al_carrito(numero_cliente, medicamento, cantidad, precio):
    carrito = obtener_carrito(numero_cliente)
    
    for item in carrito["items"]:
        if item["medicamento"] == medicamento:
            item["cantidad"] += cantidad
            return
    
    carrito["items"].append({
        "medicamento": medicamento,
        "cantidad": cantidad,
        "precio": precio,
        "subtotal": cantidad * precio
    })

def obtener_resumen_carrito(numero_cliente):
    carrito = obtener_carrito(numero_cliente)
    
    if not carrito["items"]:
        return "📋 Tu carrito está vacío", 0
    
    resumen = "📋 TU CARRITO:\n"
    total = 0
    
    for i, item in enumerate(carrito["items"], 1):
        subtotal = item["cantidad"] * item["precio"]
        total += subtotal
        resumen += f"{i}. {item['medicamento']}\n"
        resumen += f"   {item['cantidad']}x S/ {item['precio']:.2f} = S/ {subtotal:.2f}\n"
    
    resumen += f"\nTOTAL: S/ {total:.2f}\n"
    return resumen, total

# ============= BÚSQUEDA =============
def buscar_medicamentos(query):
    """Busca medicamentos en Google Sheets"""
    try:
        sheet = conectar_google_sheets()
        if not sheet:
            return []
        
        todos_registros = sheet.get_all_records()
        coincidencias = []
        
        for registro in todos_registros:
            medicamento = registro.get('Medicamento', '').lower()
            medida = registro.get('Medida', '')
            precio = float(registro.get('Precio', 0)) if registro.get('Precio') else 0
            stock = int(registro.get('Stock', 0)) if registro.get('Stock') else 0
            
            similitud = fuzz.token_set_ratio(query.lower(), medicamento)
            
            if similitud > 60 and stock > 0:
                coincidencias.append({
                    'nombre': registro.get('Medicamento', ''),
                    'medida': medida,
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

# ============= PROMPT =============
PROMPT_SISTEMA = """Eres vendedor de Force Live Farmacia. Sé profesional, amable y persuasivo.
- Sugiere medicamentos relevantes
- Confirma cantidades y precios
- Si hablan de otro tema: "Mi especialidad es medicamentos y salud"
- Si dicen "pagar" o "proceder": "Un especialista te contactará en 1 minuto"
- Mensajes cortos (máximo 3-4 líneas)"""

# ============= WEBHOOK =============
@app.route('/whatsapp', methods=['POST'])
def whatsapp_webhook():
    """Recibe mensajes de WhatsApp"""
    try:
        incoming_msg = request.values.get('Body', '').strip()
        numero_cliente = request.values.get('From')
        
        if not incoming_msg:
            return 'OK', 200
        
        print(f"📨 Mensaje: {incoming_msg}")
        
        # Buscar medicamentos
        medicamentos_encontrados = buscar_medicamentos(incoming_msg)
        
        # Preparar contexto
        contexto = ""
        if medicamentos_encontrados:
            contexto = "Medicamentos encontrados:\n"
            for med in medicamentos_encontrados[:3]:
                contexto += f"- {med['completo']} (S/ {med['precio']}) - Stock: {med['stock']}\n"
        
        # Obtener carrito
        carrito_resumen = ""
        carrito = obtener_carrito(numero_cliente)
        if carrito["items"]:
            res, total = obtener_resumen_carrito(numero_cliente)
            carrito_resumen = f"\n{res}"
        
        # Llamar a Claude
        if not claude_client:
            respuesta = "Disculpa, tengo un error técnico. Intenta de nuevo."
        else:
            try:
                response = claude_client.messages.create(
                    model="claude-3-5-sonnet-20241022",
                    max_tokens=300,
                    system=PROMPT_SISTEMA,
                    messages=[
                        {"role": "user", "content": f"{contexto}\nCliente: {incoming_msg}{carrito_resumen}"}
                    ]
                )
                respuesta = response.content[0].text
            except Exception as e:
                print(f"Error Claude: {e}")
                respuesta = "Disculpa, error con la IA. Intenta de nuevo."
        
        # Detectar agregar carrito
        palabras = ['sí', 'si', 'quiero', 'dame', 'llevo', 'ok']
        if any(p in incoming_msg.lower() for p in palabras) and medicamentos_encontrados:
            med = medicamentos_encontrados[0]
            cant = 1
            agregar_al_carrito(numero_cliente, med['completo'], cant, med['precio'])
        
        # Detectar pago
        if any(p in incoming_msg.lower() for p in ['pagar', 'proceder', 'confirmo']):
            respuesta += "\n\n✅ Un especialista te contactará en 1 minuto."
        
        # Enviar respuesta
        if twilio_client:
            twilio_client.messages.create(
                from_=TWILIO_WHATSAPP_FROM,
                to=numero_cliente,
                body=respuesta
            )
        
        return 'OK', 200
    
    except Exception as e:
        print(f"Error: {e}")
        return 'ERROR', 500

# ============= TEST =============
@app.route('/test', methods=['GET'])
def test():
    return {
        'status': 'activo',
        'timestamp': datetime.now().isoformat()
    }, 200

# ============= MAIN =============
if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)