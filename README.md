# A Ponte: um agente A2A com MCP por dentro

Entrega do desafio "A Ponte" (MBA Full Cycle) — Central de Salas da Hill Valley Tech.

Dois processos:

- **`servidor-mcp/`** — servidor MCP em Streamable HTTP (porta `7301`), com as tools
  `listar_salas`, `consultar_disponibilidade`, `reservar_sala`, o resource
  `politica://uso` e o ciclo completo de MRTR (`resultType: input_required`) na
  reserva.
- **`agente/`** — o mesmo processo é host MCP por dentro (descobre e chama as
  tools do servidor acima via HTTP, como um cliente MCP de verdade) e servidor
  A2A v1.0 por fora (porta `7300`), com Agent Card, `SendMessage`, `GetTask` e a
  máquina de estados da Task.

Stack: **Python 3.10+, biblioteca padrão apenas** — sem dependências externas,
sem framework, sem LLM. Ver "Decisões técnicas" para o porquê.

## Como rodar

Requer só Python 3.10+. A partir de um clone limpo, em três terminais:

**1. Gere e exporte o segredo do `requestState`** (uma vez por sessão de shell;
nunca comite o valor gerado — o repositório é público):

```bash
export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
```

**2. Terminal 1 — servidor MCP** (porta 7301; mantenha o stderr visível, é onde
o log estruturado aparece):

```bash
cd servidor-mcp
REQUEST_STATE_SECRET=$REQUEST_STATE_SECRET python3 servidor.py
```

> `REQUEST_STATE_SECRET` precisa ser a mesma string nos dois terminais em que o
> servidor MCP roda (inicial e depois de um restart), senão um `requestState`
> emitido antes do restart deixa de validar. Exportar a variável no shell antes
> de abrir os terminais (passo 1) resolve isso automaticamente.

**3. Terminal 2 — agente** (porta 7300; ele faz `tools/list` e
`resources/read` no servidor MCP assim que sobe — confira isso no stderr do
servidor MCP):

```bash
cd agente
MCP_URL=http://localhost:7301 python3 agente.py
```

**4. Terminal 3 — validador** (a partir da raiz do repositório):

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

Portas e URLs são configuráveis por variável de ambiente (`MCP_HOST`,
`MCP_PORT`, `AGENT_HOST`, `AGENT_PORT`, `AGENT_PUBLIC_URL`, `MCP_URL`), mas os
padrões já são os que o validador espera (`7301` e `7300`).

Suba os dois processos sempre do zero antes de rodar o validador — reservas
criadas numa execução mudam o resultado da seguinte (isso é do enunciado, não
uma limitação da entrega).

## Onde a ponte acontece

Tudo em [`agente/agente.py`](agente/agente.py), na função
`_avancar_apos_chamada_mcp` — é o único lugar do código que lê o `resultType`
que voltou do servidor MCP:

- Quando `resultType == "input_required"`: a Task recebe
  `TASK_STATE_INPUT_REQUIRED`, o `requestState` opaco é guardado em
  `task.pausa` (nunca decodificado, nunca serializado numa resposta A2A — ver
  `Task.como_dict`, que não inclui `pausa`), e a mensagem de status vira
  exatamente `alternativas: <lista>` a partir do `enum`/`const` do
  `requestedSchema` da elicitation.
- Quando o resultado é `complete` com sucesso: a Task fecha em
  `TASK_STATE_COMPLETED` com o artifact `reserva`.
- Quando o resultado é `complete` com `reservado: false` (recusa): a Task fecha
  em `TASK_STATE_CANCELED`.
- Quando `isError: true`: a Task fecha em `TASK_STATE_FAILED` com a mensagem
  exata da tool.

