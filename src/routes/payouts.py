# src/routes/payouts.py
import logging
from datetime import datetime
from flask import Blueprint, request, jsonify
from psycopg2.extras import DictCursor

from ..utils.helpers import get_db_connection, get_user_id_from_token
from ..utils.audit import log_admin_action
from ..logic.payout_processor import process_automatic_payouts
from ..providers.mp_payouts import get_payout_provider, payout_provider_mode, auto_pay_enabled

logger = logging.getLogger(__name__)
payouts_bp = Blueprint("payouts", __name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_admin(user_type: str) -> bool:
    return user_type == "admin"


def _normalize_dates(row: dict) -> dict:
    for key in ("period_start", "period_end", "created_at", "updated_at"):
        val = row.get(key)
        if val and hasattr(val, "isoformat"):
            row[key] = val.isoformat()
    return row


def _get_admin_identifier(user_id: str, conn) -> str:
    """Returns admin email if available, falls back to user_id."""
    try:
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute("SELECT email FROM users WHERE id = %s LIMIT 1", (user_id,))
            row = cur.fetchone()
            if row and row.get("email"):
                return row["email"]
    except Exception:
        pass
    return str(user_id)


# ---------------------------------------------------------------------------
# CORS pre-flight (blueprint-level)
# ---------------------------------------------------------------------------

@payouts_bp.before_request
def allow_cors_preflight():
    if request.method == "OPTIONS":
        resp = jsonify()
        resp.headers["Access-Control-Allow-Origin"] = request.headers.get("Origin", "*")
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
        resp.headers["Access-Control-Allow-Methods"] = "GET,PUT,POST,PATCH,DELETE,OPTIONS"
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        return resp


# ---------------------------------------------------------------------------
# GET /api/admin/payouts/partner-pix — utility (internal)
# ---------------------------------------------------------------------------

def _get_partner_pix_data(conn, *, partner_type: str, partner_id: str):
    """Resolve chave PIX + nome + tipo da chave do parceiro. As tabelas reais
    sao restaurant_profiles / delivery_profiles -- nao existe tabela
    `restaurants`, delivery_profiles nao tem `full_name` (tem
    first_name/last_name) e restaurant_profiles nao tem `bank_pix_key`."""
    with conn.cursor(cursor_factory=DictCursor) as cur:
        if partner_type == "delivery":
            cur.execute(
                """
                SELECT pix_key, pix_key_type,
                       NULLIF(TRIM(COALESCE(first_name, '') || ' ' || COALESCE(last_name, '')), '') AS full_name
                FROM delivery_profiles
                WHERE id = %s OR user_id = %s
                LIMIT 1
                """,
                (partner_id, partner_id),
            )
        else:
            cur.execute(
                """
                SELECT pix_key, pix_key_type,
                       COALESCE(trade_name, restaurant_name, business_name) AS full_name
                FROM restaurant_profiles
                WHERE id = %s OR user_id = %s
                LIMIT 1
                """,
                (partner_id, partner_id),
            )
        row = cur.fetchone()
    if not row:
        return None, None, None
    return row.get("pix_key"), row.get("full_name"), row.get("pix_key_type")


# ---------------------------------------------------------------------------
# GET /api/admin/payouts/provider — modo do provedor de repasse
# O front usa pra decidir se mostra o botão "Pagar via PIX (automático)".
# ---------------------------------------------------------------------------

@payouts_bp.route("/provider", methods=["GET", "OPTIONS"])
def payout_provider_status():
    _, user_type, error = get_user_id_from_token(request.headers.get("Authorization"))
    if error:
        return error
    if not _is_admin(user_type):
        return jsonify({"error": "Acesso negado"}), 403
    return jsonify({
        "status": "success",
        "mode": payout_provider_mode(),
        "auto_pay_enabled": auto_pay_enabled(),
    }), 200


# ---------------------------------------------------------------------------
# POST /api/admin/payouts/process
# ---------------------------------------------------------------------------

@payouts_bp.route("/process", methods=["POST", "OPTIONS"])
def process_payouts_route():
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get("Authorization"))
        if error:
            return error
        if not _is_admin(user_type):
            return jsonify({"error": "Acesso negado. Somente administradores."}), 403

        body = request.get_json(silent=True) or {}
        partner_type = (body.get("partner_type") or "").strip().lower() or None
        cycle_type   = (body.get("cycle_type") or "all").strip().lower()
        dry_run      = bool(body.get("dry_run", False))

        # Apps salvam 'biweekly' (sem hífen); o motor usa 'bi-weekly'. Aceita
        # os dois no request pra não depender de qual lado normaliza.
        if cycle_type == "biweekly":
            cycle_type = "bi-weekly"

        if partner_type and partner_type not in ("restaurant", "delivery"):
            return jsonify({"error": "partner_type inválido (restaurant|delivery)"}), 400
        # 'all' = todos os ciclos; parceiro sem tipo (None) = os dois tipos.
        # Default é 'all' pra quem só quer "paga todo mundo que está pendente".
        if cycle_type not in ("weekly", "bi-weekly", "monthly", "all"):
            return jsonify({"error": "cycle_type inválido (weekly|bi-weekly|monthly|all)"}), 400

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexão com banco de dados"}), 500

        result = process_automatic_payouts(
            conn,
            force_cycle=cycle_type,
            partner_type=partner_type,
            dry_run=dry_run,
        )

        admin = _get_admin_identifier(user_id, conn)
        log_admin_action(
            admin,
            "ProcessPayouts",
            (
                f"cycle={cycle_type} partner_type={partner_type or 'all'} "
                f"dry_run={dry_run} generated={result.get('total_payouts', 0)}"
            ),
            request,
        )

        return jsonify({"status": "success", **result}), 200

    except Exception:
        logger.exception("Erro ao processar payouts")
        if conn:
            conn.rollback()
        return jsonify({"error": "Erro interno ao processar payouts"}), 500
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------------
# GET /api/admin/payouts
# ---------------------------------------------------------------------------

