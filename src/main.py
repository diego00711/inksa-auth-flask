import os
import sys
import logging
from pathlib import Path
from datetime import datetime
from flask import Flask, jsonify, request, Blueprint, make_response
from flask_cors import CORS
from flask_compress import Compress
from flask_socketio import SocketIO
from dotenv import load_dotenv
import mercadopago
import psycopg2
import re

# --- Sentry Error Monitoring ---
import sentry_sdk
from sentry_sdk.integrations.flask import FlaskIntegration

SENTRY_DSN = os.environ.get("SENTRY_DSN", "https://0ab4c11f20659cb2404109e7e177f018@o4511445143912448.ingest.us.sentry.io/4511445160099840")
if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[FlaskIntegration()],
        traces_sample_rate=0.05,
        environment=os.environ.get("FLASK_ENV", "production"),
    )
    logging.info("Sentry inicializado com sucesso")
# Variável de ambiente SENTRY_DSN: configure em Render → Environment Variables

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
    from src.routes.upload import upload_bp
    from src.routes.restaurant import restaurant_bp
    from src.routes.payment import mp_payment_bp
    from src.routes.delivery_calculator import delivery_calculator_bp
    from src.routes.admin import admin_bp
    from src.routes.payouts import payouts_bp
    from src.routes.delivery_auth_profile import delivery_auth_profile_bp
    from src.routes.delivery_orders import delivery_orders_bp
    from src.routes.delivery_stats_earnings import delivery_stats_earnings_bp
    from src.routes.banners import banners_bp
    from src.utils.helpers import supabase
    # >>> importa também o blueprint “admin” de gamificação
    from src.routes.gamification_routes import gamification_bp, admin_gamification_bp
    from src.routes.rewards_routes import rewards_bp
    from src.routes.challenges_routes import challenges_bp
    from src.routes.categories import categories_bp
    from src.routes.analytics import analytics_bp
    from .routes.analytics_admin import analytics_admin_bp
    from src.routes.admin_logs import admin_logs_bp
    from src.routes.admin_users import admin_users_bp, legacy_admin_users_bp
    from src.routes.client import client_bp
    from src.routes.settings import settings_bp
    from src.routes.admin_permissions import admin_permissions_bp
    from src.routes.avaliacao.restaurante_reviews import restaurante_reviews_bp
    from src.routes.avaliacao.entregador_reviews import entregador_reviews_bp
    from src.routes.avaliacao.menu_item_reviews import menu_item_reviews_bp
    from src.routes.avaliacao.cliente_reviews import cliente_reviews_bp
    from src.routes.public_restaurants import public_restaurants_bp
    from src.routes.fcm_routes import fcm_bp
    from src.routes.tracking_routes import tracking_bp
    from src.routes.coupons_routes import coupons_bp
    from src.routes.chat_routes import chat_bp
    from src.routes.club_routes import club_bp
    from src.scheduler import start_scheduler
except ImportError as e:
    logging.error(f"Erro de importação: {e}")
    raise

# --- Inicialização do App ---
app = Flask(__name__)
app.url_map.strict_slashes = False
Compress(app)

# --- Rate Limiting ---
from flask import jsonify as _jsonify
from src.extensions import limiter

limiter.init_app(app)

@app.errorhandler(429)
def ratelimit_error(e):
    return _jsonify({
        "error": "Muitas requisições. Aguarde um momento e tente novamente.",
        "retry_after": str(e.description)
    }), 429

# --- Segredos: nunca cair para um default inseguro e conhecido ---
import secrets as _secrets

def _secure_secret(env_name, weak_default):
    """Retorna o segredo do ambiente. Se ausente ou igual ao default inseguro,
    gera um valor aleatório (fail-closed) e avisa para configurar no Render —
    em vez de subir com uma chave pública conhecida."""
    val = os.environ.get(env_name)
    if not val or val == weak_default:
        logging.critical(
            "%s nao configurado (ou usando default inseguro). Gerando valor "
            "aleatorio temporario — CONFIGURE %s nas variaveis do Render!",
            env_name, env_name,
        )
        return _secrets.token_urlsafe(48)
    return val

# --- Configuração do Banco para Gamificação ---
app.config["DB_CONN_FACTORY"] = lambda: psycopg2.connect(os.environ["DATABASE_URL"])
app.config["GAMIFICATION_INTERNAL_TOKEN"] = _secure_secret(
    "GAMIFICATION_INTERNAL_TOKEN", "token-secreto-trocar"
)

config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.py')
if os.path.exists(config_path):
    app.config.from_pyfile(config_path)
