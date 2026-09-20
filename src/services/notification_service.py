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
# Canal NOVO do entregador (APK de 16/09/2026 em diante). Nasce no NATIVO
# (MainActivity.java), junto com res/raw/inksa_alerta.mp3, e toca no fluxo de
# ALARME em vez do de notificacao.
#
# ⚠️ POR QUE A TROCA E POR AJUSTE E NAO POR CODIGO: enquanto houver entregador
# com o APK velho, mandar pro `_v2` deixa ELE MUDO — canal que nao existe faz o
# Android cair no canal padrao, que e silencioso. Entao quem vira a chave e o
# Diego, no admin, DEPOIS de o APK novo estar na rua. Sem deploy, e reversivel
# no mesmo lugar se algo der errado.
CANAL_URGENTE_V2 = 'inksa_urgente_v2'
# Canal do MEIO, criado pelo JS (notificationService.js) e por isso distribuido
# por OTA -- chega a todo mundo no proximo open, sem passar pela loja.
#
# POR QUE ELE EXISTE, JA QUE O `_v2` E MELHOR
# Em 16/09/2026 a tela de notificacoes do Android de um entregador nao mostrava
# categoria nenhuma: o `inksa_urgente` nunca tinha sido criado naquele aparelho.
# E tem um motivo provavel -- o JS pedia `sound: 'default'`, e o plugin do
# Capacitor NAO trata 'default' como palavra especial: ele monta o caminho
# literal `android.resource://<pkg>/raw/default`, um recurso que nao existe no
# pacote instalado. O `_v3` nasce SEM o campo `sound`, e ai o proprio Android
# usa o som padrao de verdade do sistema.
#
# ⚠️ NAO substitui o `_v2`. O plugin do Capacitor fixa USAGE_NOTIFICATION, o
# fluxo que nao sobe com os botoes de volume; o fluxo de ALARME so no nativo.
# O `_v3` e o degrau que da pra subir hoje, o `_v2` e o destino.
CANAL_URGENTE_V3 = 'inksa_urgente_v3'


def _canal_do_entregador():
    """Canal que o push urgente deve usar AGORA.

    Le `platform_settings.push_canal_entregador`. Vazio ou desconhecido -> o
    canal antigo, que e o comportamento de hoje. Fail-safe de proposito: errar
    aqui e deixar entregador sem aviso sonoro, e aviso que nao toca custa
    corrida perdida.
    """
    try:
        from ..utils.platform_settings import get_settings
        escolhido = str(get_settings().get('push_canal_entregador') or '').strip()
    except Exception:
        logger.exception("push_canal_entregador indisponivel; usando o canal antigo")
        return CANAL_URGENTE, 'default'
    if escolhido == CANAL_URGENTE_V2:
        # ⚠️ O `sound` tem que bater com o nome do arquivo em res/raw, SEM
        # extensao. Errar aqui nao da erro: so nao toca.
        return CANAL_URGENTE_V2, 'inksa_alerta'
    if escolhido == CANAL_URGENTE_V3:
        # `default` aqui e o valor documentado do FCM para "som padrao do
        # aparelho", e so vale em Android 7 ou anterior -- do 8 pra frente quem
        # decide e o canal. Nao confundir com o `sound: 'default'` do JS, que e
        # o bug que este canal conserta.
        return CANAL_URGENTE_V3, 'default'
    return CANAL_URGENTE, 'default'
# Canal das campanhas (oferta relampago). O `_som` no nome nao e enfeite: o
# canal `inksa_ofertas` nasceu sem som em 13/09/2026 e canal do Android e
# IMUTAVEL -- acrescentar som ao mesmo id nao teria efeito em quem ja abriu o
# app. Id novo foi a unica saida.
#
# ⚠️ So funciona a partir do APK que EMPACOTA res/raw/oferta_relampago.mp3 e
# CRIA este canal. Mandar pra um canal inexistente e pior que nao mandar
# canal nenhum: o Android descarta a notificacao em silencio.
CANAL_CAMPANHA = 'inksa_ofertas_som'