@payouts_bp.route("", methods=["GET", "OPTIONS"])
def list_payouts():
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get("Authorization"))
        if error:
            return error
        if not _is_admin(user_type):
            return jsonify({"error": "Acesso negado"}), 403

        partner_type = (request.args.get("partner_type") or "").strip().lower()
        status       = (request.args.get("status") or "").strip().lower()
        partner_id   = request.args.get("partner_id")
        start_date   = request.args.get("start_date")
        end_date     = request.args.get("end_date")
        limit        = min(int(request.args.get("limit") or 20), 200)
        offset       = int(request.args.get("offset") or 0)

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexão com banco de dados"}), 500

        valid_statuses = ("pending", "pending_transfer", "paid", "cancelled")
        where, params = [], []

        if partner_type in ("restaurant", "delivery"):
            where.append("p.partner_type = %s")
            params.append(partner_type)
        if status in valid_statuses:
            where.append("p.status = %s")
            params.append(status)
        if partner_id:
            where.append("p.partner_id = %s")
            params.append(partner_id)
        if start_date:
            where.append("p.created_at >= %s")
            params.append(start_date)
        if end_date:
            where.append("p.created_at <= %s")
            params.append(end_date)

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute(f"SELECT COUNT(*) AS total FROM payouts p {where_sql}", tuple(params))
            total = int(cur.fetchone()["total"])

            # LEFT JOIN nas duas tabelas de perfil para trazer nome + chave PIX
            # do parceiro junto -- o admin ve pra quem pagar e a chave sem ter
            # que abrir outra tela (fluxo de repasse assistido).
            cur.execute(
                f"""
                SELECT p.id, p.partner_type, p.partner_id,
                       p.period_start, p.period_end,
                       p.total_gross, p.commission_fee, p.total_net,
                       COALESCE(p.cash_debt_deducted, 0) AS cash_debt_deducted,
                       p.status, p.payment_method, p.payment_ref,
                       p.created_at, p.updated_at,
                       COALESCE(
                           rp.trade_name, rp.restaurant_name, rp.business_name,
                           NULLIF(TRIM(COALESCE(dp.first_name, '') || ' ' || COALESCE(dp.last_name, '')), '')
                       ) AS partner_name,
                       COALESCE(rp.pix_key, dp.pix_key) AS pix_key,
                       COALESCE(rp.pix_key_type, dp.pix_key_type) AS pix_key_type
                FROM payouts p
                LEFT JOIN restaurant_profiles rp ON p.partner_type = 'restaurant' AND rp.id = p.partner_id
                LEFT JOIN delivery_profiles  dp ON p.partner_type = 'delivery'   AND dp.id = p.partner_id
                {where_sql}
                ORDER BY p.created_at DESC
                LIMIT %s OFFSET %s
                """,
                tuple(params + [limit, offset]),
            )
            rows = [_normalize_dates(dict(r)) for r in cur.fetchall()]

        return jsonify({"status": "success", "items": rows, "total": total}), 200

    except Exception:
        logger.exception("Erro ao listar payouts")
        return jsonify({"error": "Erro interno ao listar payouts"}), 500
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------------
# GET /api/admin/payouts/<id>
# ---------------------------------------------------------------------------