else:
    logging.warning("Arquivo config.py não encontrado. Usando configurações padrão.")

app.config['SECRET_KEY'] = _secure_secret('JWT_SECRET', 'fallback-secret-key-change-in-production')
app.config.update(
    SESSION_COOKIE_SAMESITE="None",
    SESSION_COOKIE_SECURE=True,
)

# ---------------- CORS ROBUSTO ----------------
PROD_ORIGINS = [
    "https://clientes.inksadelivery.com.br",
    "https://restaurantes.inksadelivery.com.br",
    "https://restaurante.inksadelivery.com.br",
    "https://entregadores.inksadelivery.com.br",
    "https://admin.inksadelivery.com.br",
    "https://app.inksadelivery.com.br",
]
VERCEL_BASE = ".vercel.app"
LOCAL_HOSTS = [
    "http://localhost:3000", "http://127.0.0.1:3000",
    "http://localhost:5173", "http://127.0.0.1:5173",
]
EXTRA = [o.strip() for o in os.environ.get("EXTRA_ALLOWED_ORIGINS", "").split(",") if o.strip()]
ALLOWED_ORIGINS = set(PROD_ORIGINS + LOCAL_HOSTS + EXTRA)

def is_allowed_origin(origin: str) -> bool:
    if not origin:
        return False
    if origin in ALLOWED_ORIGINS:
        return True
    if origin.endswith(VERCEL_BASE):
        return True
    if re.match(r"^http://localhost:\d+$", origin) or re.match(r"^http://127\.0\.0\.1:\d+$", origin):
        return True
    return False

CORS(
    app,
    resources={r"/api/*": {"origins": "*"}},
    supports_credentials=True,
    allow_headers=["Content-Type", "Authorization"],
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
)

@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        origin = request.headers.get("Origin", "")
        resp = make_response()
        if is_allowed_origin(origin):
            resp.headers["Access-Control-Allow-Origin"] = origin
        else:
            resp.headers["Access-Control-Allow-Origin"] = "null"
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
        return resp, 204

_CACHEABLE_PREFIXES = ('/api/restaurants', '/api/menu', '/api/banners', '/api/categories')

@app.after_request
def add_cache_headers(response):
    if request.method == 'GET' and response.status_code == 200:
        if any(request.path.startswith(p) for p in _CACHEABLE_PREFIXES):
            response.headers.setdefault('Cache-Control', 'public, max-age=120')
    return response


