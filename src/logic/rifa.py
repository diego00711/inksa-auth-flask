# src/logic/rifa.py
"""
Numeros da campanha (sorteio). Quem gera, quando, e como nao gerar duas vezes.

## ⚠️ NASCE DESLIGADA, E ISSO NAO E DETALHE

`rifa_campanhas.ligada` comeca `false`. Sorteio de premio no Brasil exige
autorizacao previa (Lei 5.768/1971, hoje pela Secretaria de Premios e Apostas
do Ministerio da Fazenda). Enquanto a chave estiver desligada, TODA funcao
daqui devolve zero sem tocar no banco.

O software fica pronto e a campanha nao comeca por acidente. Ligar e um UPDATE
de uma linha, depois que a autorizacao sair.

## A IDEIA CENTRAL: RECONCILIAR, NAO INCREMENTAR

A tentacao e "pedido entregue -> +1 numero". Nao faco assim, por um motivo
concreto desta base: **o gatilho da entrega mora em mais de um lugar**
(orders.py e gamification_routes.py escrevem `status='delivered'`, e a entrega
propria fecha por outro caminho). Incremento disparado de tres lugares vira
premio triplo ou nenhum, e a conta so aparece errada no dia do sorteio.

Entao cada funcao aqui pergunta **"quantos numeros esta pessoa DEVERIA ter?"**
e acerta a diferenca:

    devidos = floor(total_valido / reais_por_numero)
    conceder = devidos - ja_concedidos

Isso e idempotente de graca: chamar duas vezes nao gera nada na segunda. E
conserta sozinho — se um pedido for cancelado, `devidos` cai e a mesma funcao
CANCELA o excedente. Nao preciso de um caminho separado pra estorno.

## O QUE CONTA

- Cliente  : soma de `total_amount_items` dos pedidos ENTREGUES dele.
- Parceiro : soma de `total_amount_items` dos pedidos ENTREGUES da loja.
- Entregador: quantidade de entregas concluidas — e **entrega propria nao
  conta**, porque nelas nao houve entregador Inksa (decisao do Diego).

Frete fora de proposito: `total_amount_items` e o valor dos itens, que e a
mesma base da comissao. Contar frete faria o numero depender da distancia.

## ⚠️ A ARBITRAGEM, QUE NAO E TEORICA

Quem e cliente E dono de loja ganha numero dos DOIS lados girando pedido na
propria loja. O custo e so a comissao (15%), entao cada R$50 girados custam
~R$7,50 e rendem 2 numeros — cerca de R$4 por numero. Com bolo pequeno, isso
compra o premio.

Duas travas aqui:
  1. `_mesmo_dono()` — pedido em que o cliente e o dono da loja nao conta pra
     nenhum dos dois lados.
  2. `teto_numeros_mes` — teto por pessoa por mes. Nasce em 0 (sem teto,
     que e a regra do Diego); existe pra ser ligado sem deploy se aparecer
     abuso.

Nenhuma funcao daqui levanta excecao: numero de sorteio jamais pode derrubar um
pedido que ja aconteceu no mundo real.
"""
import logging
import math

logger = logging.getLogger(__name__)

# Origens que vieram de movimento (pedido/venda/entrega), por oposicao ao
# numero de cadastro. Usadas na reconciliacao.
_ORIGEM_POR_TIPO = {
    'cliente': 'pedido',
    'parceiro': 'venda',
    'entregador': 'entrega',
}


def _campanha(cur):
    """A campanha valendo AGORA, ou None.

    None significa 'nao faca nada' — e o estado normal enquanto a autorizacao
    nao sai. Quem chama nunca precisa testar a chave.
    """
    try:
        cur.execute("""
            SELECT campanha, reais_por_numero, numero_no_cadastro, teto_numeros_mes
              FROM rifa_campanhas
             WHERE ligada = TRUE
               AND (inicio IS NULL OR inicio <= CURRENT_DATE)
               AND (fim    IS NULL OR fim    >= CURRENT_DATE)
             ORDER BY criada_em DESC
             LIMIT 1
        """)
        return cur.fetchone()
    except Exception:
        logger.warning("[RIFA] nao consegui ler a campanha", exc_info=True)
        return None


def _alocar(cur, campanha, quantos):
    """Reserva um bloco de `quantos` numeros e devolve o primeiro.

    ⚠️ O `UPDATE ... RETURNING` TRANCA A LINHA da campanha, entao dois pedidos
    simultaneos nunca recebem o mesmo numero. Fazer `max(numero)+1` teria
    corrida justamente no pico, que e quando a campanha importa.
    """
    if quantos <= 0:
        return None
    cur.execute("""
        UPDATE rifa_campanhas
           SET proximo_numero = proximo_numero + %s
         WHERE campanha = %s
        RETURNING proximo_numero - %s AS inicio
    """, (quantos, campanha, quantos))
    row = cur.fetchone()
    return int(row['inicio']) if row else None


