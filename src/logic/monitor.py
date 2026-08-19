"""Monitor de saúde: roda de hora em hora e avisa quando algo quebra.

⚠️ LIMITE QUE NÃO DÁ PRA CONTORNAR DAQUI: este monitor vive DENTRO do backend.
Se o backend cair, ele cai junto e ninguém é avisado — um sistema não consegue
anunciar a própria morte. Para isso é preciso alguém de FORA pingando
/api/health (um serviço de uptime gratuito resolve em 2 minutos). O que este
monitor cobre é a outra categoria, que é a maioria: o backend está de pé e
alguma coisa por dentro parou de funcionar.

Princípios que valem pra qualquer checagem adicionada aqui:

- **Só avisa o que exige AÇÃO.** Alerta que a pessoa aprende a ignorar é pior
  que alerta nenhum, porque some no meio do ruído justo no dia que importa.
- **Uma checagem que falha não derruba as outras.** Cada uma roda isolada; um
  erro vira alerta próprio ("a checagem X quebrou") em vez de matar o ciclo.
- **Não repete o mesmo alerta de hora em hora.** Silêncio de 6h por assunto,
  senão o e-mail vira spam e vai pro lixo — junto com o próximo, que era grave.
"""

import logging
import os
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# Quanto tempo o mesmo assunto fica em silêncio depois de avisado. Sem isto,
# um problema que dura o fim de semana manda 48 e-mails iguais e ensina o
# Diego a arquivar sem ler.
_SILENCIO_HORAS = 6
_ultimo_aviso: dict[str, datetime] = {}

# Gravidade: o assunto do e-mail muda, pra dar pra triar pelo celular.
CRITICO = "CRÍTICO"
ATENCAO = "atenção"


def _fmt_brl(v) -> str:
    try:
        return f"R$ {float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return str(v)


# ---------------------------------------------------------------------------
# As checagens. Cada uma devolve lista de (gravidade, titulo, detalhe).
# ---------------------------------------------------------------------------

def _checar_pedidos_parados(cur):
    """Pedido aceito pela loja que ninguém retirou.

    É o alerta que mais vale dinheiro: o cliente está esperando, pagou, e o
    pedido não anda. Duas horas é folgado o bastante pra não pegar hora de
    pico e curto o bastante pra dar tempo de salvar a entrega.
    """
    cur.execute("""
        SELECT count(*) AS n, MIN(created_at) AS mais_antigo
          FROM orders
         WHERE status IN ('accepted','preparing','ready')
           AND created_at < NOW() - INTERVAL '2 hours'
           AND archived_at IS NULL
    """)
    r = cur.fetchone()
    if r and r['n']:
        return [(CRITICO, f"{r['n']} pedido(s) parado(s) há mais de 2h",
                 "Cliente esperando e o pedido não anda. O mais antigo é de "
                 f"{r['mais_antigo']:%d/%m %H:%M} (UTC).")]
    return []


def _checar_pedido_sem_entregador(cur):
    """Pronto pra coleta e sem ninguém pra buscar.

    Diferente do anterior: aqui a loja fez a parte dela. Se ninguém aceita, ou
    não há entregador apto na área, ou o despacho parou.
    """
    cur.execute("""
        SELECT count(*) AS n FROM orders
         WHERE status = 'ready' AND delivery_id IS NULL
           AND created_at < NOW() - INTERVAL '45 minutes'
           AND archived_at IS NULL
    """)
    r = cur.fetchone()
    if r and r['n']:
        return [(CRITICO, f"{r['n']} pedido(s) pronto(s) sem entregador há 45min",
                 "A loja já preparou. Ou não há entregador apto na região, ou "
                 "o motor de despacho parou.")]
    return []


def _checar_pagamento_travado(cur):
    """Pedido preso em 'aguardando pagamento' apesar do job que expira em 30min.

    Se aparecer, o job de expiração parou — e aí o cliente vê um pedido
    fantasma na tela dele.
    """
    cur.execute("""
        SELECT count(*) AS n FROM orders
         WHERE status = 'awaiting_payment' AND created_at < NOW() - INTERVAL '90 minutes'
    """)
    r = cur.fetchone()
    if r and r['n']:
        return [(ATENCAO, f"{r['n']} pedido(s) travado(s) em 'aguardando pagamento'",
                 "O job que cancela esses pedidos roda a cada 5min e expira em "
                 "30min. Se sobrou algo com 90min, o agendador provavelmente parou.")]
    return []


