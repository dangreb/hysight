# Validação da V1 — 22/10/2026

## Resultado

Hysight 0.1.0 implementado e empacotado. **26 testes passaram no Linux**, executados contra o wheel instalado na imagem candidata, com rede desativada. No Windows, 25 passaram e um teste específico de permissões POSIX foi corretamente ignorado.

O projeto está em `C:/devs/Python/hysight`. Não foi criado repositório remoto nem ativada a extensão no daemon principal.

## Imagem candidata no hylab

- Tag: `hysight:0.1.0-candidate`
- ID: `sha256:ae856940b03b0a96bb9d2adde93055ed2a67105c840602b21c8ecf944cfc4921`
- Base: `agentics/hindsight-api:1.0.0-cu126`
- ID da base: `sha256:1beb58e87ea921fb09533b4a36eeb7bf0a70b6e66cfd8e2a7e49ea0d255af730`
- Fonte de preparação: `/home/dagr/helpers/hysight-v1-20261022t093953z`

Essa imagem foi construída sem rede, instalando o wheel sem dependências adicionais. O import das duas classes nativas passou no build. Os exemplos de Compose permanecem arquivos de exemplo; não foram aplicados.

## Teste do hook e RequestContext

O probe carregou as classes por meio do loader nativo do Hindsight e executou o **handler real de refresh da imagem instalada**. Apenas a geração/persistência do modelo de origem foi substituída por uma leitura de um modelo sintético já existente.

O handler construiu seu `MentalModelRefreshResult` e chamou a extensão. No contexto observado:

- `internal=true`.
- Os IDs sintéticos de tenant/chave fornecidos na tarefa foram preservados, observados apenas por indicadores booleanos no relatório.
- `authenticated_schema=null`; o schema corrente do engine foi `public`.
- `operation_id` não estava disponível nesse RequestContext da versão instalada. O código usa acesso compatível por `getattr`, sem pressupor esse campo.
- Havia outros campos, inclusive identidade/autorização, headers e cancelamento. Seus valores sensíveis não foram serializados.

Isso confirma utilidade de RequestContext para contexto de execução. **Ele não continha o conteúdo do modelo nem a lista de evidências da síntese**. Essas informações foram obtidas pelo lookup do modelo, de `content` e `reflect_response.based_on`.

## Consulta direta do modelo

Seis leituras por HTTP na rede local do contêiner tiveram duração entre **3.707 e 5.828 ms**, mediana **4.982 ms**. É uma amostra pequena, não benchmark. Não ocorreu recall nem reflect nessas leituras.

## Persistência real no destino

O destino foi um banco temporário exclusivo, usando API/armazenamento reais, com extração `chunks` e observações desativadas para não chamar LLM. Foram verificados:

1. Primeira publicação em `hysight/v1/probe`, com 2 unidades de memória.
2. Refresh repetido sem novo retain.
3. Conteúdo corrigido substituindo o mesmo documento, com mudança do hash.
4. Ausência simulada da origem, seguida de reconciliação e retração do documento.
5. Ausência de credencial do servidor no estado/diagnóstico da extensão.
6. Contadores de LLM: **zero novas chamadas na origem e zero no destino**.
7. Conteúdo do modelo de origem real inalterado.
8. Banco temporário excluído ao final.

O teste não mede qualidade semântica da extração de uma publicação em banco configurado com LLM. Em uso normal, o retain e a consolidação do agregador podem consumir inferência. Também não foi realizado um refresh real por LLM com o daemon principal já configurado para a extensão; esse teste deve acompanhar a ativação futura, com fontes/destinos definidos pelo operador.

## Controles cobertos pelos testes

- Ciclos, profundidade de encadeamento e rotas duplicadas.
- Campos desconhecidos, valores inválidos e namespace de tags reservado.
- Revisão por conteúdo/evidência, sem disparar retenção por mero novo timestamp.
- Não exportação de texto bruto, metadata privada ou query privada das fontes.
- Eventos não selecionados ou com falha ignorados.
- Modelo vazio preserva publicação anterior; modelo ausente retrai.
- Modelo sem evidência fica em espera.
- Banco inexistente não é criado silenciosamente.
- Documento de outro proprietário não é sobrescrito nem apagado.
- Limite de tentativas para a mesma revisão e retomada quando o conteúdo muda.
- Crash após commit do destino recuperado sem repetir retain.
- Evento ocorrido durante publicação não é perdido.
- Regra removida/desativada retrai após restart.
- Documento derivado removido externamente é reparado pela reconciliação.
- Segundo escritor recusado; reutilização de estado em outro schema recusada.
- SQLite de outra aplicação e diretório compartilhado com permissões inadequadas recusados sem modificá-los.
- Startup/shutdown do consumidor e processamento efetivo da fila.

[Relatório nativo estruturado](native-report.json).