@payouts_bp.route("/<uuid:payout_id>", methods=["GET", "OPTIONS"])
def get_payout_detail(payout_id):
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get("Authorization"))
        if error:
            return error
        if not _is_admin(user_type):
            return jsonify({"error": "Acesso negado"}), 403

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexão com banco de dados"}), 500

        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute(
                """
                SELECT id, partner_type, partner_id,
                       period_start, period_end,
                       total_gross, commission_fee, total_net,
                       status, payment_method, payment_ref,
                       created_at, updated_at
                FROM payouts
                WHERE id = %s
                """,
                (str(payout_id),),
            )
            head = cur.fetchone()
            if not head:
                return jsonify({"error": "Payout não encontrado"}), 404

            cur.execute(
                """
                SELECT id, order_id, order_total, delivery_fee,
                       commission_applied, net_amount
                FROM payout_items
                WHERE payout_id = %s
                ORDER BY created_at ASC
                """,
                (str(payout_id),),
            )
            items = [dict(r) for r in cur.fetchall()]

        return jsonify({
            "status": "success",
            "payout": _normalize_dates(dict(head)),
            "items": items,
        }), 200

    except Exception:
        logger.exception("Erro ao obter detalhe de payout")
        return jsonify({"error": "Erro interno ao obter payout"}), 500
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------------
# PATCH /api/admin/payouts/<id>/status
# Allows transitioning status: pending_transfer → paid | cancelled
#                               pending          → paid | cancelled
# ---------------------------------------------------------------------------

ALLOWED_TRANSITIONS = {
    "pending":          {"paid", "cancelled"},
    "pending_transfer": {"paid", "cancelled"},
}


@payouts_bp.route("/<uuid:payout_id>/status", methods=["PATCH", "OPTIONS"])
def update_payout_status(payout_id):
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get("Authorization"))
        if error:
            return error
        if not _is_admin(user_type):
            return jsonify({"error": "Acesso negado"}), 403

        body = request.get_json(silent=True) or {}
        new_status     = (body.get("status") or "").strip().lower()
        payment_method = (body.get("payment_method") or "").strip()
        payment_ref    = (body.get("payment_ref") or "").strip()

        if new_status not in ("paid", "cancelled"):
            return jsonify({"error": "status inválido — use 'paid' ou 'cancelled'"}), 400
        if new_status == "paid" and not payment_method:
            return jsonify({"error": "payment_method é obrigatório ao marcar como 'paid'"}), 400

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexão com banco de dados"}), 500

        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute(
                "SELECT id, status, partner_type, partner_id, total_net FROM payouts WHERE id = %s",
                (str(payout_id),),
            )
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "Payout não encontrado"}), 404

            current_status = row["status"]
            allowed = ALLOWED_TRANSITIONS.get(current_status, set())
            if new_status not in allowed:
                return jsonify({
                    "error": f"Transição '{current_status}' → '{new_status}' não permitida"
                }), 400

            cur.execute(
                """
                UPDATE payouts
                   SET status = %s,
                       payment_method = COALESCE(NULLIF(%s, ''), payment_method),
                       payment_ref    = COALESCE(NULLIF(%s, ''), payment_ref),
                       updated_at = NOW()
                 WHERE id = %s
                 RETURNING *
                """,
                (new_status, payment_method or None, payment_ref or None, str(payout_id)),
            )
            updated = _normalize_dates(dict(cur.fetchone()))
            conn.commit()

        admin = _get_admin_identifier(user_id, conn)
        log_admin_action(
            admin,
            "UpdatePayoutStatus",
            (
                f"payout={payout_id} {current_status}→{new_status} "
                f"partner={row['partner_type']}:{row['partner_id']} "
                f"net={row['total_net']}"
            ),
            request,
        )

        return jsonify({"status": "success", "payout": updated}), 200

    except Exception:
        logger.exception("Erro ao atualizar status do payout")
        if conn:
            conn.rollback()
        return jsonify({"error": "Erro interno ao atualizar status"}), 500
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------------
# POST /api/admin/payouts/<id>/mark-paid
# ---------------------------------------------------------------------------