@app.after_request
def add_cors_headers(response):
    origin = request.headers.get("Origin", "")
    if is_allowed_origin(origin):
        response.headers.setdefault("Access-Control-Allow-Origin", origin)
        response.headers.setdefault("Vary", "Origin")
        response.headers.setdefault("Access-Control-Allow-Credentials", "true")
        response.headers.setdefault("Access-Control-Allow-Headers", "Content-Type, Authorization")
        response.headers.setdefault("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
    return response

# --- Configuração do SocketIO ---
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet', logger=False, engineio_logger=False)

# --- REGISTRO DE BLUEPRINTS ---
app.register_blueprint(banners_bp, url_prefix='/api/banners')
app.register_blueprint(auth_bp, url_prefix='/api/auth')
app.register_blueprint(public_restaurants_bp, url_prefix='/api/restaurants')
app.register_blueprint(client_bp, url_prefix='/api/client')
app.register_blueprint(restaurant_bp, url_prefix='/api/restaurant')
app.register_blueprint(menu_bp, url_prefix='/api/menu')
app.register_blueprint(upload_bp, url_prefix='/api/upload')
app.register_blueprint(orders_bp, url_prefix='/api/orders')
app.register_blueprint(categories_bp, url_prefix='/api/categories')
app.register_blueprint(analytics_bp, url_prefix='/api/analytics')
app.register_blueprint(analytics_admin_bp, url_prefix="/api/analytics")

# Gamificação (apps) e Gamificação (Admin)
app.register_blueprint(gamification_bp, url_prefix='/api/gamification')
app.register_blueprint(admin_gamification_bp, url_prefix='/api/admin/gamification')
app.register_blueprint(rewards_bp, url_prefix='/api/rewards')
app.register_blueprint(challenges_bp, url_prefix='/api/challenges')

# Pagamento
app.register_blueprint(mp_payment_bp, url_prefix='/api')

# --- Rotas de Delivery agrupadas sob /api/delivery ---
delivery_bp = Blueprint('delivery', __name__, url_prefix='/api/delivery')
delivery_bp.register_blueprint(delivery_auth_profile_bp)
delivery_bp.register_blueprint(delivery_orders_bp, url_prefix='/orders')
delivery_bp.register_blueprint(delivery_stats_earnings_bp, url_prefix='/stats')
delivery_bp.register_blueprint(delivery_calculator_bp)
app.register_blueprint(delivery_bp)

# --- Novas Features: FCM, Rastreamento, Cupons, Chat ---
app.register_blueprint(fcm_bp, url_prefix='/api/profile')
app.register_blueprint(tracking_bp, url_prefix='/api/deliveries')
app.register_blueprint(coupons_bp, url_prefix='/api/coupons')
app.register_blueprint(chat_bp, url_prefix='/api/chat')
app.register_blueprint(club_bp, url_prefix='/api/club')

# --- Rotas de Admin ---
app.register_blueprint(admin_bp, url_prefix='/api/admin')
app.register_blueprint(payouts_bp, url_prefix='/api/admin/payouts')
app.register_blueprint(admin_logs_bp, url_prefix='/api/admin/logs')
app.register_blueprint(settings_bp, url_prefix='/api/admin/settings')
app.register_blueprint(admin_permissions_bp, url_prefix='/api/admin/permissions')

# Users: oficial e legacy
app.register_blueprint(admin_users_bp, url_prefix='/api/users')
app.register_blueprint(legacy_admin_users_bp, url_prefix='/api/admin/users')

# --- Rotas de Avaliação agrupadas sob /api/review ---
app.register_blueprint(restaurante_reviews_bp, url_prefix='/api/review')
app.register_blueprint(entregador_reviews_bp, url_prefix='/api/review')
app.register_blueprint(menu_item_reviews_bp, url_prefix='/api/review')
app.register_blueprint(cliente_reviews_bp, url_prefix='/api/review')

# --- Scheduler de Payouts Automáticos ---
# Guard: Werkzeug reloader spawns a child process with WERKZEUG_RUN_MAIN=true.
# We start the scheduler only in the child (or in non-debug/production mode)
# to avoid two scheduler instances during local development.
import os as _os
_in_reloader_child = _os.environ.get("WERKZEUG_RUN_MAIN") == "true"
_debug_mode = _os.environ.get("FLASK_DEBUG", "false").lower() == "true"
if not _debug_mode or _in_reloader_child:
    try:
        start_scheduler(app)
    except Exception as _sched_err:
        logging.warning(f"Scheduler não pôde ser iniciado: {_sched_err}")

# --- Inicialização de Serviços Externos ---
MERCADO_PAGO_ACCESS_TOKEN = os.environ.get("MERCADO_PAGO_ACCESS_TOKEN")
if MERCADO_PAGO_ACCESS_TOKEN:
    app.mp_sdk = mercadopago.SDK(MERCADO_PAGO_ACCESS_TOKEN)
    logging.info("Mercado Pago SDK inicializado com sucesso")
else:
    app.mp_sdk = None
    logging.warning("MERCADO_PAGO_ACCESS_TOKEN não encontrado!")

# --- Rotas de Status e Debug ---
@app.route('/')
def index():
    return jsonify({"status": "online", "message": "Servidor Inksa funcionando!"})

@app.route('/health')
def health_check_simple():
    return jsonify({
        "status": "ok",
        "message": "Server is running",
        "timestamp": datetime.now().isoformat(),
        "service": "Inksa Delivery API"
    }), 200

@app.route('/healthz')
def healthz():
    return jsonify({"status": "ok"}), 200

@app.route('/api/healthz')
def api_healthz():
    return jsonify({"status": "ok"}), 200

@app.route('/api/debug/routes')
def debug_routes():
    rules = []
    for rule in app.url_map.iter_rules():
        methods = sorted(m for m in rule.methods if m not in ('HEAD',))
        rules.append({"rule": str(rule), "methods": methods, "endpoint": rule.endpoint})
    return jsonify({"routes": rules})

@app.route('/api/health')
def health_check():
    # Executa uma consulta real: valida a conexão de verdade e conta como
    # atividade no Supabase, evitando a pausa automática do plano gratuito.
    db_status = "disconnected"
    if supabase:
        try:
            supabase.table("orders").select("id").limit(1).execute()
            db_status = "connected"
        except Exception as exc:
            logger.warning(f"Health check: consulta ao banco falhou: {exc}")
            db_status = "error"
    return jsonify({
        "status": "ok" if db_status == "connected" else "degraded",
        "timestamp": datetime.now().isoformat(),
        "service": "inksa-auth-flask",
        "version": "1.0.0",
        "database": db_status,
        "mercado_pago": "configured" if app.mp_sdk else "not_configured",
    }), 200

# --- Handlers de SocketIO ---
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
