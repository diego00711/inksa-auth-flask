# -*- coding: utf-8 -*-
# src/routes/rewards_routes.py
import logging
import traceback

from flask import Blueprint, request, jsonify, current_app
from flask_cors import CORS
import psycopg2
import psycopg2.extras

from ..utils.helpers import get_user_id_from_token

logger = logging.getLogger(__name__)

_CORS_ORIGINS = [
    "http://localhost:3000", "http://127.0.0.1:3000",
    "http://localhost:5173", "http://127.0.0.1:5173",
    r"https://.*\.vercel\.app",
    "https://admin.inksadelivery.com.br",
    "https://clientes.inksadelivery.com.br",
    "https://restaurantes.inksadelivery.com.br",
    "https://entregadores.inksadelivery.com.br",
]

rewards_bp = Blueprint("rewards", __name__, url_prefix="/rewards")
CORS(rewards_bp, origins=_CORS_ORIGINS, supports_credentials=True)


# ---------- infra ----------

def _db():
    factory = current_app.config.get("DB_CONN_FACTORY")
    if not factory:
        raise RuntimeError("DB_CONN_FACTORY não configurado no app")
    return factory()


def _ok(data, code=200):
    return jsonify({"status": "success", "data": data}), code


def _err(message="internal_error", code=400, **extra):
    payload = {"status": "error", "message": message}
    payload.update(extra)
    return jsonify(payload), code


# ---------- rotas ----------

