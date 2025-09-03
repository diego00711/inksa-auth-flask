# src/main.py - VERSÃO FINAL COMPLETA E CORRIGIDA

import os
import sys
import re
from pathlib import Path
from flask import Flask, jsonify, request, make_response, Blueprint
from dotenv import load_dotenv
from flask_cors import CORS
from flask_socketio import SocketIO
import mercadopago
import logging

# --- Configuração de Path e Logging ---
current_dir = Path(__file__).parent
project_root = current_dir.parent
sys.path.insert(0, str(project_root))

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()

# --- Importações dos Blueprints ---
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
    from src.routes.admin_logs import admin_logs_bp
    from src.routes.admin_users import admin_users_bp
    from src.routes.client import client_bp
    from src.routes.avaliacao.restaurante_reviews import restaurante_reviews_bp
    from src.routes.avaliacao.entregador_reviews import entregador_reviews_bp
    from src.routes.avaliacao.menu_item_reviews import menu_item_reviews_bp
    from src.routes.avaliacao.cliente_reviews import cliente_reviews_bp
except ImportError as e:
    logging.error(f"Erro de importação: {e}")
    raise

# --- Inicialização do App ---
app = Flask(__name__)
app.url_map.strict_slashes = False

config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.py')
if os.path.exists(config_path):
    app.config.from_pyfile(config_path)
else:
    logging.warning("Arquivo config.py não encontrado. Usando configurações padrão.")

app.config['SECRET_KEY'] = os.environ.get('JWT_SECRET', 'fallback-secret-key-change-in-production')
app.config.update(
    SESSION_COOKIE_SAMESITE="None",
    SESSION_COOKIE_SECURE=True,
)

# --- Configuração de CORS Global CORRIGIDA ---
production_origins = [
    "https://restaurante.inksadelivery.com.br",
    "https://admin.inksadelivery.com.br",
    "https://clientes.inksadelivery.com.br",
    "https://entregadores.inksadelivery.com.br",
    "https://app.inksadelivery.com.br",
    "https://inksa-entregadoresv0-bszzimhel-inksas-projects.vercel.app",
    "https://*.vercel.app",
    "http://localhost:3000",
    "http://localhost:5173"
]

# Configuração CORRETA do CORS
CORS(app, 
    origins=production_origins,
    supports_credentials=True,
    allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
    methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"]
)

# --- Configuração do SocketIO ---
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet', logger=False, engineio_logger=False)

# --- REGISTRO DE BLUEPRINTS ---
app.register_blueprint(auth_bp, url_prefix='/api/auth')
app.register_blueprint(client_bp, url_prefix='/api/client')
app.register_blueprint(restaurant_bp, url_prefix='/api/restaurant')
app.register_blueprint(menu_bp, url_prefix='/api/menu')

# ✅✅✅ CORREÇÃO DE CORS PARA PEDIDOS ✅✅✅
CORS(orders_bp, supports_credentials=True)
app.register_blueprint(orders_bp, url_prefix='/api/orders')

app.register_blueprint(categories_bp, url_prefix='/api/categories')
app.register_blueprint(analytics_bp, url_prefix='/api/analytics')
app.register_blueprint(gamification_bp, url_prefix='/api/gamification')

# --- Rotas de Delivery agrupadas sob /api/delivery ---
delivery_bp = Blueprint('delivery', __name__, url_prefix='/api/delivery')
CORS(delivery_bp, supports_credentials=True)
delivery_bp.register_blueprint(delivery_auth_profile_bp)
delivery_bp.register_blueprint(delivery_orders_bp, url_prefix='/orders')
delivery_bp.register_blueprint(delivery_stats_earnings_bp, url_prefix='/stats')
app.register_blueprint(delivery_bp)

# --- Rotas de Admin agrupadas sob /api/admin ---
admin_api_bp = Blueprint('admin_api', __name__, url_prefix='/api/admin')
CORS(admin_api_bp, supports_credentials=True)
admin_api_bp.register_blueprint(admin_bp)
admin_api_bp.register_blueprint(payouts_bp, url_prefix='/payouts')
admin_api_bp.register_blueprint(admin_logs_bp, url_prefix='/logs')
admin_api_bp.register_blueprint(admin_users_bp, url_prefix='/users')
app.register_blueprint(admin_api_bp)

