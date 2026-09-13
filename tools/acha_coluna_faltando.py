# -*- coding: utf-8 -*-
"""Acha SELECT enumerado que esqueceu coluna que o codigo depois LE.

A armadilha: coluna que falta num SELECT nao da erro. O `.get()` devolve None e
a regra some calada. Aconteceu 3x em 13/09/2026 (cupom, CEP da loja, teto de
tentativas), e a do cupom levou meia hora pra ser achada com o Diego testando.

COMO ACHA, e por que as versoes anteriores falharam:

  v1  contava toda chave lida na funcao, inclusive de request.get_json()
      -> 100 suspeitas, quase tudo ruido
  v2  amarrou a leitura a variavel que recebeu a consulta
      -> so 2, mas NAO achou o bug conhecido: as leituras acontecem DENTRO de
         evaluate_coupon() e precos_para_o_cupom(), que recebem a linha
  v3  passou a seguir a linha entre funcoes, mas:
      - buscava SQL linha a linha, e o SELECT/FROM moram em linhas diferentes
      - a janela ate o fetchone() era curta demais (o real tem 6 linhas)
      - colava strings com regex, o que DESLOCAVA os numeros de linha

  v4 (este) usa o AST pro que o AST ja faz de graca:
      - o parser JA concatena strings adjacentes -> SQL completo, sem regex
      - node.lineno da a linha VERDADEIRA do arquivo
      - Call nodes acham a passagem da linha pra outra funcao mesmo com
        parenteses aninhados e ternario no meio

Uso:  python caca4.py esquema.json <raiz>
"""
import ast
import io
import json
import os
import re
import sys

ESQUEMA = {k: set(v) for k, v in json.load(open(sys.argv[1], encoding="utf-8")).items()}
RAIZ = sys.argv[2]

SEL = re.compile(r"SELECT\s+(.*?)\s+FROM\s+([a-zA-Z_][\w.]*)", re.S | re.I)


def cols_do_select(trecho):
    """Nomes que o SELECT entrega (coluna final ou alias). None = tem '*'."""
    # `o.*` conta como estrela: e a mesma coisa, com prefixo de tabela.
    # Sem isto, SELECT o.* virava "lista enumerada vazia" e toda leitura
    # aparecia como coluna faltando.
    if re.search(r"(^|[\s,(])\*(\s|,|$)", trecho) or re.search(r"\w+\.\*", trecho):
        return None
    nomes, prof, atual = set(), 0, ""
    for ch in trecho:
        if ch in "([":
            prof += 1
        elif ch in ")]":
            prof -= 1
        if ch == "," and prof == 0:
            nomes.add(atual)
            atual = ""
        else:
            atual += ch
    nomes.add(atual)
    out = set()
    for n in nomes:
        n = n.strip().rstrip(",").strip()
        if not n:
            continue
        m = re.search(r"\bAS\s+([a-zA-Z_]\w*)\s*$", n, re.I)
        if m:
            out.add(m.group(1).lower())
            continue
        m = re.match(r"^[\"']?(\w+)[\"']?\.[\"']?(\w+)", n)   # o.status
        if m:
            out.add(m.group(2).lower())
            continue
        m = re.match(r"^[\"']?(\w+)[\"']?$", n)
        if m:
            out.add(m.group(1).lower())
    return out


def sql_de(no):
    """SQL do primeiro argumento de um execute(), se for string literal.

    Strings adjacentes ja vem coladas pelo parser. f-string cai fora: o SQL
    montado em runtime nao da pra conferir estaticamente.
    """
    if not no.args:
        return None
    a = no.args[0]
    if isinstance(a, ast.Constant) and isinstance(a.value, str):
        return a.value
    return None


def le_chaves(no, nome):
    """Chaves lidas de `nome` em qualquer lugar dentro de `no` (AST)."""
    achadas = set()
    for sub in ast.walk(no):
        # x['chave']
        if (isinstance(sub, ast.Subscript) and isinstance(sub.value, ast.Name)
                and sub.value.id == nome and isinstance(sub.slice, ast.Constant)
                and isinstance(sub.slice.value, str)):
            achadas.add(sub.slice.value.lower())
        # x.get('chave')
        if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "get" and isinstance(sub.func.value, ast.Name)
                and sub.func.value.id == nome and sub.args
                and isinstance(sub.args[0], ast.Constant)
                and isinstance(sub.args[0].value, str)):
            achadas.add(sub.args[0].value.lower())
    return achadas


