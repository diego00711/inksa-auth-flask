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

def _keep_alive_job() -> None:
    """Pings /api/health every 10 min to prevent Render free tier cold start."""
    import requests as _requests
    url = os.environ.get("KEEP_ALIVE_URL", "https://inksa-auth-flask-dev.onrender.com/api/health")
    try:
        resp = _requests.get(url, timeout=10)
        logger.info("[SCHEDULER] Keep-alive %s → %d", url, resp.status_code)
    except Exception as exc:
        logger.warning("[SCHEDULER] Keep-alive ping failed: %s", exc)


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
    _scheduler.add_job(
        func=_keep_alive_job,
        trigger="interval",
        minutes=10,
        id="keep_alive",
        name="Keep-alive ping to prevent Render cold start",
        replace_existing=True,
        misfire_grace_time=120,
    )
    logger.info("[SCHEDULER] Keep-alive job: a cada 10 minutos")
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
