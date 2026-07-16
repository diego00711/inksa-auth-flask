from flask import request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address


def _client_ip():
    """IP real do cliente, nao o do proxy.

    No Render TODA requisicao chega de 127.0.0.1 (o proxy), entao
    get_remote_address() devolvia o MESMO ip pra todos os usuarios — os
    120/min viravam um balde unico compartilhado por cliente+restaurante+
    entregador+admin. Bastava o polling de um app (ou o GPS do entregador)
    pra estourar e derrubar TODO mundo com 429, ate rotas nao relacionadas
    (ex.: o codigo de retirada). Usar o 1o IP do X-Forwarded-For da a cada
    usuario real o seu proprio balde.
    """
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return get_remote_address()


limiter = Limiter(
    key_func=_client_ip,
    # 240/min por IP real. Folga pro app do entregador (polling + GPS); as
    # rotas sensiveis (login, reset senha...) tem @limiter.limit proprio menor
    # que ganha deste default.
    default_limits=["240 per minute"],
    storage_uri="memory://",
)
