# src/routes/public.py
# Endpoints publicos (sem autenticacao) - leitura de configuracoes que os apps Cliente/Restaurante/Entregador consomem.

import logging
import psycopg2.extras
from flask import Blueprint, jsonify

from ..utils.helpers import get_db_connection

logger = logging.getLogger(__name__)
public_bp = Blueprint("public_bp", __name__)


@public_bp.get("/support-info")
def public_support_info():
    """Retorna informacoes de contato/suporte da plataforma. Sem autenticacao."""
    conn = get_db_connection()
    if not conn:
        return jsonify({
            "email": "suporte@inksadelivery.com.br",
            "whatsapp": "5549999679697",
            "phone": "(49) 99967-9697",
            "hours": "Seg a Sex, 8h às 18h",
            "platform_name": "Inksa Delivery",
        }), 200
    try:
        keys = ("contact_email", "contact_whatsapp", "contact_phone", "support_hours", "platform_name")
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT key, value FROM platform_settings WHERE key = ANY(%s)", (list(keys),))
            rows = {r["key"]: r["value"] for r in cur.fetchall()}
        return jsonify({
            "email": rows.get("contact_email") or "suporte@inksadelivery.com.br",
            "whatsapp": rows.get("contact_whatsapp") or "5549999679697",
            "phone": rows.get("contact_phone") or "(49) 99967-9697",
            "hours": rows.get("support_hours") or "Seg a Sex, 8h às 18h",
            "platform_name": rows.get("platform_name") or "Inksa Delivery",
        }), 200
    except Exception:
        logger.exception("Erro em public_support_info")
        return jsonify({
            "email": "suporte@inksadelivery.com.br",
            "whatsapp": "5549999679697",
            "phone": "(49) 99967-9697",
            "hours": "Seg a Sex, 8h às 18h",
            "platform_name": "Inksa Delivery",
        }), 200
    finally:
        conn.close()
