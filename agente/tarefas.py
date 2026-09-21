"""A Task do A2A: identidade, estado e produto, guardados em memoria.

O `requestState` do MCP mora aqui, amarrado a Task que o recebeu, e nunca sai
daqui para o cliente A2A: `para_wire` so serializa o que o protocolo A2A define.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field

SUBMITTED = "TASK_STATE_SUBMITTED"
WORKING = "TASK_STATE_WORKING"
INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
COMPLETED = "TASK_STATE_COMPLETED"
CANCELED = "TASK_STATE_CANCELED"
FAILED = "TASK_STATE_FAILED"

TERMINAIS = frozenset({COMPLETED, CANCELED, FAILED})


def _id(prefixo: str) -> str:
    return f"{prefixo}-{secrets.token_hex(6)}"


@dataclass
class Pausa:
    """O estado interrompido de uma Task: o que a ponte precisa para retomar.

    `request_state` e opaco para o agente. Guardar, ecoar, nunca abrir.
    """

    chave: str
    request_state: str
    alternativas: list[str]
    argumentos: dict


@dataclass
class Tarefa:
    id: str = field(default_factory=lambda: _id("task"))
    context_id: str = field(default_factory=lambda: _id("ctx"))
    estado: str = SUBMITTED
    historico: list[dict] = field(default_factory=list)
    artifacts: list[dict] = field(default_factory=list)
    status: dict | None = None
    pausa: Pausa | None = None
    traceparent: str | None = None

    @property
    def terminal(self) -> bool:
        return self.estado in TERMINAIS

    def do_usuario(self, mensagem: dict) -> None:
        """Guarda a mensagem do cliente no historico, na forma em que ela chegou."""
        self.historico.append(mensagem)

    def do_agente(self, texto: str) -> dict:
        """Registra a fala do agente no historico e a promove a mensagem de status."""
        mensagem = {
            "messageId": _id("msg"),
            "role": "ROLE_AGENT",
            "parts": [{"text": texto}],
            "taskId": self.id,
            "contextId": self.context_id,
        }
        self.historico.append(mensagem)
        self.status = mensagem
        return mensagem

    def anexar(self, nome: str, texto: str) -> None:
        self.artifacts.append(
            {
                "artifactId": _id("art"),
                "name": nome,
                "parts": [{"text": texto}],
            }
        )

    def para_wire(self) -> dict:
        status: dict = {"state": self.estado}
        if self.status is not None:
            status["message"] = self.status
        return {
            "id": self.id,
            "contextId": self.context_id,
            "status": status,
            "history": self.historico,
            "artifacts": self.artifacts,
        }


class Tarefas:
    """Repositorio em memoria. O estado pausado e por Task, nunca compartilhado."""

    def __init__(self) -> None:
        self._por_id: dict[str, Tarefa] = {}

    def nova(self) -> Tarefa:
        tarefa = Tarefa()
        self._por_id[tarefa.id] = tarefa
        return tarefa

    def buscar(self, task_id: str) -> Tarefa | None:
        return self._por_id.get(task_id)
