"""Dominio da Central de Salas: dados fixos, reservas em memoria e as regras da politica."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
DADOS = RAIZ / "dados"

FUSO_SP = timezone(timedelta(hours=-3))
JANELA_INICIO = time(8, 0)
JANELA_FIM = time(20, 0)
DURACAO_MAXIMA = timedelta(hours=2)

ERRO_SALA = "Sala inexistente: {sala}"
ERRO_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERRO_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERRO_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
ERRO_SEM_ALTERNATIVA = "Sem alternativas disponiveis no intervalo"


class ErroDeDominio(Exception):
    """Erro de execucao da tool: vira isError:true, nunca um erro de protocolo JSON-RPC."""


def _carregar_salas() -> list[dict]:
    with (DADOS / "salas.json").open(encoding="utf-8") as f:
        return json.load(f)


def _carregar_reservas_iniciais() -> list[dict]:
    with (DADOS / "reservas.json").open(encoding="utf-8") as f:
        return json.load(f)


def ler_politica() -> str:
    return (DADOS / "politica-de-uso.md").read_text(encoding="utf-8")


def versao_politica() -> str:
    primeira_linha = ler_politica().splitlines()[0]
    return primeira_linha.split(":", 1)[1].strip()


def parse_iso(valor: str) -> datetime:
    dt = datetime.fromisoformat(valor)
    if dt.tzinfo is None:
        raise ErroDeDominio(f"Data sem fuso horario: {valor}")
    return dt


@dataclass
class Sala:
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


@dataclass
class Reserva:
    id: str
    sala: str
    inicio: datetime
    fim: datetime
    responsavel: str

    def como_dict(self) -> dict:
        return {
            "id": self.id,
            "sala": self.sala,
            "inicio": self.inicio.isoformat(),
            "fim": self.fim.isoformat(),
            "responsavel": self.responsavel,
        }


class CentralDeSalas:
    """Estado em memoria do processo: reservas do dia a dia, salas fixas."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._salas: dict[str, Sala] = {
            s["id"]: Sala(s["id"], s["nome"], s["capacidade"], list(s["recursos"]))
            for s in _carregar_salas()
        }
        self._reservas: list[Reserva] = [
            Reserva(r["id"], r["sala"], parse_iso(r["inicio"]), parse_iso(r["fim"]), r["responsavel"])
            for r in _carregar_reservas_iniciais()
        ]
        self._proximo_id = 1 + max(
            (int(r.id.split("-")[1]) for r in self._reservas), default=0
        )
        self.politica_versao = versao_politica()

    def listar_salas(self) -> list[dict]:
        return [
            {"id": s.id, "nome": s.nome, "capacidade": s.capacidade, "recursos": list(s.recursos)}
            for s in self._salas.values()
        ]

    def sala_existe(self, sala_id: str) -> bool:
        return sala_id in self._salas

    def validar_janela_e_duracao(self, inicio: datetime, fim: datetime) -> None:
        if fim <= inicio:
            raise ErroDeDominio(ERRO_INTERVALO)
        inicio_sp = inicio.astimezone(FUSO_SP)
        fim_sp = fim.astimezone(FUSO_SP)
        if not (JANELA_INICIO <= inicio_sp.time() <= JANELA_FIM) or not (
            JANELA_INICIO <= fim_sp.time() <= JANELA_FIM
        ):
            raise ErroDeDominio(ERRO_JANELA)
        if fim - inicio > DURACAO_MAXIMA:
            raise ErroDeDominio(ERRO_DURACAO)

    def validar_pedido(self, sala_id: str, inicio: datetime, fim: datetime) -> None:
        if not self.sala_existe(sala_id):
            raise ErroDeDominio(ERRO_SALA.format(sala=sala_id))
        self.validar_janela_e_duracao(inicio, fim)

    def _conflitos(self, sala_id: str, inicio: datetime, fim: datetime) -> list[Reserva]:
        return [
            r
            for r in self._reservas
            if r.sala == sala_id and r.inicio < fim and inicio < r.fim
        ]

    def consultar_disponibilidade(self, sala_id: str, inicio: datetime, fim: datetime) -> dict:
        self.validar_pedido(sala_id, inicio, fim)
        with self._lock:
            conflitos = self._conflitos(sala_id, inicio, fim)
        return {
            "sala": sala_id,
            "livre": not conflitos,
            "conflitos": [
                {"id": c.id, "inicio": c.inicio.isoformat(), "fim": c.fim.isoformat(), "responsavel": c.responsavel}
                for c in conflitos
            ],
        }

    def alternativas_para(self, sala_id: str, inicio: datetime, fim: datetime) -> list[str]:
        capacidade_pedida = self._salas[sala_id].capacidade
        candidatas = []
        with self._lock:
            for s in self._salas.values():
                if s.id == sala_id or s.capacidade < capacidade_pedida:
                    continue
                if self._conflitos(s.id, inicio, fim):
                    continue
                candidatas.append(s)
        candidatas.sort(key=lambda s: (s.capacidade, s.id))
        return [s.id for s in candidatas[:3]]

    def tem_conflito(self, sala_id: str, inicio: datetime, fim: datetime) -> bool:
        with self._lock:
            return bool(self._conflitos(sala_id, inicio, fim))

    def criar_reserva(self, sala_id: str, inicio: datetime, fim: datetime, responsavel: str) -> Reserva:
        with self._lock:
            if self._conflitos(sala_id, inicio, fim):
                raise ErroDeDominio(ERRO_SEM_ALTERNATIVA)
            reserva = Reserva(f"res-{self._proximo_id:04d}", sala_id, inicio, fim, responsavel)
            self._proximo_id += 1
            self._reservas.append(reserva)
            return reserva

    def reservar_ou_pedir_alternativa(self, sala_id: str, inicio: datetime, fim: datetime, responsavel: str):
        """Retorna ("reservada", Reserva) ou ("conflito", [alternativas]) ou levanta ErroDeDominio."""
        self.validar_pedido(sala_id, inicio, fim)
        if not self.tem_conflito(sala_id, inicio, fim):
            return "reservada", self.criar_reserva(sala_id, inicio, fim, responsavel)
        alternativas = self.alternativas_para(sala_id, inicio, fim)
        if not alternativas:
            raise ErroDeDominio(ERRO_SEM_ALTERNATIVA)
        return "conflito", alternativas