def _mesmo_dono(cur, order_id):
    """True quando quem pediu e dono da loja do pedido.

    E a trava da arbitragem descrita no topo. Compara o `user_id` do perfil de
    cliente com o da loja — e tambem o telefone, porque nada impede duas contas
    de auth para a mesma pessoa.
    """
    try:
        cur.execute("""
            SELECT (cp.user_id = rp.user_id) AS mesmo_user,
                   (NULLIF(TRIM(cp.phone), '') IS NOT NULL
                    AND TRIM(cp.phone) = TRIM(rp.phone)) AS mesmo_telefone
              FROM orders o
              JOIN client_profiles     cp ON cp.id = o.client_id
              JOIN restaurant_profiles rp ON rp.id = o.restaurant_id
             WHERE o.id = %s
        """, (str(order_id),))
        r = cur.fetchone()
        return bool(r and (r['mesmo_user'] or r['mesmo_telefone']))
    except Exception:
        # Na duvida NAO bloqueia: recusar numero de quem tem direito e pior
        # que conceder um a mais, e o teto mensal continua valendo.
        logger.warning("[RIFA] checagem de mesmo dono falhou (%s)", order_id, exc_info=True)
        return False


def _ja_concedidos(cur, campanha, tipo, perfil_id):
    cur.execute("""
        SELECT COUNT(*) AS n FROM rifa_numeros
         WHERE campanha = %s AND tipo = %s AND perfil_id = %s
           AND origem = %s AND cancelado_em IS NULL
    """, (campanha, tipo, str(perfil_id), _ORIGEM_POR_TIPO[tipo]))
    return int((cur.fetchone() or {'n': 0})['n'])


def _base_valida(cur, tipo, perfil_id):
    """Quanto esta pessoa acumulou de valido — reais, ou entregas.

    'Valido' = entregue e nao arquivado, e sem os pedidos em que o cliente e o
    dono da loja (a arbitragem). Para o entregador, entrega propria fica de
    fora: nela nao houve entregador Inksa.
    """
    sem_arbitragem = """
        AND NOT EXISTS (
            SELECT 1 FROM client_profiles cp2, restaurant_profiles rp2
             WHERE cp2.id = o.client_id AND rp2.id = o.restaurant_id
               AND (cp2.user_id = rp2.user_id
                    OR (NULLIF(TRIM(cp2.phone),'') IS NOT NULL
                        AND TRIM(cp2.phone) = TRIM(rp2.phone))))
    """
    if tipo == 'cliente':
        cur.execute(f"""
            SELECT COALESCE(SUM(o.total_amount_items), 0) AS base
              FROM orders o
             WHERE o.client_id = %s AND o.status = 'delivered'
               AND o.archived_at IS NULL {sem_arbitragem}
        """, (str(perfil_id),))
    elif tipo == 'parceiro':
        cur.execute(f"""
            SELECT COALESCE(SUM(o.total_amount_items), 0) AS base
              FROM orders o
             WHERE o.restaurant_id = %s AND o.status = 'delivered'
               AND o.archived_at IS NULL {sem_arbitragem}
        """, (str(perfil_id),))
    else:  # entregador — conta ENTREGAS, nao reais
        cur.execute("""
            SELECT COUNT(*) AS base
              FROM orders o
              JOIN restaurant_profiles rp ON rp.id = o.restaurant_id
             WHERE o.delivery_id = %s AND o.status = 'delivered'
               AND o.archived_at IS NULL
               AND COALESCE(rp.delivery_type, 'platform') <> 'own'
        """, (str(perfil_id),))
    return float((cur.fetchone() or {'base': 0})['base'] or 0)


def _teto_batido(cur, camp, tipo, perfil_id):
    """True se a pessoa ja atingiu o teto de numeros DESTE mes."""
    teto = int(camp['teto_numeros_mes'] or 0)
    if teto <= 0:
        return False
    cur.execute("""
        SELECT COUNT(*) AS n FROM rifa_numeros
         WHERE campanha = %s AND tipo = %s AND perfil_id = %s
           AND cancelado_em IS NULL
           AND (created_at AT TIME ZONE 'America/Sao_Paulo') >=
               date_trunc('month', NOW() AT TIME ZONE 'America/Sao_Paulo')
    """, (camp['campanha'], tipo, str(perfil_id)))
    return int((cur.fetchone() or {'n': 0})['n']) >= teto