def _checar_repasses(cur):
    """Repasse gerado que não saiu.

    Dinheiro que devia ter ido pro parceiro ou pro entregador e ficou parado.
    """
    cur.execute("""
        SELECT count(*) AS n, COALESCE(SUM(amount),0) AS total
          FROM payouts
         WHERE status IN ('pending','processing')
           AND created_at < NOW() - INTERVAL '48 hours'
    """)
    r = cur.fetchone()
    if r and r['n']:
        return [(CRITICO, f"{r['n']} repasse(s) parado(s) há mais de 48h",
                 f"Total {_fmt_brl(r['total'])} que deveria ter sido pago. "
                 "Conferir saldo da conta Asaas e a fila de PIX.")]
    return []


def _checar_vitrine(cur):
    """Ninguém pra pedir: sem loja visível, o app do cliente está vazio.

    A regra é a MESMA da vitrine pública (aprovada + ativa + coordenada +
    ao menos um item disponível). Se divergir um dia, este alerta mente.
    """
    cur.execute("""
        SELECT count(*) AS n FROM restaurant_profiles rp
         WHERE COALESCE(rp.approved,false) AND COALESCE(rp.active,false)
           AND rp.latitude IS NOT NULL AND rp.longitude IS NOT NULL
           AND EXISTS (SELECT 1 FROM menu_items mi
                        WHERE mi.restaurant_id = rp.id
                          AND COALESCE(mi.is_available, TRUE) = TRUE)
    """)
    r = cur.fetchone()
    if r is not None and r['n'] == 0:
        return [(CRITICO, "Nenhuma loja aparece no app do cliente",
                 "Quem abrir o Inksa agora vê a vitrine vazia. Conferir se "
                 "algum parceiro foi desativado ou ficou sem cardápio.")]
    return []


def _checar_push(cur):
    """Entregador aprovado sem token de push não é avisado de nada.

    Não é falha do sistema — é cadastro pela metade. Mas sem isso ele fica
    surdo com o app em segundo plano, que é quando o aviso importa.
    """
    cur.execute("""
        SELECT count(*) AS n FROM delivery_profiles
         WHERE COALESCE(approved,false) AND fcm_token IS NULL
           AND COALESCE(current_lat, latitude) IS NOT NULL
    """)
    r = cur.fetchone()
    if r and r['n']:
        return [(ATENCAO, f"{r['n']} entregador(es) aprovado(s) sem push",
                 "Eles não recebem aviso de corrida com o app fechado. "
                 "Precisam abrir o app e aceitar a permissão de notificação.")]
    return []


def _checar_credenciais():
    """As chaves que só falham no dia em que são usadas.

    Não toca no banco: lê ambiente e disco. Se o Secret File do FCM sumir num
    redeploy, o push para de sair CALADO — este é o aviso.
    """
    faltando = []
    if not os.path.exists("/etc/secrets/firebase-service-account.json"):
        faltando.append("FCM (push) — Secret File ausente")
    if not os.environ.get("ASAAS_API_KEY"):
        faltando.append("Asaas — ASAAS_API_KEY ausente")
    if not (os.environ.get("SMTP_HOST") and os.environ.get("SMTP_PASSWORD")):
        faltando.append("SMTP — este próprio alerta pode não ter saído")
    if faltando:
        return [(CRITICO, "Credencial faltando no servidor", "; ".join(faltando))]
    return []


_CHECAGENS_BANCO = [
    ("pedidos parados", _checar_pedidos_parados),
    ("pedido sem entregador", _checar_pedido_sem_entregador),
    ("pagamento travado", _checar_pagamento_travado),
    ("repasses", _checar_repasses),
    ("vitrine", _checar_vitrine),
    ("push dos entregadores", _checar_push),
]