def _montar_mensagem(token: str, title: str, body: str, data: dict = None,
                     urgente: bool = False, canal: str = None,
                     fixa: bool = False, tag: str = None):
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
        _canal, _som = _canal_do_entregador()
        notif = messaging.AndroidNotification(
            title=title, body=body,
            channel_id=_canal,
            sound=_som,
            default_vibrate_timings=True,
        )
        config['priority'] = 'high'

    # Canal explícito (campanha). O urgente já definiu o dele acima.
    #
    # `sound` aqui NÃO é redundante com o canal: no Android 8+ quem manda é o
    # canal, mas em 7 e abaixo (que não tem canal nenhum) é este campo que toca.
    #
    # `priority='high'` é o que faz a oferta CHEGAR com o app fechado. Sem ele o
    # aparelho em Doze segura o push até a próxima janela de sincronismo — que
    # pode ser 15 minutos depois. Numa oferta que dura 5, chegar atrasado é o
    # mesmo que não chegar, e ainda gera a reclamação de "já expirou".
    if canal and not urgente:
        notif = messaging.AndroidNotification(
            title=title, body=body,
            channel_id=canal,
            sound='oferta_relampago',
            default_vibrate_timings=True,
        )
        config['priority'] = 'high'

    # FIXA E COM ETIQUETA — o atalho de volta do Waze (20/09/2026).
    #
    # `sticky=True`: a notificação NÃO some quando a pessoa toca nela. É o que
    # transforma um aviso em ATALHO: o entregador volta pro app, aperta Dirigir
    # de novo, e o caminho de volta continua lá. Sem isso ele precisaria sair e
    # voltar do Waze só pra fazer a notificação reaparecer.
    #
    # `tag`: notificação com a mesma etiqueta SUBSTITUI a anterior em vez de
    # empilhar. Sem ela, quem apertasse Dirigir três vezes na mesma corrida
    # ficaria com três avisos iguais na barra, e aí a barra deixa de ajudar.
    #
    # ⚠️ Não dá pra APAGAR notificação pelo FCM. Quem limpa é o app, ao
    # concluir a entrega (removeAllDeliveredNotifications) — senão a corrida
    # terminada ficaria na barra convidando a abrir um pedido que já acabou.
    if fixa or tag:
        campos = {'title': title, 'body': body}
        # Preserva o que os ramos acima já decidiram (canal, som, vibração).
        for k in ('channel_id', 'sound', 'default_vibrate_timings'):
            v = getattr(notif, k, None)
            if v is not None:
                campos[k] = v
        if fixa:
            campos['sticky'] = True
        if tag:
            campos['tag'] = tag
        notif = messaging.AndroidNotification(**campos)

    return messaging.Message(
        data={**corpo_dados, 'urgente': '1' if urgente else '0'},
        android=messaging.AndroidConfig(notification=notif, **config),
        token=token,
    )


def send_campaign(destinos: list, title: str, body: str, data: dict = None,
                  canal: str = CANAL_CAMPANHA) -> dict:
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
            messaging.send(_montar_mensagem(token, title, body, data, canal=canal))
            resultado["enviados"] += 1
        except messaging.UnregisteredError:
            # App desinstalado ou token trocado: marca pra limpeza.
            resultado["invalidos"].append(client_id)
        except Exception as e:
            logger.warning("FCM campanha: falha em %s: %s", str(client_id)[:8], e)
            resultado["falhas"] += 1
            # GUARDA O MOTIVO, não só a contagem.
            #
            # Em 13/09/2026 o Diego testou o push da oferta relâmpago e nada
            # chegou. Eu passei meia hora conferindo service worker, token e
            # primeiro plano — e não tinha como saber o que o FCM tinha dito,
            # porque aqui só se contava "1 falha". "Falhou" sem motivo não é
            # diagnóstico, é adivinhação.
            resultado.setdefault("erros", []).append(f"{type(e).__name__}: {e}")

    logger.info("FCM campanha: %d enviados, %d falhas, %d inválidos",
                resultado["enviados"], resultado["falhas"], len(resultado["invalidos"]))
    return resultado


def send_push_notification(token: str, title: str, body: str, data: dict = None,
                           urgente: bool = False, fixa: bool = False,
                           tag: str = None) -> bool:
    """Envia push notification via FCM usando firebase_admin. Retorna True se sucesso.

    `fixa` = a notificação não some ao ser tocada (vira atalho, não aviso).
    `tag`  = notificações com a mesma etiqueta se substituem em vez de empilhar.
    """
    if not token:
        logger.warning("FCM: token ausente, notificacao ignorada")
        return False

    if not _init_firebase():
        return False

    try:
        response = messaging.send(_montar_mensagem(token, title, body, data,
                                                   urgente=urgente, fixa=fixa, tag=tag))
        logger.info("FCM: notificacao enviada — message_id=%s token=%s...", response, token[:10])
        return True
    except messaging.UnregisteredError:
        logger.warning("FCM: token inválido/não registrado: %s...", token[:10])
        return False
    except Exception as e:
        logger.warning("FCM send failed: %s", e)
        return False
