# src/services/notification_service.py
import os
import logging
import requests

logger = logging.getLogger(__name__)

FIREBASE_SERVER_KEY = os.environ.get("FIREBASE_SERVER_KEY", "")  # Diego: preencha no .env


def send_push_notification(token: str, title: str, body: str, data: dict = None) -> bool:
    """Envia push notification via FCM HTTP v1. Retorna True se sucesso."""
    if not token or not FIREBASE_SERVER_KEY:
        logger.warning("FCM: token ou chave ausente, notificacao ignorada")
        return False
    try:
        payload = {
            "to": token,
            "notification": {"title": title, "body": body},
            "data": data or {}
        }
        resp = requests.post(
            "https://fcm.googleapis.com/fcm/send",
            json=payload,
            headers={
                "Authorization": f"key={FIREBASE_SERVER_KEY}",
                "Content-Type": "application/json"
            },
            timeout=5
        )
        resp.raise_for_status()
        logger.info(f"FCM: notificacao enviada com sucesso para token={token[:10]}...")
        return True
    except Exception as e:
        logger.warning(f"FCM send failed: {e}")
        return False
