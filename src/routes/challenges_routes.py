# -*- coding: utf-8 -*-
# src/routes/challenges_routes.py
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

challenges_bp = Blueprint("challenges", __name__, url_prefix="/challenges")
CORS(challenges_bp, origins=_CORS_ORIGINS, supports_credentials=True)


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


def _tables_exist(cur, *table_names):
    """Retorna dict {table_name: bool} indicando quais tabelas existem."""
    placeholders = ",".join(["%s"] * len(table_names))
    cur.execute(f"""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name IN ({placeholders})
    """, list(table_names))
    found = {r["table_name"] for r in cur.fetchall()}
    return {t: (t in found) for t in table_names}


# ---------- rotas ----------

@challenges_bp.get("/active")
def list_active_challenges():
    """
    GET /api/challenges/active?user_type=client|deliverer|restaurant
    Lista desafios ativos, opcionalmente filtrados por tipo de usuário.
    Retorna lista vazia graciosamente se a tabela não existir.
    """
    user_type_filter = request.args.get("user_type")

    conn = None
    try:
        conn = _db()
        with conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            exists = _tables_exist(cur, "challenges")
            if not exists["challenges"]:
                logger.warning("Tabela 'challenges' não encontrada. Retornando lista vazia.")
                return _ok({"items": [], "total": 0})

            where = ["is_active = TRUE", "(end_date IS NULL OR end_date >= NOW())"]
            params = []

            if user_type_filter:
                type_map = {
                    "client": "client",
                    "deliverer": "delivery",
                    "delivery": "delivery",
                    "restaurant": "restaurant",
                }
                mapped = type_map.get(user_type_filter, user_type_filter)
                where.append("(target_user_type = %s OR target_user_type = 'all')")
                params.append(mapped)

            where_sql = "WHERE " + " AND ".join(where)

            cur.execute(f"""
                SELECT id, name, description, target_user_type, goal_type,
                       goal_target, reward_points, start_date, end_date,
                       is_active, created_at
                FROM public.challenges
                {where_sql}
                ORDER BY end_date ASC NULLS LAST, created_at DESC
            """, params)

            rows = [dict(r) for r in cur.fetchall()]
            for r in rows:
                r["id"] = str(r["id"])
                for field in ("start_date", "end_date", "created_at"):
                    if r.get(field) and hasattr(r[field], "isoformat"):
                        r[field] = r[field].isoformat()

            return _ok({"items": rows, "total": len(rows)})

    except Exception as e:
        current_app.logger.exception("challenges.list_active_challenges failed")
        return _ok({"items": [], "total": 0})
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@challenges_bp.post("/progress")
def update_challenge_progress():
    """
    POST /api/challenges/progress
    Body: {user_id, challenge_id, increment}
    Atualiza progresso do usuário num desafio.
    Se o desafio for concluído, concede pontos automaticamente.
    """
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id")
    challenge_id = body.get("challenge_id")
    increment = body.get("increment", 1)

    if not user_id or not challenge_id:
        return _err("user_id e challenge_id são obrigatórios", 422)

    try:
        increment = int(increment)
        if increment <= 0:
            return _err("increment deve ser maior que zero", 422)
    except (ValueError, TypeError):
        return _err("increment deve ser um número inteiro", 422)

    conn = None
    try:
        conn = _db()
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                exists = _tables_exist(cur, "challenges", "user_challenges")
                if not exists["challenges"] or not exists["user_challenges"]:
                    return _err("Sistema de desafios não configurado ainda", 503)

                # Busca desafio
                cur.execute("""
                    SELECT id, name, goal_target, reward_points, is_active,
                           (end_date IS NULL OR end_date >= NOW()) AS still_valid
                    FROM public.challenges
                    WHERE id = %s
                """, (challenge_id,))
                challenge = cur.fetchone()
                if not challenge:
                    return _err("Desafio não encontrado", 404)
                if not challenge["is_active"] or not challenge["still_valid"]:
                    return _err("Desafio não está mais ativo", 400)

                goal_target = int(challenge["goal_target"])
                reward_points = int(challenge["reward_points"])

                # Upsert progresso
                cur.execute("""
                    INSERT INTO public.user_challenges
                        (id, user_id, challenge_id, current_progress, is_completed, started_at)
                    VALUES (gen_random_uuid(), %s, %s, %s, FALSE, NOW())
                    ON CONFLICT (user_id, challenge_id)
                    DO UPDATE SET
                        current_progress = LEAST(
                            public.user_challenges.current_progress + EXCLUDED.current_progress,
                            (SELECT goal_target FROM public.challenges WHERE id = %s)
                        ),
                        updated_at = NOW()
                    WHERE NOT public.user_challenges.is_completed
                    RETURNING current_progress, is_completed
                """, (user_id, challenge_id, increment, challenge_id))

                row = cur.fetchone()
                if row is None:
                    # Já estava concluído — sem atualização
                    cur.execute("""
                        SELECT current_progress, is_completed
                        FROM public.user_challenges
                        WHERE user_id = %s AND challenge_id = %s
                    """, (user_id, challenge_id))
                    row = cur.fetchone()

                current_progress = int(row["current_progress"])
                is_completed = bool(row["is_completed"])
                points_awarded = 0

                # Verifica se chegou ao objetivo (e ainda não foi marcado completo)
                if current_progress >= goal_target and not is_completed:
                    cur.execute("""
                        UPDATE public.user_challenges
                        SET is_completed = TRUE, completed_at = NOW()
                        WHERE user_id = %s AND challenge_id = %s
                    """, (user_id, challenge_id))
                    is_completed = True

                    # Concede pontos de recompensa via tabela user_points
                    if reward_points > 0:
                        cur.execute("""
                            INSERT INTO public.user_points (user_id, total_points, last_updated)
                            VALUES (%s, %s, NOW())
                            ON CONFLICT (user_id) DO UPDATE
                              SET total_points = public.user_points.total_points + EXCLUDED.total_points,
                                  last_updated = NOW()
                        """, (user_id, reward_points))

                        # Registra no histórico se a tabela existir
                        hist_exists = _tables_exist(cur, "points_history")
                        if hist_exists.get("points_history"):
                            cur.execute("""
                                INSERT INTO public.points_history
                                    (id, user_id, points_earned, points_type, description)
                                VALUES (gen_random_uuid(), %s, %s, 'challenge_completed', %s)
                            """, (user_id, reward_points, f"Desafio concluído: {challenge['name']}"))

                        points_awarded = reward_points

                return _ok({
                    "challenge_id": str(challenge_id),
                    "current_progress": current_progress,
                    "goal_target": goal_target,
                    "progress_pct": round((current_progress / goal_target) * 100, 1) if goal_target > 0 else 0,
                    "is_completed": is_completed,
                    "points_awarded": points_awarded,
                })

    except Exception as e:
        current_app.logger.exception("challenges.update_challenge_progress failed")
        traceback.print_exc()
        return _err("Erro interno ao atualizar progresso", 500, detail=str(e))
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@challenges_bp.get("/user/<user_id>")
def get_user_challenges(user_id):
    """
    GET /api/challenges/user/<user_id>
    Retorna todos os desafios ativos com o progresso do usuário.
    """
    try:
        include_completed = request.args.get("include_completed", "false").lower() == "true"
    except Exception:
        include_completed = False

    conn = None
    try:
        conn = _db()
        with conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            exists = _tables_exist(cur, "challenges", "user_challenges")
            if not exists["challenges"]:
                return _ok({"items": [], "total": 0})

            if exists["user_challenges"]:
                # Query com progresso do usuário
                where = ["c.is_active = TRUE"]
                params = [user_id]

                if not include_completed:
                    where.append("(uc.is_completed IS NULL OR uc.is_completed = FALSE)")

                where_sql = "WHERE " + " AND ".join(where) if where else ""

                cur.execute(f"""
                    SELECT c.id, c.name, c.description, c.target_user_type,
                           c.goal_type, c.goal_target, c.reward_points,
                           c.start_date, c.end_date,
                           COALESCE(uc.current_progress, 0) AS current_progress,
                           COALESCE(uc.is_completed, FALSE) AS is_completed,
                           uc.completed_at
                    FROM public.challenges c
                    LEFT JOIN public.user_challenges uc
                        ON uc.challenge_id = c.id AND uc.user_id = %s
                    {where_sql}
                    ORDER BY c.end_date ASC NULLS LAST, c.created_at DESC
                """, params)
            else:
                # Sem tabela de progresso: retorna desafios sem progresso
                cur.execute("""
                    SELECT id, name, description, target_user_type,
                           goal_type, goal_target, reward_points,
                           start_date, end_date,
                           0 AS current_progress, FALSE AS is_completed, NULL AS completed_at
                    FROM public.challenges
                    WHERE is_active = TRUE
                    ORDER BY end_date ASC NULLS LAST, created_at DESC
                """)

            rows = [dict(r) for r in cur.fetchall()]
            for r in rows:
                r["id"] = str(r["id"])
                goal_target = int(r.get("goal_target") or 1)
                current = int(r.get("current_progress") or 0)
                r["progress_pct"] = round((current / goal_target) * 100, 1) if goal_target > 0 else 0
                for field in ("start_date", "end_date", "completed_at"):
                    if r.get(field) and hasattr(r[field], "isoformat"):
                        r[field] = r[field].isoformat()

            return _ok({"items": rows, "total": len(rows)})

    except Exception as e:
        current_app.logger.exception("challenges.get_user_challenges failed")
        return _ok({"items": [], "total": 0})
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
