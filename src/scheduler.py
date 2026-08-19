# src/scheduler.py
"""
Daily payout scheduler for Inksa Delivery.

Runs process_automatic_payouts() every day at 06:00 America/Sao_Paulo.

Concurrency safety (Render multi-dyno):
  Uses pg_try_advisory_lock so only one instance processes payouts
  even when multiple dynos fire the job simultaneously.
"""
import logging
import os

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

# Arbitrary fixed key for the pg advisory lock (must be the same across all dynos)
_PAYOUT_LOCK_KEY = 7_777_777_777

_scheduler: BackgroundScheduler | None = None


# ---------------------------------------------------------------------------
# Scheduled job
# ---------------------------------------------------------------------------

def _run_payouts_job() -> None:
    """Entry point executed by APScheduler at 06:00 BRT every day."""
    from .utils.helpers import get_db_connection
    from .logic.payout_processor import process_automatic_payouts

    logger.info("[SCHEDULER] Starting daily payout job")
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            logger.error("[SCHEDULER] Cannot connect to DB — job aborted")
            return

        # Acquire session-level advisory lock to prevent concurrent runs
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (_PAYOUT_LOCK_KEY,))
            acquired = cur.fetchone()[0]

        if not acquired:
            logger.info("[SCHEDULER] Lock held by another instance — skipping this run")
            return

        try:
            result = process_automatic_payouts(conn)
            logger.info(
                "[SCHEDULER] Payouts done: %d created, cycles=%s",
                result.get("total_payouts", 0),
                result.get("cycles_processed", []),
            )
        finally:
            # Always release the session-level lock before closing the connection
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (_PAYOUT_LOCK_KEY,))
            except Exception:
                pass

    except Exception:
        logger.exception("[SCHEDULER] Unhandled error in payout job")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Expire pending payments job
# ---------------------------------------------------------------------------


def _expire_pending_payments_job() -> None:
    """Cancela pedidos em status 'awaiting_payment' criados há mais de 30 minutos."""
    import os
    from datetime import datetime, timedelta, timezone

    logger.info("[SCHEDULER] Iniciando expiração de pedidos awaiting_payment")
    try:
        from supabase import create_client as _create_client
        _url = os.environ.get("SUPABASE_URL")
        _key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_SERVICE_KEY")
        if not _url or not _key:
            logger.error("[SCHEDULER] Supabase não configurado — job de expiração abortado")
            return
        _sb = _create_client(_url, _key)
        threshold = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        result = _sb.table("orders").update({
            "status": "cancelled",
            "cancellation_reason": "payment_timeout",
        }).eq("status", "awaiting_payment").lt("created_at", threshold).execute()
        expired_count = len(result.data) if result.data else 0
        if expired_count:
            logger.info("[SCHEDULER] %d pedido(s) expirado(s) por timeout de pagamento", expired_count)
        else:
            logger.info("[SCHEDULER] Nenhum pedido para expirar")
    except Exception:
        logger.exception("[SCHEDULER] Erro no job de expiração de pagamentos")


def _monitor_job() -> None:
    """Verificacao horaria de saude. Ver src/logic/monitor.py.

    Envolvido em try/except proprio: o monitor NUNCA pode derrubar o
    agendador. Um monitor que mata os outros jobs seria pior que nao ter
    monitor — o job de repasse e o de expirar pagamento valem mais que ele.
    """
    try:
        from .logic.monitor import executar_monitor
        r = executar_monitor()
        logger.info("[MONITOR] %d alerta(s), %d enviado(s)",
                    r.get("alertas", 0), r.get("enviados", 0))
    except Exception:
        logger.exception("[MONITOR] job falhou")


# ---------------------------------------------------------------------------
# Abertura automatica por horario de funcionamento
# ---------------------------------------------------------------------------

_WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _is_within_hours(opening_hours, now) -> bool:
    """True se `now` (datetime local) cai dentro do horario configurado para o dia.

    Formato esperado de opening_hours:
      { "mon": {"enabled": true, "open": "18:00", "close": "23:00"}, ... }
    Suporta intervalos que cruzam a meia-noite (close < open).
    """
    if not isinstance(opening_hours, dict):
        return False
    day = _WEEKDAY_KEYS[now.weekday()]
    today = opening_hours.get(day)
    # Verifica tambem o dia anterior para intervalos que cruzam a meia-noite
    prev = opening_hours.get(_WEEKDAY_KEYS[(now.weekday() - 1) % 7])
    cur_min = now.hour * 60 + now.minute

    def _parse(hhmm):
        try:
            h, m = str(hhmm).split(":")
            return int(h) * 60 + int(m)
        except Exception:
            return None

    def _check(slot, allow_overnight_tail=False):
        if not isinstance(slot, dict) or not slot.get("enabled"):
            return False
        o = _parse(slot.get("open"))
        c = _parse(slot.get("close"))
        if o is None or c is None:
            return False
        if c > o:  # mesmo dia
            return o <= cur_min < c
        # cruza a meia-noite
        if allow_overnight_tail:
            return cur_min < c  # madrugada do dia seguinte
        return cur_min >= o
    return _check(today) or _check(prev, allow_overnight_tail=True)