O outro lado da ponte — o `requestState` voltando ao servidor — está em
`_continuar_reserva` (mesmo arquivo): um `SendMessage` de continuação
(`escolha=<valor>`) monta um novo `tools/call` via
`ClienteMcp.retomar_reservar_sala` (em
[`agente/mcp_cliente.py`](agente/mcp_cliente.py)), com **um id de JSON-RPC
novo** (gerado por `secrets.token_hex(8)` a cada chamada — nunca reaproveita o
id da chamada inicial), levando `inputResponses` com a mesma chave que veio no
`inputRequests` e o `requestState` ecoado sem modificação nenhuma. Se a escolha
não está no `enum` que o próprio agente guardou (`Pausa.alternativas`), a Task
continua pausada e a mesma linha `alternativas: ...` é repetida — sem nem
chamar o MCP, porque essa é uma validação de protocolo A2A, não uma regra de
domínio.

No servidor MCP, o espelho disso fica em
[`servidor-mcp/servidor.py`](servidor-mcp/servidor.py):
`_handle_reservar_sala_inicial` é quem decide entre reservar direto ou devolver
`input_required` (chamando `estado_requisicao.selar` para montar o token), e
`_handle_reservar_sala_retry` é quem reconstrói o pedido **inteiramente a
partir do `requestState`** (nunca dos `arguments` que vieram no retry) e conclui
a operação.

## Decisões técnicas

**`requestState`.** Formato `v1.<payload em base64url>.<assinatura em base64url>`
(sem os pontos de fato no token — a assinatura, de tamanho fixo, é só
concatenada ao final; ver [`servidor-mcp/estado_requisicao.py`](servidor-mcp/estado_requisicao.py)).
Integridade via **HMAC-SHA256** (`hmac.compare_digest`, comparação em tempo
constante) sobre o payload — qualquer byte adulterado no meio do token muda a
assinatura recomputada e o servidor rejeita com `-32602`. Cifrar não é feito
(o enunciado não exige e o conteúdo pode ser legível), só assinar. TTL de
**15 minutos** (`TTL_PADRAO_SEGUNDOS`), dentro da janela de 5–30 min pedida. A
chave HMAC vem de `REQUEST_STATE_SECRET` (variável de ambiente, nunca hardcoded
— o servidor recusa subir se a variável estiver ausente ou tiver menos de 32
caracteres). O payload selado carrega `sala`, `inicio`, `fim`, `responsavel`,
a chave da elicitation e a lista de alternativas — tudo que o servidor precisa
para reconstruir e concluir o pedido sem guardar nada em memória entre o
`input_required` e o retry. Por isso um retry sobrevive a um restart do
processo, desde que `REQUEST_STATE_SECRET` seja o mesmo.

Cada token selado também carrega um `jti` (identificador único, gerado em
`estado_requisicao.selar`). O servidor mantém um `set` em memória dos `jti`
já redimidos (`servidor-mcp/servidor.py:_JTIS_CONSUMIDOS`, protegido por
`threading.Lock`) e recusa com `-32602` uma segunda apresentação do mesmo
`requestState` — sem isso, um token válido (assinatura e TTL corretos)
poderia ser reenviado várias vezes e produzir várias reservas a partir de um
único conflito. Esse rastreamento é em memória pelo mesmo motivo que as
reservas são em memória: o requisito de sobreviver a um restart é sobre a
*validade criptográfica* do token (um retry ainda não usado antes do restart
precisa funcionar depois), não sobre o histórico de tokens já gastos — um
token já redimido antes de um restart poderia, em tese, ser reapresentado uma
vez após o restart. É um risco residual aceito, coerente com a arquitetura
100% em memória do resto do sistema.

