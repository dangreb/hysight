# hysight — promoção de modelos mentais para Hindsight

Primeira versão, **0.1.0**. Uma extensão para publicar modelos mentais explicitamente selecionados em bancos agregadores. A configuração dos bancos usados pelo Hermes continua no plugin do Hermes.

Validação: [docs/VALIDACAO.md](docs/VALIDACAO.md), com testes, limites e resultado do probe no hylab. Há uma imagem candidata construída; ela não foi ativada no daemon principal.

## Escopo desta versão

- Origem única: **modelos mentais selecionados por banco e ID**.
- Disparo: `on_mental_model_refresh_complete`, com reconciliação periódica para recuperar eventos perdidos e detectar remoções.
- Destino: um documento derivado estável, gravado por `retain` no banco indicado pela regra.
- Sem novas chamadas de `reflect` ou LLM de curadoria na extensão. **O retain do destino pode consumir inferência**, de acordo com a configuração daquele banco.
- Sem hooks de consolidação, interface administrativa, MCP, alterações no plugin Hermes ou integração de outras harness.

```text
refresh de modelo selecionado → hook → fila persistente → consulta direta do modelo
  → comparação de revisão → retain no agregador → extração/consolidação nativas
```

O modelo escolhido deve ser escrito para compartilhamento. Sua seleção numa regra autoriza publicar **todo o conteúdo desse modelo**. Não há uma segunda LLM decidindo quais trechos podem sair; não aponte uma regra para um modelo que misture informações privadas e compartilháveis.

## Controles implementados

- Documento de destino `hysight/v1/<rule-id>`, atualizado por replace.
- Hash de conteúdo, referências de evidência e configuração de tags. Mudar apenas `last_refreshed_at` não produz outra retenção.
- Verificação de ownership antes de sobrescrever ou excluir um documento no destino.
- Reconhecimento de uma publicação já confirmada no destino mesmo após crash antes da confirmação local, evitando repetir retain nessa recuperação.
- Fila SQLite persistente, transações e lock de processo do sistema operacional: **um escritor**.
- Coalescência de eventos, processamento sequencial, prazo e retries limitados com backoff. Um evento recebido durante uma publicação deixa a próxima revisão pendente.
- Reconciliação: recupera o intervalo entre commit da origem e recebimento do hook; repara documento de destino removido externamente.
- Modelos ausentes e regras removidas/desativadas provocam retração do documento derivado. Modelos vazios, ainda não gerados ou sem evidência ficam em espera; não apagam uma publicação anterior apenas por falha de refresh.
- Ciclos recusados. Por padrão, apenas **um salto**; mais níveis precisam de `MAX_HOPS` explícito, até oito, e continuam sujeitos a validação de ciclos.
- Banco destino precisa existir; não há criação automática silenciosa de bancos.
- Proveniência preserva banco/modelo de origem e IDs das evidências, sem copiar texto bruto das fontes, trace ou `source_query` potencialmente privado.
- `request_context` não é serializado. O log local guarda somente campos selecionados e indicadores de presença de identidade; chaves, headers e tokens não são guardados.

Não há promessa de exatamente uma chamada de inferência. Se a gravação falhar antes de um estado verificável no destino, a tentativa pode precisar ser repetida. O documento é idempotente e a retomada verifica a revisão já persistida antes de repetir a retenção.

## Regras

Veja [examples/promotion-rules.json](examples/promotion-rules.json). É um arquivo próprio da extensão, não um template nativo do Hindsight.

```json
{
  "schema_version": 1,
  "rules": [
    {
      "id": "executor-lessons-to-team",
      "source_bank": "executor",
      "source_model_id": "shared-lessons",
      "target_bank": "collective",
      "enabled": true,
      "target_tags": ["team:hylab", "flow:promoted"],
      "require_evidence": true
    }
  ]
}
```

`require_evidence=true` exige IDs de referência em `reflect_response.based_on`. Isso identifica proveniência, **não valida a veracidade das afirmações ou das citações**. Modelos sem referências só podem ser publicados se o operador escolher explicitamente `false`.

Os IDs de banco devem ser canônicos. A V1 não interpreta aliases como relações de promoção. A identidade de uma regra (origem, modelo e destino) não pode ser trocada usando o mesmo ID: desative/remova a regra antiga e crie outra. Tags e enabled podem mudar mantendo a identidade.

Arquivo de regras é lido na inicialização. **Mudanças exigem reiniciar a extensão/serviço**. Não há hot reload. Regras removidas ficam registradas no estado para permitir a retração do que publicaram; não exclua a base de estado para tentar desativar uma regra.

## Instalação em desenvolvimento

Python 3.11 ou superior. O núcleo usa apenas a biblioteca padrão; o adaptador nativo deve rodar dentro do ambiente do Hindsight.

```powershell
uv sync
uv run hysight validate examples/promotion-rules.json
uv run python -m unittest discover -s tests -v
uv build
```

Sem uv, o núcleo pode ser testado adicionando `src` ao `PYTHONPATH`. Não instalar todo o servidor Hindsight na estação só para rodar os testes do núcleo.

## Carregamento no Hindsight

```text
HINDSIGHT_API_OPERATION_VALIDATOR_EXTENSION=hysight.extension:PromotionExtension
HINDSIGHT_API_OPERATION_VALIDATOR_RULES_FILE=/etc/hysight/promotion-rules.json
HINDSIGHT_API_OPERATION_VALIDATOR_STATE_PATH=/var/lib/hysight/state.sqlite3
HINDSIGHT_API_OPERATION_VALIDATOR_SCHEMA=public
HINDSIGHT_API_HTTP_EXTENSION=hysight.extension:PromotionLifecycle
```