def menciona(no, nome):
    return any(isinstance(s, ast.Name) and s.id == nome for s in ast.walk(no))


# ---------- passada 1: o que cada funcao le dos proprios parametros ----------
arquivos = []
for raiz, _, arqs in os.walk(RAIZ):
    if any(x in raiz for x in ("venv", "node_modules", "__pycache__")):
        continue
    arquivos += [os.path.join(raiz, n) for n in arqs if n.endswith(".py")]

LE_DO_PARAM = {}   # funcao -> {parametro -> {chaves}}
ARVORES = {}
for c in arquivos:
    try:
        arv = ast.parse(io.open(c, encoding="utf-8", errors="replace").read())
    except SyntaxError:
        continue
    ARVORES[c] = arv
    for no in ast.walk(arv):
        if isinstance(no, (ast.FunctionDef, ast.AsyncFunctionDef)):
            LE_DO_PARAM[no.name] = {p.arg: le_chaves(no, p.arg) for p in no.args.args}

# ---------- passada 2: SELECT -> variavel -> quem le ----------
achados = []
for c, arv in ARVORES.items():
    for fn in ast.walk(arv):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        corpo = list(ast.walk(fn))
        for no in corpo:
            if not (isinstance(no, ast.Call) and isinstance(no.func, ast.Attribute)
                    and no.func.attr == "execute"):
                continue
            sql = sql_de(no)
            if not sql:
                continue
            m = SEL.search(sql)
            if not m:
                continue
            tabela = m.group(2).split(".")[-1].lower()
            cols = ESQUEMA.get(tabela)
            if not cols:
                continue
            disp = cols_do_select(m.group(1))
            if disp is None:      # SELECT * -> imune por construcao
                continue

            # quem recebe: primeiro fetch* depois deste execute, na mesma funcao
            alvo = None
            for atr in corpo:
                if not isinstance(atr, ast.Assign):
                    continue
                if getattr(atr, "lineno", 0) < getattr(no, "lineno", 0):
                    continue
                if getattr(atr, "lineno", 0) > getattr(no, "lineno", 0) + 16:
                    continue
                txt = ast.dump(atr.value)
                if "'fetchone'" in txt or "'fetchall'" in txt:
                    if isinstance(atr.targets[0], ast.Name):
                        alvo = atr.targets[0].id
                        break
            if not alvo:
                continue

            precisa = le_chaves(fn, alvo)          # leituras diretas
            # + leituras dentro das funcoes que RECEBEM a linha
            for ch in corpo:
                if not isinstance(ch, ast.Call):
                    continue
                fname = (ch.func.id if isinstance(ch.func, ast.Name)
                         else getattr(ch.func, "attr", None))
                if fname not in LE_DO_PARAM:
                    continue
                pars = list(LE_DO_PARAM[fname].keys())
                for idx, arg in enumerate(ch.args):
                    if menciona(arg, alvo) and idx < len(pars):
                        precisa |= LE_DO_PARAM[fname][pars[idx]]
                for kw in ch.keywords:
                    if kw.arg and kw.value is not None and menciona(kw.value, alvo):
                        precisa |= LE_DO_PARAM[fname].get(kw.arg, set())

            faltando = sorted(k for k in precisa if k in cols and k not in disp)
            if faltando:
                achados.append((os.path.relpath(c, RAIZ), no.lineno, fn.name,
                                tabela, alvo, tuple(faltando)))

print("%-30s %-6s %-26s %-16s %-10s %s" %
      ("ARQUIVO", "LINHA", "FUNCAO", "TABELA", "VARIAVEL", "FALTANDO NO SELECT"))
print("-" * 122)
for a in sorted(set(achados)):
    print("%-30s %-6d %-26s %-16s %-10s %s" %
          (a[0][:30], a[1], a[2][:26], a[3][:16], a[4][:10], ", ".join(a[5])))
print()
print("suspeitas:", len(set(achados)))