def sincronizar(cur, tipo, perfil_id):
    """Acerta os numeros desta pessoa com a realidade. Devolve o saldo mexido.

    Positivo = concedeu; negativo = cancelou (pedido estornado/cancelado);
    0 = ja estava certo. Chamar de novo nao faz nada — e o ponto.
    """
    camp = _campanha(cur)
    if not camp or tipo not in _ORIGEM_POR_TIPO:
        return 0
    try:
        base = _base_valida(cur, tipo, perfil_id)
        por = float(camp['reais_por_numero'] or 50)
        devidos = int(base) if tipo == 'entregador' else int(math.floor(base / por)) if por > 0 else 0
        ja = _ja_concedidos(cur, camp['campanha'], tipo, perfil_id)

        if devidos > ja:
            if _teto_batido(cur, camp, tipo, perfil_id):
                logger.info("[RIFA] teto do mes atingido: %s %s", tipo, perfil_id)
                return 0
            faltam = devidos - ja
            inicio = _alocar(cur, camp['campanha'], faltam)
            if inicio is None:
                return 0
            origem = _ORIGEM_POR_TIPO[tipo]
            cur.executemany("""
                INSERT INTO rifa_numeros
                    (campanha, numero, tipo, perfil_id, origem, valor_base, ordinal)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
            """, [(camp['campanha'], inicio + i, tipo, str(perfil_id), origem,
                   None if tipo == 'entregador' else por, ja + i + 1)
                  for i in range(faltam)])
            return faltam

        if devidos < ja:
            # Pedido cancelado/estornado derrubou a base. Cancela o EXCEDENTE,
            # do numero mais alto pro mais baixo — nunca apaga a linha, porque
            # sorteio precisa de trilha.
            sobra = ja - devidos
            cur.execute("""
                UPDATE rifa_numeros
                   SET cancelado_em = NOW(), motivo_cancel = 'pedido cancelado ou estornado'
                 WHERE id IN (
                    SELECT id FROM rifa_numeros
                     WHERE campanha = %s AND tipo = %s AND perfil_id = %s
                       AND origem = %s AND cancelado_em IS NULL
                     ORDER BY numero DESC LIMIT %s)
            """, (camp['campanha'], tipo, str(perfil_id), _ORIGEM_POR_TIPO[tipo], sobra))
            return -sobra

        return 0
    except Exception:
        logger.warning("[RIFA] sincronizar falhou: %s %s", tipo, perfil_id, exc_info=True)
        return 0


def conceder_cadastro(cur, tipo, perfil_id):
    """O numero de boas-vindas. Um por pessoa, por campanha.

    O indice unico parcial no banco garante o 'um so' mesmo se esta funcao for
    chamada duas vezes (cadastro tem retentativa).
    """
    camp = _campanha(cur)
    if not camp or not camp['numero_no_cadastro'] or tipo not in _ORIGEM_POR_TIPO:
        return 0
    try:
        cur.execute("""
            SELECT 1 FROM rifa_numeros
             WHERE campanha = %s AND tipo = %s AND perfil_id = %s AND origem = 'cadastro'
        """, (camp['campanha'], tipo, str(perfil_id)))
        if cur.fetchone():
            return 0
        n = _alocar(cur, camp['campanha'], 1)
        if n is None:
            return 0
        cur.execute("""
            INSERT INTO rifa_numeros (campanha, numero, tipo, perfil_id, origem)
            VALUES (%s, %s, %s, %s, 'cadastro')
            ON CONFLICT DO NOTHING
        """, (camp['campanha'], n, tipo, str(perfil_id)))
        return 1
    except Exception:
        logger.warning("[RIFA] cadastro falhou: %s %s", tipo, perfil_id, exc_info=True)
        return 0


def sincronizar_do_pedido(cur, order_id):
    """UM ponto de entrada para o pedido inteiro — as tres pontas de uma vez.

    Existe para que os varios caminhos que fecham um pedido (entrega pela
    plataforma, entrega propria, cancelamento, estorno) chamem a MESMA linha.
    Regra que entra num caminho so vira buraco nesta base; ja aconteceu.
    """
    camp = _campanha(cur)
    if not camp:
        return {}
    try:
        cur.execute("""
            SELECT client_id, restaurant_id, delivery_id FROM orders WHERE id = %s
        """, (str(order_id),))
        o = cur.fetchone()
        if not o:
            return {}
        r = {}
        if o['client_id']:
            r['cliente'] = sincronizar(cur, 'cliente', o['client_id'])
        if o['restaurant_id']:
            r['parceiro'] = sincronizar(cur, 'parceiro', o['restaurant_id'])
        if o['delivery_id']:
            r['entregador'] = sincronizar(cur, 'entregador', o['delivery_id'])
        return r
    except Exception:
        logger.warning("[RIFA] sincronizar_do_pedido falhou (%s)", order_id, exc_info=True)
        return {}


def meus_numeros(cur, tipo, perfil_id):
    """O que a tela do app mostra: os numeros validos e de onde vieram."""
    camp = _campanha(cur)
    if not camp or tipo not in _ORIGEM_POR_TIPO:
        return {"ligada": False, "numeros": [], "total": 0}
    try:
        cur.execute("""
            SELECT numero, origem, valor_base, created_at
              FROM rifa_numeros
             WHERE campanha = %s AND tipo = %s AND perfil_id = %s
               AND cancelado_em IS NULL
             ORDER BY numero
        """, (camp['campanha'], tipo, str(perfil_id)))
        linhas = [dict(x) for x in cur.fetchall()]
        for l in linhas:
            l['created_at'] = l['created_at'].isoformat() if l['created_at'] else None
            l['valor_base'] = float(l['valor_base']) if l['valor_base'] is not None else None
        return {"ligada": True, "campanha": camp['campanha'],
                "numeros": linhas, "total": len(linhas)}
    except Exception:
        logger.warning("[RIFA] meus_numeros falhou: %s %s", tipo, perfil_id, exc_info=True)
        return {"ligada": False, "numeros": [], "total": 0}
