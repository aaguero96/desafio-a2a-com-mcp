"""Dominio da central de salas: dados, politica de uso e as regras de reserva.

Tudo o que o servidor MCP sabe sobre salas mora aqui. O agente nao conhece nada
disto: ele traduz protocolo, nao dominio.

As reservas vivem em memoria e nao sobrevivem a um restart, como o enunciado
permite. O que precisa sobreviver e o `requestState`, e ele viaja com o cliente.
"""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
DADOS = RAIZ / "dados"

FUSO = timezone(timedelta(hours=-3))
ABERTURA = time(8, 0)
FECHAMENTO = time(20, 0)
DURACAO_MAXIMA = timedelta(hours=2)
MAXIMO_DE_ALTERNATIVAS = 3

# Fonte unica de verdade das mensagens de erro de execucao. O validador compara
# o texto exato, entao estas constantes nao devem ser reescritas.
ERRO_SALA = "Sala inexistente: {sala}"
ERRO_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERRO_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERRO_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
ERRO_SEM_ALTERNATIVA = "Sem alternativas disponiveis no intervalo"


class ErroDeUso(Exception):
    """Violacao de regra de negocio: vira isError na resposta da tool."""


def _ler_json(nome: str) -> list[dict]:
    return json.loads((DADOS / nome).read_text(encoding="utf-8"))


SALAS: list[dict] = _ler_json("salas.json")
SALAS_POR_ID: dict[str, dict] = {s["id"]: s for s in SALAS}

_RESERVAS: list[dict] = _ler_json("reservas.json")


def texto_da_politica() -> str:
    """O conteudo cru de dados/politica-de-uso.md, que o resource expoe."""
    return (DADOS / "politica-de-uso.md").read_text(encoding="utf-8")


def versao_da_politica() -> str:
    """A versao declarada na primeira linha da politica (`versao: 2026-11-01`)."""
    primeira = texto_da_politica().splitlines()[0]
    return primeira.split(":", 1)[1].strip()


VERSAO_DA_POLITICA = versao_da_politica()


def _instante(valor: str, campo: str) -> datetime:
    try:
        momento = datetime.fromisoformat(valor)
    except ValueError:
        raise ErroDeUso(ERRO_INTERVALO) from None
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=FUSO)
    return momento.astimezone(FUSO)


def validar(sala: str, inicio: str, fim: str) -> tuple[datetime, datetime]:
    """Aplica sala, ordem do intervalo, janela de uso e duracao, nessa ordem.

    A ordem importa: um intervalo invertido dentro da janela tem que reportar o
    erro de intervalo, e um intervalo de tres horas dentro da janela tem que
    reportar o de duracao.
    """
    if sala not in SALAS_POR_ID:
        raise ErroDeUso(ERRO_SALA.format(sala=sala))
    comeco, termino = _instante(inicio, "inicio"), _instante(fim, "fim")
    if termino <= comeco:
        raise ErroDeUso(ERRO_INTERVALO)
    if comeco.timetz().replace(tzinfo=None) < ABERTURA or termino.timetz().replace(tzinfo=None) > FECHAMENTO:
        raise ErroDeUso(ERRO_JANELA)
    if termino - comeco > DURACAO_MAXIMA:
        raise ErroDeUso(ERRO_DURACAO)
    return comeco, termino


def _sobrepoe(reserva: dict, comeco: datetime, termino: datetime) -> bool:
    outro_comeco = datetime.fromisoformat(reserva["inicio"])
    outro_termino = datetime.fromisoformat(reserva["fim"])
    return outro_comeco < termino and comeco < outro_termino


def conflitos(sala: str, comeco: datetime, termino: datetime) -> list[dict]:
    """As reservas existentes da sala que colidem com o intervalo."""
    return [r for r in _RESERVAS if r["sala"] == sala and _sobrepoe(r, comeco, termino)]


def esta_livre(sala: str, comeco: datetime, termino: datetime) -> bool:
    return not conflitos(sala, comeco, termino)


def alternativas(sala: str, comeco: datetime, termino: datetime) -> list[str]:
    """Salas livres no intervalo com capacidade igual ou maior que a pedida.

    No maximo tres, ordenadas por capacidade crescente e, no empate, por id em
    ordem alfabetica. Esta e a regra que define o enum da elicitation.
    """
    capacidade_minima = SALAS_POR_ID[sala]["capacidade"]
    candidatas = [
        s
        for s in SALAS
        if s["id"] != sala and s["capacidade"] >= capacidade_minima and esta_livre(s["id"], comeco, termino)
    ]
    candidatas.sort(key=lambda s: (s["capacidade"], s["id"]))
    return [s["id"] for s in candidatas[:MAXIMO_DE_ALTERNATIVAS]]


def _proximo_id() -> str:
    numeros = [int(r["id"].split("-")[1]) for r in _RESERVAS if r["id"].startswith("res-")]
    return f"res-{max(numeros, default=0) + 1:04d}"


def criar_reserva(sala: str, comeco: datetime, termino: datetime, responsavel: str) -> dict:
    """Grava a reserva em memoria e devolve o registro criado."""
    reserva = {
        "id": _proximo_id(),
        "sala": sala,
        "inicio": comeco.isoformat(),
        "fim": termino.isoformat(),
        "responsavel": responsavel,
    }
    _RESERVAS.append(reserva)
    return reserva