# --- Rotas com prefixos customizados ---
app.register_blueprint(mp_payment_bp, url_prefix='/payment')
app.register_blueprint(delivery_calculator_bp, url_prefix='/delivery-calc')

# --- Rotas de Avaliação agrupadas sob /api/review ---
app.register_blueprint(restaurante_reviews_bp, url_prefix='/api/review')
app.register_blueprint(entregador_reviews_bp, url_prefix='/api/review')
app.register_blueprint(menu_item_reviews_bp, url_prefix='/api/review')
app.register_blueprint(cliente_reviews_bp, url_prefix='/api/review')

# --- Inicialização de Serviços Externos ---
MERCADO_PAGO_ACCESS_TOKEN = os.environ.get("MERCADO_PAGO_ACCESS_TOKEN")
if MERCADO_PAGO_ACCESS_TOKEN:
    app.mp_sdk = mercadopago.SDK(MERCADO_PAGO_ACCESS_TOKEN)
    logging.info("Mercado Pago SDK inicializado com sucesso")
else:
    app.mp_sdk = None
    logging.warning("MERCADO_PAGO_ACCESS_TOKEN não encontrado!")

# --- Middleware CORS para headers ---
@app.after_request
def after_request(response):
    origin = request.headers.get('Origin')
    if origin in production_origins:
        response.headers.add('Access-Control-Allow-Origin', origin)
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization,X-Requested-With')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    response.headers.add('Access-Control-Allow-Credentials', 'true')
    return response

# --- Handler para OPTIONS requests ---
@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        response = make_response()
        origin = request.headers.get('Origin')
        if origin in production_origins:
            response.headers.add("Access-Control-Allow-Origin", origin)
        response.headers.add("Access-Control-Allow-Headers", "Content-Type,Authorization,X-Requested-With")
        response.headers.add("Access-Control-Allow-Methods", "GET,PUT,POST,DELETE,OPTIONS")
        response.headers.add("Access-Control-Allow-Credentials", "true")
        return response

# --- Rotas de Status e Debug ---
@app.route('/')
def index():
    return jsonify({"status": "online", "message": "Servidor Inksa funcionando!"})

@app.route('/api/debug/routes')
def debug_routes():
    rules = []
    for rule in app.url_map.iter_rules():
        methods = sorted(m for m in rule.methods if m not in ('HEAD',))
        rules.append({"rule": str(rule), "methods": methods, "endpoint": rule.endpoint})
    return jsonify({"routes": rules})

@app.route('/api/health')
def health_check():
    return jsonify({
        "status": "healthy",
        "database": "connected" if supabase else "disconnected",
        "mercado_pago": "configured" if app.mp_sdk else "not_configured",
        "cors_enabled": True
    })

# --- Handlers WebSocket ---
@socketio.on('connect')
def handle_connect():
    logger.info(f'Cliente conectado via WebSocket: {request.sid}')
    return {'status': 'connected', 'sid': request.sid}

@socketio.on('disconnect')
def handle_disconnect():
    logger.info(f'Cliente desconectado: {request.sid}')

@socketio.on('ping')
def handle_ping(data):
    logger.info(f'Ping recebido de {request.sid}: {data}')
    return {'response': 'pong', 'sid': request.sid}

# --- Handlers de Erro ---
@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Endpoint não encontrado", "path": request.path}), 404

@app.errorhandler(500)
def internal_error(error):
    logger.error(f"Erro interno: {error}", exc_info=True)
    return jsonify({"error": "Erro interno do servidor"}), 500

@app.errorhandler(405)
def method_not_allowed(error):
    return jsonify({"error": "Método não permitido", "method": request.method}), 405

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    logger.info(f"Iniciando servidor na porta {port} (debug: {debug})")
    socketio.run(app, host='0.0.0.0', port=port, debug=debug, use_reloader=debug)
