# src/utils/catalogo.py
#
# IMPORTAÇÃO DE CARDÁPIO — UM LUGAR SÓ.
#
# Esta lógica nasceu dentro da rota POST /api/menu/import, usada pela tela
# "Importar catálogo" do app do Parceiro. Quando a API pública de parceiro
# (PDV/ERP) passou a precisar da mesma coisa, a saída fácil era copiar o laço
# para lá.
#
# Não foi copiado de propósito. Este projeto já pagou caro por regra que mora
# em dois lugares: o pedido nasce em três funções diferentes, e tanto a regra
# de idade quanto a de frete nasceram em apenas uma delas — viraram buraco
# silencioso. Cardápio importado por CSV e cardápio importado por API TÊM que
# reconciliar igual, senão a mesma loja ganha catálogo diferente conforme o
# caminho.
#
# As duas regras sutis que estão aqui e não são óbvias:
#
#  1. Reconcilia por EAN quando existe, senão por NOME dentro da loja. Sem
#     isso, reimportar a mesma planilha duplicaria o catálogo inteiro — e
#     reimportar é o caso NORMAL (o parceiro mexeu no preço e mandou de novo).
#
#  2. Não sobrescreve descrição com vazio. O parceiro caprichou na descrição
#     pelo app; a planilha do ERP quase nunca tem esse campo. Deixar o vazio
#     ganhar apagaria o trabalho dele a cada sincronização.

import logging

logger = logging.getLogger(__name__)


def preco_para_float(valor):
    """Aceita 12,90 / 12.90 / R$ 12,90 / 1.234,56 — o que vier da planilha.

    Planilha de ERP brasileiro vem com vírgula decimal e às vezes com ponto de
    milhar. Um float() direto rejeita tudo isso e o item some da importação
    sem o parceiro entender por quê.
    """
    if valor is None:
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    texto = str(valor).strip().replace('R$', '').replace(' ', '')
    if not texto:
        return None
    if ',' in texto:
        # 1.234,56 -> 1234.56 ; 12,90 -> 12.90
        texto = texto.replace('.', '').replace(',', '.')
    try:
        return float(texto)
    except ValueError:
        return None


def importar_itens(cur, user_id, restaurant_id, itens, dry_run=False):
    """Cria ou atualiza itens do cardápio de UMA loja.

    Recebe o cursor de quem chamou — não abre nem fecha conexão, e não faz
    commit. Quem chamou decide a transação, porque a rota do app e a da API
    têm ciclos de vida diferentes.

    Devolve (criados, atualizados, ignorados) — `ignorados` é lista de
    dicionários com o motivo, para o parceiro corrigir a linha em vez de
    receber "deu erro".
    """
    criados = atualizados = 0
    ignorados = []

    for i, it in enumerate(itens):
        nome = str(it.get('name') or '').strip()
        preco = preco_para_float(it.get('price'))

        if not nome:
            ignorados.append({"linha": i + 1, "motivo": "sem nome"})
            continue
        if preco is None or preco < 0:
            ignorados.append({"linha": i + 1, "nome": nome[:40],
                              "motivo": f"preço inválido ({it.get('price')!r})"})
            continue

        ean = (str(it.get('ean') or '').strip() or None)
        categoria = (str(it.get('category') or '').strip() or 'Geral')
        descricao = (str(it.get('description') or '').strip() or '')

        try:
            estoque = int(it['stock']) if str(it.get('stock', '')).strip() != '' else None
        except (TypeError, ValueError):
            estoque = None

        # Estoque zerado NÃO some do cardápio: fica visível e indisponível,
        # senão o cliente nunca sabe que a loja trabalha com o produto.
        disponivel = True if estoque is None else estoque > 0

        if dry_run:
            continue

        existente = None
        if ean:
            cur.execute("SELECT id FROM menu_items WHERE restaurant_id = %s AND ean = %s",
                        (restaurant_id, ean))
            existente = cur.fetchone()
        if not existente:
            cur.execute("""SELECT id FROM menu_items
                            WHERE restaurant_id = %s AND LOWER(TRIM(name)) = LOWER(%s)
                            LIMIT 1""", (restaurant_id, nome))
            existente = cur.fetchone()

        if existente:
            cur.execute("""
                UPDATE menu_items
                   SET name = %s, price = %s, category = %s,
                       description = CASE WHEN %s <> '' THEN %s ELSE description END,
                       ean = COALESCE(%s, ean), stock = %s,
                       is_available = %s, updated_at = NOW()
                 WHERE id = %s
            """, (nome, preco, categoria, descricao, descricao, ean, estoque,
                  disponivel, existente['id']))
            atualizados += 1
        else:
            cur.execute("""
                INSERT INTO menu_items
                    (user_id, restaurant_id, name, description, price, category,
                     is_available, ean, stock)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (user_id, restaurant_id, nome, descricao, preco, categoria,
                  disponivel, ean, estoque))
            criados += 1

    return criados, atualizados, ignorados
