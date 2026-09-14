#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Acha acesso a Storage feito pelo cliente Supabase CONTAMINÁVEL.

    python tools/acha_storage_contaminado.py src

Sai com código 1 se achar — serve como trava de regressão.

POR QUE EXISTE
--------------
`supabase` e `supabase_admin` são os dois clientes do backend. Os dois nascem
com a mesma SUPABASE_SERVICE_KEY, então parecem iguais — e é aí que engana.

O `supabase` é o usado em `sign_in_with_password`. Quando alguém loga, o
supabase-py troca o header Authorization do CLIENTE INTEIRO pelo token daquele
usuário. A partir daí, naquele worker, `supabase.storage` vai como
`authenticated`, não como `service_role`.

`service_role` tem BYPASSRLS. `authenticated` não. Resultado: gravar em bucket
sem policy de INSERT falha com

    StorageException 400: "new row violates row-level security"

mas SÓ nos workers onde já houve login. Worker novo funciona. Por isso o
sintoma é intermitente e parece "às vezes o upload não vai".

Em 14/09/2026 isso deixou a migração de fotos da Mister fast-food parada: a
capa da loja subiu (worker limpo) e 30 minutos depois a mesma operação falhou
num worker contaminado.

⚠️ Buckets SEM policy de INSERT (quebram): menu-images, logos,
   incident-photos, rewards-images.
⚠️ Buckets COM policy pra `authenticated` (nunca reclamaram, e foi isso que
   escondeu o problema por meses): avatars, delivery-avatars, banner-images.

REGRA: `supabase_admin.storage`, sempre.
"""
import os
import re
import sys

PADRAO = re.compile(r'(?<![\w_])supabase\.storage\b')


def eh_comentario(linha):
    """Linha que so fala sobre o assunto nao e uso.

    Sem isto a trava acusa o proprio aviso que escrevemos em helpers.py
    explicando por que a regra existe — e uma trava que grita no texto que a
    explica e uma trava que a gente aprende a ignorar.
    """
    return linha.lstrip().startswith('#')


def varrer(raiz):
    achados = []
    for pasta, _, arquivos in os.walk(raiz):
        if '__pycache__' in pasta:
            continue
        for nome in arquivos:
            if not nome.endswith('.py'):
                continue
            caminho = os.path.join(pasta, nome)
            with open(caminho, encoding='utf-8') as f:
                for n, linha in enumerate(f, 1):
                    if eh_comentario(linha):
                        continue
                    if PADRAO.search(linha):
                        achados.append((caminho, n, linha.strip()))
    return achados


def main():
    raiz = sys.argv[1] if len(sys.argv) > 1 else 'src'
    achados = varrer(raiz)
    if not achados:
        print('OK: nenhum acesso a Storage pelo cliente contaminavel.')
        return 0
    print('ACHEI %d acesso(s) a Storage pelo cliente que faz login:' % len(achados))
    print()
    for caminho, n, linha in achados:
        print('  %s:%d' % (caminho, n))
        print('      %s' % linha[:100])
    print()
    print('Troque por `supabase_admin.storage`. O porque esta no topo deste')
    print('arquivo e em src/utils/helpers.py.')
    return 1


if __name__ == '__main__':
    sys.exit(main())
