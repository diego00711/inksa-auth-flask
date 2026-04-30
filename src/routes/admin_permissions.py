# src/routes/admin_permissions.py
import logging
from functools import wraps

import psycopg2.extras
from flask import Blueprint, jsonify, request

from ..utils.helpers import get_db_connection, get_user_id_from_token

logger = logging.getLogger(__name__)
admin_permissions_bp = Blueprint("admin_permissions_bp", __name__)

VALID_ROLES = {"super_admin", "admin", "operator", "viewer"}
VALID_PAGES = {
    "dashboard", "usuarios", "restaurantes", "avaliacoes", "banners",
    "logs", "administradores", "relatorios", "financeiro", "payouts",
    "suporte", "configuracoes", "integracoes",
}


def _admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user_id, user_type, err = get_user_id_from_token(request.headers.get("Authorization"))
        if err:
            return err
        if user_type != "admin":
            return jsonify({"error": "Acesso não autorizado"}), 403
        return fn(*args, user_id=user_id, **kwargs)
    return wrapper


def _ensure_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS admin_permissions (
                user_id    UUID PRIMARY KEY,
                role       TEXT NOT NULL DEFAULT 'admin',
                pages      TEXT[] NOT NULL DEFAULT '{}',
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
    conn.commit()


@admin_permissions_bp.get("")
@admin_permissions_bp.get("/")
@_admin_required
def list_permissions(user_id):
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "DB indisponível"}), 503
    try:
        _ensure_table(conn)
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT user_id, role, pages, updated_at FROM admin_permissions ORDER BY updated_at DESC"
            )
            rows = cur.fetchall()
        data = [
            {
                "user_id": str(r["user_id"]),
                "role": r["role"],
                "pages": list(r["pages"] or []),
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            }
            for r in rows
        ]
        return jsonify({"data": data}), 200
    except Exception:
        logger.exception("list_permissions failed")
        return jsonify({"error": "Erro interno"}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass


@admin_permissions_bp.get("/<uuid:target_user_id>")
@_admin_required
def get_permission(user_id, target_user_id):
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "DB indisponível"}), 503
    try:
        _ensure_table(conn)
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT user_id, role, pages, updated_at FROM admin_permissions WHERE user_id = %s",
                (str(target_user_id),),
            )
            row = cur.fetchone()
        if not row:
            return jsonify({"data": {"user_id": str(target_user_id), "role": "admin", "pages": []}}), 200
        return jsonify({
            "data": {
                "user_id": str(row["user_id"]),
                "role": row["role"],
                "pages": list(row["pages"] or []),
                "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
            }
        }), 200
    except Exception:
        logger.exception("get_permission failed")
        return jsonify({"error": "Erro interno"}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass


@admin_permissions_bp.put("/<uuid:target_user_id>")
@_admin_required
def update_permission(user_id, target_user_id):
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "DB indisponível"}), 503
    try:
        _ensure_table(conn)
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT role FROM admin_permissions WHERE user_id = %s",
                (str(user_id),),
            )
            caller_row = cur.fetchone()
        caller_role = caller_row["role"] if caller_row else "admin"
    except Exception:
        logger.exception("update_permission role check failed")
        try:
            conn.close()
        except Exception:
            pass
        return jsonify({"error": "Erro interno"}), 500

    if caller_role != "super_admin":
        try:
            conn.close()
        except Exception:
            pass
        return jsonify({"error": "Apenas super_admin pode alterar permissões"}), 403

    body = request.get_json(silent=True) or {}
    role = body.get("role", "admin")
    pages = body.get("pages", [])

    if role not in VALID_ROLES:
        try:
            conn.close()
        except Exception:
            pass
        return jsonify({"error": f"Role inválido: {role}"}), 400

    invalid_pages = [p for p in pages if p not in VALID_PAGES]
    if invalid_pages:
        try:
            conn.close()
        except Exception:
            pass
        return jsonify({"error": f"Páginas inválidas: {invalid_pages}"}), 400

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO admin_permissions (user_id, role, pages, updated_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (user_id) DO UPDATE
                    SET role = EXCLUDED.role,
                        pages = EXCLUDED.pages,
                        updated_at = NOW()
                """,
                (str(target_user_id), role, pages),
            )
        conn.commit()
        return jsonify({"message": "Permissões atualizadas com sucesso"}), 200
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.exception("update_permission upsert failed")
        return jsonify({"error": "Erro interno"}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass
