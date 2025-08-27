# Política de Segurança

## Versões Suportadas

| Versão | Suporte de Segurança |
| ------ | -------------------- |
| 1.0.x  | ✅ Sim               |

## Melhores Práticas de Segurança

### 🔐 Gerenciamento de Segredos

#### ✅ O que FAZER:
- Usar `.env.example` como template para configurações
- Manter arquivos `.env` apenas localmente
- Usar variáveis de ambiente em produção
- Gerar chaves JWT fortes e únicas
- Usar senhas de aplicativo para email

#### ❌ O que NÃO fazer:
- **NUNCA** commitar arquivos `.env` ou similares
- **NUNCA** expor chaves de API em código
- **NUNCA** compartilhar credenciais via chat/email
- **NUNCA** usar credenciais de produção em desenvolvimento

### 🛡️ Configurações de Segurança

#### Variáveis Sensíveis
```bash
# Exemplos de configurações que DEVEM ser protegidas:
JWT_SECRET=              # Chave para assinatura de tokens
SUPABASE_SERVICE_KEY=    # Chave de serviço do Supabase
DATABASE_URL=            # String de conexão do banco
MERCADO_PAGO_ACCESS_TOKEN= # Token do Mercado Pago
EMAIL_PASS=              # Senha de aplicativo do email
```

#### Proteções Implementadas
- Pre-commit hooks bloqueiam commits de arquivos `.env`
- CI/CD falha se arquivos `.env` estão rastreados
- Logs de auditoria para ações administrativas
- Validação de entrada em todos os endpoints

### 🔍 Auditoria e Monitoramento

O sistema implementa logs de auditoria para ações administrativas:
- Endpoint: `/api/logs`
- Tabela: `admin_logs`
- Campos rastreados: ator, ação, recursos, metadados, timestamp

### 🚨 Incidentes de Segurança

#### Se você descobrir uma vulnerabilidade:

1. **NÃO** crie uma issue pública
2. **NÃO** compartilhe detalhes publicamente
3. Entre em contato imediatamente via email: `security@inksadelivery.com.br`

#### Informações para incluir:
- Descrição detalhada da vulnerabilidade
- Passos para reprodução
- Impacto potencial
- Versão afetada
- Sua informação de contato

#### Nosso compromisso:
- Resposta inicial em até 48 horas
- Avaliação de impacto em até 5 dias úteis
- Correção para vulnerabilidades críticas em até 7 dias
- Reconhecimento público (opcional) após correção

### 📋 Checklist de Desenvolvimento Seguro

Antes de fazer commit/deploy:

- [ ] Nenhum arquivo `.env` incluído
- [ ] Credenciais usando variáveis de ambiente
- [ ] Validação de entrada implementada
- [ ] Logs de auditoria para ações sensíveis
- [ ] Autenticação e autorização adequadas
- [ ] Pre-commit hooks passando
- [ ] Teste em ambiente isolado

### 🔄 Atualizações de Segurança

- Dependências são auditadas regularmente
- Patches de segurança aplicados prioritariamente
- Políticas de segurança revisadas trimestralmente

### 📞 Contato

Para questões de segurança:
- Email: `security@inksadelivery.com.br`
- Para problemas não-críticos: GitHub Issues

---

*Última atualização: Agosto 2025*