# src/main.py

# APLICA O PATCH DO EVENTLET ANTES DE QUALQUER OUTRO IMPORT
import eventlet
eventlet.monkey_patch( )

# Imports padrão do projeto continuam aqui
import os
import sys
from pathlib import Path
from flask import Flask, jsonify
from dotenv import load_dotenv
from flask_cors import CORS
from flask_socketio import SocketIO
import mercadopago
import logging

# Configuração do caminho para imports
current_dir = Path(__file__).parent
project_root = current_dir.parent
sys.path.insert(0, str(project_root))

# Configuração de logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Carrega as variáveis de ambiente
load_dotenv()

# Importações de blueprints
try:
    from src.routes.auth import auth_bp
    from src.routes.orders import orders_bp
    from src.routes.menu import menu_bp
    from src.routes.restaurant import restaurant_bp
    from src.routes.payment import mp_payment_bp
    from src.routes.delivery_calculator import delivery_calculator_bp
    from src.routes.admin import admin_bp
    from src.routes.payouts import payouts_bp
    from src.routes.delivery_auth_profile import delivery_auth_profile_bp
    from src.routes.delivery_orders import delivery_orders_bp
    from src.routes.delivery_stats_earnings import delivery_stats_earnings_bp
    from src.utils.helpers import supabase
    from src.routes.gamification_routes import gamification_bp
    from src.routes.categories import categories_bp
    from src.routes.analytics import analytics_bp
except ImportError as e:
    logging.error(f"Erro de importação: {e}")
    raise

app = Flask(__name__)
app.url_map.strict_slashes = False

# Configuração do ficheiro de config
config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.py')
app.config.from_pyfile(config_path)

# --- INÍCIO DA CORREÇÃO ---

# Lista de todas as origens permitidas para CORS (desenvolvimento e produção)
allowed_origins = [
    # Origens de Desenvolvimento
    "http://localhost:5173", 
    "http://127.0.0.1:5173", 
    "http://localhost:3000", 
    "http://localhost:5174",
    
    # Origens de Produção
    "https://clientes.inksadelivery.com.br",
    "https://admin.inksadelivery.com.br", # Adicionei caso você tenha um painel de admin
    "https://entregador.inksadelivery.com.br" # Adicionei para o app de entregador
]

# Configuração do CORS para permitir as origens listadas em todas as rotas /api/*
CORS(app, 
     resources={r"/api/*": {"origins": allowed_origins}},
     supports_credentials=True
 )

# Configuração do SocketIO para usar a mesma lista de origens permitidas
# Adicionamos async_mode='eventlet' para ser explícito
socketio = SocketIO(app, cors_allowed_origins=allowed_origins, async_mode='eventlet')

# --- FIM DA CORREÇÃO ---

# Registro de blueprints
app.register_blueprint(auth_bp, url_prefix='/api/auth')
app.register_blueprint(orders_bp, url_prefix='/api/orders')
app.register_blueprint(menu_bp, url_prefix='/api/menu')
app.register_blueprint(restaurant_bp, url_prefix='/api/restaurant')
app.register_blueprint(mp_payment_bp, url_prefix='/api/payment')
app.register_blueprint(delivery_calculator_bp, url_prefix='/api/delivery-calc')
app.register_blueprint(delivery_auth_profile_bp, url_prefix='/api/delivery')
app.register_blueprint(delivery_orders_bp, url_prefix='/api/delivery')
app.register_blueprint(delivery_stats_earnings_bp, url_prefix='/api/delivery')
app.register_blueprint(admin_bp, url_prefix='/api/admin')
app.register_blueprint(payouts_bp, url_prefix='/api/admin') 
app.register_blueprint(gamification_bp, url_prefix='/api/gamification')
app.register_blueprint(categories_bp, url_prefix='/api/categories')
app.register_blueprint(analytics_bp, url_prefix='/api/analytics')

# Configuração do Mercado Pago
MERCADO_PAGO_ACCESS_TOKEN = os.environ.get("MERCADO_PAGO_ACCESS_TOKEN")
app.mp_sdk = mercadopago.SDK(MERCADO_PAGO_ACCESS_TOKEN) if MERCADO_PAGO_ACCESS_TOKEN else None
if not MERCADO_PAGO_ACCESS_TOKEN:
    logging.warning("MERCADO_PAGO_ACCESS_TOKEN não encontrado!")

@app.route('/')
def index():
    return jsonify({
        "status": "online",
        "message": "Servidor Inksa funcionando!"
    })

@socketio.on('connect')
def handle_connect():
    logging.info('Cliente conectado via WebSocket')

@socketio.on('disconnect')
def handle_disconnect():
    logging.info('Cliente desconectado')

