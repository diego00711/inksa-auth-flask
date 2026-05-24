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
