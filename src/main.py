# src/main.py

import os
import re
from flask import Flask, jsonify
from dotenv import load_dotenv
from flask_cors import CORS # Importa CORS
from flask_socketio import SocketIO # Importa SocketIO
import mercadopago

# Carrega as variáveis de ambiente do arquivo .env
load_dotenv()

# Importações relativas para os blueprints
from .routes.auth import auth_bp
from .routes.orders import orders_bp
from .routes.menu import menu_bp
from .routes.restaurant import restaurant_bp
from .routes.delivery import delivery_bp # Importa o blueprint de delivery
from .routes.payment import mp_payment_bp
from .routes.delivery_calculator import delivery_calculator_bp
from .routes.admin import admin_bp 
from .utils.helpers import supabase # Importa supabase (se usado globalmente)

app = Flask(__name__)

# --- CORREÇÃO PRINCIPAL DO CORS ---
# Esta é a configuração global de CORS para o Flask app.
# Definimos explicitamente as origens permitidas para o frontend e os métodos/headers.
# É crucial que esta configuração esteja aqui, ANTES do registro dos blueprints
# e ANTES da inicialização do SocketIO para garantir que o preflight request seja tratado.
CORS(app, resources={r"/api/*": {"origins": ["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:3000"], 
                                   "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
                                   "allow_headers": ["Content-Type", "Authorization"],
                                   "supports_credentials": True}})
# Se precisar de mais portas ou domínios para o frontend, adicione-os na lista 'origins'.

# Configuração do SocketIO
# O 'cors_allowed_origins' do SocketIO é separado do CORS do Flask normal.
# Também pode ser uma lista explícita como acima.
socketio = SocketIO(app, cors_allowed_origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:3000"])


# Configuração do Mercado Pago SDK
MERCADO_PAGO_ACCESS_TOKEN = os.environ.get("MERCADO_PAGO_ACCESS_TOKEN")

if not MERCADO_PAGO_ACCESS_TOKEN:
    print("AVISO: MERCADO_PAGO_ACCESS_TOKEN não encontrado nas variáveis de ambiente.")
    app.mp_sdk = None
else:
    sdk = mercadopago.SDK(MERCADO_PAGO_ACCESS_TOKEN)
    app.mp_sdk = sdk

# Registra todos os blueprints
# O url_prefix aqui define o caminho base para as rotas dentro do blueprint.
app.register_blueprint(auth_bp, url_prefix='/api/auth') # Prefixo para rotas de autenticação
app.register_blueprint(orders_bp, url_prefix='/api/orders') # Exemplo: /api/orders
app.register_blueprint(menu_bp, url_prefix='/api/menu') # Exemplo: /api/menu
app.register_blueprint(restaurant_bp, url_prefix='/api/restaurant') # Exemplo: /api/restaurant
app.register_blueprint(mp_payment_bp, url_prefix='/api/payment') # Exemplo: /api/payment
app.register_blueprint(delivery_calculator_bp, url_prefix='/api/delivery-calc') # Exemplo: /api/delivery-calc
app.register_blueprint(delivery_bp, url_prefix='/api/delivery') # Prefixo para rotas de entregadores
app.register_blueprint(admin_bp, url_prefix='/api/admin') # Prefixo para rotas de admin


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