**Estado das Tasks.** Em memória, num `dict[str, Task]` dentro de
`agente/agente.py` (classe `Estado`, com lock), igual às reservas do servidor
MCP — não precisa sobreviver a um restart, só precisa ser visível entre
chamadas do mesmo processo. Cada `Task` guarda sua própria `Pausa` (chave,
`requestState`, alternativas), então duas Tasks pausadas ao mesmo tempo nunca
compartilham ou trocam esse estado entre si. Cada `Task` também tem seu
próprio `threading.Lock` (`Task.lock`), adquirido em `_send_message` durante
todo o tratamento de uma continuação (checagem de estado terminal, checagem
de pausa, retry ao MCP e aplicação do resultado): sem isso, duas continuações
`SendMessage` concorrentes na mesma Task pausada passavam ambas pela checagem
de estado e disparavam dois retries ao MCP, e o agregado Task podia terminar
reportando `FAILED` com o artifact de uma reserva que na verdade tinha sido
criada com sucesso (o domínio, protegido por lock e conditional-write em
`servidor-mcp/dominio.py:criar_reserva`, nunca duplicava a reserva em si — o
bug era só na consistência do agregado Task do lado do agente).

**Erros de transporte MCP.** `agente/mcp_cliente.py` distingue `ErroMcp` (o
servidor MCP respondeu com um `error` JSON-RPC) de `ErroTransporteMcp`
(conexão recusada, timeout, resposta que não é JSON válido — a chamada não
chegou a completar). Sem essa distinção, uma falha de transporte no meio de
uma continuação deixava a Task presa para sempre em `TASK_STATE_WORKING`
(a transição para `WORKING` já tinha acontecido antes da chamada, e a exceção
não tratada pulava as duas transições terminais) e a conexão HTTP do cliente
A2A caía sem nenhuma resposta JSON-RPC. Agora ambos os tipos de erro fecham a
Task em `TASK_STATE_FAILED` (`agente.py:_falhar_por_erro_mcp`), e um backstop
`except Exception` em `do_POST` garante que nenhuma exceção inesperada
derruba a conexão sem resposta.

**Por que sem SDK oficial do MCP/A2A.** O enunciado descreve uma revisão do
MCP (`2026-07-28`) com um primitivo de MRTR (`resultType: input_required` como
resposta de `tools/call`, e não uma elicitation síncrona com canal de volta) e
códigos de erro (`-32020`, `-32021`) que não existem nos SDKs oficiais
disponíveis até o corte de conhecimento deste modelo (janeiro de 2026). Não há
como instalar ou verificar contra uma versão do `@modelcontextprotocol/server`
ou do pacote `mcp` que implemente esse dialeto sem acesso a uma revisão futura
da spec. Diante disso — e seguindo a orientação do próprio enunciado de
"documentar a limitação no README em vez de contornar reescrevendo o
protocolo" —, os dois processos foram implementados diretamente contra o
contrato de wire descrito em `exemplos/wire/` e verificado pelo
`validador/validar.py`, usando só a biblioteca padrão do Python
(`http.server`, `urllib`, `hmac`, `hashlib`, `json`). Isso também elimina
qualquer dependência de terceiros do caminho de execução, o que reforça o
determinismo exigido (mesmo pedido, mesmo resultado) e a ausência de qualquer
SDK de LLM.

**Validações da política, em ordem** (`servidor-mcp/dominio.py`): sala existe →
intervalo válido (`fim > inicio`) → janela 08:00–20:00 em `-03:00` → duração
máxima de 2h → conflito com reservas existentes → alternativas (capacidade ≥ a
da sala pedida, livres no intervalo, no máximo 3, ordenadas por capacidade e
depois por id).

## Saída do validador

Execução mais recente, com os dois processos recém-iniciados
(`python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301`):

```
trace-id desta execucao: fa4f5764055f3d29d504089a8b035c6f
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```

Verificado também manualmente, fora do validador (não coberto por ele, por
depender de restart de processo / leitura de log — ver `validador/README.md`):
um `requestState` emitido antes de um restart do servidor MCP continua válido
e conclui a reserva depois do restart, mantendo o mesmo
`REQUEST_STATE_SECRET`; um `requestState` com um único caractere trocado é
rejeitado com `-32602`; o stderr do servidor MCP mostra o `traceparent` com o
mesmo trace-id impresso pelo validador.