def _apply_opening_hours_job() -> None:
    """A cada poucos minutos, abre/fecha restaurantes com hours_auto ligado."""
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(os.environ.get("SCHEDULER_TIMEZONE", "America/Sao_Paulo")))
    except Exception:
        now = datetime.now()

    from .utils.helpers import get_db_connection
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            logger.error("[HOURS] Sem conexao ao banco — job abortado")
            return
        import psycopg2.extras
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT id, is_open, opening_hours FROM restaurant_profiles WHERE hours_auto = true"
            )
            rows = cur.fetchall()
            changed = 0
            for r in rows:
                should_open = _is_within_hours(r["opening_hours"], now)
                if bool(r["is_open"]) != should_open:
                    cur.execute(
                        "UPDATE restaurant_profiles SET is_open = %s WHERE id = %s",
                        (should_open, r["id"]),
                    )
                    changed += 1
            if changed:
                conn.commit()
                logger.info("[HOURS] %d restaurante(s) atualizados por horario", changed)
    except Exception:
        logger.exception("[HOURS] Erro no job de abertura automatica")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _close_stale_restaurants_job() -> None:
    """Fecha restaurantes 'abertos' cujo painel parou de dar sinal de vida.

    Cobre o caso do restaurante que perde o token (sessão expira) ou fecha o
    app sem clicar em 'Fechar': sem heartbeat ha 45 min, volta para Fechado
    para o cliente nao fazer pedido em restaurante ausente.
    Restaurantes com hours_auto ligado sao ignorados (o job de horario cuida).
    """
    from .utils.helpers import get_db_connection
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            logger.error("[STALE] Sem conexao ao banco — job abortado")
            return
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE restaurant_profiles
                   SET is_open = false
                   WHERE is_open = true
                     AND COALESCE(hours_auto, false) = false
                     AND (last_heartbeat IS NULL OR last_heartbeat < NOW() - INTERVAL '45 minutes')"""
            )
            closed = cur.rowcount
            conn.commit()
        if closed:
            logger.info("[STALE] %d restaurante(s) fechados por inatividade", closed)
    except Exception:
        logger.exception("[STALE] Erro no job de fechamento por inatividade")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _offline_stale_couriers_job() -> None:
    """Desliga entregadores 'online' que pararam de dar sinal de vida.

    `is_available` era pegajosa: só voltava a false pelo botão de sair ou pelo
    timer de inatividade do app — que morre junto com o app. Resultado prático
    medido em 2026-08-10: entregador marcado online havia 25 horas. O painel
    contava gente que não estava trabalhando, e o motor de despacho gastava
    oferta de pedido com quem não ia responder.

    TOLERÂNCIA FOLGADA (30 min) de propósito: desligar cedo demais tira da rua
    quem ESTÁ trabalhando e só ficou sem rede um instante. O custo de deixar um
    fantasma 30 min a mais é bem menor que o de derrubar um entregador real.

    Entregador com entrega em curso NUNCA é desligado, mesmo sem heartbeat —
    ele está com o pedido na mão; sumir com ele do sistema seria pior.
    """
    from .utils.helpers import get_db_connection
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            logger.error("[COURIER] Sem conexao ao banco — job abortado")
            return
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE delivery_profiles dp
                      SET is_available = false
                    WHERE dp.is_available IS TRUE
                      AND (dp.last_heartbeat IS NULL
                           OR dp.last_heartbeat < NOW() - INTERVAL '30 minutes')
                      AND NOT EXISTS (
                            SELECT 1 FROM orders o
                             WHERE o.delivery_id = dp.id
                               AND o.status IN ('accepted_by_delivery', 'delivering')
                          )"""
            )
            desligados = cur.rowcount
            conn.commit()
        if desligados:
            logger.info("[COURIER] %d entregador(es) desligados por inatividade", desligados)
    except Exception:
        logger.exception("[COURIER] Erro no job de desligamento por inatividade")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _expire_awaiting_restaurant_incidents_job() -> None:
    """Ocorrências 'awaiting_restaurant' (esperando o restaurante dizer se quer a
    devolução) que passaram do prazo caem pro DESCARTE — libera o entregador,
    comida fria dificilmente serve. Fica registrado como decisão do bot."""
    from datetime import datetime, timedelta, timezone
    from .utils.helpers import get_db_connection
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            logger.error("[INCIDENTS] Sem conexao ao banco — job de timeout abortado")
            return
        # Prazo importado lazily pra nao criar import circular no topo.
        try:
            from .routes.orders import INCIDENT_RESTAURANT_WAIT_MIN as _wait
        except Exception:
            _wait = 10
        threshold = datetime.now(timezone.utc) - timedelta(minutes=_wait)
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE delivery_incidents
                      SET outcome = 'dispose', return_code = NULL
                    WHERE outcome = 'awaiting_restaurant'
                      AND created_at < %s""",
                (threshold,),
            )
            n = cur.rowcount
            conn.commit()
        if n:
            logger.info("[INCIDENTS] %d ocorrencia(s) sem resposta do restaurante -> descarte", n)
    except Exception:
        logger.exception("[INCIDENTS] Erro no job de timeout de ocorrencia")
        if conn:
            try: conn.rollback()
            except Exception: pass
    finally:
        if conn:
            try: conn.close()
            except Exception: pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def start_scheduler(app=None) -> None:
    """Initialises and starts the APScheduler BackgroundScheduler.

    Safe to call multiple times — subsequent calls are no-ops.

    Args:
        app: Flask app instance (unused, kept for future app-context needs).
    """
    global _scheduler

    if _scheduler is not None and _scheduler.running:
        logger.debug("[SCHEDULER] Already running, skipping start")
        return

    # Honour opt-out env var (useful for worker-only dynos or tests)
    if os.environ.get("DISABLE_SCHEDULER", "").lower() in ("1", "true", "yes"):
        logger.info("[SCHEDULER] Disabled via DISABLE_SCHEDULER env var")
        return

    tz = os.environ.get("SCHEDULER_TIMEZONE", "America/Sao_Paulo")
    hour = int(os.environ.get("PAYOUT_SCHEDULE_HOUR", "6"))
    minute = int(os.environ.get("PAYOUT_SCHEDULE_MINUTE", "0"))

    _scheduler = BackgroundScheduler(timezone=tz, daemon=True)
    _scheduler.add_job(
        func=_run_payouts_job,
        trigger=CronTrigger(hour=hour, minute=minute, timezone=tz),
        id="daily_payouts",
        name="Daily automatic payout processing",
        replace_existing=True,
        misfire_grace_time=3600,  # tolerate up to 1-hour misfire (e.g. cold start)
    )
    _scheduler.add_job(
        func=_expire_pending_payments_job,
        trigger="interval",
        minutes=30,
        id="expire_pending_payments",
        name="Cancel stale awaiting_payment orders",
        replace_existing=True,
        misfire_grace_time=300,
    )
    logger.info("[SCHEDULER] Job de expiração de pagamentos: a cada 30 minutos")
    # Keep-alive interno REMOVIDO (25/07/2026): o Render agora e pago (Starter,
    # sempre ligado), entao nao precisa mais se auto-pingar pra evitar cold start.
    # O self-ping ainda dava gevent.Timeout de DNS (dois tracebacks vermelhos no
    # log). O Supabase segue protegido pelo GitHub Action
    # (.github/workflows/keep-alive.yml, a cada 6h, que bate no /api/health).
    _scheduler.add_job(
        func=_apply_opening_hours_job,
        trigger="interval",
        minutes=5,
        id="opening_hours",
        name="Abre/fecha restaurantes por horario de funcionamento",
        replace_existing=True,
        misfire_grace_time=120,
    )
    logger.info("[SCHEDULER] Abertura automatica por horario: a cada 5 minutos")
    _scheduler.add_job(
        func=_close_stale_restaurants_job,
        trigger="interval",
        minutes=10,
        id="close_stale_restaurants",
        name="Fecha restaurantes abertos sem heartbeat recente",
        replace_existing=True,
        misfire_grace_time=120,
    )
    logger.info("[SCHEDULER] Fechamento por inatividade: a cada 10 minutos")
    _scheduler.add_job(
        func=_offline_stale_couriers_job,
        trigger="interval",
        minutes=10,
        id="offline_stale_couriers",
        name="Desliga entregadores online sem heartbeat recente",
        replace_existing=True,
        misfire_grace_time=120,
    )
    logger.info("[SCHEDULER] Entregador offline por inatividade: a cada 10 minutos")
    _scheduler.add_job(
        func=_expire_awaiting_restaurant_incidents_job,
        trigger="interval",
        minutes=3,
        id="expire_awaiting_restaurant_incidents",
        name="Descarta ocorrencias sem resposta do restaurante",
        replace_existing=True,
        misfire_grace_time=120,
    )
    logger.info("[SCHEDULER] Timeout de ocorrencia (restaurante mudo): a cada 3 minutos")
    _scheduler.add_job(
        func=_monitor_job,
        trigger="interval",
        hours=1,
        id="monitor_saude",
        name="Monitor de saude (avisa por e-mail quando algo quebra)",
        replace_existing=True,
        # 10 min de tolerancia: se o Render reiniciou na hora exata do ciclo,
        # roda assim que voltar em vez de pular a hora inteira.
        misfire_grace_time=600,
    )
    logger.info("[SCHEDULER] Monitor de saude: a cada 1 hora")
    _scheduler.start()

    logger.info(
        "[SCHEDULER] Started — daily payouts at %02d:%02d %s",
        hour, minute, tz,
    )


def stop_scheduler() -> None:
    """Gracefully stops the scheduler (useful in tests)."""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("[SCHEDULER] Stopped")