@rewards_bp.get("")
@rewards_bp.get("/")
def list_rewards():
    """
    GET /api/rewards?type=all|clients|deliverers|restaurants
    Lista recompensas disponíveis, opcionalmente filtradas por tipo de usuário.
    Se a tabela rewards não existir, retorna lista vazia graciosamente.
    """
    user_type_filter = request.args.get("type", "all")

    conn = None
    try:
        conn = _db()
        with conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Verifica se a tabela rewards existe
            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = 'rewards'
                ) AS exists
            """)
            if not cur.fetchone()["exists"]:
                logger.warning("Tabela 'rewards' não encontrada. Retornando lista vazia.")
                return _ok({"items": [], "total": 0})

            where = []
            params = []

            if user_type_filter != "all":
                # Mapeia query param para valor armazenado no banco
                type_map = {
                    "clients": "client",
                    "deliverers": "delivery",
                    "restaurants": "restaurant",
                }
                mapped = type_map.get(user_type_filter, user_type_filter)
                where.append("(target_user_type = %s OR target_user_type = 'all')")
                params.append(mapped)

            where.append("is_active = TRUE")
            where_sql = "WHERE " + " AND ".join(where) if where else ""

            cur.execute(f"""
                SELECT id, name, description, points_cost, target_user_type,
                       reward_type, reward_value, stock_quantity,
                       valid_until, is_active, created_at
                FROM public.rewards
                {where_sql}
                ORDER BY points_cost ASC
            """, params)

            rows = [dict(r) for r in cur.fetchall()]
            for r in rows:
                r["id"] = str(r["id"])
                if r.get("valid_until") and hasattr(r["valid_until"], "isoformat"):
                    r["valid_until"] = r["valid_until"].isoformat()
                if r.get("created_at") and hasattr(r["created_at"], "isoformat"):
                    r["created_at"] = r["created_at"].isoformat()

            return _ok({"items": rows, "total": len(rows)})

    except Exception as e:
        current_app.logger.exception("rewards.list_rewards failed")
        return _ok({"items": [], "total": 0})  # gracioso: retorna vazio em vez de 500
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@rewards_bp.post("/redeem")
def redeem_reward():
    """
    POST /api/rewards/redeem
    Body: {user_id, reward_id}
    Debita pontos do usuário e registra o resgate.
    """
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id")
    reward_id = body.get("reward_id")

    if not user_id or not reward_id:
        return _err("user_id e reward_id são obrigatórios", 422)

    conn = None
    try:
        conn = _db()
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                # Verifica se as tabelas existem
                cur.execute("""
                    SELECT
                        EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='rewards') AS r_exists,
                        EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='reward_redemptions') AS rr_exists,
                        EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='user_points') AS up_exists
                """)
                exists = cur.fetchone()
                if not exists["r_exists"] or not exists["up_exists"]:
                    return _err("Sistema de recompensas não configurado ainda", 503)

                # Busca recompensa
                cur.execute("""
                    SELECT id, name, points_cost, stock_quantity, is_active
                    FROM public.rewards
                    WHERE id = %s
                """, (reward_id,))
                reward = cur.fetchone()
                if not reward:
                    return _err("Recompensa não encontrada", 404)
                if not reward["is_active"]:
                    return _err("Recompensa não está disponível", 400)
                if reward["stock_quantity"] is not None and reward["stock_quantity"] <= 0:
                    return _err("Recompensa esgotada", 400)

                # Busca pontos do usuário
                cur.execute("""
                    SELECT total_points FROM public.user_points WHERE user_id = %s
                """, (user_id,))
                up = cur.fetchone()
                total_points = int(up["total_points"]) if up else 0

                points_cost = int(reward["points_cost"])
                if total_points < points_cost:
                    return _err(
                        "Pontos insuficientes",
                        400,
                        required=points_cost,
                        available=total_points
                    )

                # Debita pontos
                cur.execute("""
                    UPDATE public.user_points
                    SET total_points = total_points - %s, last_updated = NOW()
                    WHERE user_id = %s
                    RETURNING total_points
                """, (points_cost, user_id))
                new_total = int(cur.fetchone()["total_points"])

                # Registra resgate (se tabela existir)
                redemption_id = None
                if exists["rr_exists"]:
                    cur.execute("""
                        INSERT INTO public.reward_redemptions
                            (id, user_id, reward_id, points_spent, status, redeemed_at)
                        VALUES (gen_random_uuid(), %s, %s, %s, 'pending', NOW())
                        RETURNING id
                    """, (user_id, reward_id, points_cost))
                    row = cur.fetchone()
                    if row:
                        redemption_id = str(row["id"])

                # Atualiza estoque se houver controle
                if reward["stock_quantity"] is not None:
                    cur.execute("""
                        UPDATE public.rewards
                        SET stock_quantity = stock_quantity - 1
                        WHERE id = %s
                    """, (reward_id,))

                return _ok({
                    "success": True,
                    "redemption_id": redemption_id,
                    "reward_name": reward["name"],
                    "points_spent": points_cost,
                    "remaining_points": new_total,
                })

    except Exception as e:
        current_app.logger.exception("rewards.redeem_reward failed")
        traceback.print_exc()
        return _err("Erro interno ao processar resgate", 500, detail=str(e))
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@rewards_bp.get("/history/<user_id>")
def redemption_history(user_id):
    """
    GET /api/rewards/history/<user_id>
    Retorna histórico de resgates do usuário.
    """
    try:
        limit = min(max(int(request.args.get("limit", 50)), 1), 200)
        page = max(int(request.args.get("page", 1)), 1)
        offset = (page - 1) * limit
    except (ValueError, TypeError):
        limit, page, offset = 50, 1, 0

    conn = None
    try:
        conn = _db()
        with conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Verifica tabela
            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = 'reward_redemptions'
                ) AS exists
            """)
            if not cur.fetchone()["exists"]:
                return _ok({"items": [], "total": 0, "page": page, "limit": limit})

            cur.execute("""
                SELECT rr.id, rr.reward_id, r.name AS reward_name,
                       rr.points_spent, rr.status, rr.redeemed_at
                FROM public.reward_redemptions rr
                LEFT JOIN public.rewards r ON r.id = rr.reward_id
                WHERE rr.user_id = %s
                ORDER BY rr.redeemed_at DESC
                LIMIT %s OFFSET %s
            """, (user_id, limit, offset))

            rows = [dict(r) for r in cur.fetchall()]
            for r in rows:
                r["id"] = str(r["id"])
                r["reward_id"] = str(r["reward_id"]) if r.get("reward_id") else None
                if r.get("redeemed_at") and hasattr(r["redeemed_at"], "isoformat"):
                    r["redeemed_at"] = r["redeemed_at"].isoformat()

            cur.execute(
                "SELECT COUNT(*)::int AS c FROM public.reward_redemptions WHERE user_id = %s",
                (user_id,)
            )
            total = int(cur.fetchone()["c"])

            return _ok({"items": rows, "total": total, "page": page, "limit": limit})

    except Exception as e:
        current_app.logger.exception("rewards.redemption_history failed")
        return _ok({"items": [], "total": 0, "page": page, "limit": limit})
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
