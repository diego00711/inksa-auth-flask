# tools/acha_coluna_faltando.py

Procura `SELECT` enumerado que **esqueceu** uma coluna que o código depois lê.

```bash
python tools/acha_coluna_faltando.py tools/esquema_do_banco.json src
```

## Por que existe

Coluna que falta num `SELECT` **não dá erro**. O `.get()` devolve `None`, a
regra some calada, e o sintoma aparece longe da causa — numa tela dizendo
"cupom inválido", num aviso que nunca toca, numa trava que nunca trava.

Aconteceu **três vezes em 13/09/2026**:

| Onde | Coluna esquecida | O que quebrou |
|---|---|---|
| `/api/coupons/validate` | `menu_item_id`, `reserva_minutos` | oferta relâmpago recusada com a reserva viva |
| perfil do restaurante | `address_zipcode` | conferência de CEP nunca rodaria |
| `/orders/<id>/complete` | `code_attempts` | teto de tentativas nunca travaria |

As duas últimas foram pegas antes de subir. A primeira só apareceu com o
Diego testando em produção.

## ⚠️ Como confiar no resultado

**Uma varredura que não acha nada pode só estar quebrada.** Antes de acreditar
num resultado limpo, rode contra a árvore de ANTES do conserto do cupom:

```bash
git archive 2a416e4 src | tar -x -C /tmp/antes && \
  python tools/acha_coluna_faltando.py tools/esquema_do_banco.json /tmp/antes/src
```

Tem que aparecer:

```
routes/coupons_routes.py  234  validate_coupon  coupons  menu_item_id, reserva_minutos
```

Se não aparecer, a ferramenta regrediu — conserte ela antes de confiar no
silêncio dela. As versões 1 a 3 desta varredura davam "zero" e estavam erradas.

## O que ela NÃO vê

- **SQL montado em runtime** (f-string, concatenação com variável)
- **`.select('a,b')` do cliente Supabase** — outro caminho, não coberto
- **Ramos diferentes reusando o mesmo nome de variável** — ela soma as
  leituras dos três e acusa demais (falso positivo, não falso negativo)
- **Mais de um nível** de passagem entre funções

Falso positivo ela dá; falso negativo é o que importa evitar, e é pra isso
que serve o teste de regressão acima.

## Esquema

`esquema_do_banco.json` é `{tabela: [colunas]}` e **envelhece**. Pra atualizar:

```sql
SELECT table_name, string_agg(column_name, ',' ORDER BY ordinal_position) AS cols
  FROM information_schema.columns
 WHERE table_schema = 'public'
 GROUP BY table_name ORDER BY table_name;
```
