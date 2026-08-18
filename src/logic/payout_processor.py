# src/logic/payout_processor.py
import logging
import uuid as uuidlib
from datetime import date, datetime, timedelta, timezone, time as dtime
from decimal import Decimal, ROUND_HALF_UP
import psycopg2.extras

from ..utils.platform_settings import get_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def get_commission_rate() -> Decimal:
    """Returns platform commission rate from platform_settings (admin editable)."""
    try:
        rate = get_settings()["commission_rate"]
        if not (Decimal("0") < rate < Decimal("1")):
            raise ValueError("must be between 0 and 1")
        return rate
    except Exception:
        return Decimal("0.10")


# Dias de espera do ciclo express. Vem do benefício `payout_express_days` do
# nível; este é só o padrão de quando o nível não disser.
_EXPRESS_DIAS = 2


def _delivery_ids_express(conn) -> set:
    """IDs dos entregadores cujo nível do Clube dá repasse express.

    Espelha `club.level_for_activity`: o nível vale é o MAIS ALTO cujo
    min_activity <= atividade, e atividade (para entregador) é a contagem de
    pedidos ENTREGUES no mês corrente — não o total da vida. Se um dia o
    club.py mudar essa definição, esta query tem que mudar junto.

    Quem manda é o benefício `payout_express_days` no JSON do nível, não o
    nome "Diamante": assim o admin move o benefício de nível sem tocar aqui.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SAVEPOINT express_ids")
        try:
            cur.execute("""
                WITH atividade AS (
                  SELECT dp.id AS partner_id,
                         (SELECT COUNT(*) FROM orders o
                           WHERE o.delivery_id = dp.id
                             AND o.status = 'delivered'
                             AND DATE_TRUNC('month', o.created_at) = DATE_TRUNC('month', NOW())
                         ) AS ativ
                    FROM delivery_profiles dp
                   WHERE COALESCE(dp.active, true) = true
                )
                SELECT a.partner_id
                  FROM atividade a
                 WHERE COALESCE((
                         SELECT (cl.benefits ->> 'payout_express_days')::numeric
                           FROM club_levels cl
                          WHERE cl.audience = 'delivery'
                            AND COALESCE(cl.is_active, true) = true
                            AND cl.min_activity <= a.ativ
                          ORDER BY cl.min_activity DESC
                          LIMIT 1
                       ), 0) > 0
            """)
            return {str(r["partner_id"]) for r in cur.fetchall()}
        except Exception as exc:
            cur.execute("ROLLBACK TO SAVEPOINT express_ids")
            # Fail-closed DE PROPÓSITO: sem conseguir saber quem é express,
            # ninguém entra no ciclo rápido e todo mundo cai no ciclo normal.
            # O contrário (pagar todo mundo em 2 dias por causa de um erro de
            # consulta) mexeria no caixa sem ninguém mandar.
            logger.warning("Não deu pra apurar entregadores express: %s", exc)
            return set()


def is_payout_day(cycle_type: str, reference_date: date = None) -> bool:
    """Returns True if *reference_date* (defaults to today) is a scheduled payout day.

    - express  → TODO DIA (benefício de nível: recebe em 2 dias)
    - weekly   → every Friday
    - bi-weekly → every other Friday (even ISO-week numbers)
    - monthly  → 1st of each month
    """
    today = reference_date or date.today()
    if cycle_type == "express":
        return True
    if cycle_type == "monthly":
        return today.day == 1
    if cycle_type == "bi-weekly":
        return today.weekday() == 4 and today.isocalendar()[1] % 2 == 0
    if cycle_type == "weekly":
        return today.weekday() == 4
    return False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _period_bounds(cycle_type: str, reference_date: date = None):
    """Returns (period_start, period_end) UTC datetimes for the given cycle."""
    today = reference_date or date.today()
    now = datetime.now(timezone.utc)
    if cycle_type == "express":
        # É AQUI que moram os "2 dias".
        #
        # A janela fecha por DIA DE CALENDÁRIO, não por 48 horas corridas.
        # Com horas corridas, uma entrega das 14h só era paga depois das 14h
        # do terceiro dia — o job roda 06:00 e às 06:00 do segundo dia só
        # tinham passado 40 horas. O entregador conta em dias ("entreguei
        # segunda, recebo quarta"), então a conta tem que ser em dias.
        #
        # Fecha às 00:00 de (hoje - 1): isso inclui tudo que foi entregue até
        # o fim de (hoje - 2). Entregou segunda -> recebe quarta, não importa
        # a hora.
        #
        # Fuso: o scheduler roda em America/Sao_Paulo (06:00), mas as datas do
        # banco são UTC. Entrega depois das 21h de Brasília cai no dia
        # seguinte em UTC e espera um dia a mais. É o pior caso, é raro, e
        # errar pra MAIS é melhor que prometer e pagar atrasado.
        fim = datetime.combine(
            today - timedelta(days=_EXPRESS_DIAS - 1), dtime.min, tzinfo=timezone.utc
        )
        # Início largo de propósito: _get_eligible_orders já ignora o que foi
        # pago (payout_id IS NOT NULL), então janela ampla só recolhe atrasado,
        # nunca paga duas vezes.
        return fim - timedelta(days=45), fim
    if cycle_type == "monthly":
        # O ciclo mensal roda no dia 1 e paga o MES ANTERIOR inteiro. Antes
        # cobria do dia 1 do mes CORRENTE ate agora — ou seja, so as primeiras
        # horas do proprio dia 1: pedidos do meio do mes nunca entravam em
        # repasse mensal nenhum (ficavam orfaos com payout_status=pending).
        first_this = datetime(today.year, today.month, 1, 0, 0, 0, tzinfo=timezone.utc)
        last_prev = first_this - timedelta(days=1)
        start = datetime(last_prev.year, last_prev.month, 1, 0, 0, 0, tzinfo=timezone.utc)
        return start, first_this
    elif cycle_type == "bi-weekly":
        start = (now - timedelta(days=14)).replace(microsecond=0)
    else:  # weekly
        start = (now - timedelta(days=7)).replace(microsecond=0)
    return start, now


def _get_partners_for_cycle(conn, partner_type: str, cycle_type: str) -> list:
    """Returns list of partner IDs whose CHOSEN frequency matches *cycle_type*.

    A frequencia que vale e a que o parceiro escolheu NO APP, salva em
    `payout_frequency` ('weekly' | 'biweekly' | 'monthly'; NULL = nao escolheu
    -> default 'weekly'). Antes isto lia `payout_cycle`, uma coluna legada que
    nenhum app escreve e que nasce 'monthly' por default — a escolha do
    parceiro era simplesmente ignorada (Gabriel escolheu semanal e o motor o
    tratava como mensal).

    Normalizacao: os apps salvam 'biweekly' (sem hifen); o motor usa
    'bi-weekly'. Valores desconhecidos caem no default 'weekly'.
    """
    table = "restaurant_profiles" if partner_type == "restaurant" else "delivery_profiles"

    # O express é benefício de nível do Clube, não frequência escolhida — e só
    # existe pra entregador. Restaurante nunca entra.
    if cycle_type == "express":
        return sorted(_delivery_ids_express(conn)) if partner_type == "delivery" else []

    # Quem está no express SAI dos outros ciclos. Sem isto o mesmo entregador
    # apareceria em dois ciclos no mesmo dia; não pagaria duas vezes (o filtro
    # payout_id IS NULL segura), mas a corrida decidiria em qual repasse cada
    # pedido cai — e repasse é dinheiro, não pode depender de ordem de laço.
    _fora = _delivery_ids_express(conn) if partner_type == "delivery" else set()

    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        # SAVEPOINT (e não conn.rollback() no except): esta função roda no MEIO
        # da transação do ciclo — um rollback total apagaria payouts já
        # inseridos pro tipo/parceiro anterior sem ninguém perceber.
        cur.execute("SAVEPOINT partners_cycle")
        try:
            cur.execute(
                f"""
                SELECT id AS partner_id
                FROM {table}
                WHERE CASE lower(trim(COALESCE(payout_frequency, 'weekly')))
                        WHEN 'monthly'   THEN 'monthly'
                        WHEN 'mensal'    THEN 'monthly'
                        WHEN 'biweekly'  THEN 'bi-weekly'
                        WHEN 'bi-weekly' THEN 'bi-weekly'
                        WHEN 'quinzenal' THEN 'bi-weekly'
                        ELSE 'weekly'
                      END = %s
                  AND COALESCE(active, true) = true
                """,
                (cycle_type,),
            )
            return [str(row["partner_id"]) for row in cur.fetchall()
                    if str(row["partner_id"]) not in _fora]
        except Exception as exc:
            # payout_frequency column may not exist yet — fall back gracefully
            cur.execute("ROLLBACK TO SAVEPOINT partners_cycle")
            if "payout_frequency" in str(exc) and cycle_type == "weekly":
                logger.warning("payout_frequency column missing on %s; treating all as 'weekly'", table)
                cur.execute(
                    f"SELECT id AS partner_id FROM {table} WHERE COALESCE(active, true) = true"
                )
                return [str(row["partner_id"]) for row in cur.fetchall()
                        if str(row["partner_id"]) not in _fora]
            return []


def _get_eligible_orders(conn, partner_type: str, partner_id: str, period_start, period_end) -> list:
    """Returns orders eligible for payout: delivered, payment approved, not yet in a payout."""
    if partner_type == "restaurant":
        partner_col = "restaurant_id"
        amount_col = "valor_repassado_restaurante"
        payout_col = "restaurant_payout_id"
        # PEDIDO EM DINHEIRO conta pro repasse do RESTAURANTE. O dinheiro foi
        # recolhido pelo entregador (status_pagamento='pending_cash', nunca vira
        # 'approved'), e a plataforma cobra dele via cash_debt — mas o
        # restaurante segue com o valor a receber. Antes o filtro exigia
        # 'approved', então NENHUM pedido em dinheiro gerava repasse e o
        # parceiro simplesmente não recebia por essas vendas.
        pagamento_clause = (
            "(status_pagamento IN ('approved', 'pending_cash') OR status = 'delivery_failed')"
        )
    else:
        partner_col = "delivery_id"
        amount_col = "valor_repassado_entregador"
        payout_col = "delivery_payout_id"
        # ENTREGADOR: pedido em dinheiro NÃO entra. Ele já ficou com o frete em
        # espécie no ato (cash_debt = total - frete dele). Gerar repasse aqui
        # pagaria o frete duas vezes — em dinheiro e de novo abatendo a dívida.
        pagamento_clause = "(status_pagamento = 'approved' OR status = 'delivery_failed')"

    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        # NÃO filtrar por orders.payout_status aqui. Um pedido gera DOIS
        # repasses (restaurante + entregador), mas payout_status é UMA coluna
        # só, compartilhada. O motor processa restaurante e depois entregador
        # na mesma rodada: quando a passada do restaurante marcava o pedido
        # como 'processed', a passada do entregador filtrava por
        # payout_status='pending' e não enxergava mais o pedido — o repasse do
        # entregador NUNCA nascia (nem no manual, nem no job das 6h). O
        # dedup correto e POR TIPO já existe: {payout_col} IS NULL
        # (restaurant_payout_id / delivery_payout_id são colunas separadas).
        cur.execute(
            f"""
            SELECT id,
                   COALESCE({amount_col}, 0)      AS repasse,
                   COALESCE(delivery_fee, 0)      AS delivery_fee,
                   COALESCE(comissao_plataforma, 0) AS comissao_historica
            FROM orders
            WHERE {partner_col} = %s
              AND status IN ('delivered', 'delivery_failed')
              AND {pagamento_clause}
              AND {payout_col} IS NULL
              AND COALESCE({amount_col}, 0) > 0
              AND updated_at >= %s AND updated_at <= %s
            ORDER BY updated_at ASC
            """,
            (partner_id, period_start, period_end),
        )
        return cur.fetchall()


def _calculate_amounts(orders, partner_type: str, commission_rate: Decimal):
    """Returns (total_gross, commission_fee, total_net, per_order list).

    Usa a comissao HISTORICA de cada pedido (orders.comissao_plataforma,
    gravada no momento da compra) em vez de reconstruir "gross" reaplicando
    a taxa de comissao ATUAL sobre o valor liquido -- se a taxa mudar entre
    a data do pedido e o dia do repasse, reaplicar a taxa atual produzia um
    detalhamento (bruto/comissao) matematicamente inconsistente pra pedidos
    antigos (o valor liquido pago sempre esteve correto, so o detalhamento
    exibido no admin que ficava errado).
    """
    per_order = []
    total_gross = Decimal("0")
    total_net = Decimal("0")

    for order in orders:
        net = Decimal(str(order["repasse"] or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if partner_type == "restaurant":
            comm = Decimal(str(order.get("comissao_historica") or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if comm <= 0:
                # Fallback so para pedidos antigos sem comissao_plataforma
                # gravada (antes dessa coluna existir) -- reconstroi com a
                # taxa atual como melhor esforco.
                divisor = (Decimal("1") - commission_rate)
                gross = (net / divisor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                comm = (gross - net).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            else:
                gross = (net + comm).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        else:
            # Entregador: modelo atual paga base + por km, sem "comissao"
            # descontada do valor do entregador -- gross = net, sem desconto.
            gross = net
            comm = Decimal("0.00")

        total_gross += gross
        total_net += net
        per_order.append({
            "order_id": str(order["id"]),
            "order_total": float(gross),
            "delivery_fee": float(order.get("delivery_fee") or 0),
            "commission_applied": float(comm),
            "net_amount": float(net),
        })

    total_gross = total_gross.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    total_net = total_net.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    commission_fee = (total_gross - total_net).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(total_gross), float(commission_fee), float(total_net), per_order


# Onde mora a dívida em dinheiro de cada tipo de parceiro.
#   entregador  -> recolheu o pedido em espécie e deve a parte dos outros
#   restaurante -> entrega PRÓPRIA em dinheiro: ficou com 100% e deve a comissão
_DEBT_SOURCE = {
    "delivery":   ("delivery_profiles",   "cash_debt"),
    "restaurant": ("restaurant_profiles", "commission_debt"),
}


def _apply_cash_debt_netting(conn, partner_type, partner_id, net_gross: float):
    """Abate a dívida em dinheiro do parceiro do valor a repassar.

    ENTREGADOR: recolhe os pedidos pagos em dinheiro EM ESPÉCIE e fica devendo à
    plataforma a parte dela (comida do restaurante + comissão + margem do frete).

    RESTAURANTE COM ENTREGA PRÓPRIA: nesses pedidos em dinheiro quem recolhe é o
    motoboy DELE — a loja fica com tudo e passa a dever só a comissão.

    Nos dois casos a dívida é descontada do repasse dos pedidos ONLINE: paga por
    PIX só a diferença. Se a dívida for MAIOR que o repasse, zera o repasse e o
    resto continua registrado pra ser abatido no próximo ciclo.

    Retorna (net_a_pagar, abatido). Reduz a dívida no banco (mesma transação —
    respeita o dry_run/commit do process_automatic_payouts)."""
    fonte = _DEBT_SOURCE.get(partner_type)
    if not fonte:
        return float(net_gross), 0.0
    tabela, coluna = fonte

    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(
            f"SELECT COALESCE({coluna}, 0) AS divida FROM {tabela} WHERE id = %s FOR UPDATE",
            (partner_id,),
        )
        row = cur.fetchone()
        debt = Decimal(str((row or {}).get("divida") or 0))
        if debt <= 0:
            return float(net_gross), 0.0

        net_dec = Decimal(str(net_gross))
        deducted = min(net_dec, debt)
        remaining = (debt - deducted).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        cur.execute(
            f"UPDATE {tabela} SET {coluna} = %s, updated_at = NOW() WHERE id = %s",
            (float(remaining), partner_id),
        )
        net_after = float((net_dec - deducted).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        return net_after, float(deducted.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _insert_payout(conn, partner_type, partner_id, period_start, period_end,
                   total_gross, commission_fee, total_net, per_order,
                   cash_debt_deducted: float = 0.0):
    """Inserts payouts + payout_items rows, updates orders. Returns payout summary dict.

    total_net = valor a PAGAR por PIX (já líquido do abatimento da dívida em
    dinheiro, quando houver). cash_debt_deducted = quanto da dívida foi abatido
    neste repasse. Se o abatimento zerou o repasse, o payout nasce já 'paid'
    (nada a transferir), senão 'pending_transfer' como sempre."""
    payout_id = str(uuidlib.uuid4())
    payout_col = "restaurant_payout_id" if partner_type == "restaurant" else "delivery_payout_id"
    order_ids = [item["order_id"] for item in per_order]

    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        # 1. Insert payout
        # `amount` é NOT NULL sem default no schema (coluna legada) -- precisa
        # ser preenchida senao o INSERT falha e nenhum repasse e criado.
        # Usamos o valor liquido (o que efetivamente sera pago).
        # Se o abatimento da dívida em dinheiro zerou o valor a pagar, o repasse
        # nasce já quitado (nada a transferir por PIX). Senão segue pro fluxo
        # normal de transferência.
        fully_offset = total_net <= 0 and cash_debt_deducted > 0
        status = 'paid' if fully_offset else 'pending_transfer'
        pay_method = 'abatimento_dinheiro' if fully_offset else None
        pay_ref = 'Abatido integralmente da dívida em dinheiro' if fully_offset else None
        cur.execute(
            """
            INSERT INTO payouts (
                id, partner_id, partner_type,
                amount, total_gross, commission_fee, total_net, cash_debt_deducted,
                period_start, period_end,
                status, payment_method, payment_ref, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
            """,
            (payout_id, partner_id, partner_type,
             total_net, total_gross, commission_fee, total_net, cash_debt_deducted,
             period_start, period_end,
             status, pay_method, pay_ref),
        )

        # 2. Insert payout_items
        # Colunas reais: partner_type/partner_id (NOT NULL), gross_amount, fee,
        # net_amount. Antes inseria em order_total/delivery_fee/commission_applied
        # que NAO existem -- o INSERT quebrava e derrubava a geracao inteira.
        for item in per_order:
            cur.execute(
                """
                INSERT INTO payout_items (
                    id, payout_id, order_id, partner_type, partner_id,
                    gross_amount, fee, net_amount, created_at
                ) VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s, %s, NOW())
                """,
                (payout_id, item["order_id"], partner_type, partner_id,
                 item["order_total"], item["commission_applied"], item["net_amount"]),
            )

        # 3. Mark orders as processed
        # `id = ANY(%s)` SEM cast era o que derrubava a geração inteira
        # ("operator does not exist: uuid = text"): o psycopg2 transforma a
        # lista Python em ARRAY['...'], e o construtor ARRAY com literais
        # resolve como text[] ANTES do operador — diferente de um literal
        # solto, que o Postgres coage pra uuid pelo contexto. O ::uuid[]
        # força o tipo do lado direito.
        cur.execute("SAVEPOINT mark_orders")
        try:
            cur.execute(
                f"""
                UPDATE orders
                   SET {payout_col} = %s,
                       payout_status = 'processed',
                       updated_at = NOW()
                 WHERE id = ANY(%s::uuid[])
                """,
                (payout_id, order_ids),
            )
        except Exception:
            # payout_status column may not exist. ROLLBACK TO (e não
            # conn.rollback()): o rollback total apagava o payout inserido nos
            # passos 1-2 desta MESMA transação, e o fallback então marcava
            # pedidos apontando pra um payout que nunca chegou a existir.
            cur.execute("ROLLBACK TO SAVEPOINT mark_orders")
            cur.execute(
                f"""
                UPDATE orders
                   SET {payout_col} = %s,
                       updated_at = NOW()
                 WHERE id = ANY(%s::uuid[])
                """,
                (payout_id, order_ids),
            )

    return {
        "payout_id": payout_id,
        "partner_type": partner_type,
        "partner_id": partner_id,
        "total_gross": total_gross,
        "commission_fee": commission_fee,
        "total_net": total_net,
        "cash_debt_deducted": cash_debt_deducted,
        "orders_count": len(order_ids),
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "status": status,
    }


def _log_to_admin_logs(created: list, cycles: list):
    """Best-effort: write summary to admin_logs via Supabase."""
    try:
        from ..utils.helpers import supabase
        if not supabase or not created:
            return
        details = (
            f"Payouts automáticos gerados: {len(created)} | "
            f"Ciclos: {', '.join(cycles)} | "
            f"IDs: {', '.join(p['payout_id'] for p in created[:10])}"
        )
        supabase.table("admin_logs").insert({
            "admin": "scheduler",
            "action": "AutomaticPayouts",
            "details": details[:16384],
        }).execute()
    except Exception as exc:
        logger.warning("Failed to write payout summary to admin_logs: %s", exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def process_automatic_payouts(
    conn,
    force_cycle: str = None,
    partner_type: str = None,
    dry_run: bool = False,
) -> dict:
    """Generates payouts for all partners whose payout cycle is due today.

    Args:
        conn:          Active psycopg2 connection (uncommitted).
        force_cycle:   If set, skip the is_payout_day check and process this cycle.
        partner_type:  If set ('restaurant'|'delivery'), process only that type.
        dry_run:       If True, all DB changes are rolled back (for testing).

    Returns:
        dict with keys: created, cycles_processed, total_payouts, today, dry_run
    """
    today = date.today()
    commission_rate = get_commission_rate()
    created = []

    # Determine which cycles are due
    if force_cycle:
        if force_cycle == "all":
            # Força TODOS os ciclos. Cada parceiro entra em exatamente um
            # (o da frequência que ele escolheu — _get_partners_for_cycle
            # filtra por isso), então "all" = "processe todo mundo que tem
            # repasse pendente, cada um no ciclo dele". É o modo certo pro
            # botão manual: o admin não precisa adivinhar a frequência de
            # cada parceiro.
            cycles_due = ["express", "weekly", "bi-weekly", "monthly"]
        elif force_cycle not in ("express", "weekly", "bi-weekly", "monthly"):
            raise ValueError(f"Invalid cycle_type: {force_cycle!r}")
        else:
            cycles_due = [force_cycle]
    else:
        # express PRIMEIRO: ele é o mais restrito (só quem tem o benefício) e
        # roda todo dia. Vindo antes, o pedido de um Diamante cai no repasse
        # rápido dele; os outros ciclos já o encontram pago.
        cycles_due = [c for c in ("express", "weekly", "bi-weekly", "monthly") if is_payout_day(c, today)]

    if not cycles_due:
        logger.info("process_automatic_payouts: no cycles due today (%s)", today.isoformat())
        return {"created": [], "cycles_due": [], "today": today.isoformat(), "dry_run": dry_run}

    logger.info(
        "process_automatic_payouts: cycles=%s partner_type=%s date=%s",
        cycles_due, partner_type or "all", today.isoformat(),
    )

    partner_types = [partner_type] if partner_type else ["restaurant", "delivery"]

    for ptype in partner_types:
        for cycle in cycles_due:
            period_start, period_end = _period_bounds(cycle, today)
            partners = _get_partners_for_cycle(conn, ptype, cycle)
            logger.info("  %s/%s: %d partners found", ptype, cycle, len(partners))

            for pid in partners:
                orders = _get_eligible_orders(conn, ptype, pid, period_start, period_end)
                if not orders:
                    continue

                gross, comm, net, per_order = _calculate_amounts(orders, ptype, commission_rate)
                if net <= 0:
                    continue

                # Abate a dívida em dinheiro do repasse online — paga por PIX só
                # a diferença. Vale pro entregador (recolheu em espécie) e pro
                # restaurante de entrega própria (ficou com 100% do pedido em
                # dinheiro e deve a comissão).
                net_to_pay, deducted = _apply_cash_debt_netting(conn, ptype, pid, net)

                record = _insert_payout(
                    conn, ptype, pid, period_start, period_end,
                    gross, comm, net_to_pay, per_order,
                    cash_debt_deducted=deducted,
                )
                created.append(record)
                logger.info(
                    "    payout %s: %s %s | bruto=%.2f abatido=%.2f a_pagar=%.2f orders=%d",
                    record["payout_id"], ptype, pid, net, deducted, net_to_pay, len(per_order),
                )

    if dry_run:
        conn.rollback()
        logger.info("process_automatic_payouts: DRY RUN — rolled back %d payouts", len(created))
    else:
        conn.commit()
        _log_to_admin_logs(created, cycles_due)

    logger.info("process_automatic_payouts: done — %d payouts created", len(created))
    return {
        "created": created,
        "cycles_processed": cycles_due,
        "total_payouts": len(created),
        "today": today.isoformat(),
        "dry_run": dry_run,
    }
