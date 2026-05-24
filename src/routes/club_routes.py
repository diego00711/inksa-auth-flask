# -*- coding: utf-8 -*-
# src/routes/club_routes.py — Clube Inksa: fidelidade automática
import logging
from datetime import datetime
from flask import Blueprint, request, jsonify
from flask_cors import CORS
import psycopg2.extras
from ..utils.helpers import get_db_connection, get_user_id_from_token

logger = logging.getLogger(__name__)

club_bp = Blueprint('club', __name__)

_CORS_ORIGINS = [
    "http://localhost:3000", "http://127.0.0.1:3000",
    "http://localhost:5173", "http://127.0.0.1:5173",
    r"https://.*\.vercel\.app",
    "https://clientes.inksadelivery.com.br",
]
CORS(club_bp, origins=_CORS_ORIGINS, supports_credentials=True)

LEVELS = [
    {
        "level": "bronze", "label": "Bronze", "emoji": "🥉",
        "min_orders": 1, "max_orders": 3,
        "color": "#cd7f32",
        "benefits": ["Acesso a cupons exclusivos Inksa"],
    },
    {
        "level": "prata", "label": "Prata", "emoji": "🥈",
        "min_orders": 4, "max_orders": 7,
        "color": "#a8a9ad",
        "benefits": ["Frete grátis no 5º pedido do mês", "Acesso a cupons exclusivos Inksa"],
    },
    {
        "level": "ouro", "label": "Ouro", "emoji": "🥇",
        "min_orders": 8, "max_orders": 14,
        "color": "#ffd700",
        "benefits": ["Frete grátis em todos os pedidos", "10% de desconto no subtotal", "Cupons exclusivos"],
    },
    {
        "level": "diamante", "label": "Diamante", "emoji": "💎",
        "min_orders": 15, "max_orders": None,
        "color": "#b9f2ff",
        "benefits": ["Frete grátis em todos os pedidos", "15% de desconto no subtotal", "Prioridade no suporte", "Cupons VIP"],
    },
]


def _get_level_for_orders(count: int) -> dict:
    current = LEVELS[0]
    for lvl in LEVELS:
        if count >= lvl["min_orders"]:
            current = lvl
    return current


def _get_next_level(current_level: dict) -> dict | None:
    for i, lvl in enumerate(LEVELS):
        if lvl["level"] == current_level["level"] and i + 1 < len(LEVELS):
            return LEVELS[i + 1]
    return None


@club_bp.route('/levels', methods=['GET'])
def get_levels():
    """GET /api/club/levels — tabela pública de níveis e benefícios."""
    return jsonify({"status": "success", "data": LEVELS}), 200


@club_bp.route('/status', methods=['GET'])
def get_club_status():
    """GET /api/club/status — status do clube do cliente autenticado."""
    try:
        user_auth_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            return error
        if user_type != 'client':
            return jsonify({"error": "Apenas clientes podem consultar o status do clube"}), 403

        conn = get_db_connection()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                # Client profile id and current saved level
                cur.execute("""
                    SELECT id, club_level
                    FROM client_profiles
                    WHERE user_id = %s
                """, (user_auth_id,))
                profile = cur.fetchone()
                if not profile:
                    return jsonify({"error": "Perfil de cliente não encontrado"}), 404

                client_id = profile['id']

                # Count delivered orders this month
                cur.execute("""
                    SELECT COUNT(*) AS cnt,
                           json_agg(json_build_object(
                               'id', o.id,
                               'created_at', o.created_at,
                               'total_amount', o.total_amount
                           ) ORDER BY o.created_at DESC) AS orders
                    FROM orders o
                    WHERE o.client_id = %s
                      AND o.status = 'delivered'
                      AND DATE_TRUNC('month', o.created_at) = DATE_TRUNC('month', NOW())
                """, (client_id,))
                row = cur.fetchone()
                orders_count = int(row['cnt']) if row else 0
                recent_orders = row['orders'] or []

                current_level = _get_level_for_orders(orders_count)
                next_level = _get_next_level(current_level)
                orders_to_next = (next_level['min_orders'] - orders_count) if next_level else 0

                # Update stored level
                cur.execute("""
                    UPDATE client_profiles
                    SET club_level = %s, club_orders_month = %s
                    WHERE id = %s
                """, (current_level['level'], orders_count, client_id))
                conn.commit()

                if next_level:
                    motivation = f"Faltam {orders_to_next} pedido{'s' if orders_to_next != 1 else ''} para você ser {next_level['label']}! {next_level['emoji']}"
                else:
                    motivation = "Você atingiu o nível máximo! 💎 Aproveite todos os benefícios."

                return jsonify({
                    "status": "success",
                    "data": {
                        "current_level": current_level,
                        "next_level": next_level,
                        "orders_this_month": orders_count,
                        "orders_to_next_level": max(0, orders_to_next),
                        "recent_orders": recent_orders[:10],
                        "motivation": motivation,
                    }
                }), 200
        finally:
            conn.close()
    except Exception as e:
        logger.exception("club.get_club_status failed")
        return jsonify({"error": "Erro interno do servidor"}), 500
