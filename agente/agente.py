"""O agente: host MCP por dentro, servidor A2A por fora, e a ponte no meio.

Por fora ele publica um Agent Card e atende SendMessage e GetTask no binding
JSON-RPC. Por dentro ele e um cliente MCP comum. A ponte e `_aplicar`: e la que
o `input_required` do MCP vira `TASK_STATE_INPUT_REQUIRED`, e em `_continuar`
que o `requestState` guardado volta para o servidor no retry.

Rode com:

    REQUEST_STATE_SECRET=... python agente/agente.py
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
from contextlib import asynccontextmanager

import mcp_types as tipos
import tarefas as t
import uvicorn
from cliente_mcp import HostMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

NOME = "Central de Salas"
VERSAO = "1.0.0"
SKILL = "reservar-sala"

PEDIDO = re.compile(r"^\s*reservar\s+(?P<campos>.+)$", re.IGNORECASE | re.DOTALL)
CAMPO = re.compile(r"(\w+)=(.*?)(?=\s+\w+=|\s*$)", re.DOTALL)
ESCOLHA = re.compile(r"^\s*escolha=(?P<valor>.+?)\s*$", re.IGNORECASE | re.DOTALL)

CAMPOS_DO_PEDIDO = ("sala", "inicio", "fim", "responsavel")
FORMATO = "reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>"


class ErroA2A(Exception):
    def __init__(self, codigo: int, mensagem: str) -> None:
        super().__init__(mensagem)
        self.codigo = codigo
        self.mensagem = mensagem


def cartao(url_base: str) -> dict:
    """O Agent Card v1.0: identidade publica e ponto de descoberta."""
    return {
        "name": NOME,
        "description": "Reserva salas de reuniao da Hill Valley Tech.",
        "provider": {"organization": "Hill Valley Tech", "url": "https://hillvalley.example"},
        "version": VERSAO,
        "supportedInterfaces": [
            {"url": f"{url_base}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
        ],
        "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": SKILL,
                "name": "Reservar sala",
                "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
                "tags": ["salas", "agenda"],
                "inputModes": ["text/plain"],
                "outputModes": ["text/plain"],
                "examples": [
                    "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 "
                    "fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
                ],
            }
        ],
    }


def _texto_da_mensagem(mensagem: dict) -> str:
    return " ".join(p.get("text", "") for p in mensagem.get("parts") or [])


def interpretar_pedido(texto: str) -> dict | None:
    """Le o pedido em formato fixo. Sem LLM: mesma entrada, mesma saida, sempre."""
    achado = PEDIDO.match(texto)
    if not achado:
        return None
    campos = {chave: valor.strip() for chave, valor in CAMPO.findall(achado.group("campos"))}
    if not all(campo in campos and campos[campo] for campo in CAMPOS_DO_PEDIDO):
        return None
    return {campo: campos[campo] for campo in CAMPOS_DO_PEDIDO}


def interpretar_escolha(texto: str) -> str | None:
    achado = ESCOLHA.match(texto)
    return achado.group("valor").strip() if achado else None


def _alternativas_da_elicitation(pedido_de_input) -> list[str]:
    """Os ids oferecidos, na ordem em que vieram no enum da elicitation."""
    bruto = pedido_de_input.model_dump(by_alias=True, mode="json", exclude_none=True)
    campo = ((bruto.get("params") or {}).get("requestedSchema") or {}).get("properties", {}).get("sala", {})
    if campo.get("enum"):
        return list(campo["enum"])
    return [campo["const"]] if "const" in campo else []


def _texto_do_resultado(resultado) -> str:
    return " ".join(getattr(bloco, "text", "") for bloco in resultado.content or [])


class Agente:
    def __init__(self, host: HostMCP, url_base: str) -> None:
        self.host = host
        self.url_base = url_base
        self.tarefas = t.Tarefas()

    # ------------------------------------------------------------------ #
    # Trace context: o mesmo trace-id do cliente A2A em todo request MCP.
    # ------------------------------------------------------------------ #

    def _traceparent(self, tarefa: t.Tarefa, cabecalho: str | None) -> str:
        trace_id = None
        if cabecalho:
            partes = cabecalho.split("-")
            if len(partes) >= 3 and len(partes[1]) == 32:
                trace_id = partes[1]
        if trace_id is None and tarefa.traceparent:
            trace_id = tarefa.traceparent.split("-")[1]
        if trace_id is None:
            trace_id = secrets.token_hex(16)
        # O span-id pode ser novo a cada salto; o trace-id, nunca.
        tarefa.traceparent = f"00-{trace_id}-{secrets.token_hex(8)}-01"
        return tarefa.traceparent

    # ------------------------------------------------------------------ #
    # A ponte
    # ------------------------------------------------------------------ #

    def _aplicar(self, tarefa: t.Tarefa, resultado, argumentos: dict) -> None:
        """Traduz o resultado do MCP em estado de Task. Aqui e a ponte.

        `input_required` nao e erro nem resposta: e uma pausa. O agente nao
        responde a elicitation por conta propria e nao trava esperando. Ele
        interrompe a Task, devolve a pergunta ao cliente A2A e guarda o
        `requestState` amarrado a esta Task.
        """
        if isinstance(resultado, tipos.InputRequiredResult):
            chave, pedido_de_input = next(iter(resultado.input_requests.items()))
            alternativas = _alternativas_da_elicitation(pedido_de_input)
            tarefa.pausa = t.Pausa(
                chave=chave,
                request_state=resultado.request_state or "",
                alternativas=alternativas,
                argumentos=argumentos,
            )
            tarefa.estado = t.INPUT_REQUIRED
            tarefa.do_agente(f"alternativas: {', '.join(alternativas)}")
            return

        if resultado.is_error:
            # Erro de execucao da tool: a mensagem exata chega ao cliente A2A.
            tarefa.estado = t.FAILED
            tarefa.do_agente(_texto_do_resultado(resultado))
            return

        dados = resultado.structured_content or {}
        if not dados.get("reservado"):
            tarefa.estado = t.CANCELED
            tarefa.do_agente(f"Reserva nao realizada: {dados.get('motivo') or 'recusado'}.")
            return

        reserva = {
            "reserva": dados.get("reserva"),
            "sala": dados.get("sala"),
            "inicio": dados.get("inicio"),
            "fim": dados.get("fim"),
            "responsavel": dados.get("responsavel"),
            # A versao vem do resource que o agente leu, nao do que a tool devolveu.
            "politica": self.host.versao_da_politica,
        }
        tarefa.anexar("reserva", json.dumps(reserva, ensure_ascii=False))
        tarefa.estado = t.COMPLETED
        tarefa.do_agente(f"Reserva {reserva['reserva']} confirmada na {reserva['sala']}.")

    async def _abrir(self, tarefa: t.Tarefa, texto: str, cabecalho: str | None) -> None:
        traceparent = self._traceparent(tarefa, cabecalho)
        pedido = interpretar_pedido(texto)
        if pedido is None:
            tarefa.estado = t.FAILED
            tarefa.do_agente(f"Pedido invalido. Use: {FORMATO}")
            return
        tarefa.estado = t.WORKING
        await self.host.descobrir(traceparent)
        resultado = await self.host.reservar(pedido, traceparent)
        self._aplicar(tarefa, resultado, pedido)

    async def _continuar(self, tarefa: t.Tarefa, texto: str, cabecalho: str | None) -> None:
        pausa = tarefa.pausa
        assert pausa is not None
        escolha = interpretar_escolha(texto)
        if escolha is None or (escolha != "recusar" and escolha not in pausa.alternativas):
            # Escolha fora do enum: a Task continua pausada e a lista e repetida.
            tarefa.do_agente(f"alternativas: {', '.join(pausa.alternativas)}")
            return

        if escolha == "recusar":
            resposta = tipos.ElicitResult(action="decline")
        else:
            resposta = tipos.ElicitResult(action="accept", content={"sala": escolha})

        traceparent = self._traceparent(tarefa, cabecalho)
        tarefa.estado = t.WORKING
        # O `requestState` volta para o servidor exatamente como veio, em um
        # request novo, com um id de JSON-RPC diferente do inicial.
        resultado = await self.host.retomar(
            pausa.argumentos, pausa.chave, resposta, pausa.request_state, traceparent
        )
        tarefa.pausa = None
        self._aplicar(tarefa, resultado, pausa.argumentos)

    # ------------------------------------------------------------------ #
    # Metodos A2A
    # ------------------------------------------------------------------ #

    async def send_message(self, params: dict, cabecalho: str | None) -> dict:
        mensagem = params.get("message") or {}
        texto = _texto_da_mensagem(mensagem)
        task_id = mensagem.get("taskId") or params.get("taskId")

        if task_id:
            tarefa = self.tarefas.buscar(task_id)
            if tarefa is None:
                raise ErroA2A(-32602, f"Task desconhecida: {task_id}")
            if tarefa.terminal:
                # Estado terminal e definitivo: nao volta a WORKING.
                raise ErroA2A(-32602, f"Task {task_id} ja terminou em {tarefa.estado}")
            if tarefa.estado != t.INPUT_REQUIRED or tarefa.pausa is None:
                raise ErroA2A(-32602, f"Task {task_id} nao esta aguardando entrada")
            tarefa.do_usuario(mensagem)
            await self._continuar(tarefa, texto, cabecalho)
            return {"task": tarefa.para_wire()}

        tarefa = self.tarefas.nova()
        tarefa.do_usuario(mensagem)
        await self._abrir(tarefa, texto, cabecalho)
        return {"task": tarefa.para_wire()}

    async def get_task(self, params: dict) -> dict:
        task_id = params.get("id") or params.get("taskId") or ""
        tarefa = self.tarefas.buscar(task_id)
        if tarefa is None:
            raise ErroA2A(-32602, f"Task desconhecida: {task_id}")
        return {"task": tarefa.para_wire()}


def criar_app() -> Starlette:
    url_mcp = os.environ.get("MCP_URL", "http://127.0.0.1:7301/mcp")
    host_publico = os.environ.get("AGENTE_HOST", "127.0.0.1")
    porta = int(os.environ.get("AGENTE_PORT", "7300"))
    url_base = os.environ.get("AGENTE_URL_BASE", f"http://{host_publico}:{porta}")

    host_mcp = HostMCP(url_mcp)
    agente = Agente(host_mcp, url_base)

    @asynccontextmanager
    async def ciclo(_app: Starlette):
        await host_mcp.conectar()
        print(f"[a2a] agente em {url_base}, servidor MCP em {url_mcp}", file=sys.stderr, flush=True)
        try:
            yield
        finally:
            await host_mcp.fechar()

    async def agent_card(_request: Request) -> JSONResponse:
        return JSONResponse(cartao(url_base))

    async def rpc(request: Request) -> JSONResponse:
        try:
            corpo = await request.json()
        except Exception:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, status_code=400
            )
        identificador = corpo.get("id")
        metodo = corpo.get("method")
        params = corpo.get("params") or {}
        traceparent = request.headers.get("traceparent")
        print(f"[a2a] method={metodo} id={identificador!r} traceparent={traceparent!r}", file=sys.stderr, flush=True)
        try:
            if metodo == "SendMessage":
                resultado = await agente.send_message(params, traceparent)
            elif metodo == "GetTask":
                resultado = await agente.get_task(params)
            else:
                raise ErroA2A(-32601, f"Metodo desconhecido: {metodo}")
        except ErroA2A as erro:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": identificador, "error": {"code": erro.codigo, "message": erro.mensagem}}
            )
        return JSONResponse({"jsonrpc": "2.0", "id": identificador, "result": resultado})

    return Starlette(
        lifespan=ciclo,
        routes=[
            Route("/.well-known/agent-card.json", agent_card, methods=["GET"]),
            Route("/a2a", rpc, methods=["POST"]),
        ],
    )


def main() -> None:
    host = os.environ.get("AGENTE_HOST", "127.0.0.1")
    porta = int(os.environ.get("AGENTE_PORT", "7300"))
    uvicorn.run(criar_app(), host=host, port=porta, log_level="info")


if __name__ == "__main__":
    main()
