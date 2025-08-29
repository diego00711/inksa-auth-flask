import os
import sys
import re
from pathlib import Path
from flask import Flask, jsonify, request
from dotenv import load_dotenv
from flask_cors import CORS
from flask_socketio import SocketIO
import mercadopago
import logging

current_dir = Path(__file__).parent
project_root = current_dir.parent
sys.path.insert(0, str(project_root))

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()

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
    # IMPORTANTE: importar o blueprint do cliente
    from src.routes.client import client_bp
except ImportError as e:
    logging.error(f"Erro de importação: {e}")
    raise

app = Flask(__name__)
app.url_map.strict_slashes = False

config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.py')
if os.path.exists(config_path):
    app.config.from_pyfile(config_path)
else:
    logging.warning("Arquivo config.py não encontrado. Usando configurações padrão.")

app.config['SECRET_KEY'] = os.environ.get('JWT_SECRET', 'fallback-secret-key-change-in-production')

# Se usar cookies HttpOnly para autenticação, mantenha estes flags:
app.config.update(
    SESSION_COOKIE_SAMESITE="None",
    SESSION_COOKIE_SECURE=True,
)

# Configuração CORS simplificada e eficaz
CORS(
    app,
    origins=[
        "https://restaurante.inksadelivery.com.br",
        "https://admin.inksadelivery.com.br",
        "https://clientes.inksadelivery.com.br",
        "https://entregadores.inksadelivery.com.br",
        "https://app.inksadelivery.com.br",
        "https://inksa-admin-v0.vercel.app",
        "https://inksa-clientes-v0.vercel.app",
        "https://inksa-restaurantes-v0.vercel.app",
        "https://inksa-entregadores-v0.vercel.app",
        "http://localhost:3000",
        "http://localhost:5173",
        "http://localhost:5000"
    ],
    supports_credentials=True,
    allow_headers=["Content-Type", "Authorization", "X-Requested-With", "Accept"],
    methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH", "HEAD"],
    expose_headers=["Content-Type", "Authorization"],
    max_age=600,
)

socketio = SocketIO(
    app,
    cors_allowed_origins=[
        "https://restaurante.inksadelivery.com.br",
        "https://admin.inksadelivery.com.br",
        "https://clientes.inksadelivery.com.br",
        "https://entregadores.inksadelivery.com.br",
        "http://localhost:3000",
        "http://localhost:5173"
    ],
    async_mode='eventlet',
    logger=False,
    engineio_logger=False
)

# Registro de blueprints
app.register_blueprint(auth_bp, url_prefix='/api/auth')   # login/registro
app.register_blueprint(client_bp, url_prefix='/api')      # RESULTA: /api/auth/profile
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
app.register_blueprint(admin_logs_bp)
app.register_blueprint(admin_users_bp)

MERCADO_PAGO_ACCESS_TOKEN = os.environ.get("MERCADO_PAGO_ACCESS_TOKEN")
if MERCADO_PAGO_ACCESS_TOKEN:
    app.mp_sdk = mercadopago.SDK(MERCADO_PAGO_ACCESS_TOKEN)
    logging.info("Mercado Pago SDK inicializado com sucesso")
else:
    app.mp_sdk = None
    logging.warning("MERCADO_PAGO_ACCESS_TOKEN não encontrado!")

@app.before_request
def before_request():
    if request.method == 'OPTIONS':
        return '', 200
    origin = request.headers.get('Origin')
    logger.info(f"{request.method} {request.path} - Origin: {origin}")

@app.after_request
def after_request(response):
    origin = request.headers.get('Origin')
    if origin and origin in [
        "https://restaurante.inksadelivery.com.br",
        "https://admin.inksadelivery.com.br",
        "https://clientes.inksadelivery.com.br",
        "https://entregadores.inksadelivery.com.br",
        "http://localhost:3000",
        "http://localhost:5173"
    ]:
        response.headers.add('Access-Control-Allow-Origin', origin)
        response.headers.add('Access-Control-Allow-Credentials', 'true')
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type, Authorization, X-Requested-With, Accept')
        response.headers.add('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS, PATCH')
    return response

@app.route('/')
def index():
    return jsonify({
        "status": "online",
        "message": "Servidor Inksa funcionando!",
        "version": "1.0.0",
        "cors_allowed": True,
        "endpoints": {
            "auth": "/api/auth",
            "orders": "/api/orders",
            "menu": "/api/menu",
            "restaurant": "/api/restaurant",
            "payment": "/api/payment",
            "admin": "/api/admin",
            "delivery": "/api/delivery",
            "health": "/api/health",
            "logs": "/api/logs"
        }
    })

@app.route('/api/debug/routes')
def debug_routes():
    # Ajuda a confirmar se /api/auth/profile está registrado
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

@app.route('/api/cors-test')
def cors_test():
    try:
        origin = request.headers.get('Origin')
        allowed_origins = [
            "https://restaurante.inksadelivery.com.br",
            "https://admin.inksadelivery.com.br",
            "https://clientes.inksadelivery.com.br",
            "https://entregadores.inksadelivery.com.br",
            "http://localhost:3000",
            "http://localhost:5173"
        ]
        allowed = origin in allowed_origins if origin else False
        
        return jsonify({
            "message": "CORS test successful",
            "your_origin": origin,
            "allowed": allowed,
            "allowed_origins": allowed_origins
        })
    except Exception as e:
        logger.error(f"Erro em /api/cors-test: {e}")
        return jsonify({"error": "Erro interno no /api/cors-test", "details": str(e)}), 500

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

@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Endpoint não encontrado", "path": request.path}), 404

@app.errorhandler(500)
def internal_error(error):
    logger.error(f"Erro interno: {error}")
    return jsonify({"error": "Erro interno do servidor"}), 500

@app.errorhandler(405)
def method_not_allowed(error):
    return jsonify({"error": "Método não permitido", "method": request.method}), 405

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    logger.info(f"Iniciando servidor na porta {port} (debug: {debug})")
    socketio.run(app, host='0.0.0.0', port=port, debug=debug, use_reloader=debug)
