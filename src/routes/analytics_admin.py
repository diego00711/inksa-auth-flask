# src/routes/analytics_admin.py
import re
from flask import Blueprint, jsonify, request
from flask_cors import CORS
from ..utils.helpers import get_db_connection, get_user_id_from_token
from .admin import _build_dashboard_payload  # reaproveita queries do admin dashboard

analytics_admin_bp = Blueprint("analytics_admin_bp", __name__)

# Permitir CORS igual ao admin
CORS(
    analytics_admin_bp,
    origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        re.compile(r"^https://.*\.vercel\.app$"),
        "https://admin.inksadelivery.com.br",
        "https://clientes.inksadelivery.com.br",
        "https://restaurantes.inksadelivery.com.br",
        "https://entregadores.inksadelivery.com.br",
    ],
    supports_credentials=True,
)

def _admin_required():
    auth = request.headers.get("Authorization")
    user_id, user_type, error = get_user_id_from_token(auth)
    if error:
        return None, error
    if user_type != "admin":
        return None, (jsonify({"status": "error", "message": "Acesso não autorizado"}), 403)
    return user_id, None


@analytics_admin_bp.route("/overview", methods=["GET", "OPTIONS"])
def overview():
    """Tudo do dashboard numa chamada só.

    /metrics, /revenue-series e /transactions chamam _build_dashboard_payload
    cada um por sua conta — ou seja, o dashboard rodava o conjunto INTEIRO de
    queries 3 vezes a cada carregamento. No plano free do Render isso é a maior
    parte do tempo de tela. As três rotas continuam existindo pra não quebrar
    nada que ainda as use.
    """
    if request.method == "OPTIONS":
        return jsonify({}), 204
    _, err = _admin_required()
    if err: return err

    date_from = request.args.get("from")
    date_to = request.args.get("to")
    limit = int(request.args.get("limit", 20))

    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão com banco"}), 500

    try:
        p = _build_dashboard_payload(conn, date_from, date_to, limit)
        return jsonify({"status": "success", "data": {
            "kpis": p["kpis"],
            "chartData": p["chartData"],
            "recentOrders": p["recentOrders"],
            # ordersStatus mora FORA de kpis. O front lia kpis.ordersStatus, que
            # nunca existiu — por isso a rosca de "Pedidos por Status" nunca
            # aparecia. Aqui vai no nível certo.
            "ordersStatus": p["ordersStatus"],
            "clientsGrowth": p["clientsGrowth"],
            "operacao": p.get("operacao", {}),
        }}), 200
    finally:
        conn.close()


@analytics_admin_bp.route("/metrics", methods=["GET", "OPTIONS"])
def metrics():
    if request.method == "OPTIONS":
        return jsonify({}), 204
    _, err = _admin_required()
    if err: return err

    date_from = request.args.get("from")
    date_to = request.args.get("to")

    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão com banco"}), 500

    try:
        payload = _build_dashboard_payload(conn, date_from, date_to)
        return jsonify({"status": "success", "data": payload["kpis"]}), 200
    finally:
        conn.close()


@analytics_admin_bp.route("/revenue-series", methods=["GET", "OPTIONS"])
def revenue_series():
    if request.method == "OPTIONS":
        return jsonify({}), 204
    _, err = _admin_required()
    if err: return err

    date_from = request.args.get("from")
    date_to = request.args.get("to")

    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão com banco"}), 500

    try:
        payload = _build_dashboard_payload(conn, date_from, date_to)
        return jsonify({"status": "success", "data": payload["chartData"]}), 200
    finally:
        conn.close()


@analytics_admin_bp.route("/transactions", methods=["GET", "OPTIONS"])
def transactions():
    if request.method == "OPTIONS":
        return jsonify({}), 204
    _, err = _admin_required()
    if err: return err

    date_from = request.args.get("from")
    date_to = request.args.get("to")
    limit = int(request.args.get("limit", 20))

    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão com banco"}), 500

    try:
        payload = _build_dashboard_payload(conn, date_from, date_to, limit)
        return jsonify({"status": "success", "data": payload["recentOrders"]}), 200
    finally:
        conn.close()