@payouts_bp.route("/<uuid:payout_id>/mark-paid", methods=["POST", "OPTIONS"])
def mark_payout_paid(payout_id):
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get("Authorization"))
        if error:
            return error
        if not _is_admin(user_type):
            return jsonify({"error": "Acesso negado"}), 403

        body = request.get_json(silent=True) or {}
        method = (body.get("payment_method") or "").strip()
        ref    = (body.get("payment_ref") or "").strip()

        if not method:
            return jsonify({"error": "payment_method é obrigatório"}), 400

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexão com banco de dados"}), 500

        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute(
                "SELECT status, partner_type, partner_id, total_net FROM payouts WHERE id = %s",
                (str(payout_id),),
            )
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "Payout não encontrado"}), 404
            if row["status"] in ("paid", "cancelled"):
                return jsonify({"error": f"Não permitido no status atual: {row['status']}"}), 400

            cur.execute(
                """
                UPDATE payouts
                   SET status = 'paid',
                       payment_method = %s,
                       payment_ref = %s,
                       updated_at = NOW()
                 WHERE id = %s
                 RETURNING *
                """,
                (method, ref or None, str(payout_id)),
            )
            updated = _normalize_dates(dict(cur.fetchone()))
            conn.commit()

        admin = _get_admin_identifier(user_id, conn)
        log_admin_action(
            admin,
            "MarkPayoutPaid",
            (
                f"payout={payout_id} method={method} "
                f"partner={row['partner_type']}:{row['partner_id']} "
                f"net={row['total_net']}"
            ),
            request,
        )

        return jsonify({"status": "success", "payout": updated}), 200

    except Exception:
        logger.exception("Erro ao marcar payout como pago")
        if conn:
            conn.rollback()
        return jsonify({"error": "Erro interno ao marcar payout como pago"}), 500
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------------
# POST /api/admin/payouts/<id>/cancel
# ---------------------------------------------------------------------------

@payouts_bp.route("/<uuid:payout_id>/cancel", methods=["POST", "OPTIONS"])
def cancel_payout(payout_id):
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get("Authorization"))
        if error:
            return error
        if not _is_admin(user_type):
            return jsonify({"error": "Acesso negado"}), 403

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexão com banco de dados"}), 500

        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute(
                "SELECT status, partner_type, partner_id, total_net FROM payouts WHERE id = %s",
                (str(payout_id),),
            )
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "Payout não encontrado"}), 404
            if row["status"] in ("paid", "cancelled"):
                return jsonify({"error": f"Não permitido no status atual: {row['status']}"}), 400

            cur.execute(
                """
                UPDATE payouts
                   SET status = 'cancelled', updated_at = NOW()
                 WHERE id = %s
                 RETURNING *
                """,
                (str(payout_id),),
            )
            updated = _normalize_dates(dict(cur.fetchone()))
            conn.commit()

        admin = _get_admin_identifier(user_id, conn)
        log_admin_action(
            admin,
            "CancelPayout",
            (
                f"payout={payout_id} "
                f"partner={row['partner_type']}:{row['partner_id']} "
                f"net={row['total_net']}"
            ),
            request,
        )

        return jsonify({"status": "success", "payout": updated}), 200

    except Exception:
        logger.exception("Erro ao cancelar payout")
        if conn:
            conn.rollback()
        return jsonify({"error": "Erro interno ao cancelar payout"}), 500
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------------
# POST /api/admin/payouts/<id>/auto-pay
# Executes PIX transfer via configured provider and marks payout as paid.
# ---------------------------------------------------------------------------

