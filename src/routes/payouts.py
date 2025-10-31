# src/routes/payouts.py
import logging
from flask import Blueprint, request, jsonify
from ..utils.helpers import get_db_connection, get_user_id_from_token
from ..logic.payout_processor import process_payouts

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

payouts_bp = Blueprint('payouts', __name__)

def _is_admin(user_type: str) -> bool:
    # ajuste se seu sistema utilizar outra checagem (ex: role em tabela users)
    return user_type == 'admin'

@payouts_bp.before_request
def handle_options():
    if request.method == "OPTIONS":
        resp = jsonify()
        resp.headers.add("Access-Control-Allow-Origin", request.headers.get("Origin", "*"))
        resp.headers.add("Access-Control-Allow-Headers", "Content-Type,Authorization")
        resp.headers.add("Access-Control-Allow-Methods", "GET,PUT,POST,DELETE,OPTIONS")
        resp.headers.add("Access-Control-Allow-Credentials", "true")
        return resp

# IMPORTANTE: como o blueprint é montado em main.py com url_prefix='/api/admin/payouts',
# aqui a rota deve ser somente '/process' (evita duplicação /payouts/payouts/process)
@payouts_bp.route('/process', methods=['POST'])
def process_payouts_route():
    """
    POST /api/admin/payouts/process
    Body:
      {
        "partner_type": "restaurant" | "delivery",
        "cycle_type": "weekly" | "bi-weekly" | "monthly"
      }
    """
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            return error
        if not _is_admin(user_type):
            return jsonify({"error": "Acesso negado. Somente administradores."}), 403

        payload = request.get_json(silent=True) or {}
        partner_type = (payload.get("partner_type") or "").strip().lower()
        cycle_type = (payload.get("cycle_type") or "weekly").strip().lower()

        if partner_type not in ("restaurant", "delivery"):
            return jsonify({"error": "partner_type inválido (use 'restaurant' ou 'delivery')"}), 400
        if cycle_type not in ("weekly", "bi-weekly", "monthly"):
            return jsonify({"error": "cycle_type inválido (weekly | bi-weekly | monthly)"}), 400

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexão com banco de dados"}), 500

        result = process_payouts(conn, partner_type=partner_type, cycle_type=cycle_type)
        conn.commit()

        summary = {
            "status": "success",
            "partner_type": partner_type,
            "cycle_type": cycle_type,
            "generated_count": len(result),
            "payouts": result
        }
        return jsonify(summary), 200

    except Exception as e:
        logger.exception("Erro ao processar payouts")
        if conn:
            conn.rollback()
        return jsonify({"error": "Erro interno ao processar payouts"}), 500
    finally:
        if conn:
            conn.close()
