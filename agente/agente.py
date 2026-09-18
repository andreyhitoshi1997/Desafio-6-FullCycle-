#!/usr/bin/env python3
"""Agente Central de Salas.

Por dentro, e um host MCP: descobre as tools do servidor MCP via tools/list,
le o resource politica://uso, e chama reservar_sala como um cliente HTTP de
verdade (mcp_cliente.py), sem importar nenhuma funcao de tool.

Por fora, e um servidor A2A v1.0 (JSON-RPC sobre HTTP): publica um Agent Card,
aceita SendMessage e GetTask, e representa o pedido como uma Task com
identidade, estado e produto.

A PONTE fica em `_avancar_apos_chamada_mcp`: e ali que um resultType
input_required vindo do MCP vira TASK_STATE_INPUT_REQUIRED na Task, e e ali
que a continuacao do cliente A2A dispara o retry do tools/call original com
um id novo, levando o requestState de volta ao servidor MCP. O requestState
fica guardado em `Task.pausa`, nunca decodificado, nunca exposto em resposta
A2A nenhuma.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mcp_cliente import ClienteMcp, ErroMcp

AGENT_HOST = os.environ.get("AGENT_HOST", "0.0.0.0")
AGENT_PORT = int(os.environ.get("AGENT_PORT", "7300"))
AGENT_PUBLIC_URL = os.environ.get("AGENT_PUBLIC_URL", f"http://localhost:{AGENT_PORT}")
MCP_URL = os.environ.get("MCP_URL", "http://localhost:7301")

TERMINAIS = {"TASK_STATE_COMPLETED", "TASK_STATE_CANCELED", "TASK_STATE_FAILED"}

RE_RESERVAR = re.compile(
    r"^reservar\s+sala=(?P<sala>\S+)\s+inicio=(?P<inicio>\S+)\s+fim=(?P<fim>\S+)\s+responsavel=(?P<responsavel>\S+)\s*$"
)
RE_ESCOLHA = re.compile(r"^escolha=(?P<valor>\S+)\s*$")


# ---------------------------------------------------------------------------
# Estado das Tasks (em memoria, por processo)
# ---------------------------------------------------------------------------

class Pausa:
    __slots__ = ("chave", "request_state", "alternativas")

    def __init__(self, chave: str, request_state: str, alternativas: list[str]):
        self.chave = chave
        self.request_state = request_state
        self.alternativas = alternativas


class Task:
    def __init__(self, task_id: str, context_id: str):
        self.id = task_id
        self.contextId = context_id
        self.state = "TASK_STATE_SUBMITTED"
        self.status_message: dict | None = None
        self.history: list[dict] = []
        self.artifacts: list[dict] = []
        self.pausa: Pausa | None = None
        self.trace_id: str | None = None

    def registrar_trace(self, traceparent: str | None) -> None:
        if traceparent and self.trace_id is None:
            partes = traceparent.split("-")
            if len(partes) >= 2:
                self.trace_id = partes[1]

    def novo_traceparent(self) -> str:
        trace_id = self.trace_id or secrets.token_hex(16)
        return f"00-{trace_id}-{secrets.token_hex(8)}-01"

    def como_dict(self) -> dict:
        status: dict = {"state": self.state}
        if self.status_message:
            status["message"] = self.status_message
        return {
            "id": self.id,
            "contextId": self.contextId,
            "status": status,
            "history": self.history,
            "artifacts": self.artifacts,
        }


class Estado:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.tasks: dict[str, Task] = {}

    def criar_task(self) -> Task:
        with self._lock:
            t = Task(f"task-{uuid.uuid4().hex[:12]}", f"ctx-{uuid.uuid4().hex[:12]}")
            self.tasks[t.id] = t
            return t

    def obter(self, task_id: str) -> Task | None:
        with self._lock:
            return self.tasks.get(task_id)


ESTADO = Estado()
MCP: ClienteMcp | None = None
POLITICA_VERSAO: str | None = None


def _mensagem(role: str, texto: str, task_id: str | None = None, context_id: str | None = None) -> dict:
    msg = {"messageId": f"msg-{uuid.uuid4().hex[:12]}", "role": role, "parts": [{"text": texto}]}
    if task_id:
        msg["taskId"] = task_id
    if context_id:
        msg["contextId"] = context_id
    return msg


def _inicializar_host_mcp() -> None:
    """Descoberta em runtime: tools/list roda uma vez, antes de qualquer tools/call."""
    global POLITICA_VERSAO
    tools = MCP.tools_list()
    nomes = sorted(t.get("name") for t in tools)
    print(f"[agente] tools descobertas via tools/list: {nomes}", file=sys.stderr, flush=True)
    recurso = MCP.resources_read("politica://uso")
    primeira_linha = (recurso.get("text") or "").splitlines()[0] if recurso.get("text") else ""
    POLITICA_VERSAO = primeira_linha.split(":", 1)[1].strip() if ":" in primeira_linha else None
    print(f"[agente] politica de uso lida via resources/read, versao={POLITICA_VERSAO}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# A PONTE: MRTR (MCP) <-> Task (A2A)
# ---------------------------------------------------------------------------

def _artifact_de_reserva(structured: dict) -> dict:
    conteudo = {
        "reserva": structured.get("reserva"),
        "sala": structured.get("sala"),
        "inicio": structured.get("inicio"),
        "fim": structured.get("fim"),
        "responsavel": structured.get("responsavel"),
        "politica": structured.get("politica"),
    }
    return {
        "artifactId": f"art-{uuid.uuid4().hex[:12]}",
        "name": "reserva",
        "parts": [{"text": json.dumps(conteudo, ensure_ascii=False)}],
    }


def _texto_erro_tool(resultado: dict) -> str:
    return " ".join(p.get("text", "") for p in resultado.get("content", []))


def _avancar_apos_chamada_mcp(task: Task, resultado: dict) -> None:
    """O ponto exato da ponte: traduz o resultType do MCP para o estado da Task."""
    result_type = resultado.get("resultType")

    if result_type == "input_required":
        pedidos = resultado.get("inputRequests") or {}
        chave = next(iter(pedidos), None)
        params_pedido = (pedidos.get(chave) or {}).get("params", {}) if chave else {}
        campo = ((params_pedido.get("requestedSchema") or {}).get("properties") or {}).get("sala", {})
        alternativas = campo.get("enum") or ([campo["const"]] if "const" in campo else [])
        request_state = resultado.get("requestState")

        task.pausa = Pausa(chave or "", request_state or "", alternativas)
        task.state = "TASK_STATE_INPUT_REQUIRED"
        texto = f"alternativas: {', '.join(alternativas)}"
        msg = _mensagem("ROLE_AGENT", texto, task.id, task.contextId)
        task.status_message = msg
        task.history.append(msg)
        return

    if resultado.get("isError"):
        task.pausa = None
        task.state = "TASK_STATE_FAILED"
        texto = _texto_erro_tool(resultado)
        msg = _mensagem("ROLE_AGENT", texto, task.id, task.contextId)
        task.status_message = msg
        task.history.append(msg)
        return

    # resultType == "complete" e nao e erro: ou reservou, ou o usuario recusou (accept/decline).
    structured = resultado.get("structuredContent") or {}
    task.pausa = None
    if structured.get("reservado") is False:
        task.state = "TASK_STATE_CANCELED"
        texto = "Reserva recusada; nenhuma sala foi reservada."
    else:
        task.state = "TASK_STATE_COMPLETED"
        task.artifacts.append(_artifact_de_reserva(structured))
        texto = f"Reserva {structured.get('reserva')} confirmada na {structured.get('sala')}."
    msg = _mensagem("ROLE_AGENT", texto, task.id, task.contextId)
    task.status_message = msg
    task.history.append(msg)


def _iniciar_reserva(task: Task, pedido: dict, traceparent: str | None) -> None:
    task.state = "TASK_STATE_WORKING"
    try:
        resultado = MCP.reservar_sala(
            pedido["sala"], pedido["inicio"], pedido["fim"], pedido["responsavel"], task.novo_traceparent()
        )
    except ErroMcp as e:
        task.state = "TASK_STATE_FAILED"
        msg = _mensagem("ROLE_AGENT", f"Erro de protocolo MCP: {e.message}", task.id, task.contextId)
        task.status_message = msg
        task.history.append(msg)
        return
    _avancar_apos_chamada_mcp(task, resultado)


def _continuar_reserva(task: Task, escolha: str) -> None:
    pausa = task.pausa
    assert pausa is not None
    if escolha == "recusar":
        acao = {"action": "decline"}
    elif escolha in pausa.alternativas:
        acao = {"action": "accept", "content": {"sala": escolha}}
    else:
        # Escolha fora do enum: a Task permanece pausada, repetindo as alternativas.
        # Sem chamada ao MCP: e uma validacao de protocolo A2A, nao uma regra de dominio.
        texto = f"alternativas: {', '.join(pausa.alternativas)}"
        msg = _mensagem("ROLE_AGENT", texto, task.id, task.contextId)
        task.status_message = msg
        task.history.append(msg)
        return

    task.state = "TASK_STATE_WORKING"
    try:
        # Retry com id de JSON-RPC novo (garantido por ClienteMcp/secrets.token_hex a cada chamada).
        resultado = MCP.retomar_reservar_sala(pausa.chave, acao, pausa.request_state, task.novo_traceparent())
    except ErroMcp as e:
        task.state = "TASK_STATE_FAILED"
        msg = _mensagem("ROLE_AGENT", f"Erro de protocolo MCP: {e.message}", task.id, task.contextId)
        task.status_message = msg
        task.history.append(msg)
        return
    if escolha == "recusar":
        # action=decline conclui com resultType complete e reservado=false; a Task
        # em si termina em CANCELED (nao em COMPLETED), por decisao de produto do enunciado.
        task.pausa = None
        task.state = "TASK_STATE_CANCELED"
        msg = _mensagem("ROLE_AGENT", "Reserva recusada; nenhuma sala foi reservada.", task.id, task.contextId)
        task.status_message = msg
        task.history.append(msg)
        return
    _avancar_apos_chamada_mcp(task, resultado)


# ---------------------------------------------------------------------------
# A2A: SendMessage / GetTask
# ---------------------------------------------------------------------------

class ErroA2a(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _send_message(params: dict, traceparent: str | None) -> dict:
    mensagem = params.get("message") or {}
    texto = " ".join(p.get("text", "") for p in mensagem.get("parts", []))
    task_id = mensagem.get("taskId")

    if task_id:
        task = ESTADO.obter(task_id)
        if task is None:
            raise ErroA2a(-32001, f"Task nao encontrada: {task_id}")
        if task.state in TERMINAIS:
            raise ErroA2a(-32002, f"Task {task_id} ja esta em estado terminal ({task.state})")
        task.registrar_trace(traceparent)
        task.history.append(_mensagem("ROLE_USER", texto, task.id, task.contextId))

        casamento = RE_ESCOLHA.match(texto.strip())
        if not casamento or task.pausa is None:
            raise ErroA2a(-32003, "Continuacao esperada no formato escolha=<valor>")
        _continuar_reserva(task, casamento.group("valor"))
        return {"task": task.como_dict()}

    task = ESTADO.criar_task()
    task.registrar_trace(traceparent)
    task.history.append(_mensagem("ROLE_USER", texto, task.id, task.contextId))

    casamento = RE_RESERVAR.match(texto.strip())
    if not casamento:
        task.state = "TASK_STATE_FAILED"
        msg = _mensagem("ROLE_AGENT", "Pedido em formato invalido, esperado: reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>", task.id, task.contextId)
        task.status_message = msg
        task.history.append(msg)
        return {"task": task.como_dict()}

    pedido = casamento.groupdict()
    _iniciar_reserva(task, pedido, traceparent)
    return {"task": task.como_dict()}


def _get_task(params: dict) -> dict:
    task_id = params.get("id") or params.get("taskId")
    task = ESTADO.obter(task_id)
    if task is None:
        raise ErroA2a(-32001, f"Task nao encontrada: {task_id}")
    return {"task": task.como_dict()}


# ---------------------------------------------------------------------------
# Agent Card
# ---------------------------------------------------------------------------

def _agent_card() -> dict:
    return {
        "name": "Central de Salas",
        "description": "Reserva salas de reuniao da Hill Valley Tech.",
        "provider": {"organization": "Hill Valley Tech", "url": "https://hillvalley.example"},
        "version": "1.0.0",
        "supportedInterfaces": [
            {"url": f"{AGENT_PUBLIC_URL}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
        ],
        "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "reservar-sala",
                "name": "Reservar sala",
                "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
                "tags": ["salas", "agenda"],
                "inputModes": ["text/plain"],
                "outputModes": ["text/plain"],
                "examples": [
                    "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
                ],
            }
        ],
    }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path.rstrip("/") == "/.well-known/agent-card.json":
            self._responder(200, _agent_card())
            return
        self._responder(404, {"error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") != "/a2a":
            self._responder(404, {"jsonrpc": "2.0", "id": None, "error": {"code": -32601, "message": "not found"}})
            return
        tamanho = int(self.headers.get("Content-Length", 0))
        bruto = self.rfile.read(tamanho) if tamanho else b"{}"
        try:
            corpo = json.loads(bruto)
        except json.JSONDecodeError:
            self._responder(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            return

        rpc_id = corpo.get("id")
        metodo = corpo.get("method")
        params = corpo.get("params") or {}
        traceparent = self.headers.get("traceparent")

        try:
            if metodo == "SendMessage":
                resultado = _send_message(params, traceparent)
            elif metodo == "GetTask":
                resultado = _get_task(params)
            else:
                raise ErroA2a(-32601, f"Metodo desconhecido: {metodo}")
            self._responder(200, {"jsonrpc": "2.0", "id": rpc_id, "result": resultado})
        except ErroA2a as e:
            self._responder(200, {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": e.code, "message": e.message}})

    def _responder(self, status: int, corpo: dict) -> None:
        dados = json.dumps(corpo).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)


def main() -> None:
    global MCP
    MCP = ClienteMcp(MCP_URL)
    _inicializar_host_mcp()
    servidor = ThreadingHTTPServer((AGENT_HOST, AGENT_PORT), Handler)
    print(f"[agente] servidor A2A em http://{AGENT_HOST}:{AGENT_PORT}/a2a, card em /.well-known/agent-card.json", file=sys.stderr, flush=True)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
