#!/usr/bin/env python3
"""Validador do desafio A Ponte.

Fala HTTP direto com o servidor MCP para as verificacoes de protocolo, e A2A com
o agente para as verificacoes de fluxo. Biblioteca padrao apenas, Python 3.10+.

    python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301

Rode sempre com os dois processos recem-iniciados: as reservas criadas por uma
execucao mudam o resultado da seguinte.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import urllib.error
import urllib.request

PROTOCOLO = "2026-07-28"
CAP_COM_ELICITATION = {"elicitation": {"form": {}}}
POLITICA = "2026-11-01"

ERRO_SALA = "Sala inexistente: sala-delorean"
ERRO_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERRO_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERRO_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
ERRO_SEM_ALTERNATIVA = "Sem alternativas disponiveis no intervalo"

TRACE_ID = secrets.token_hex(16)
TRACEPARENT = f"00-{TRACE_ID}-{secrets.token_hex(8)}-01"

DIA = "2026-11-03"


def h(hora: str) -> str:
    return f"{DIA}T{hora}:00-03:00"


class Resultado:
    def __init__(self) -> None:
        self.passou = 0
        self.falhou = 0
        self.corpos: list[str] = []
        self.corpos_a2a: list[str] = []

    def check(self, numero: int, descricao: str, condicao: bool, motivo: str = "") -> bool:
        if condicao:
            self.passou += 1
            print(f"PASS {numero:02d} {descricao}")
        else:
            self.falhou += 1
            print(f"FAIL {numero:02d} {descricao}: {motivo}")
        return condicao


R = Resultado()


def post(url: str, corpo: dict, cabecalhos: dict) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=json.dumps(corpo).encode(), headers=cabecalhos, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            bruto = r.read().decode()
            status = r.status
    except urllib.error.HTTPError as e:
        bruto = e.read().decode()
        status = e.code
    except urllib.error.URLError as e:
        raise SystemExit(f"nao consegui falar com {url}: {e}")
    R.corpos.append(bruto)
    try:
        return status, json.loads(bruto)
    except json.JSONDecodeError:
        return status, {"_bruto": bruto}


def mcp(url: str, metodo: str, params: dict, nome: str | None = None, capabilities: dict | None = None,
        omitir: str | None = None, traceparent: str | None = None) -> tuple[int, dict]:
    meta: dict = {
        "io.modelcontextprotocol/protocolVersion": PROTOCOLO,
        "io.modelcontextprotocol/clientCapabilities": CAP_COM_ELICITATION if capabilities is None else capabilities,
    }
    if traceparent:
        meta["traceparent"] = traceparent
    if omitir == "protocolVersion":
        meta.pop("io.modelcontextprotocol/protocolVersion")
    if omitir == "clientCapabilities":
        meta.pop("io.modelcontextprotocol/clientCapabilities")
    cabecalhos = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOLO,
        "Mcp-Method": metodo,
    }
    if nome:
        cabecalhos["Mcp-Name"] = nome
    corpo = {"jsonrpc": "2.0", "id": secrets.token_hex(6), "method": metodo, "params": {**params, "_meta": meta}}
    return post(url, corpo, cabecalhos)


def reservar(url: str, sala: str, inicio: str, fim: str, responsavel: str = "Validador", **kw) -> tuple[int, dict]:
    argumentos = {"sala": sala, "inicio": inicio, "fim": fim, "responsavel": responsavel}
    return mcp(url, "tools/call", {"name": "reservar_sala", "arguments": argumentos}, "reservar_sala", **kw)


def retomar(url: str, sala: str, inicio: str, fim: str, chave: str, resposta: dict, estado: str,
            responsavel: str = "Validador") -> tuple[int, dict]:
    params = {
        "name": "reservar_sala",
        "arguments": {"sala": sala, "inicio": inicio, "fim": fim, "responsavel": responsavel},
        "inputResponses": {chave: resposta},
        "requestState": estado,
    }
    return mcp(url, "tools/call", params, "reservar_sala")


def texto(resultado: dict) -> str:
    return " ".join(p.get("text", "") for p in resultado.get("content", []))


def erro(resposta: dict) -> dict:
    return resposta.get("error") or {}


def a2a(url: str, metodo: str, params: dict, traceparent: str | None = None) -> dict:
    cabecalhos = {"Content-Type": "application/json"}
    if traceparent:
        cabecalhos["traceparent"] = traceparent
    _, resposta = post(url, {"jsonrpc": "2.0", "id": secrets.token_hex(6), "method": metodo, "params": params}, cabecalhos)
    # So o que o agente devolve entra na varredura de vazamento: as respostas do
    # servidor MCP carregam requestState por contrato e reprovariam um agente correto.
    R.corpos_a2a.append(R.corpos[-1] if R.corpos else "")
    return resposta


def enviar(url: str, texto_pedido: str, task_id: str | None = None, traceparent: str | None = None) -> dict:
    mensagem: dict = {
        "messageId": f"msg-{secrets.token_hex(6)}",
        "role": "ROLE_USER",
        "parts": [{"text": texto_pedido}],
    }
    if task_id:
        mensagem["taskId"] = task_id
    return a2a(url, "SendMessage", {"message": mensagem}, traceparent)


def tarefa_de(resposta: dict) -> dict:
    resultado = resposta.get("result") or {}
    return resultado.get("task") or resultado


def estado(resposta: dict) -> str:
    return (tarefa_de(resposta).get("status") or {}).get("state", "")


def mensagem(resposta: dict) -> str:
    status = tarefa_de(resposta).get("status") or {}
    partes = (status.get("message") or {}).get("parts") or []
    return " ".join(p.get("text", "") for p in partes)


def verificar_mcp(url: str) -> str:
    status, resposta = mcp(url, "tools/list", {})
    tools = {t["name"]: t for t in (resposta.get("result") or {}).get("tools", [])}
    esperadas = {"listar_salas", "consultar_disponibilidade", "reservar_sala"}
    R.check(1, "tools/list traz as tres tools", esperadas <= set(tools), f"veio {sorted(tools)}")
    R.check(
        2,
        "toda tool tem inputSchema de objeto",
        all(t.get("inputSchema", {}).get("type") == "object" for t in tools.values()) and bool(tools),
        "alguma tool sem inputSchema type object",
    )

    _, resposta = mcp(url, "tools/call", {"name": "listar_salas", "arguments": {}}, "listar_salas")
    resultado = resposta.get("result") or {}
    estruturado = resultado.get("structuredContent")
    bloco = texto(resultado)
    mesmo_json = False
    if estruturado is not None and bloco:
        try:
            mesmo_json = json.loads(bloco) == estruturado
        except json.JSONDecodeError:
            mesmo_json = False
    R.check(3, "listar_salas devolve structuredContent e o mesmo JSON em texto", mesmo_json, "structuredContent e bloco de texto divergem")

    status, resposta = mcp(url, "tools/call", {"name": "listar_salas", "arguments": {}}, "listar_salas", omitir="protocolVersion")
    R.check(4, "_meta sem protocolVersion devolve -32602 e HTTP 400", erro(resposta).get("code") == -32602 and status == 400, f"code={erro(resposta).get('code')} http={status}")

    status, resposta = mcp(url, "tools/call", {"name": "listar_salas", "arguments": {}}, "listar_salas", omitir="clientCapabilities")
    R.check(5, "_meta sem clientCapabilities devolve -32602 e HTTP 400", erro(resposta).get("code") == -32602 and status == 400, f"code={erro(resposta).get('code')} http={status}")

    _, resposta = mcp(url, "tools/call", {"name": "voar_delorean", "arguments": {}}, "voar_delorean")
    # A spec trata tool desconhecida como erro de protocolo, mas os SDKs divergem e
    # alguns devolvem erro de execucao. Aceitamos os dois, desde que a chamada seja recusada.
    recusou = erro(resposta).get("code") == -32602 or (resposta.get("result") or {}).get("isError") is True
    R.check(6, "tool inexistente e recusada, por -32602 ou por isError", recusou, json.dumps(resposta)[:200])

    _, resposta = mcp(url, "resources/read", {"uri": "politica://uso"}, "politica://uso")
    conteudo = ((resposta.get("result") or {}).get("contents") or [{}])[0].get("text", "")
    R.check(7, "resources/read de politica://uso devolve a politica", POLITICA in conteudo, "versao da politica nao encontrada no recurso")

    _, resposta = mcp(url, "resources/read", {"uri": "politica://inexistente"}, "politica://inexistente")
    R.check(8, "resources/read de URI inexistente devolve -32602", erro(resposta).get("code") == -32602, f"code={erro(resposta).get('code')}")

    _, resposta = reservar(url, "sala-delorean", h("09:00"), h("10:00"))
    R.check(9, "sala inexistente devolve isError com a mensagem exata", (resposta.get("result") or {}).get("isError") and ERRO_SALA in texto(resposta.get("result") or {}), texto(resposta.get("result") or {}))

    _, resposta = mcp(url, "tools/call", {"name": "consultar_disponibilidade", "arguments": {"sala": "sala-aquario", "inicio": h("07:00"), "fim": h("08:00")}}, "consultar_disponibilidade")
    R.check(10, "fora da janela devolve isError com a mensagem exata", (resposta.get("result") or {}).get("isError") and ERRO_JANELA in texto(resposta.get("result") or {}), texto(resposta.get("result") or {}))

    _, resposta = mcp(url, "tools/call", {"name": "consultar_disponibilidade", "arguments": {"sala": "sala-aquario", "inicio": h("09:00"), "fim": h("12:00")}}, "consultar_disponibilidade")
    R.check(11, "duracao acima de 2h devolve isError com a mensagem exata", (resposta.get("result") or {}).get("isError") and ERRO_DURACAO in texto(resposta.get("result") or {}), texto(resposta.get("result") or {}))

    _, resposta = mcp(url, "tools/call", {"name": "consultar_disponibilidade", "arguments": {"sala": "sala-aquario", "inicio": h("10:00"), "fim": h("09:00")}}, "consultar_disponibilidade")
    R.check(12, "intervalo invertido devolve isError com a mensagem exata", (resposta.get("result") or {}).get("isError") and ERRO_INTERVALO in texto(resposta.get("result") or {}), texto(resposta.get("result") or {}))

    _, resposta = reservar(url, "sala-garagem", h("14:00"), h("15:00"), traceparent=TRACEPARENT)
    resultado = resposta.get("result") or {}
    pedidos = resultado.get("inputRequests") or {}
    request_state = resultado.get("requestState")
    R.check(13, "conflito devolve input_required com inputRequests e requestState", resultado.get("resultType") == "input_required" and len(pedidos) == 1 and bool(request_state), f"resultType={resultado.get('resultType')} chaves={list(pedidos)}")
    chave = next(iter(pedidos), None)
    enum = []
    if chave:
        params_pedido = pedidos[chave].get("params", {})
        campo = ((params_pedido.get("requestedSchema") or {}).get("properties") or {}).get("sala", {})
        enum = campo.get("enum") or ([campo["const"]] if "const" in campo else [])
        R.check(14, "a elicitation e form mode e oferece as alternativas na ordem certa", params_pedido.get("mode") == "form" and enum == ["sala-fusca", "sala-mirante"], f"mode={params_pedido.get('mode')} enum={enum}")
    else:
        R.check(14, "a elicitation e form mode e oferece as alternativas na ordem certa", False, "nenhum inputRequest para inspecionar")

    status, resposta = reservar(url, "sala-garagem", h("14:00"), h("15:00"), capabilities={})
    dados = erro(resposta).get("data") or {}
    R.check(15, "conflito sem a capability elicitation devolve -32021 e HTTP 400", erro(resposta).get("code") == -32021 and "requiredCapabilities" in dados and status == 400, f"code={erro(resposta).get('code')} http={status} data={dados}")

    _, resposta = reservar(url, "sala-fusca", h("16:00"), h("17:00"))
    resultado = resposta.get("result") or {}
    chave2 = next(iter(resultado.get("inputRequests") or {}), None)
    estado2 = resultado.get("requestState")
    if chave2 and estado2:
        _, resposta = retomar(url, "sala-fusca", h("16:00"), h("17:00"), chave2, {"action": "accept", "content": {"sala": "sala-garagem"}}, estado2)
        resultado = resposta.get("result") or {}
        dados = resultado.get("structuredContent") or {}
        R.check(16, "retry com inputResponses e requestState conclui a reserva", resultado.get("resultType") == "complete" and not resultado.get("isError") and dados.get("sala") == "sala-garagem", json.dumps(dados))
    else:
        R.check(16, "retry com inputResponses e requestState conclui a reserva", False, "conflito de 16h nao pediu input")

    if request_state and chave:
        adulterado = request_state[:-6] + ("AAAAAA" if not request_state.endswith("AAAAAA") else "BBBBBB")
        _, resposta = retomar(url, "sala-garagem", h("14:00"), h("15:00"), chave, {"action": "accept", "content": {"sala": "sala-fusca"}}, adulterado)
        R.check(17, "requestState adulterado e rejeitado com -32602", erro(resposta).get("code") == -32602, f"code={erro(resposta).get('code')} result={resposta.get('result')}")

        # O retry carrega argumentos adulterados. O que nao pode acontecer e eles
        # tomarem efeito: ou o servidor rejeita o estado, ou usa os valores selados.
        reservar(url, "sala-garagem", h("09:00"), h("10:00"), "Ocupante")
        _, resposta = reservar(url, "sala-garagem", h("09:00"), h("10:00"), "Doc")
        pedidos2 = (resposta.get("result") or {}).get("inputRequests") or {}
        chave3 = next(iter(pedidos2), None)
        estado3 = (resposta.get("result") or {}).get("requestState")
        if chave3 and estado3:
            _, resposta = retomar(url, "sala-mirante", h("13:00"), h("14:00"), chave3, {"action": "accept", "content": {"sala": "sala-fusca"}}, estado3, responsavel="Biff")
            dados = (resposta.get("result") or {}).get("structuredContent") or {}
            selado_venceu = dados.get("inicio") == h("09:00") and dados.get("responsavel") == "Doc"
            R.check(18, "argumentos adulterados no retry nao tomam efeito", bool(erro(resposta)) or selado_venceu, f"result={json.dumps(dados)}")
        else:
            R.check(18, "argumentos adulterados no retry nao tomam efeito", False, "o conflito das 9h nao pediu input")

        _, resposta = retomar(url, "sala-garagem", h("14:00"), h("15:00"), chave, {"action": "decline"}, request_state)
        resultado = resposta.get("result") or {}
        dados = resultado.get("structuredContent") or {}
        R.check(19, "recusa conclui sem reservar e sem isError", resultado.get("resultType") == "complete" and not resultado.get("isError") and dados.get("reservado") is False, json.dumps(dados) or texto(resultado))
    else:
        for n, d in ((17, "requestState adulterado e rejeitado com -32602"), (18, "requestState apresentado em outra tool e rejeitado"), (19, "recusa conclui sem reservar e sem isError")):
            R.check(n, d, False, "sem requestState do passo 13")

    reservar(url, "sala-mirante", h("11:00"), h("12:00"))
    _, resposta = reservar(url, "sala-mirante", h("11:00"), h("12:00"))
    resultado = resposta.get("result") or {}
    R.check(20, "conflito sem alternativa possivel devolve isError com a mensagem exata", resultado.get("isError") and ERRO_SEM_ALTERNATIVA in texto(resultado), texto(resultado) or json.dumps(resultado)[:200])
    return request_state or ""


def grafia_card(card: dict, interfaces: list) -> str:
    pistas = []
    if "interfaces" in card and not interfaces:
        pistas.append("nao existe campo interfaces na v1.0: o nome e supportedInterfaces")
    if ("preferredTransport" in card or "additionalInterfaces" in card) and not interfaces:
        pistas.append("preferredTransport e additionalInterfaces sao da v0.x: na v1.0 url, transporte e versao vivem dentro de supportedInterfaces[]")
    if any("preferredTransport" in i for i in interfaces):
        pistas.append("dentro de supportedInterfaces o campo do transporte e protocolBinding, nao preferredTransport")
    rotulo, alvo = ("supportedInterfaces", interfaces) if interfaces else ("card", card)
    return "; ".join(pistas + [f"{rotulo}={json.dumps(alvo)[:200]}"])


def verificar_a2a(url_base: str, url_rpc: str, request_state: str) -> None:
    try:
        with urllib.request.urlopen(f"{url_base}/.well-known/agent-card.json", timeout=15) as r:
            bruto = r.read().decode()
            status = r.status
    except Exception as e:
        R.check(21, "agent card responde no well-known", False, str(e))
        for n in range(22, 37):
            R.check(n, "dependente do agent card", False, "card indisponivel")
        return
    try:
        card = json.loads(bruto)
    except json.JSONDecodeError:
        card = {}
    R.check(21, "agent card responde 200 no well-known com JSON", status == 200 and bool(card), f"http={status}")

    interfaces = [i for i in (card.get("supportedInterfaces") or []) if isinstance(i, dict)]
    jsonrpc = [i for i in interfaces if str(i.get("protocolBinding", "")).upper().startswith("JSONRPC")]
    R.check(22, "o card declara a interface JSON-RPC com url e versao 1.0", bool(jsonrpc) and any(i.get("url") for i in jsonrpc) and any(str(i.get("protocolVersion", "")).startswith("1.0") for i in jsonrpc), grafia_card(card, interfaces))

    skills = {s.get("id") for s in card.get("skills", [])}
    R.check(23, "o card declara a skill reservar-sala", "reservar-sala" in skills, f"skills={sorted(x for x in skills if x)}")

    resposta = enviar(url_rpc, f"reservar sala=sala-porao inicio={h('09:00')} fim={h('10:00')} responsavel=Doc", traceparent=TRACEPARENT)
    R.check(24, "SendMessage com sala livre conclui a Task", estado(resposta) == "TASK_STATE_COMPLETED", f"estado={estado(resposta)} msg={mensagem(resposta)}")
    tarefa = tarefa_de(resposta)
    artifacts = tarefa.get("artifacts") or []
    artifact = artifacts[0] if artifacts else {}
    conteudo = {}
    if artifact:
        try:
            conteudo = json.loads(" ".join(p.get("text", "") for p in artifact.get("parts", [])))
        except json.JSONDecodeError:
            conteudo = {}
    R.check(25, "o artifact chama reserva e traz a versao da politica", artifact.get("name") == "reserva" and conteudo.get("politica") == POLITICA and conteudo.get("sala") == "sala-porao", json.dumps(artifact)[:200])

    resposta_get = a2a(url_rpc, "GetTask", {"id": tarefa.get("id")})
    t = tarefa_de(resposta_get)
    R.check(26, "GetTask devolve id, contextId e estado corrente", t.get("id") == tarefa.get("id") and bool(t.get("contextId")) and (t.get("status") or {}).get("state") == "TASK_STATE_COMPLETED", json.dumps(t)[:200])

    resposta = enviar(url_rpc, f"reservar sala=sala-garagem inicio={h('14:00')} fim={h('15:00')} responsavel=Marty", traceparent=TRACEPARENT)
    pausada = tarefa_de(resposta)
    R.check(27, "SendMessage com sala ocupada pausa a Task", estado(resposta) == "TASK_STATE_INPUT_REQUIRED", f"estado={estado(resposta)} msg={mensagem(resposta)}")
    R.check(28, "a Task pausada lista as alternativas na ordem certa", "alternativas: sala-fusca, sala-mirante" in mensagem(resposta), mensagem(resposta))

    resposta = enviar(url_rpc, "escolha=sala-aquario", task_id=pausada.get("id"))
    R.check(29, "escolha fora do enum mantem a Task pausada", estado(resposta) == "TASK_STATE_INPUT_REQUIRED", f"estado={estado(resposta)}")

    resposta = enviar(url_rpc, "escolha=sala-fusca", task_id=pausada.get("id"), traceparent=TRACEPARENT)
    tarefa = tarefa_de(resposta)
    artifacts = tarefa.get("artifacts") or []
    conteudo = {}
    if artifacts:
        try:
            conteudo = json.loads(" ".join(p.get("text", "") for p in artifacts[0].get("parts", [])))
        except json.JSONDecodeError:
            conteudo = {}
    R.check(30, "a continuacao conclui a Task na sala escolhida", estado(resposta) == "TASK_STATE_COMPLETED" and conteudo.get("sala") == "sala-fusca", f"estado={estado(resposta)} artifact={json.dumps(conteudo)}")

    resposta = enviar(url_rpc, "escolha=sala-mirante", task_id=pausada.get("id"))
    R.check(31, "SendMessage em Task terminal e recusado", bool(resposta.get("error")), json.dumps(resposta)[:200])

    resposta = enviar(url_rpc, f"reservar sala=sala-garagem inicio={h('14:30')} fim={h('15:30')} responsavel=Biff")
    recusavel = tarefa_de(resposta)
    resposta = enviar(url_rpc, "escolha=recusar", task_id=recusavel.get("id"))
    R.check(32, "a recusa termina a Task em CANCELED", estado(resposta) == "TASK_STATE_CANCELED", f"estado={estado(resposta)} msg={mensagem(resposta)}")

    a = tarefa_de(enviar(url_rpc, f"reservar sala=sala-fusca inicio={h('16:00')} fim={h('17:00')} responsavel=Lorraine"))
    b = tarefa_de(enviar(url_rpc, f"reservar sala=sala-garagem inicio={h('14:00')} fim={h('15:00')} responsavel=George"))
    pausadas = (a.get("status", {}).get("state") == "TASK_STATE_INPUT_REQUIRED" and b.get("status", {}).get("state") == "TASK_STATE_INPUT_REQUIRED")
    resp_a = enviar(url_rpc, "escolha=sala-mirante", task_id=a.get("id"))
    resp_b = enviar(url_rpc, "escolha=sala-mirante", task_id=b.get("id"))

    def reserva_de(resposta: dict) -> dict:
        arts = tarefa_de(resposta).get("artifacts") or []
        if not arts:
            return {}
        try:
            return json.loads(" ".join(p.get("text", "") for p in arts[0].get("parts", [])))
        except json.JSONDecodeError:
            return {}

    ra, rb = reserva_de(resp_a), reserva_de(resp_b)
    R.check(33, "duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva", pausadas and estado(resp_a) == "TASK_STATE_COMPLETED" and estado(resp_b) == "TASK_STATE_COMPLETED" and ra.get("reserva") != rb.get("reserva") and ra.get("inicio") != rb.get("inicio"), f"a={json.dumps(ra)} b={json.dumps(rb)}")

    vazou = bool(request_state) and any(request_state[:40] in corpo for corpo in R.corpos_a2a)
    R.check(34, "nenhuma resposta A2A carrega o requestState", not vazou, "o requestState apareceu em uma resposta do agente")

    resposta = enviar(url_rpc, f"reservar sala=sala-delorean inicio={h('09:00')} fim={h('10:00')} responsavel=Doc")
    R.check(35, "sala inexistente termina a Task em FAILED com a mensagem da tool", estado(resposta) == "TASK_STATE_FAILED" and ERRO_SALA in mensagem(resposta), f"estado={estado(resposta)} msg={mensagem(resposta)}")

    # sala-porao das 9h as 10h foi reservada na verificacao 24, entao este pedido
    # conflita e ainda tem alternativas, e nenhuma das duas chamadas escreve nada.
    pedido_repetido = f"reservar sala=sala-porao inicio={h('09:00')} fim={h('10:00')} responsavel=Doc"
    m1 = mensagem(enviar(url_rpc, pedido_repetido))
    m2 = mensagem(enviar(url_rpc, pedido_repetido))
    R.check(36, "o agente e deterministico: o mesmo pedido produz a mesma pausa", m1 == m2 and m1.startswith("alternativas:"), f"1={m1} 2={m2}")


def main() -> int:
    p = argparse.ArgumentParser(description="Valida a entrega do desafio A Ponte")
    p.add_argument("--agente", default="http://localhost:7300")
    p.add_argument("--mcp", default="http://localhost:7301")
    p.add_argument("--caminho-mcp", default="/mcp", help="caminho do endpoint Streamable HTTP")
    p.add_argument("--caminho-a2a", default="/a2a", help="caminho do endpoint JSON-RPC do agente")
    args = p.parse_args()

    print(f"trace-id desta execucao: {TRACE_ID}")
    print("procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.")
    print()
    request_state = verificar_mcp(args.mcp.rstrip("/") + args.caminho_mcp)
    print()
    verificar_a2a(args.agente.rstrip("/"), args.agente.rstrip("/") + args.caminho_a2a, request_state)
    print()
    print(f"resumo: {R.passou} passaram, {R.falhou} falharam, de {R.passou + R.falhou} verificacoes")
    return 0 if R.falhou == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