@payouts_bp.route("/<uuid:payout_id>/auto-pay", methods=["POST", "OPTIONS"])
def auto_pay_payout(payout_id):
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get("Authorization"))
        if error:
            return error
        if not _is_admin(user_type):
            return jsonify({"error": "Acesso negado"}), 403

        # Trava de segurança: com o provider em modo teste (mock) o transfer_pix
        # só simula sucesso. Deixar o auto-pay rodar nesse modo marcaria o
        # repasse como 'pago' SEM o dinheiro sair. Só libera com provider real.
        if not auto_pay_enabled():
            return jsonify({
                "error": "Pagamento automático não está ativo (provedor de repasse em modo teste). "
                         "Use o pagamento manual assistido."
            }), 409

        body = request.get_json(silent=True) or {}
        description = (body.get("description") or "Repasse Inksa").strip()
        pix_key_type = (body.get("pix_key_type") or "").strip() or None

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexão com banco de dados"}), 500

        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute(
                """
                SELECT id, partner_type, partner_id, total_net, status
                FROM payouts
                WHERE id = %s
                """,
                (str(payout_id),),
            )
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "Payout não encontrado"}), 404
            row = dict(row)

            if row["status"] not in ("pending", "pending_transfer"):
                return jsonify({
                    "error": f"Somente payouts 'pending' ou 'pending_transfer' podem ser pagos (atual: {row['status']})"
                }), 400

            pix_key, full_name, stored_key_type = _get_partner_pix_data(
                conn, partner_type=row["partner_type"], partner_id=row["partner_id"]
            )
            if not pix_key:
                return jsonify({"error": "Parceiro sem PIX cadastrado"}), 400

            # Precedência do tipo da chave: escolha manual no modal > tipo que o
            # parceiro cadastrou > inferência pelo formato (dentro do provider).
            effective_key_type = pix_key_type or stored_key_type

            amount_cents = int(round(float(row["total_net"]) * 100))
            provider = get_payout_provider()
            result = provider.transfer_pix(
                amount_cents=amount_cents,
                pix_key=pix_key,
                description=f"{description} - {full_name or row['partner_id']}",
                pix_key_type=effective_key_type,
            )

            if not result["ok"]:
                logger.error("Falha no provider ao pagar payout %s: %s", payout_id, result["raw"])
                return jsonify({"error": "Falha ao executar repasse no provedor"}), 502

            txid = result["txid"]

            cur.execute(
                """
                UPDATE payouts
                   SET status = 'paid',
                       payment_method = 'pix',
                       payment_ref = %s,
                       updated_at = NOW()
                 WHERE id = %s
                 RETURNING *
                """,
                (txid, str(payout_id)),
            )
            updated = _normalize_dates(dict(cur.fetchone()))
            conn.commit()

        admin = _get_admin_identifier(user_id, conn)
        log_admin_action(
            admin,
            "AutoPayPayout",
            (
                f"payout={payout_id} txid={txid} "
                f"partner={row['partner_type']}:{row['partner_id']} "
                f"net={row['total_net']}"
            ),
            request,
        )

        return jsonify({"status": "success", "payout": updated, "provider_txid": txid}), 200

    except NotImplementedError as exc:
        logger.warning("Provider não implementado: %s", exc)
        return jsonify({"error": str(exc)}), 501
    except Exception:
        logger.exception("Erro no auto-pay")
        if conn:
            conn.rollback()
        return jsonify({"error": "Erro interno no auto-pay"}), 500
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------------
# GET /api/admin/payouts/cash-debts — entregadores devendo em dinheiro
# ---------------------------------------------------------------------------

