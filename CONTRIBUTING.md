# Guia de Contribuição

Obrigado por contribuir com o projeto Inksa Auth Flask!

## Configuração do Ambiente de Desenvolvimento

### 1. Pré-requisitos

- Python 3.11+
- Git
- pip

### 2. Setup Inicial

```bash
# 1. Clonar o repositório
git clone https://github.com/diego00711/inksa-auth-flask.git
cd inksa-auth-flask

# 2. Instalar dependências
pip install -r requirements.txt

# 3. Instalar e configurar pre-commit
pip install pre-commit black ruff
pre-commit install

# 4. Configurar variáveis de ambiente
cp .env.example .env.local
# Editar .env.local com suas configurações
```

### 3. Configuração de Variáveis de Ambiente

Copie `.env.example` para `.env.local` e configure:

- **SUPABASE_URL**: URL do seu projeto Supabase
- **SUPABASE_KEY**: Chave anônima do Supabase
- **SUPABASE_SERVICE_KEY**: Chave de serviço do Supabase
- **DATABASE_URL**: String de conexão PostgreSQL
- **JWT_SECRET**: Chave secreta para tokens JWT
- **EMAIL_USER**: Email para envio de notificações
- **EMAIL_PASS**: Senha de aplicativo do email
- **MERCADO_PAGO_ACCESS_TOKEN**: Token de acesso do Mercado Pago

## Fluxo de Trabalho

### 1. Criando uma Branch

```bash
git checkout -b feature/nova-funcionalidade
```

### 2. Desenvolvimento

- Escreva código seguindo os padrões do projeto
- Execute pre-commit hooks antes de commitar:
  ```bash
  pre-commit run --all-files
  ```

### 3. Commits

- Use mensagens de commit descritivas
- Os hooks do pre-commit executarão automaticamente

### 4. Pull Request

- Abra um PR para a branch `main`
- Descreva as mudanças realizadas
- Aguarde a revisão de código

## Padrões de Código

### Python

- **Formatação**: Black (executado automaticamente pelo pre-commit)
- **Linting**: Ruff (executado automaticamente pelo pre-commit)
- **Nomenclatura**: snake_case para funções e variáveis, PascalCase para classes

### Estrutura de Arquivos

```
src/
├── main.py              # Arquivo principal da aplicação
├── config.py            # Configurações centrais
├── routes/              # Blueprints das rotas
│   ├── auth.py
│   ├── orders.py
│   └── ...
└── utils/
    └── helpers.py       # Utilitários compartilhados
```

## Política de Segurança

### ⚠️ IMPORTANTE: Gerenciamento de Segredos

- **NUNCA** commite arquivos `.env` ou similares
- Use `.env.example` como template
- Mantenha credenciais apenas localmente
- Pre-commit hooks bloqueiam commits de arquivos `.env`

### Tratamento de Dados Sensíveis

- Sempre use variáveis de ambiente para configurações sensíveis
- Implemente logs de auditoria para ações administrativas
- Valide e sanitize todas as entradas de usuário

## Testes

```bash
# Executar testes (quando disponíveis)
python -m pytest

# Executar verificações de código
pre-commit run --all-files
```

## Reportando Problemas

- Use as issues do GitHub para reportar bugs
- Inclua passos para reprodução
- Anexe logs relevantes (sem dados sensíveis)

## Suporte

Para dúvidas sobre desenvolvimento, abra uma issue ou entre em contato com a equipe de desenvolvimento.