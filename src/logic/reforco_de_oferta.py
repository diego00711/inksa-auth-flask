# src/logic/reforco_de_oferta.py
"""
Repete o aviso de "entrega disponivel" enquanto ninguem aceitou.

## POR QUE REPETICAO, E NAO VOLUME

Reclamacao do entregador em 16/09/2026: *"o tok ta baixo"*, *"se tiver andando
de moto nao escuta"*, *"e ele nao sobe"*.

O diagnostico feito no aparelho dele (card da Central de Suporte do app)
mostrou que o canal esta CERTO: `inksa_urgente`, importancia 5, som
`content://settings/system/notification_sound`. Ou seja, nao ha bug de canal —
o que ha e um som curto e grave, no fluxo de notificacao, competindo com o
ronco do motor dentro de um capacete.

Subir o som de verdade exige `USAGE_ALARM` + arquivo proprio em `res/raw`, o
que so existe no APK 1.0.7, que depende da loja. **Repeticao e a unica coisa
que da pra fazer por software hoje** — e dentro do capacete ela vale mais que
volume: um toque perdido no ronco vira tres chances.

## O QUE SEGURA O ABUSO

Notificacao repetida demais e o caminho mais curto pro entregador desligar a
notificacao do app, e ai perdemos o canal inteiro — nao so o reforco. Por isso:

  1. **So enquanto ninguem aceitou.** Cada disparo re-le o pedido: se saiu de
     `ready` ou ja tem `delivery_id`, a serie morre ali.
  2. **So pra quem pode pegar.** Recalcula os aptos a cada toque, com a mesma
     regra do primeiro push (`tokens_para_avisar`). Quem ficou online no meio
     do caminho entra; quem saiu, sai.
  3. **Desligavel sem deploy.** `platform_settings.push_reforco_oferta_segundos`
     vazio desliga tudo. Nao inventei um numero fixo no codigo justamente
     porque o numero certo so a rua diz.

## POR QUE NO APSCHEDULER, E NAO NUMA THREAD SOLTA

`_notify()` em routes/orders.py carrega uma cicatriz: em 29/08/2026 o envio do
FCM foi movido pra uma thread daemon por requisicao e os pushes simplesmente
pararam de chegar. Foi revertido no mesmo dia, com a nota de que o caminho
certo seria fila de verdade, nao thread solta dentro do processo web.

O BackgroundScheduler nao e essa thread solta: ele ja roda desde o boot e ja
dispara push em producao (o lembrete de carrinho abandonado usa `send_campaign`
por esse mesmo caminho). Agendar aqui e andar por trilho testado.

⚠️ Job em memoria morre com o worker. Se o Render reiniciar dentro da janela de
30 s, o reforco daquele pedido se perde. E aceitavel de proposito: o primeiro
push ja saiu (esse e sincrono, na requisicao), e persistir agendamento de 15 s
custaria mais do que vale.
"""
import logging

logger = logging.getLogger(__name__)

# Teto de sanidade. Nao e configuracao: e anteparo pra um dedo errado no admin
# transformar o aviso de corrida em perseguicao.
_MAX_REFORCOS = 4
_MAX_SEGUNDOS = 300


# Como DESLIGAR. Apagar o campo NAO desliga: o merge do platform_settings faz
# `valor or padrao`, entao vazio volta pro padrao "15,30". Quem quiser desligar
# escreve uma destas palavras (ou `0`).
_DESLIGADO = {'0', 'off', 'nao', 'não', 'desligado', 'desligada', 'false'}


def _segundos_configurados():
    """Le a lista de atrasos do admin. Lista vazia = reforco desligado."""
    try:
        from ..utils.platform_settings import get_settings
        bruto = str(get_settings().get('push_reforco_oferta_segundos') or '')
    except Exception:
        logger.exception("[REFORCO] settings indisponivel; reforco desligado")
        return []

    if bruto.strip().lower() in _DESLIGADO:
        return []

    segundos = []
    for pedaco in bruto.split(','):
        pedaco = pedaco.strip()
        if not pedaco:
            continue
        try:
            n = int(float(pedaco))
        except (TypeError, ValueError):
            logger.warning("[REFORCO] valor invalido em push_reforco_oferta_segundos: %r", pedaco)
            continue
        if 0 < n <= _MAX_SEGUNDOS:
            segundos.append(n)
    return sorted(set(segundos))[:_MAX_REFORCOS]


def agendar(order_id) -> int:
    """Agenda os reforcos deste pedido. Devolve quantos foram agendados.

    Nunca levanta excecao: e enfeite em cima do push que ja saiu. Se falhar, o
    primeiro aviso continua valendo e o pedido segue a vida.
    """
    try:
        atrasos = _segundos_configurados()
        if not atrasos:
            return 0

        from datetime import datetime, timedelta, timezone
        from ..scheduler import get_scheduler

        sched = get_scheduler()
        if sched is None or not sched.running:
            logger.warning("[REFORCO] scheduler fora do ar; pedido %s sem reforco", order_id)
            return 0

        agora = datetime.now(timezone.utc)
        agendados = 0
        for i, s in enumerate(atrasos, start=1):
            sched.add_job(
                func=_disparar,
                trigger='date',
                run_date=agora + timedelta(seconds=s),
                args=[str(order_id), i, len(atrasos)],
                # id por pedido+toque: se a rota rodar duas vezes pro mesmo
                # pedido, `replace_existing` reaproveita em vez de duplicar o
                # aviso. Duplicar aqui e exatamente o abuso que queremos evitar.
                id=f"reforco_oferta_{order_id}_{i}",
                name=f"Reforco {i} da oferta {order_id}",
                replace_existing=True,
                misfire_grace_time=30,
            )
            agendados += 1
        logger.info("[REFORCO] pedido %s: %d reforco(s) agendado(s) em %s s", order_id, agendados, atrasos)
        return agendados
    except Exception:
        logger.exception("[REFORCO] falha ao agendar reforco do pedido %s", order_id)
        return 0


def _disparar(order_id: str, toque: int, total: int) -> None:
    """Reenvia a oferta se o pedido ainda estiver pronto e sem entregador."""
    from ..utils.helpers import get_db_connection

    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            logger.error("[REFORCO] sem conexao com o banco; pedido %s toque %d abortado", order_id, toque)
            return

        from psycopg2.extras import RealDictCursor
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT o.status, o.delivery_id, o.items, o.delivery_distance_km,
                       rp.latitude, rp.longitude
                  FROM orders o
                  JOIN restaurant_profiles rp ON rp.id = o.restaurant_id
                 WHERE o.id = %s
            """, (order_id,))
            pedido = cur.fetchone()

            if not pedido:
                logger.info("[REFORCO] pedido %s sumiu; toque %d cancelado", order_id, toque)
                return
            # A trava que importa: quem ja aceitou nunca recebe reforco.
            if pedido['status'] != 'ready' or pedido['delivery_id']:
                logger.info("[REFORCO] pedido %s ja saiu da fila (status=%s, entregador=%s); toque %d cancelado",
                            order_id, pedido['status'], bool(pedido['delivery_id']), toque)
                return

            from ..utils.carga import tokens_para_avisar, peso_do_pedido
            from ..utils.platform_settings import get_settings

            try:
                peso = float(peso_do_pedido(cur, pedido['items']) or 0)
            except Exception:
                peso = 0.0

            tokens = tokens_para_avisar(
                peso, pedido['latitude'], pedido['longitude'], get_settings(),
                distancia_km=pedido.get('delivery_distance_km'))

        if not tokens:
            logger.info("[REFORCO] pedido %s toque %d: ninguem apto e online agora", order_id, toque)
            return

        # Texto diferente do primeiro aviso de proposito. Notificacao repetida
        # com o MESMO texto o olho descarta como "ja vi isso"; dizer que ainda
        # esta esperando e informacao nova, e e verdade.
        titulo = "Entrega ainda disponivel! 🛵"
        corpo = ("Ninguem pegou este pedido ainda. Toque para ver."
                 if toque < total else
                 "Ultima chamada: o pedido continua esperando.")

        from ..services.notification_service import send_push_notification
        enviados = 0
        for tk in tokens:
            try:
                send_push_notification(tk, titulo, corpo,
                                       {"order_id": str(order_id), "status": "ready",
                                        # o worker do entregador espera exatamente esta chave
                                        "type": "new_delivery"},
                                       urgente=True)
                enviados += 1
            except Exception:
                logger.warning("[REFORCO] push falhou num token do pedido %s", order_id, exc_info=True)

        logger.info("[REFORCO] pedido %s toque %d/%d: %d aviso(s) reenviado(s)",
                    order_id, toque, total, enviados)
    except Exception:
        logger.exception("[REFORCO] erro no toque %d do pedido %s", toque, order_id)
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