São duas classes do mesmo pacote. Na versão inspecionada do Hindsight, os hooks de ciclo de vida HTTP são efetivamente executados, mas os do slot de operações não iniciam o consumidor. `PromotionLifecycle` usa o slot HTTP **somente para startup/shutdown**, sem rotas ou tela. Esse arranjo também garante reconciliação após restart sem esperar uma nova mensagem do Hermes.

Se algum desses slots já estiver ocupado, não sobrescreva a extensão existente: componha as responsabilidades antes. A V1 exige a API e o consumidor no mesmo processo; não é uma implantação para workers distribuídos.

Configurações opcionais, com o mesmo prefixo `HINDSIGHT_API_OPERATION_VALIDATOR_`:

| Sufixo | Padrão | Função |
|---|---:|---|
| `POLL_SECONDS` | 1 | Intervalo ocioso do consumidor |
| `RECONCILE_SECONDS` | 60 | Verificação dos modelos/regras registrados; não chama LLM |
| `OPERATION_TIMEOUT_SECONDS` | 180 | Prazo do processamento de uma publicação |
| `RETRY_BASE_SECONDS` | 5 | Backoff exponencial, limitado a 300 s |
| `MAX_ATTEMPTS` | 5 | Limite de tentativas para a mesma revisão de conteúdo |
| `MAX_HOPS` | 1 | Profundidade máxima do grafo de promoção |
| `MAX_CONTENT_CHARS` | 100000 | Limite de tamanho do modelo publicado |

Após esgotar tentativas, a reconciliação pode consultar novamente a origem, mas não repete inferência para o mesmo hash. Uma nova revisão de conteúdo permite nova sequência de tentativas. Falhas anteriores a uma revisão identificável, como indisponibilidade do banco, podem continuar sendo sondadas; essas sondagens não fazem retenção.

## Empacotamento Docker

Use a imagem **fixada da instalação atual**, não `latest`, como base. O projeto fornece [deploy/Dockerfile](deploy/Dockerfile) e um [override de exemplo](deploy/compose.override.example.yaml).

```text
uv build
docker build -f deploy/Dockerfile --build-arg HINDSIGHT_IMAGE=<imagem-fixada> -t hysight:hindsight-1.0.0-v0.1.0 .
```

O wheel é instalado offline, sem novas dependências. A montagem do estado deve ser persistente e em filesystem local, com diretório dedicado. Hysight usa WAL e FULL synchronous; SQLite, WAL e lock precisam ficar juntos. Faça backup do diretório inteiro de maneira consistente, preferencialmente com o consumidor parado ou com a API de backup SQLite.

No Linux, prepare o diretório dedicado com modo **0700**, pertencente ao usuário do serviço. Hysight recusa um diretório existente com permissões mais abertas, em vez de alterar permissões de um diretório compartilhado. A base tem identificação própria; um SQLite de outra aplicação é recusado sem inicializá-lo.

**Limites operacionais:** uma instância escritora; schema padrão do servidor; nenhum suporte multi-tenant distribuído nesta V1. A API interna do engine é usada com contexto interno, mas somente para destinos definidos pelo operador. Essa autoridade não deve ser exposta a arquivos de regras editáveis por agentes ou a conteúdo recuperado.

## Estado e diagnóstico

```text
hysight status /var/lib/hysight/state.sqlite3
```

O comando abre a base somente para leitura. Estados incluem pending, running, retry, failed, waiting e done. `last_action` explica espera por conteúdo/evidência ou limite de tentativas. Os últimos eventos registram o hook e as publicações sem conteúdo de memória ou credenciais. Erros arbitrários do backend são reduzidos ao nome da exceção; consulte os logs próprios do Hindsight para detalhes do provedor.

O namespace `hysight/v1/` deve ser mantido pela extensão. Ownership e hashes previnem colisões e duplicação operacional; não são assinatura criptográfica nem substituem autorização na API.

Retrair um documento usa a exclusão nativa do Hindsight e suas regras de invalidação. Não significa apagar backups, auditoria ou toda síntese histórica que já o citou. Modelos do agregador podem precisar de nova geração antes de refletir a correção.

## O que o teste do hook verifica

[tools/probe_native.py](tools/probe_native.py) executa o **handler real** de refresh da imagem instalada e entrega seu resultado à extensão real. A geração do modelo de origem é substituída por um modelo sintético já armazenado, evitando nova inferência. O destino usa a API e o armazenamento reais, num banco temporário com extração `chunks` e consolidação desativada.

O teste distingue:

- **RequestContext:** identidade/contexto de execução, campos internos e identificação de operação, conforme o handler os preencher.
- **Proveniência:** modelo consultado e referências de `reflect_response.based_on`. Não é o mesmo objeto que RequestContext.

Consulta direta do modelo não chama recall ou reflect. O relatório do probe mede suas leituras e compara os contadores de LLM antes/depois; isso é uma amostra funcional, não benchmark de produção.

Não se afirma que esse probe executou uma geração real por LLM com a extensão instalada no daemon. Ele testa a entrega pelo handler instalado, a integração do adaptador e a publicação real. Uma ativação permanente no serviço é uma etapa separada.