@payouts_bp.route("/cash-debts", methods=["GET", "OPTIONS"])
def list_cash_debts():
    """Lista entregadores com dívida em dinheiro em aberto (cash_debt > 0).

    Complementa o abatimento automático (que só resolve quem tem repasse online):
    aqui o admin vê quem só faz dinheiro e precisa acertar na mão."""
    conn = None
    try:
        _, user_type, error = get_user_id_from_token(request.headers.get("Authorization"))
        if error:
            return error
        if not _is_admin(user_type):
            return jsonify({"error": "Acesso negado"}), 403

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexão com banco de dados"}), 500

        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute(
                """
                SELECT id,
                       NULLIF(TRIM(COALESCE(first_name, '') || ' ' || COALESCE(last_name, '')), '') AS name,
                       pix_key, pix_key_type,
                       COALESCE(cash_debt, 0)          AS cash_debt,
                       COALESCE(total_cash_received, 0) AS total_cash_received
                FROM delivery_profiles
                WHERE COALESCE(cash_debt, 0) > 0
                ORDER BY cash_debt DESC
                """
            )
            rows = [dict(r) for r in cur.fetchall()]
        return jsonify({"status": "success", "items": rows, "total": len(rows)}), 200

    except Exception:
        logger.exception("Erro ao listar dívidas em dinheiro")
        return jsonify({"error": "Erro interno ao listar dívidas"}), 500
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------------
# POST /api/admin/payouts/cash-debts/<delivery_id>/settle — registrar acerto
# ---------------------------------------------------------------------------

@payouts_bp.route("/cash-debts/<uuid:delivery_id>/settle", methods=["POST", "OPTIONS"])
def settle_cash_debt(delivery_id):
    """Registra que o entregador quitou (total ou parcial) a dívida em dinheiro:
    reduz delivery_profiles.cash_debt (nunca abaixo de zero) e grava o histórico."""
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get("Authorization"))
        if error:
            return error
        if not _is_admin(user_type):
            return jsonify({"error": "Acesso negado"}), 403

        body = request.get_json(silent=True) or {}
        note = ((body.get("note") or "").strip()[:500]) or None
        try:
            amount = round(float(body.get("amount")), 2)
        except (TypeError, ValueError):
            return jsonify({"error": "Valor inválido"}), 400
        if amount <= 0:
            return jsonify({"error": "O valor do acerto deve ser maior que zero"}), 400

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexão com banco de dados"}), 500

        admin_ident = _get_admin_identifier(user_id, conn)

        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute(
                "SELECT COALESCE(cash_debt, 0) AS cash_debt FROM delivery_profiles WHERE id = %s FOR UPDATE",
                (str(delivery_id),),
            )
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "Entregador não encontrado"}), 404
            debt_before = float(row["cash_debt"] or 0)
            if debt_before <= 0:
                return jsonify({"error": "Este entregador não tem dívida em aberto"}), 400

            applied = min(amount, debt_before)            # nunca deixa a dívida negativa
            debt_after = round(debt_before - applied, 2)

            cur.execute(
                "UPDATE delivery_profiles SET cash_debt = %s, updated_at = NOW() WHERE id = %s",
                (debt_after, str(delivery_id)),
            )
            cur.execute(
                """
                INSERT INTO cash_debt_settlements
                    (delivery_id, amount, debt_before, debt_after, note, admin)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (str(delivery_id), applied, debt_before, debt_after, note, admin_ident),
            )
            conn.commit()

        log_admin_action(
            admin_ident,
            "SettleCashDebt",
            f"delivery={delivery_id} pago={applied:.2f} divida {debt_before:.2f}->{debt_after:.2f}",
            request,
        )
        return jsonify({
            "status": "success",
            "data": {"applied": applied, "debt_before": debt_before, "debt_after": debt_after},
        }), 200

    except Exception:
        logger.exception("Erro ao registrar acerto de dívida")
        if conn:
            conn.rollback()
        return jsonify({"error": "Erro interno ao registrar acerto"}), 500
    finally:
        if conn:
            conn.close()
