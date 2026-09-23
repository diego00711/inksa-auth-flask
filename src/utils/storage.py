# src/utils/storage.py
#
# SUBIR ARQUIVO NO STORAGE SEM DEPENDER DA SESSÃO DO CLIENTE SUPABASE.
#
# ## O PROBLEMA, QUE JÁ VOLTOU DUAS VEZES
#
# `supabase-py` guarda UM header Authorization por cliente. Quando qualquer
# rota chama `sign_in_with_password`, esse header vira o token do usuário — e
# PostgREST e Storage passam a valer como `authenticated`, não `service_role`.
#
# No Storage isso é fatal e silencioso: `service_role` tem BYPASSRLS,
# `authenticated` não. Os buckets `menu-images`, `logos`, `incident-photos` e
# `rewards-images` NÃO TÊM política de INSERT (conferido no banco em
# 22/09/2026 — só `avatars` e `delivery-avatars` têm, o que escondeu o
# problema por meses). Sem BYPASSRLS, o upload morre com:
#
#     {'statusCode': 400, 'error': 'Unauthorized',
#      'message': 'new row violates row-level security policy'}
#
# E morre SÓ nos workers onde alguém já logou. No worker recém-subido funciona.
# Por isso parece intermitente e some quando alguém "testa de novo".
#
# ## POR QUE NÃO BASTOU USAR `supabase_admin`
#
# Foi a correção de 14/09/2026: 26 chamadas migradas de `supabase.storage` pra
# `supabase_admin.storage`, que nunca faz login. Em 22/09/2026 o erro voltou
# assim mesmo, com um parceiro travado sem conseguir pôr foto no cardápio.
#
# Seja qual for o caminho exato da contaminação, a lição é a mesma: a garantia
# estava apoiada em quem NÃO chama um método, e isso não é garantia — é
# combinado. Qualquer código novo, ou uma versão nova da biblioteca, quebra de
# novo e o sintoma aparece semanas depois, num worker, pra um parceiro só.
#
# ## O QUE ESTE MÓDULO FAZ
#
# Fala com a API REST do Storage por HTTP, montando o header na hora, a partir
# da variável de ambiente. Não existe sessão pra contaminar: a chave usada é a
# que está escrita aqui embaixo, em toda chamada, sempre.

import logging
import os

import requests

logger = logging.getLogger(__name__)

_TIMEOUT = (10, 60)   # (conectar, ler) — upload de foto pode demorar


def _base_e_chave():
    url = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
    chave = os.environ.get("SUPABASE_SERVICE_KEY") or ""
    if not url or not chave:
        raise RuntimeError("SUPABASE_URL/SUPABASE_SERVICE_KEY ausentes")
    return url, chave


def public_url(bucket, caminho):
    """URL pública do arquivo. Montada aqui pra não precisar do cliente."""
    url, _ = _base_e_chave()
    return f"{url}/storage/v1/object/public/{bucket}/{caminho.lstrip('/')}"


def upload(bucket, caminho, conteudo, content_type=None, upsert=True):
    """Sobe bytes e devolve a URL pública. Levanta em caso de falha.

    `upsert=True` por padrão: reenviar o mesmo caminho substitui em vez de
    estourar 409. Quem chama aqui está salvando a foto de um item — se a
    pessoa tentar de novo depois de um erro de rede, tem que funcionar.
    """
    url, chave = _base_e_chave()
    caminho = caminho.lstrip("/")
    destino = f"{url}/storage/v1/object/{bucket}/{caminho}"

    cabecalhos = {
        # A chave vai INTEIRA e AGORA, não herdada de sessão nenhuma.
        # É este o ponto do módulo.
        "Authorization": f"Bearer {chave}",
        "apikey": chave,
        "x-upsert": "true" if upsert else "false",
    }
    if content_type:
        cabecalhos["Content-Type"] = content_type

    r = requests.post(destino, data=conteudo, headers=cabecalhos, timeout=_TIMEOUT)
    if r.status_code >= 400:
        # Loga o corpo: a mensagem do Storage é a que diz se foi RLS, tamanho
        # ou tipo de arquivo. Sem ela, "falha ao subir" não ajuda ninguém.
        logger.error("storage: upload falhou em %s/%s -> %s %s",
                     bucket, caminho, r.status_code, r.text[:300])
        r.raise_for_status()

    return public_url(bucket, caminho)
