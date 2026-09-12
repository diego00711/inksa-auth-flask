"""Token de sinal de vida — credencial estreita para o serviço nativo do Android.

POR QUE NÃO USAR O TOKEN DA SESSÃO

O serviço em primeiro plano do Android precisa bater em `/api/delivery/heartbeat`
de 2 em 2 minutos durante o turno inteiro, inclusive com o app fechado. O token
de sessão do Supabase dura ~1 hora e é renovado trocando o `refresh_token` por
um par novo — e o Supabase ROTACIONA o refresh_token a cada troca.

Se o serviço nativo também renovasse, teríamos dois renovadores independentes
sobre a mesma corrente: o nativo troca, o refresh_token guardado pelo JS vira
lixo, e na próxima renovação o app DESLOGA o entregador no meio do turno. O
remédio seria pior que a doença que ele veio curar.

Daí esta credencial separada: assinada por nós, longa, e que só serve pra UMA
coisa.

O QUE ELA PODE E O QUE NÃO PODE

Vale exclusivamente em `/api/delivery/heartbeat`, cujo efeito é atualizar
`last_heartbeat` e `current_lat/lng` DO PRÓPRIO dono do token. Não lê pedido,
não aceita corrida, não mexe em dinheiro, não serve em nenhuma outra rota — a
validação exige `scope == "heartbeat"`, e o decorador de sessão normal rejeita
este token porque ele não tem `aud: authenticated`.

Roubada, ela deixa alguém manter um entregador marcado como vivo e mentir a
posição dele. É o mesmo estrago que o token de sessão já permitiria hoje, com o
resto da conta fora de alcance.
"""
import logging
import os
from datetime import datetime, timedelta, timezone

import jwt

logger = logging.getLogger(__name__)

SCOPE = "heartbeat"

# 30 dias: o app renova a credencial toda vez que o entregador fica online, e
# ficar online é o que ele faz pra trabalhar. Quem some por um mês inteiro faz
# login de novo — e nesse cenário o menor dos problemas é o token.
VALIDADE_DIAS = 30


def _segredo() -> str | None:
    """Segredo de assinatura.

    Preferimos um dedicado (HEARTBEAT_TOKEN_SECRET). Sem ele, cai no segredo de
    JWT que o serviço já tem — assim o recurso funciona sem exigir variável nova
    no Render, que é como um recurso novo costuma morrer em silêncio.
    """
    for nome in ("HEARTBEAT_TOKEN_SECRET", "SUPABASE_JWT_SECRET", "JWT_SECRET"):
        v = os.environ.get(nome)
        if v:
            return v
    return None


def emitir(user_id: str) -> tuple[str | None, datetime | None]:
    """Devolve (token, expira_em) ou (None, None) se não houver segredo."""
    segredo = _segredo()
    if not segredo or not user_id:
        return None, None
    agora = datetime.now(timezone.utc)
    expira = agora + timedelta(days=VALIDADE_DIAS)
    token = jwt.encode(
        {"sub": str(user_id), "scope": SCOPE, "iat": agora, "exp": expira},
        segredo,
        algorithm="HS256",
    )
    # PyJWT 1.x devolvia bytes; 2.x devolve str. Normaliza.
    if isinstance(token, bytes):
        token = token.decode("utf-8")
    return token, expira


def validar(token: str) -> str | None:
    """Devolve o user_id se o token for um sinal de vida válido, senão None.

    Nunca levanta: quem chama já tem um caminho de token normal pra tentar.
    """
    segredo = _segredo()
    if not segredo or not token:
        return None
    try:
        claims = jwt.decode(
            token, segredo,
            algorithms=["HS256"],
            options={"require": ["exp", "sub"], "verify_aud": False},
        )
    except Exception:
        return None
    # ⚠️ A checagem de escopo é o que impede um token de SESSÃO do Supabase de
    # ser aceito aqui (ele é assinado com o mesmo segredo quando o dedicado não
    # existe). Sem ela, esta porta aceitaria qualquer usuário logado da
    # plataforma — cliente, parceiro, admin — como se fosse entregador.
    if claims.get("scope") != SCOPE:
        return None
    sub = claims.get("sub")
    return str(sub) if sub else None
