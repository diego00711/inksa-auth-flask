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

    payload = {k: str(v) for k, v in (data or {}).items()}
    for client_id, token in destinos:
        if not token:
            continue
        try:
            messaging.send(messaging.Message(
                notification=messaging.Notification(title=title, body=body),
                data=payload,
                token=token,
            ))
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


def send_push_notification(token: str, title: str, body: str, data: dict = None) -> bool:
    """Envia push notification via FCM usando firebase_admin. Retorna True se sucesso."""
    if not token:
        logger.warning("FCM: token ausente, notificacao ignorada")
        return False

    if not _init_firebase():
        return False

    try:
        message = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            data={k: str(v) for k, v in (data or {}).items()},
            token=token,
        )
        response = messaging.send(message)
        logger.info("FCM: notificacao enviada — message_id=%s token=%s...", response, token[:10])
        return True
    except messaging.UnregisteredError:
        logger.warning("FCM: token inválido/não registrado: %s...", token[:10])
        return False
    except Exception as e:
        logger.warning("FCM send failed: %s", e)
        return False
