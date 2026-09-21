# A Ponte: um agente A2A com MCP por dentro

Dois processos: um servidor MCP que expõe as salas da Hill Valley Tech, e um agente
que consome esse servidor por dentro (como host MCP) e se oferece ao mundo por fora
(como servidor A2A). No meio, a ponte: o `input_required` do MRTR vira uma Task
pausada em `TASK_STATE_INPUT_REQUIRED`, e o `requestState` fica guardado até a
resposta chegar.

**Stack:** Python 3.10+ com o SDK oficial `mcp` 2.2.0 (revisão 2026-07-28 da spec).
Sem LLM em nenhum ponto do caminho de execução — o agente lê um formato fixo e
decide por regra, então o mesmo pedido produz sempre o mesmo resultado.

```
servidor-mcp/
  dominio.py      salas, política de uso e as regras de reserva
  servidor.py     as três tools, o resource, o MRTR e o log de stderr
agente/
  cliente_mcp.py  o host MCP: descoberta, chamadas e o input_required cru
  tarefas.py      a Task do A2A e o estado pausado, por Task
  agente.py       o servidor A2A e a ponte
```

## Como rodar

A partir de um clone limpo, na raiz do repositório.

**1. Crie o ambiente e instale as dependências.** As versões estão travadas em
`pyproject.toml`; `requirements.txt` espelha a mesma lista para quem usa pip.

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Com [uv](https://docs.astral.sh/uv/), `uv sync` faz as duas coisas e resolve
contra o `uv.lock` versionado junto.

**2. Gere a chave de integridade do `requestState`.** Ela precisa ter no mínimo
32 bytes de aleatoriedade e nunca pode entrar no repositório, que é público:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

**3. Exporte a chave nos dois terminais** (o agente só precisa dela se você subir
os dois pelo mesmo script; o servidor MCP sempre precisa):

```bash
export REQUEST_STATE_SECRET=<o valor gerado no passo 2>
```

No PowerShell: `$env:REQUEST_STATE_SECRET = "<valor>"`.

Você também pode copiar `.env.example` para `.env` e carregá-lo com
`set -a && . ./.env && set +a`. O `.env` está no `.gitignore`.

**4. Suba o servidor MCP** (terminal 1, deixe o stderr visível):

```bash
python3 servidor-mcp/servidor.py
```

Atende em `http://127.0.0.1:7301/mcp`.

**5. Suba o agente** (terminal 2):

```bash
python3 agente/agente.py
```

Atende em `http://127.0.0.1:7300`, com o card em `/.well-known/agent-card.json` e
o endpoint JSON-RPC em `/a2a`.

**6. Rode o validador** (terminal 3), com os dois processos recém-iniciados:

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

As portas e as URLs são parametrizáveis por variável de ambiente — `MCP_HOST`,
`MCP_PORT`, `AGENTE_HOST`, `AGENTE_PORT`, `AGENTE_URL_BASE`, `MCP_URL` — e os
padrões já são os que o validador assume.

## Onde a ponte acontece

A ponte é o método `Agente._aplicar`, em `agente/agente.py:141`. É ele que recebe
o que o servidor MCP devolveu e decide o que isso significa para a Task. Quando o
resultado é um `InputRequiredResult` (`agente/agente.py:149`), o agente não
responde a elicitation por conta própria nem trava esperando: ele lê a única
entrada de `inputRequests`, extrai os ids do enum do `requestedSchema`, guarda
chave, `requestState` e os argumentos originais em `Tarefa.pausa`
(`agente/agente.py:152`), coloca a Task em `TASK_STATE_INPUT_REQUIRED`
(`agente/agente.py:158`) e devolve ao cliente A2A exatamente a linha
`alternativas: <ids>`. O `requestState` fica só ali, amarrado àquela Task —
`Tarefa.para_wire` (`agente/tarefas.py`) serializa apenas o que o A2A define,
então ele nunca aparece em nenhuma resposta.

O caminho de volta é `Agente._continuar` (`agente/agente.py:199`). O
`SendMessage` de continuação traz `taskId` e `escolha=<valor>`; uma escolha fora
do enum repete a lista e mantém a Task pausada, e `escolha=recusar` vira
`action: decline`. Com uma escolha válida, o agente chama
`HostMCP.retomar` (`agente/agente.py:217`, implementado em
`agente/cliente_mcp.py:115`), que repete o mesmo `tools/call` levando
`inputResponses` com a chave que veio no `inputRequests` e o `requestState`
ecoado sem nenhuma modificação. O id de JSON-RPC do retry é diferente do inicial
porque a sessão numera cada request — são requests independentes, e isso aparece
no stderr do servidor MCP como um par de `tools/call` com ids consecutivos.

Do outro lado, quem transforma o conflito em pergunta é o resolver
`escolha_de_sala` (`servidor-mcp/servidor.py:208`). Ele devolve um marcador
`Elicit` (`servidor-mcp/servidor.py:224`) e o framework do SDK termina a resposta
com `resultType: input_required`. O servidor nunca inicia um request em direção
ao cliente: no transporte stateless não existe canal de volta.

## Decisões técnicas

**Proteção do `requestState`.** Fica a cargo do `RequestStateBoundary` do SDK,
configurado em `servidor-mcp/servidor.py:131` com
`RequestStateSecurity(keys=[<segredo>], ttl=900)`. O codec embutido é
AES-256-GCM com a chave derivada por HKDF-SHA256, ou seja, o token é cifrado e
autenticado, não só assinado. Trocar um caractere qualquer do token faz a
verificação do GCM falhar e o servidor responde `-32602` com a mensagem
`Invalid or expired requestState`, sem revelar o motivo real, que vai só para o
log. O envelope ainda carrega `iat`, `exp`, a audiência (o nome do servidor) e
uma amarração ao request: método, nome da tool e um digest dos `arguments`. É
essa amarração que faz um retry com argumentos adulterados ser rejeitado em vez
de tomar efeito.

**Validade.** 15 minutos (`VALIDADE_DO_REQUEST_STATE`, `servidor-mcp/servidor.py:39`),
dentro da faixa de 5 a 30 exigida.

**A chave.** Vem sempre de `REQUEST_STATE_SECRET`, e o processo se recusa a subir
se ela tiver menos de 32 bytes (`_segredo`, `servidor-mcp/servidor.py:109`). Não
há segredo no código. Como a chave é do ambiente e não do processo, e como o
servidor não guarda nada em memória entre o `input_required` e o retry, um
`requestState` emitido antes de um restart continua válido depois dele —
verificado à mão matando o processo entre a pausa e a retomada.

**Onde mora o estado das Tasks.** Em memória, no dicionário de `agente/tarefas.py`
(`Tarefas._por_id`), vivo enquanto o processo do agente estiver de pé. Cada Task
tem o seu próprio objeto `Pausa`, então duas Tasks pausadas ao mesmo tempo nunca
trocam de `requestState`. As reservas também são em memória, no servidor MCP,
como o enunciado permite.

**O `requestState` é opaco para o agente.** Ele é guardado como string e ecoado
como string. O agente nunca abre, interpreta ou reconstrói o conteúdo.

**Descoberta em runtime.** O agente não tem lista de ferramentas no código: na
primeira Task ele faz `tools/list`, confere que `reservar_sala` existe e lê o
resource `politica://uso` para extrair a versão da primeira linha
(`HostMCP.descobrir`, `agente/cliente_mcp.py:89`). É essa versão lida do resource
que vai no campo `politica` do artifact.

**Trace context.** O agente extrai o trace-id do header `traceparent` da chamada
A2A e monta um `traceparent` novo — mesmo trace-id, span-id novo — para o `_meta`
de todos os requests MCP daquela Task (`Agente._traceparent`,
`agente/agente.py:123`). O servidor registra método, id, nome, traceparent e
`clientCapabilities` de cada request no stderr.

**Log no nível do ASGI, não no middleware do SDK.** O SDK dispara um `tools/list`
interno antes de cada `tools/call` para validar headers `Mcp-Param-*`. Esse
request nunca passou pela rede, e registrá-lo poluiria justamente a evidência que
o avaliador vai procurar. Por isso o log lê o corpo de cada POST que chega de
fato (`LogDeRequests`, `servidor-mcp/servidor.py:77`).

### Uma limitação do SDK, com a evidência

`ClientSession._build_capabilities` (em `mcp/client/session.py`) só anuncia
`elicitation` quando a sessão recebe um `elicitation_callback`, e nesse caso
anuncia `form` e `url` juntos:

```python
elicitation = (
    types.ElicitationCapability(form=types.FormElicitationCapability(), url=types.UrlElicitationCapability())
    if self._elicitation_callback is not _default_elicitation_callback
    else None
)
```

O `_meta` que o chamador passa por request não ajuda, porque o stamp da sessão
sobrescreve a chave:

```python
meta[CLIENT_CAPABILITIES_META_KEY] = capabilities
```

Como o enunciado pede a forma `{"elicitation": {"form": {}}}` — form mode, e não
elicitation em geral —, `SessaoDoAgente` (`agente/cliente_mcp.py:28`) substitui
esse único método para declarar exatamente isso. Nenhuma outra parte do protocolo
foi alterada; o stderr do servidor MCP mostra a capability na forma pedida em
todos os requests do agente. Um `elicitation_callback` não foi usado de propósito:
ele nunca dispararia nesta revisão da spec, mas anunciaria `url` sem que o agente
saiba responder a uma elicitation em URL mode.

## Saída do validador

Última execução, com os dois processos recém-iniciados:

```
trace-id desta execucao: 9fb39de5af181610cf7b3596798501d5
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

Código de saída: `0`.

Trecho correspondente do stderr do servidor MCP, com o mesmo trace-id, o
`tools/list` anterior ao primeiro `tools/call` do agente e o par de `tools/call`
da reserva que passou pela pausa (ids diferentes entre si):

```
[mcp] method='tools/list' id=1 nome='' traceparent='00-9fb39de5af181610cf7b3596798501d5-c0f09fba5f7d4c31-01' clientCapabilities={"elicitation": {"form": {}}}
[mcp] method='resources/read' id=2 nome='politica://uso' traceparent='00-9fb39de5af181610cf7b3596798501d5-c0f09fba5f7d4c31-01' clientCapabilities={"elicitation": {"form": {}}}
[mcp] method='tools/call' id=3 nome='reservar_sala' traceparent='00-9fb39de5af181610cf7b3596798501d5-c0f09fba5f7d4c31-01' clientCapabilities={"elicitation": {"form": {}}}
[mcp] method='tools/call' id=4 nome='reservar_sala' traceparent='00-9fb39de5af181610cf7b3596798501d5-cecf5e93ae1d06b2-01' clientCapabilities={"elicitation": {"form": {}}}
[mcp] method='tools/call' id=5 nome='reservar_sala' traceparent='00-9fb39de5af181610cf7b3596798501d5-c907abf61090e83b-01' clientCapabilities={"elicitation": {"form": {}}}
```