# ---------------------------------------------------------------------------
def coletar_alertas() -> list[tuple[str, str, str]]:
    """Roda todas as checagens. Nunca levanta — falha vira alerta."""
    import psycopg2.extras
    from ..utils.helpers import get_db_connection

    alertas: list[tuple[str, str, str]] = []
    try:
        alertas += _checar_credenciais()
    except Exception as e:
        alertas.append((ATENCAO, "Checagem de credenciais falhou", str(e)[:200]))

    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return alertas + [(CRITICO, "Banco de dados inacessível",
                               "O backend está de pé mas não conecta no Supabase. "
                               "Nada funciona nesse estado.")]
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            for nome, fn in _CHECAGENS_BANCO:
                try:
                    alertas += fn(cur)
                except Exception as e:
                    # Uma checagem quebrada não pode calar as outras — e a
                    # própria quebra é notícia (coluna renomeada, por exemplo).
                    logger.warning("[MONITOR] checagem '%s' falhou", nome, exc_info=True)
                    alertas.append((ATENCAO, f"A checagem '{nome}' quebrou", str(e)[:200]))
    except Exception as e:
        alertas.append((CRITICO, "Monitor não conseguiu consultar o banco", str(e)[:200]))
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
    return alertas


def _deve_avisar(titulo: str) -> bool:
    """Silêncio por assunto, pra não transformar aviso em ruído."""
    agora = datetime.now(timezone.utc)
    ultimo = _ultimo_aviso.get(titulo)
    if ultimo and agora - ultimo < timedelta(hours=_SILENCIO_HORAS):
        return False
    _ultimo_aviso[titulo] = agora
    return True


def executar_monitor() -> dict:
    """Ponto de entrada do agendador. Devolve resumo (útil pro log e pro admin)."""
    from ..utils import email_service

    alertas = coletar_alertas()
    if not alertas:
        logger.info("[MONITOR] tudo certo — nenhum alerta")
        return {"alertas": 0, "enviados": 0, "detalhes": []}

    novos = [a for a in alertas if _deve_avisar(a[1])]
    for grav, titulo, detalhe in alertas:
        logger.warning("[MONITOR] %s: %s — %s", grav, titulo, detalhe)

    if not novos:
        logger.info("[MONITOR] %d alerta(s), todos já avisados nas últimas %dh",
                    len(alertas), _SILENCIO_HORAS)
        return {"alertas": len(alertas), "enviados": 0,
                "detalhes": [a[1] for a in alertas]}

    destino = os.environ.get("MONITOR_EMAIL") or os.environ.get("ADMIN_EMAIL")
    if not destino:
        logger.error("[MONITOR] %d alerta(s) SEM DESTINO — configure MONITOR_EMAIL", len(novos))
        return {"alertas": len(alertas), "enviados": 0, "erro": "sem MONITOR_EMAIL",
                "detalhes": [a[1] for a in alertas]}

    tem_critico = any(g == CRITICO for g, _, _ in novos)
    assunto = ("[Inksa] CRÍTICO: " if tem_critico else "[Inksa] atenção: ") + novos[0][1]
    if len(novos) > 1:
        assunto += f" (+{len(novos)-1})"

    linhas = "".join(
        f'<li style="margin-bottom:10px"><b>{g}</b> — {t}<br>'
        f'<span style="color:#555">{d}</span></li>'
        for g, t, d in novos
    )
    html = email_service.render_simple(
        "Monitor da Inksa",
        f"<p>A verificação automática encontrou {len(novos)} problema(s):</p>"
        f"<ul>{linhas}</ul>"
        f"<p style='color:#777;font-size:13px'>Verificado em "
        f"{datetime.now(timezone.utc):%d/%m/%Y %H:%M} UTC. Este assunto não será "
        f"repetido nas próximas {_SILENCIO_HORAS} horas.</p>",
    )
    try:
        ok = email_service.send_email(destino, assunto, html)
    except Exception:
        logger.exception("[MONITOR] falha ao enviar e-mail de alerta")
        ok = False

    return {"alertas": len(alertas), "enviados": len(novos) if ok else 0,
            "email_ok": ok, "detalhes": [a[1] for a in alertas]}
