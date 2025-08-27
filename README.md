# Inksa Auth Flask

Sistema de autenticação e backend para o ecossistema Inksa Delivery.

## Tecnologias

- Flask 3.0.0
- Supabase
- PostgreSQL
- Mercado Pago API
- Flask-SocketIO

## Desenvolvimento Local

### Pré-requisitos

- Python 3.11+
- pip

### Configuração

1. **Instalar pre-commit (recomendado)**:
   ```bash
   pip install pre-commit
   pre-commit install
   ```

2. **Configurar variáveis de ambiente**:
   ```bash
   # Copiar arquivo de exemplo
   cp .env.example .env.local
   
   # Editar .env.local com suas configurações
   # IMPORTANTE: NÃO commitar arquivos .env
   ```

3. **Instalar dependências**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Executar aplicação**:
   ```bash
   python -m src.main
   ```

### Endpoints Principais

- `/` - Status da aplicação
- `/api/health` - Health check
- `/api/auth` - Autenticação
- `/api/orders` - Gestão de pedidos
- `/api/menu` - Cardápio
- `/api/restaurant` - Dados do restaurante
- `/api/payment` - Pagamentos
- `/api/admin` - Administração
- `/api/delivery` - Entregadores
- `/api/logs` - Logs administrativos

## Contribuição

Consulte [CONTRIBUTING.md](CONTRIBUTING.md) para instruções detalhadas de desenvolvimento.

## Segurança

Consulte [SECURITY.md](SECURITY.md) para práticas de segurança e como reportar vulnerabilidades.