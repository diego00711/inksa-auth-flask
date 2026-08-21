"""Lembrete automático de carrinho abandonado.

Ideia do Diego (19/08/2026): antes o lembrete só saía se ele clicasse na tela
de Carrinhos do admin — o que, na prática, significa "no dia seguinte, ou
nunca". Carrinho recuperado é das automações de melhor retorno que existem, e
depender de gente para apertar o botão é o jeito mais seguro de não acontecer.

TRÊS TRAVAS, e nenhuma é preciosismo:

1. **Tempo parado.** Ele sugeriu 5 minutos; ficou em 20 (configurável). Aos 5
   minutos a pessoa costuma estar AINDA no checkout — escolhendo pagamento,
   digitando endereço, conferindo frete. "Esqueceu algo?" no meio do pagamento
   é pior que silêncio.

2. **Janela de horário.** Push de comida às 3 da manhã é como se desinstala um
   app. Só nos horários em que faz sentido comer.

3. **Loja aberta.** Chamar alguém para completar o pedido com tudo fechado
   gasta a única chance do dia — o teto é 1 push por pessoa por dia.

O teto de 1/dia já existia e vinha do botão manual: índice único em
push_campaign_log (client_id, campanha) com a campanha carimbada com a data.
Com timer automático ele deixa de ser conforto e vira essencial.
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

TZ = ZoneInfo("America/Sao_Paulo")

# Faixas em que faz sentido lembrar alguém de comida. Fora delas o job nem
# consulta o banco.
JANELAS = ((11, 14), (18, 22))

# Teto de segurança por ciclo. Se algo der errado na consulta e ela devolver a
# base inteira, o estrago para aqui em vez de virar disparo em massa.
MAX_POR_CICLO = 40


def _dentro_da_janela(agora=None) -> bool:
    h = (agora or datetime.now(TZ)).hour
    return any(ini <= h < fim for ini, fim in JANELAS)


def _tem_loja_aberta(cur) -> bool:
    """Mesma regra da vitrine pública, mais o is_open.

    Se divergir da vitrine um dia, este job convida alguém a pedir numa loja
    que o app não mostra — e a pessoa abre o carrinho pra encontrar nada.
    """
    cur.execute("""
        SELECT EXISTS (
          SELECT 1 FROM restaurant_profiles rp
           WHERE COALESCE(rp.approved,false) AND COALESCE(rp.active,false)
             AND COALESCE(rp.is_open,false)
             AND rp.latitude IS NOT NULL AND rp.longitude IS NOT NULL
             AND EXISTS (SELECT 1 FROM menu_items mi
                          WHERE mi.restaurant_id = rp.id
                            AND COALESCE(mi.is_available, TRUE) = TRUE)
        ) AS tem
    """)
    r = cur.fetchone()
    return bool(r and r["tem"])


def executar(dry_run: bool = False) -> dict:
    """Um ciclo. Nunca levanta — devolve dict com o que aconteceu."""
    import psycopg2.extras
    from ..utils.helpers import get_db_connection
    from ..utils.platform_settings import get_settings

    try:
        minutos = int(float(get_settings().get("cart_reminder_minutes") or 0))
    except Exception:
        minutos = 0
    if minutos <= 0:
        return {"pulou": "desligado no admin (cart_reminder_minutes = 0)"}

    if not _dentro_da_janela():
        return {"pulou": "fora da janela de horário"}

    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return {"erro": "banco indisponível"}
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            if not _tem_loja_aberta(cur):
                return {"pulou": "nenhuma loja aberta agora"}

            hoje = datetime.now(TZ).strftime("%Y-%m-%d")
            campanha = f"cart:{hoje}"

            # A JANELA TEM FIM, e isso importa: sem o limite de 48h, carrinho
            # esquecido há duas semanas continuaria gerando push todo dia.
            # Número que nunca zera é número que se aprende a ignorar — e aqui
            # o "número" é a paciência do cliente.
            cur.execute("""
                SELECT cp.id, NULLIF(TRIM(cp.fcm_token),'') AS token,
                       COALESCE(cp.cart_value,0)       AS valor,
                       COALESCE(cp.cart_items_count,0) AS itens
                  FROM client_profiles cp
                 WHERE COALESCE(cp.cart_items_count,0) > 0
                   AND NULLIF(TRIM(cp.fcm_token),'') IS NOT NULL
                   AND cp.cart_updated_at <  NOW() - (%s || ' minutes')::interval
                   AND cp.cart_updated_at >= NOW() - INTERVAL '48 hours'
                   AND NOT EXISTS (SELECT 1 FROM push_campaign_log l
                                    WHERE l.client_id = cp.id AND l.campanha = %s)
                 ORDER BY cp.cart_value DESC NULLS LAST
                 LIMIT %s
            """, (str(minutos), campanha, MAX_POR_CICLO))
            alvos = cur.fetchall()

            if not alvos:
                return {"candidatos": 0, "enviados": 0, "minutos": minutos}
            if dry_run:
                return {"candidatos": len(alvos), "enviados": 0, "dry_run": True,
                        "minutos": minutos}

            # RESERVA ANTES DE ENVIAR. Se gravasse depois, uma falha no meio do
            # envio deixaria a pessoa sem registro — e no ciclo seguinte (10
            # min depois) ela levaria o push de novo.
            enviados, destinos = [], []
            for c in alvos:
                try:
                    cur.execute("""
                        INSERT INTO push_campaign_log (client_id, campanha, tipo, sent_at)
                        VALUES (%s, %s, 'cart', NOW())
                        ON CONFLICT DO NOTHING RETURNING id
                    """, (c["id"], campanha))
                    if cur.fetchone():
                        destinos.append((str(c["id"]), c["token"]))
                        enviados.append(c)
                except Exception:
                    logger.warning("reserva do lembrete falhou p/ %s", c["id"],
                                   exc_info=True)
            conn.commit()

        if not destinos:
            return {"candidatos": len(alvos), "enviados": 0, "minutos": minutos}

        from ..services.notification_service import send_campaign
        r = send_campaign(
            destinos,
            "Seu pedido está esperando 🍔",
            "Você deixou itens no carrinho. Toque para finalizar!",
            {"tipo": "cart", "url": "/carrinho"},
        )
        logger.info("[CARRINHO] %d candidato(s), %d push(es) — corte de %d min",
                    len(alvos), r.get("enviados", 0), minutos)
        return {"candidatos": len(alvos), "enviados": r.get("enviados", 0),
                "falhas": r.get("falhas", 0), "minutos": minutos}

    except Exception:
        logger.exception("[CARRINHO] ciclo falhou")
        return {"erro": "exceção no ciclo"}
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def conversao(dias: int = 7) -> dict:
    """Quantos dos lembrados pediram depois de receber.

    Sem isto a automação roda no escuro: dá pra ver quantos pushes saíram e
    nunca saber se ajudaram ou irritaram. A conta é simples — pedido criado
    DEPOIS do envio, no mesmo dia, pela mesma pessoa.

    Não é prova de causa (a pessoa podia voltar sozinha), mas é o sinal que
    separa "está funcionando" de "estamos incomodando de graça".
    """
    import psycopg2.extras
    from ..utils.helpers import get_db_connection

    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return {"erro": "banco indisponível"}
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                WITH envios AS (
                  SELECT client_id, sent_at
                    FROM push_campaign_log
                   WHERE tipo = 'cart' AND sent_at >= NOW() - (%s || ' days')::interval
                )
                SELECT count(*) AS enviados,
                       count(*) FILTER (WHERE EXISTS (
                         SELECT 1 FROM orders o
                          WHERE o.client_id = e.client_id
                            AND o.created_at > e.sent_at
                            AND o.created_at < e.sent_at + INTERVAL '6 hours'
                            AND o.status NOT IN ('cancelled','canceled')
                       )) AS converteram
                  FROM envios e
            """, (str(dias),))
            r = cur.fetchone()
            env = int(r["enviados"] or 0)
            conv = int(r["converteram"] or 0)
            return {"dias": dias, "enviados": env, "converteram": conv,
                    "taxa_pct": round(100.0 * conv / env, 1) if env else None}
    except Exception:
        logger.exception("[CARRINHO] cálculo de conversão falhou")
        return {"erro": "exceção no cálculo"}
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
