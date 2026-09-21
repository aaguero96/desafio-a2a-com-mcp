"""O agente por dentro: um host MCP de verdade, falando HTTP com o servidor.

Nada de importar a funcao da tool: as capacidades sao descobertas em runtime por
`tools/list` e chamadas por `tools/call` no transporte Streamable HTTP.

O ponto que importa aqui e o `allow_input_required=True`. O cliente do SDK sabe
resolver o ciclo de MRTR sozinho, fechando a elicitation com um callback e
devolvendo so o resultado final. Se o agente usasse isso, a Task nunca pausaria e
metade do desafio evaporaria. Passando essa flag, o `input_required` chega cru,
e quem decide o que fazer com ele e a ponte.
"""

from __future__ import annotations

from contextlib import AsyncExitStack

import mcp_types as tipos
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

PROTOCOLO = "2026-07-28"
URI_DA_POLITICA = "politica://uso"
NOME_DA_TOOL = "reservar_sala"

IDENTIDADE = tipos.Implementation(name="agente-central-de-salas", version="1.0.0")


class SessaoDoAgente(ClientSession):
    """Sessao que declara exatamente `{"elicitation": {"form": {}}}`.

    Limitacao real do SDK, documentada no README: `ClientSession._build_capabilities`
    so anuncia elicitation quando ha um `elicitation_callback`, e nesse caso anuncia
    `form` e `url` juntos; o `_meta` do chamador e sobrescrito pelo stamp da sessao,
    entao nao ha como ajustar a chave por request. O enunciado pede a forma com
    `form` apenas, e e isso que este override produz.
    """

    def _build_capabilities(self, version: str) -> tipos.ClientCapabilities:
        return tipos.ClientCapabilities(
            elicitation=tipos.ElicitationCapability(form=tipos.FormElicitationCapability())
        )


def _discover_sintetico() -> tipos.DiscoverResult:
    """Adota a revisao moderna sem nenhum request de negociacao.

    Cada request ja carrega versao e capabilities no proprio `_meta`, entao nao ha
    nada que precise ser combinado antes: o objeto cliente fica vivo, o estado de
    protocolo nao.
    """
    return tipos.DiscoverResult(
        supported_versions=[PROTOCOLO],
        capabilities=tipos.ServerCapabilities(),
        result_type="complete",
        ttl_ms=0,
        cache_scope="public",
    )


class HostMCP:
    """Mantem o cliente MCP vivo e expoe as tres operacoes que a ponte usa."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._pilha: AsyncExitStack | None = None
        self._sessao: SessaoDoAgente | None = None
        self.tools: dict[str, tipos.Tool] = {}
        self.versao_da_politica: str | None = None

    @property
    def sessao(self) -> SessaoDoAgente:
        if self._sessao is None:
            raise RuntimeError("o host MCP ainda nao foi conectado")
        return self._sessao

    async def conectar(self) -> None:
        pilha = AsyncExitStack()
        leitura, escrita = await pilha.enter_async_context(streamable_http_client(self._url))
        sessao = await pilha.enter_async_context(SessaoDoAgente(leitura, escrita, client_info=IDENTIDADE))
        sessao.adopt(_discover_sintetico())
        self._pilha, self._sessao = pilha, sessao

    async def fechar(self) -> None:
        if self._pilha is not None:
            await self._pilha.aclose()
            self._pilha = None
            self._sessao = None

    async def descobrir(self, traceparent: str) -> None:
        """`tools/list` antes da primeira chamada, e a politica lida do resource.

        A lista de ferramentas nao esta escrita no codigo do agente: ela vem do
        servidor. A leitura do resource e escolha da aplicacao, nao do modelo.
        """
        if self.tools:
            return
        listagem = await self.sessao.list_tools(params=tipos.PaginatedRequestParams(_meta={"traceparent": traceparent}))
        self.tools = {t.name: t for t in listagem.tools}
        if NOME_DA_TOOL not in self.tools:
            raise RuntimeError(f"o servidor MCP nao expoe a tool {NOME_DA_TOOL!r}")
        leitura = await self.sessao.read_resource(URI_DA_POLITICA, meta={"traceparent": traceparent})
        texto = "".join(getattr(c, "text", "") for c in leitura.contents)
        primeira = texto.splitlines()[0]
        self.versao_da_politica = primeira.split(":", 1)[1].strip()

    async def reservar(self, argumentos: dict, traceparent: str):
        """O `tools/call` inicial. Pode voltar `complete` ou `input_required`."""
        return await self.sessao.call_tool(
            NOME_DA_TOOL,
            argumentos,
            meta={"traceparent": traceparent},
            allow_input_required=True,
        )

    async def retomar(self, argumentos: dict, chave: str, resposta: tipos.ElicitResult, estado: str, traceparent: str):
        """O retry: mesmo pedido, id de JSON-RPC novo, `inputResponses` e `requestState`.

        O id novo sai de graca porque a sessao numera cada request; sao requests
        independentes, e reaproveitar o id quebraria a verificacao.
        """
        return await self.sessao.call_tool(
            NOME_DA_TOOL,
            argumentos,
            input_responses={chave: resposta},
            request_state=estado,
            meta={"traceparent": traceparent},
            allow_input_required=True,
        )
