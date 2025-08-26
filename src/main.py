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

# Configuração do caminho para imports
current_dir = Path(__file__).parent
project_root = current_dir.parent
sys.path.insert(0, str(project_root))

# Configuração de logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

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
if os.path.exists(config_path):
    app.config.from_pyfile(config_path)
else:
    logging.warning("Arquivo config.py não encontrado. Usando configurações padrão.")

# Configuração da chave secreta
app.config['SECRET_KEY'] = os.environ.get('JWT_SECRET', 'fallback-secret-key-change-in-production')

# ----------- CORS: Usando regex para permitir todos os domínios autorizados -----------

ALLOWED_ORIGIN_REGEX = (
    r"^https:\/\/admin\.inksadelivery\.com\.br$|"
    r"^https:\/\/inksa-admin-v0\.vercel\.app$|"
    r"^https:\/\/inksa-admin-v0\-[a-z0-9]+\-inksas-projects\.vercel\.app$|"
    r"^https:\/\/clientes\.inksadelivery\.com\.br$|"
    r"^https:\/\/restaurante\.inksadelivery\.com\.br$|"
    r"^https:\/\/entregadores\.inksadelivery\.com\.br$|"
    r"^https:\/\/app\.inksadelivery\.com\.br$|"
    r"^https:\/\/inksadelivery\.com\.br$"
)

CORS(
    app,
    origins=re.compile(ALLOWED_ORIGIN_REGEX),
    supports_credentials=True,
    allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
    methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"],
    max_age=600,
)

# ---------------------------------------------------------------------------------------------------------------

# Configuração do SocketIO
socketio = SocketIO(app, 
                   cors_allowed_origins=re.compile(ALLOWED_ORIGIN_REGEX),
                   async_mode='eventlet',
                   logger=False,
                   engineio_logger=False)

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
if MERCADO_PAGO_ACCESS_TOKEN:
    app.mp_sdk = mercadopago.SDK(MERCADO_PAGO_ACCESS_TOKEN)
    logging.info("Mercado Pago SDK inicializado com sucesso")
else:
    app.mp_sdk = None
    logging.warning("MERCADO_PAGO_ACCESS_TOKEN não encontrado!")

# Middleware para logging de requests (APENAS LOGGING)
@app.before_request
def before_request():
    logger.info(f"{request.method} {request.path} - Origin: {request.headers.get('Origin')}")

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
            "health": "/api/health"
        }
    })

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
    """Endpoint para testar CORS"""
    try:
        origin = request.headers.get('Origin')
        allowed = bool(re.match(ALLOWED_ORIGIN_REGEX, origin)) if origin else False
        logger.info(f"/api/cors-test | Origin: {origin} | Allowed: {allowed}")
        return jsonify({
            "message": "CORS test successful",
            "your_origin": origin,
            "allowed": allowed
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

# Error handlers
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
    # Para desenvolvimento local
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    
    logger.info(f"Iniciando servidor na porta {port} (debug: {debug})")
    socketio.run(app, host='0.0.0.0', port=port, debug=debug, use_reloader=debug)
