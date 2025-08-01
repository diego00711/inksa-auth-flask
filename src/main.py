# src/main.py

import os
import re
from flask import Flask, jsonify
from dotenv import load_dotenv
from flask_cors import CORS
from flask_socketio import SocketIO
import mercadopago

# Carrega as variáveis de ambiente do arquivo .env
load_dotenv()

# Importações relativas para os blueprints
from .routes.auth import auth_bp
from .routes.orders import orders_bp
from .routes.menu import menu_bp
from .routes.restaurant import restaurant_bp
from .routes.delivery import delivery_bp
from .routes.payment import mp_payment_bp
from .routes.delivery_calculator import delivery_calculator_bp
from .routes.admin import admin_bp 
from .utils.helpers import supabase

app = Flask(__name__)

# Configuração do CORS
CORS(app, origins="*", supports_credentials=True)

# Configuração do SocketIO
socketio = SocketIO(app, cors_allowed_origins="*")

# Configuração do Mercado Pago SDK
MERCADO_PAGO_ACCESS_TOKEN = os.environ.get("MERCADO_PAGO_ACCESS_TOKEN")

if not MERCADO_PAGO_ACCESS_TOKEN:
    print("AVISO: MERCADO_PAGO_ACCESS_TOKEN não encontrado nas variáveis de ambiente.")
    app.mp_sdk = None
else:
    sdk = mercadopago.SDK(MERCADO_PAGO_ACCESS_TOKEN)
    app.mp_sdk = sdk

# Registra todos os blueprints
app.register_blueprint(auth_bp, url_prefix='/api')
app.register_blueprint(orders_bp, url_prefix='/api')
app.register_blueprint(menu_bp, url_prefix='/api')
app.register_blueprint(restaurant_bp, url_prefix='/api')
app.register_blueprint(mp_payment_bp, url_prefix='/api')
app.register_blueprint(delivery_calculator_bp, url_prefix='/api')
app.register_blueprint(delivery_bp, url_prefix='/api/delivery')

# ✅ CORREÇÃO: Registando o blueprint de admin com o prefixo completo e correto.
app.register_blueprint(admin_bp, url_prefix='/api/admin')


@app.route('/')
def index():
    """Rota principal para verificar se o servidor está online."""
    return jsonify({
        "status": "online",
        "message": "Servidor de autenticação Inksa está funcionando!"
    })

@socketio.on('connect')
def handle_connect():
    """Lida com a conexão de um cliente WebSocket."""
    print('Um cliente se conectou ao WebSocket!')

@socketio.on('disconnect')
def handle_disconnect():
    """Lida com a desconexão de um cliente WebSocket."""
    print('Um cliente desconectou.')

if __name__ == '__main__':
    # Obtém a porta das variáveis de ambiente ou usa 5000 como padrão
    port = int(os.environ.get('PORT', 5000))
    # Inicia o aplicativo Flask usando SocketIO.run
    socketio.run(app, host='0.0.0.0', port=port, debug=True)