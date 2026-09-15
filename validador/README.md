# Validador

Cliente de conformidade do desafio. Biblioteca padrao do Python apenas, sem
dependencia externa, porque a stack da sua entrega e livre.

## Como rodar

```
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

Flags opcionais: `--caminho-mcp` (padrao `/mcp`) e `--caminho-a2a` (padrao `/a2a`).

Suba os dois processos do zero antes de rodar. As reservas criadas durante uma
execucao mudam o resultado da execucao seguinte, entao rodar duas vezes seguidas
sem reiniciar produz falsos negativos.

## O que ele faz

Sao 36 verificacoes. As de 1 a 20 falam HTTP direto com o seu servidor MCP: elas
cobrem descoberta, schemas, os campos obrigatorios de `_meta`, os dois tipos de
erro, o resource, e o ciclo completo de MRTR, incluindo capability faltando,
`requestState` adulterado e recusa do usuario.

As de 21 a 36 falam A2A com o seu agente: Agent Card, Task, pausa em
`TASK_STATE_INPUT_REQUIRED`, retomada, recusa, estado terminal e duas Tasks
pausadas ao mesmo tempo.

Cada verificacao imprime `PASS` ou `FAIL` com o motivo. O processo termina com
codigo de saida 0 somente se todas passarem.

## O que ele nao faz

Duas exigencias do enunciado dependem de reiniciar processo ou de ler log, e
ficam no Fluxo do avaliador, nao aqui:

- o `requestState` continuar valido depois de o servidor MCP ser reiniciado;
- o `traceparent` aparecer no stderr do servidor MCP.

Para a segunda, o validador imprime na primeira linha o trace-id que vai usar.
Procure esse valor no stderr do seu servidor.
