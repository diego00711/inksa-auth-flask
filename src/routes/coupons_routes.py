# src/routes/coupons_routes.py
# Blueprint: coupons_bp, prefix /api/coupons

import logging
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify
import psycopg2
import psycopg2.extras
from ..utils.helpers import get_db_connection, get_user_id_from_token

logger = logging.getLogger(__name__)

coupons_bp = Blueprint('coupons', __name__)


def _table_exists(cur) -> bool:
    """Verifica se a tabela coupons existe."""
    try:
        cur.execute("""
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'coupons'
        """)
        return cur.fetchone() is not None
    except Exception:
        return False


@coupons_bp.route('/validate', methods=['POST'])
def validate_coupon():
    """
    POST /api/coupons/validate
    Body: { "code": str, "order_total": float }
    Retorna: { valid, discount_type, discount_value, discount_amount, message }
    """
    conn = None
    try:
        data = request.get_json(silent=True) or {}
        code = str(data.get('code', '')).strip().upper()
        try:
            order_total = float(data.get('order_total', 0))
        except (ValueError, TypeError):
            order_total = 0.0

        if not code:
            return jsonify({"valid": False, "message": "Codigo do cupom e obrigatorio"}), 400

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexao com o banco de dados"}), 500

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            if not _table_exists(cur):
                logger.warning("Tabela coupons nao existe. Execute create_coupons.sql")
                return jsonify({"valid": False, "message": "Sistema de cupons nao configurado. Execute create_coupons.sql"}), 200

            cur.execute("""
                SELECT id, code, discount_type, discount_value, min_order_value,
                       max_uses, uses_count, valid_until, is_active
                FROM public.coupons
                WHERE code = %s
            """, (code,))
            coupon = cur.fetchone()

        if not coupon:
            return jsonify({"valid": False, "message": "Cupom nao encontrado"}), 200

        if not coupon['is_active']:
            return jsonify({"valid": False, "message": "Este cupom nao esta ativo"}), 200

        if coupon['valid_until'] and coupon['valid_until'] < datetime.now(timezone.utc):
            return jsonify({"valid": False, "message": "Este cupom expirou"}), 200

        if coupon['max_uses'] is not None and coupon['uses_count'] >= coupon['max_uses']:
            return jsonify({"valid": False, "message": "Este cupom atingiu o limite de usos"}), 200

        min_val = float(coupon['min_order_value'] or 0)
        if order_total < min_val:
            return jsonify({
                "valid": False,
                "message": f"Pedido minimo para este cupom e R$ {min_val:.2f}"
            }), 200

        disc_type = coupon['discount_type']
        disc_value = float(coupon['discount_value'])
        discount_amount = 0.0

        if disc_type == 'percentage':
            discount_amount = round(order_total * disc_value / 100, 2)
        elif disc_type == 'fixed':
            discount_amount = min(disc_value, order_total)
        elif disc_type == 'free_delivery':
            discount_amount = 0.0  # Frontend aplica isenção da taxa de entrega

        return jsonify({
            "valid": True,
            "coupon_id": str(coupon['id']),
            "code": coupon['code'],
            "discount_type": disc_type,
            "discount_value": disc_value,
            "discount_amount": discount_amount,
            "message": "Cupom valido!"
        }), 200

    except Exception as e:
        logger.error(f"Erro em validate_coupon: {e}", exc_info=True)
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        if conn:
            conn.close()


@coupons_bp.route('/admin', methods=['GET'])
def list_coupons():
    """
    GET /api/coupons/admin
    Lista todos os cupons. Requer autenticacao admin.
    """
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            return error
        if user_type != 'admin':
            return jsonify({"error": "Acesso restrito a administradores"}), 403

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexao com o banco de dados"}), 500

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            if not _table_exists(cur):
                return jsonify({"error": "Tabela coupons nao existe. Execute create_coupons.sql"}), 503

            cur.execute("""
                SELECT id, code, discount_type, discount_value, min_order_value,
                       max_uses, uses_count, valid_until, is_active, created_at
                FROM public.coupons
                ORDER BY created_at DESC
            """)
            rows = cur.fetchall()

        result = []
        for row in rows:
            r = dict(row)
            r['id'] = str(r['id'])
            if r.get('valid_until'):
                r['valid_until'] = r['valid_until'].isoformat()
            if r.get('created_at'):
                r['created_at'] = r['created_at'].isoformat()
            r['discount_value'] = float(r['discount_value'])
            r['min_order_value'] = float(r['min_order_value'] or 0)
            result.append(r)

        return jsonify({"coupons": result, "total": len(result)}), 200

    except Exception as e:
        logger.error(f"Erro em list_coupons: {e}", exc_info=True)
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        if conn:
            conn.close()


@coupons_bp.route('/admin', methods=['POST'])
def create_coupon():
    """
    POST /api/coupons/admin
    Body: { code, discount_type, discount_value, min_order_value, max_uses, valid_until }
    Requer autenticacao admin.
    """
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            return error
        if user_type != 'admin':
            return jsonify({"error": "Acesso restrito a administradores"}), 403

        data = request.get_json(silent=True) or {}
        code = str(data.get('code', '')).strip().upper()
        discount_type = str(data.get('discount_type', '')).strip().lower()
        try:
            discount_value = float(data['discount_value'])
        except (KeyError, ValueError, TypeError):
            return jsonify({"error": "discount_value invalido ou ausente"}), 400

        if not code:
            return jsonify({"error": "code e obrigatorio"}), 400
        if discount_type not in ('percentage', 'fixed', 'free_delivery'):
            return jsonify({"error": "discount_type invalido. Use: percentage, fixed, free_delivery"}), 400

        min_order_value = float(data.get('min_order_value', 0) or 0)
        max_uses = data.get('max_uses')
        valid_until = data.get('valid_until')

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexao com o banco de dados"}), 500

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            if not _table_exists(cur):
                return jsonify({"error": "Tabela coupons nao existe. Execute create_coupons.sql"}), 503

            try:
                cur.execute("""
                    INSERT INTO public.coupons
                        (code, discount_type, discount_value, min_order_value, max_uses, valid_until)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id, code, discount_type, discount_value, min_order_value,
                              max_uses, uses_count, valid_until, is_active, created_at
                """, (code, discount_type, discount_value, min_order_value, max_uses, valid_until))
                new_coupon = dict(cur.fetchone())
                conn.commit()
            except psycopg2.errors.UniqueViolation:
                conn.rollback()
                return jsonify({"error": f"Cupom com codigo '{code}' ja existe"}), 409

        new_coupon['id'] = str(new_coupon['id'])
        if new_coupon.get('valid_until'):
            new_coupon['valid_until'] = new_coupon['valid_until'].isoformat()
        if new_coupon.get('created_at'):
            new_coupon['created_at'] = new_coupon['created_at'].isoformat()
        new_coupon['discount_value'] = float(new_coupon['discount_value'])
        new_coupon['min_order_value'] = float(new_coupon['min_order_value'] or 0)

        return jsonify({"success": True, "coupon": new_coupon}), 201

    except Exception as e:
        logger.error(f"Erro em create_coupon: {e}", exc_info=True)
        if conn:
            conn.rollback()
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        if conn:
            conn.close()
