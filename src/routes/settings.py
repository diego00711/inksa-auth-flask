# src/routes/settings.py
import logging
from functools import wraps

import psycopg2.extras
from flask import Blueprint, jsonify, request

from ..utils.helpers import get_db_connection, get_user_id_from_token

logger = logging.getLogger(__name__)
settings_bp = Blueprint("settings_bp", __name__)


def _admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user_id, user_type, err = get_user_id_from_token(request.headers.get("Authorization"))
        if err:
            return err
        if user_type != "admin":
            return jsonify({"error": "Acesso não autorizado"}), 403
        return fn(*args, **kwargs)
    return wrapper


def _ensure_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS platform_settings (
                key         TEXT PRIMARY KEY,
                value       TEXT,
                description TEXT,
                updated_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """)
    conn.commit()


@settings_bp.get("")
@settings_bp.get("/")
@_admin_required
def get_settings():
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "DB indisponível"}), 503
    try:
        _ensure_table(conn)
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT key, value FROM platform_settings ORDER BY key")
            rows = cur.fetchall()
        return jsonify({"data": {r["key"]: r["value"] for r in rows}}), 200
    except Exception:
        logger.exception("get_settings failed")
        return jsonify({"error": "Erro interno"}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass


@settings_bp.put("")
@settings_bp.put("/")
@_admin_required
def update_settings():
    body = request.get_json(silent=True) or {}
    if not body:
        return jsonify({"error": "Body vazio"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "DB indisponível"}), 503
    try:
        _ensure_table(conn)
        with conn.cursor() as cur:
            for key, value in body.items():
                cur.execute(
                    """
                    INSERT INTO platform_settings (key, value, updated_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (key) DO UPDATE
                        SET value = EXCLUDED.value, updated_at = NOW()
                    """,
                    (str(key), str(value) if value is not None else None),
                )
        conn.commit()
        return jsonify({"message": "Configurações salvas com sucesso"}), 200
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.exception("update_settings failed")
        return jsonify({"error": "Erro interno"}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass
