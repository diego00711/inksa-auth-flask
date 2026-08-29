# src/services/notification_service.py
import os
import logging

import firebase_admin
from firebase_admin import credentials, messaging

logger = logging.getLogger(__name__)

# Caminhos do arquivo de credenciais
_PROD_CRED_PATH = "/etc/secrets/firebase-service-account.json"
_DEV_CRED_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "firebase-service-account.json")

_firebase_initialized = False


def _init_firebase() -> bool:
    global _firebase_initialized
    if _firebase_initialized:
        return True
    if firebase_admin._apps:
        _firebase_initialized = True
        return True

    cred_path = None
    if os.path.exists(_PROD_CRED_PATH):
        cred_path = _PROD_CRED_PATH
    elif os.path.exists(_DEV_CRED_PATH):
        cred_path = os.path.normpath(_DEV_CRED_PATH)
    else:
        logger.warning(
            "FCM: arquivo de credenciais não encontrado em '%s' nem em '%s'",
            _PROD_CRED_PATH,
            _DEV_CRED_PATH,
        )
        return False

    try:
        cred = credentials.Certificate(cred_path)
        firebase_admin.initialize_app(cred)
        _firebase_initialized = True
        logger.info("FCM: firebase_admin inicializado com '%s'", cred_path)
        return True
    except Exception as e:
        logger.error("FCM: falha ao inicializar firebase_admin: %s", e)
        return False


def status_firebase() -> dict:
    """Diz se o backend CONSEGUE enviar push, e por quê não, quando não consegue.

    Sem isto a única forma de descobrir que falta o arquivo de credenciais era
    ler o log do Render: `_init_firebase()` devolve False, `send_push_*`
    devolve False, e ninguém acima olha esse retorno. Push que não sai não
    deixa rastro em lugar nenhum — some sem virar problema.
    """
    prod = os.path.exists(_PROD_CRED_PATH)
    dev = os.path.exists(os.path.normpath(_DEV_CRED_PATH))
    ok = _init_firebase()
    return {
        "pode_enviar": ok,
        "credencial_producao": prod,      # /etc/secrets/... (Secret File do Render)
        "credencial_local": dev,
        "caminho_producao": _PROD_CRED_PATH,
        "motivo": None if ok else (
            "arquivo de credenciais não encontrado"
            if not (prod or dev)
            else "arquivo existe mas o firebase_admin não inicializou (veja o log)"
        ),
    }


def enviar_teste(token: str, user_type: str = "client") -> dict:
    """Push de teste com o motivo da falha DE VOLTA, não só um bool.

    send_push_notification devolve True/False e joga a exceção no log. Pra
    diagnosticar um envio que não chega, o texto do erro do FCM é a única
    coisa que importa — então aqui ele sobe junto.

    O TESTE SAI IGUAL AO REAL, e isso é o ponto.

    Antes ele mandava {"tipo": "teste"} sem urgência: ia pro canal padrão do
    Android (silencioso) e sem o `type` que o service worker do web usa pra
    decidir se o aviso fica na tela. Ou seja, testava um caminho que nenhum
    pedido percorre — dava "enviado com sucesso" e não dizia nada sobre o que
    acontece quando um pedido chega de verdade.

    Agora, pra restaurante e entregador, o teste vai pelo MESMO caminho
    urgente do pedido novo: canal de alta importância no APK, e
    requireInteraction + vibração no web. Se tocar aqui, toca no pedido.
    """
    st = status_firebase()
    if not st["pode_enviar"]:
        return {"enviado": False, "erro": st["motivo"], "status": st}
    try:
        # Mesmo `type` que o pedido real manda, pra o service worker do web
        # tratar igual. Ver o worker: requireInteraction: d.type === 'new_order'.
        tipo_evento = {"restaurant": "new_order", "delivery": "new_delivery"}.get(user_type)
        message_id = messaging.send(_montar_mensagem(
            token,
            "Inksa — teste de alarme",
            "Se você ouviu isto, o aviso de pedido novo vai funcionar igual.",
            {"tipo": "teste", **({"type": tipo_evento} if tipo_evento else {})},
            urgente=bool(tipo_evento),
        ))
        return {"enviado": True, "message_id": message_id, "status": st}
    except messaging.UnregisteredError:
        return {"enviado": False, "erro": "token recusado pelo FCM (app desinstalado ou token trocado)", "status": st}
    except Exception as e:
        return {"enviado": False, "erro": f"{type(e).__name__}: {e}", "status": st}


# Canal URGENTE do Android. Existe pra dois eventos e só dois: "novo pedido"
# pro parceiro e "nova entrega" pro entregador. São os únicos em que alguém
# está esperando o aviso pra AGIR — o resto (aceito, a caminho, entregue) é
# informativo e não merece furar a atenção de ninguém.
#
# POR QUE UM CANAL, E NÃO SÓ "sound" NA MENSAGEM
# No Android 8+ quem manda no som, na vibração e no heads-up é o CANAL, não a
# mensagem. Mandar `sound` numa notificação cujo canal não existe não faz
# barulho nenhum — cai no canal padrão, que é justamente o silencioso. O canal
# precisa ser criado pelo app (PushNotifications.createChannel) com o MESMO id
# daqui, senão isto vira enfeite.
#
# ⚠️ O id é contrato entre este arquivo e o JS dos apps. Mudar de um lado só
# faz o som sumir sem erro nenhum em lugar nenhum.
CANAL_URGENTE = 'inksa_urgente'


def _montar_mensagem(token: str, title: str, body: str, data: dict = None,
                     urgente: bool = False):
    """Monta a Message do FCM. Existe pra corrigir o PUSH DUPLICADO.

    O bug: a mensagem ia com `notification=` no nível de cima. No WEB, isso faz
    o SDK do Firebase EXIBIR a notificação sozinho — e o nosso
    `onBackgroundMessage` no service worker também chamava `showNotification`.
    Duas notificações pro mesmo push, uma do SDK e outra nossa.

    A saída não é tirar o showNotification do worker: sem ele a gente perde o
    ícone, o agrupamento por pedido (`tag`) e o clique que leva pra tela certa.
    A saída é o contrário — mandar SÓ DADOS pro web, e deixar o worker ser o
    único que desenha.

    Mas o APK nativo precisa do bloco de notificação, senão não aparece nada
    com o app fechado. Por isso ele vai em `android=`, que o web ignora:

        web    -> só `data`      -> só o service worker desenha  -> 1
        nativo -> `android.notification` -> o Android desenha    -> 1

    title/body também entram em `data` porque, sem o bloco de cima, é de lá
    que o service worker lê.
    """
    extra = {k: str(v) for k, v in (data or {}).items()}
    corpo_dados = {**extra, "title": title, "body": body}
    notif = messaging.AndroidNotification(title=title, body=body)
    config = {}
    if urgente:
        # priority='high' acorda o aparelho em Doze; sem isso o push pode
        # esperar a próxima janela de sincronismo e chegar minutos depois —
        # inútil pra um pedido esperando aceite.
        notif = messaging.AndroidNotification(
            title=title, body=body,
            channel_id=CANAL_URGENTE,
            sound='default',
            default_vibrate_timings=True,
        )
        config['priority'] = 'high'

    return messaging.Message(
        data={**corpo_dados, 'urgente': '1' if urgente else '0'},
        android=messaging.AndroidConfig(notification=notif, **config),
        token=token,
    )


def send_campaign(destinos: list, title: str, body: str, data: dict = None) -> dict:
    """Envia a MESMA notificação pra vários clientes de uma vez.

    `destinos` = lista de (client_profile_id, fcm_token).

    Devolve {enviados, falhas, invalidos:[client_ids]} — os inválidos são
    tokens que o FCM recusou (app desinstalado); quem chama deve limpá-los,
    senão a base de tokens só cresce com lixo.

    Diferente do envio individual, aqui vale a REGRA DE FREQUÊNCIA: quem
    chama já filtrou quem pode receber hoje. Notificação de campanha é a
    única coisa que faz o cliente desinstalar o app — e cliente que
    desinstala não volta.
    """
    resultado = {"enviados": 0, "falhas": 0, "invalidos": []}
    if not destinos:
        return resultado
    if not _init_firebase():
        logger.warning("FCM: campanha ignorada — firebase não inicializado")
        resultado["falhas"] = len(destinos)
        return resultado

    for client_id, token in destinos:
        if not token:
            continue
        try:
            messaging.send(_montar_mensagem(token, title, body, data))
            resultado["enviados"] += 1
        except messaging.UnregisteredError:
            # App desinstalado ou token trocado: marca pra limpeza.
            resultado["invalidos"].append(client_id)
        except Exception as e:
            logger.warning("FCM campanha: falha em %s: %s", str(client_id)[:8], e)
            resultado["falhas"] += 1

    logger.info("FCM campanha: %d enviados, %d falhas, %d inválidos",
                resultado["enviados"], resultado["falhas"], len(resultado["invalidos"]))
    return resultado


def send_push_notification(token: str, title: str, body: str, data: dict = None,
                           urgente: bool = False) -> bool:
    """Envia push notification via FCM usando firebase_admin. Retorna True se sucesso."""
    if not token:
        logger.warning("FCM: token ausente, notificacao ignorada")
        return False

    if not _init_firebase():
        return False

    try:
        response = messaging.send(_montar_mensagem(token, title, body, data, urgente=urgente))
        logger.info("FCM: notificacao enviada — message_id=%s token=%s...", response, token[:10])
        return True
    except messaging.UnregisteredError:
        logger.warning("FCM: token inválido/não registrado: %s...", token[:10])
        return False
    except Exception as e:
        logger.warning("FCM send failed: %s", e)
        return False
