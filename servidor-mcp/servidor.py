#!/usr/bin/env python3
"""Servidor MCP (Streamable HTTP) da Central de Salas.

Endpoint unico em /mcp, porta 7301 por padrao (MCP_PORT). Sem sessao: cada
request carrega sua propria versao de protocolo e capabilities em _meta, e o
servidor nunca infere nada de um request anterior.

MRTR (Multi-Round Tool Response) na tool reservar_sala: quando o intervalo
pedido conflita, a tool nao tenta abrir um canal de volta para o cliente (o
transporte stateless nao tem um) - ela termina a resposta com
resultType=input_required, oferecendo uma elicitation em form mode e um
requestState opaco e assinado (ver estado_requisicao.py). O cliente volta com
um novo tools/call, id novo, levando inputResponses + requestState.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime

import dominio
import estado_requisicao as estado

PROTOCOLO = "2026-07-28"
CHAVE_ELICITATION = "central-de-salas:escolha_de_sala"

META_PROTOCOLO = "io.modelcontextprotocol/protocolVersion"
META_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"

SERVER_INFO = {"name": "central-de-salas", "version": "1.0.0"}


def _segredo() -> bytes:
    valor = os.environ.get("REQUEST_STATE_SECRET", "")
    if len(valor) < 32:
        sys.stderr.write(
            "ERRO FATAL: variavel de ambiente REQUEST_STATE_SECRET ausente ou curta demais.\n"
            "Gere uma com: python3 -c \"import secrets; print(secrets.token_hex(32))\"\n"
            "e exporte antes de subir o servidor: export REQUEST_STATE_SECRET=<valor gerado>\n"
        )
        raise SystemExit(1)
    return valor.encode("utf-8")


SEGREDO = None  # preenchido em main(), lido uma vez


CENTRAL = dominio.CentralDeSalas()

# jti (identificador unico) de cada requestState ja redimido (accept ou decline),
# para recusar um segundo uso do mesmo token: sem isso, um requestState valido
# (assinatura e TTL corretos) poderia ser reenviado varias vezes e produzir
# varias reservas a partir de um unico conflito. Em memoria, protegido por lock
# porque ThreadingHTTPServer atende cada conexao numa thread propria.
_JTIS_CONSUMIDOS: set[str] = set()
_LOCK_JTIS = threading.Lock()


def _redimir_jti(jti: str) -> None:
    """Marca um requestState como usado; levanta ErroProtocolo se ja tinha sido."""
    with _LOCK_JTIS:
        if jti in _JTIS_CONSUMIDOS:
            raise ErroProtocolo(-32602, "requestState ja foi utilizado (replay detectado)")
        _JTIS_CONSUMIDOS.add(jti)


# ---------------------------------------------------------------------------
# Schemas das tools
# ---------------------------------------------------------------------------

def _schema_listar_salas() -> dict:
    return {"type": "object", "properties": {}, "title": "listar_salasArguments"}


def _outputschema_listar_salas() -> dict:
    return {
        "type": "object",
        "title": "ListaDeSalas",
        "properties": {
            "salas": {
                "type": "array",
                "title": "Salas",
                "items": {
                    "type": "object",
                    "title": "SalaOut",
                    "properties": {
                        "id": {"type": "string", "title": "Id"},
                        "nome": {"type": "string", "title": "Nome"},
                        "capacidade": {"type": "integer", "title": "Capacidade"},
                        "recursos": {"type": "array", "title": "Recursos", "items": {"type": "string"}},
                    },
                    "required": ["id", "nome", "capacidade", "recursos"],
                },
            }
        },
        "required": ["salas"],
    }


def _schema_consultar_disponibilidade() -> dict:
    return {
        "type": "object",
        "title": "consultar_disponibilidadeArguments",
        "properties": {
            "sala": {"type": "string", "title": "Sala"},
            "inicio": {"type": "string", "title": "Inicio"},
            "fim": {"type": "string", "title": "Fim"},
        },
        "required": ["sala", "inicio", "fim"],
    }


def _outputschema_consultar_disponibilidade() -> dict:
    return {
        "type": "object",
        "title": "Disponibilidade",
        "properties": {
            "sala": {"type": "string", "title": "Sala"},
            "livre": {"type": "boolean", "title": "Livre"},
            "conflitos": {
                "type": "array",
                "title": "Conflitos",
                "items": {
                    "type": "object",
                    "title": "ConflitoOut",
                    "properties": {
                        "id": {"type": "string", "title": "Id"},
                        "inicio": {"type": "string", "title": "Inicio"},
                        "fim": {"type": "string", "title": "Fim"},
                        "responsavel": {"type": "string", "title": "Responsavel"},
                    },
                    "required": ["id", "inicio", "fim", "responsavel"],
                },
            },
        },
        "required": ["sala", "livre", "conflitos"],
    }


def _schema_reservar_sala() -> dict:
    return {
        "type": "object",
        "title": "reservar_salaArguments",
        "properties": {
            "sala": {"type": "string", "title": "Sala"},
            "inicio": {"type": "string", "title": "Inicio"},
            "fim": {"type": "string", "title": "Fim"},
            "responsavel": {"type": "string", "title": "Responsavel"},
        },
        "required": ["sala", "inicio", "fim", "responsavel"],
    }


def _outputschema_reservar_sala() -> dict:
    campo_opcional = lambda t, titulo: {"anyOf": [{"type": t}, {"type": "null"}], "default": None, "title": titulo}
    return {
        "type": "object",
        "title": "ReservaOut",
        "properties": {
            "reserva": campo_opcional("string", "Reserva"),
            "reservado": {"type": "boolean", "default": True, "title": "Reservado"},
            "sala": campo_opcional("string", "Sala"),
            "inicio": campo_opcional("string", "Inicio"),
            "fim": campo_opcional("string", "Fim"),
            "responsavel": campo_opcional("string", "Responsavel"),
            "politica": campo_opcional("string", "Politica"),
            "motivo": campo_opcional("string", "Motivo"),
        },
    }


def _tools() -> list[dict]:
    return [
        {
            "name": "listar_salas",
            "description": "Lista todas as salas com capacidade e recursos.",
            "inputSchema": _schema_listar_salas(),
            "outputSchema": _outputschema_listar_salas(),
        },
        {
            "name": "consultar_disponibilidade",
            "description": "Diz se uma sala esta livre no intervalo, e quais reservas conflitam.",
            "inputSchema": _schema_consultar_disponibilidade(),
            "outputSchema": _outputschema_consultar_disponibilidade(),
        },
        {
            "name": "reservar_sala",
            "description": "Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.",
            "inputSchema": _schema_reservar_sala(),
            "outputSchema": _outputschema_reservar_sala(),
        },
    ]


# ---------------------------------------------------------------------------
# Erros JSON-RPC
# ---------------------------------------------------------------------------

class ErroProtocolo(Exception):
    def __init__(self, code: int, message: str, data: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _resultado_erro_execucao(mensagem: str) -> dict:
    return {
        "content": [{"type": "text", "text": mensagem}],
        "isError": True,
        "resultType": "complete",
        "_meta": {"io.modelcontextprotocol/serverInfo": SERVER_INFO},
    }


def _resultado_ok(structured: dict) -> dict:
    texto = json.dumps(structured, indent=2, ensure_ascii=False)
    return {
        "content": [{"type": "text", "text": texto}],
        "isError": False,
        "resultType": "complete",
        "structuredContent": structured,
        "_meta": {"io.modelcontextprotocol/serverInfo": SERVER_INFO},
    }


# ---------------------------------------------------------------------------
# Handlers de tools
# ---------------------------------------------------------------------------

def _handle_listar_salas() -> dict:
    structured = {"salas": CENTRAL.listar_salas()}
    texto = json.dumps(structured, ensure_ascii=False)
    return {
        "content": [{"type": "text", "text": texto}],
        "isError": False,
        "resultType": "complete",
        "structuredContent": structured,
        "_meta": {"io.modelcontextprotocol/serverInfo": SERVER_INFO},
    }


def _handle_consultar_disponibilidade(args: dict) -> dict:
    try:
        inicio = dominio.parse_iso(args["inicio"])
        fim = dominio.parse_iso(args["fim"])
        resultado = CENTRAL.consultar_disponibilidade(args["sala"], inicio, fim)
    except dominio.ErroDeDominio as e:
        return _resultado_erro_execucao(str(e))
    return _resultado_ok(resultado)


def _requested_schema(alternativas: list[str]) -> dict:
    campo: dict = {"type": "string", "title": "Sala", "description": "Sala alternativa escolhida"}
    if len(alternativas) == 1:
        campo["const"] = alternativas[0]
    else:
        campo["enum"] = alternativas
    return {"type": "object", "properties": {"sala": campo}, "required": ["sala"]}


def _capability_form_ok(capabilities: dict) -> bool:
    elic = (capabilities or {}).get("elicitation")
    return isinstance(elic, dict) and "form" in elic


def _handle_reservar_sala_inicial(args: dict, capabilities: dict) -> dict:
    sala, inicio_s, fim_s, responsavel = args["sala"], args["inicio"], args["fim"], args["responsavel"]
    try:
        inicio = dominio.parse_iso(inicio_s)
        fim = dominio.parse_iso(fim_s)
        tipo, valor = CENTRAL.reservar_ou_pedir_alternativa(sala, inicio, fim, responsavel)
    except dominio.ErroDeDominio as e:
        return _resultado_erro_execucao(str(e))

    if tipo == "reservada":
        reserva = valor
        structured = {
            "reserva": reserva.id,
            "reservado": True,
            "sala": reserva.sala,
            "inicio": reserva.inicio.isoformat(),
            "fim": reserva.fim.isoformat(),
            "responsavel": reserva.responsavel,
            "politica": CENTRAL.politica_versao,
            "motivo": None,
        }
        return _resultado_ok(structured)

    alternativas = valor
    if not _capability_form_ok(capabilities):
        raise ErroProtocolo(
            -32021,
            f"Client did not declare the form elicitation capability required by resolver '{CHAVE_ELICITATION}'",
            {"requiredCapabilities": {"elicitation": {"form": {}}}},
        )

    payload = {
        "tool": "reservar_sala",
        "sala": sala,
        "inicio": inicio_s,
        "fim": fim_s,
        "responsavel": responsavel,
        "key": CHAVE_ELICITATION,
        "alternativas": alternativas,
    }
    token = estado.selar(payload, SEGREDO)
    return {
        "inputRequests": {
            CHAVE_ELICITATION: {
                "method": "elicitation/create",
                "params": {
                    "message": "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.",
                    "mode": "form",
                    "requestedSchema": _requested_schema(alternativas),
                },
            }
        },
        "requestState": token,
        "resultType": "input_required",
        "_meta": {"io.modelcontextprotocol/serverInfo": SERVER_INFO},
    }


def _handle_reservar_sala_retry(params: dict) -> dict:
    token = params.get("requestState", "")
    try:
        payload = estado.abrir(token, SEGREDO)
    except estado.EstadoInvalido as e:
        raise ErroProtocolo(-32602, f"requestState invalido: {e}") from e

    # Token criptograficamente valido: agora redime o jti. Um segundo tools/call
    # com este mesmo requestState (replay) e recusado a partir daqui, mesmo que
    # a assinatura e o TTL continuem validos - previne dupla reserva a partir de
    # um unico conflito.
    _redimir_jti(payload.get("jti", token))

    chave = payload.get("key", CHAVE_ELICITATION)
    respostas = params.get("inputResponses") or {}
    resposta = respostas.get(chave) or next(iter(respostas.values()), None)
    if not isinstance(resposta, dict):
        raise ErroProtocolo(-32602, "inputResponses nao contem uma resposta para a chave esperada")

    acao = resposta.get("action")
    if acao in ("decline", "cancel"):
        structured = {
            "reserva": None,
            "reservado": False,
            "sala": None,
            "inicio": None,
            "fim": None,
            "responsavel": None,
            "politica": None,
            "motivo": "recusado",
        }
        return _resultado_ok(structured)

    if acao != "accept":
        raise ErroProtocolo(-32602, f"action desconhecida em inputResponses: {acao!r}")

    escolhida = (resposta.get("content") or {}).get("sala")
    alternativas = payload.get("alternativas") or []
    if escolhida not in alternativas:
        raise ErroProtocolo(-32602, "sala escolhida nao esta entre as alternativas seladas no requestState")

    # Os argumentos que o cliente reenviou nao sao confiaveis: o pedido original
    # e reconstruido inteiramente a partir do que foi selado no requestState.
    inicio = dominio.parse_iso(payload["inicio"])
    fim = dominio.parse_iso(payload["fim"])
    responsavel = payload["responsavel"]

    try:
        reserva = CENTRAL.criar_reserva(escolhida, inicio, fim, responsavel)
    except dominio.ErroDeDominio as e:
        return _resultado_erro_execucao(str(e))

    structured = {
        "reserva": reserva.id,
        "reservado": True,
        "sala": reserva.sala,
        "inicio": reserva.inicio.isoformat(),
        "fim": reserva.fim.isoformat(),
        "responsavel": reserva.responsavel,
        "politica": CENTRAL.politica_versao,
        "motivo": None,
    }
    return _resultado_ok(structured)


def _handle_reservar_sala(params: dict, capabilities: dict) -> dict:
    args = params.get("arguments") or {}
    if params.get("requestState") or params.get("inputResponses"):
        return _handle_reservar_sala_retry(params)
    for campo in ("sala", "inicio", "fim", "responsavel"):
        if campo not in args:
            raise ErroProtocolo(-32602, f"argumento obrigatorio ausente: {campo}")
    return _handle_reservar_sala_inicial(args, capabilities)


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------

def _handle_resources_read(params: dict) -> dict:
    uri = params.get("uri")
    if uri != "politica://uso":
        raise ErroProtocolo(-32602, f"Resource nao encontrado: {uri}")
    return {
        "contents": [{"uri": uri, "mimeType": "text/markdown", "text": dominio.ler_politica()}],
        "resultType": "complete",
        "_meta": {"io.modelcontextprotocol/serverInfo": SERVER_INFO},
    }


# ---------------------------------------------------------------------------
# Dispatcher JSON-RPC
# ---------------------------------------------------------------------------

def _validar_meta(params: dict) -> dict:
    meta = params.get("_meta")
    if not isinstance(meta, dict) or META_PROTOCOLO not in meta or META_CAPABILITIES not in meta:
        raise ErroProtocolo(
            -32602,
            "_meta deve conter io.modelcontextprotocol/protocolVersion e io.modelcontextprotocol/clientCapabilities",
        )
    return meta


def _validar_headers(headers, metodo: str, params: dict, meta: dict) -> None:
    cabecalho_metodo = headers.get("Mcp-Method")
    if cabecalho_metodo is not None and cabecalho_metodo != metodo:
        raise ErroProtocolo(-32020, f"Mcp-Method ({cabecalho_metodo}) nao bate com method do corpo ({metodo})")
    cabecalho_versao = headers.get("MCP-Protocol-Version")
    if cabecalho_versao is not None and cabecalho_versao != meta.get(META_PROTOCOLO):
        raise ErroProtocolo(-32020, "MCP-Protocol-Version nao bate com io.modelcontextprotocol/protocolVersion")
    esperado_nome = None
    if metodo == "tools/call":
        esperado_nome = (params.get("name"))
    elif metodo == "resources/read":
        esperado_nome = params.get("uri")
    if esperado_nome is not None:
        cabecalho_nome = headers.get("Mcp-Name")
        if cabecalho_nome is not None and cabecalho_nome != esperado_nome:
            raise ErroProtocolo(-32020, f"Mcp-Name ({cabecalho_nome}) nao bate com o corpo ({esperado_nome})")


def despachar(corpo: dict, headers) -> tuple[int, dict]:
    rpc_id = corpo.get("id")
    metodo = corpo.get("method")
    params = corpo.get("params") or {}

    # Logado antes de qualquer validacao: um request rejeitado por _meta ausente
    # ou header divergente precisa aparecer no stderr tanto quanto um aceito -
    # e exatamente o tipo de request que mais se quer ver ao depurar a ponte.
    traceparent = (params.get("_meta") or {}).get("traceparent", "-")
    print(f"[mcp] method={metodo} id={rpc_id} traceparent={traceparent}", file=sys.stderr, flush=True)

    try:
        meta = _validar_meta(params)
        _validar_headers(headers, metodo, params, meta)

        if metodo == "tools/list":
            resultado = {
                "cacheScope": "private",
                "resultType": "complete",
                "tools": _tools(),
                "ttlMs": 0,
                "_meta": {"io.modelcontextprotocol/serverInfo": SERVER_INFO},
            }
            return 200, {"jsonrpc": "2.0", "id": rpc_id, "result": resultado}

        if metodo == "tools/call":
            nome = params.get("name")
            capabilities = meta.get(META_CAPABILITIES) or {}
            if nome == "listar_salas":
                resultado = _handle_listar_salas()
            elif nome == "consultar_disponibilidade":
                resultado = _handle_consultar_disponibilidade(params.get("arguments") or {})
            elif nome == "reservar_sala":
                resultado = _handle_reservar_sala(params, capabilities)
            else:
                raise ErroProtocolo(-32602, f"Tool desconhecida: {nome}")
            return 200, {"jsonrpc": "2.0", "id": rpc_id, "result": resultado}

        if metodo == "resources/read":
            resultado = _handle_resources_read(params)
            return 200, {"jsonrpc": "2.0", "id": rpc_id, "result": resultado}

        raise ErroProtocolo(-32601, f"Metodo desconhecido: {metodo}")

    except ErroProtocolo as e:
        erro = {"code": e.code, "message": e.message}
        if e.data is not None:
            erro["data"] = e.data
        return 400, {"jsonrpc": "2.0", "id": rpc_id, "error": erro}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # silencia o log de acesso padrao; stderr ja tem o log estruturado
        pass

    def do_POST(self):
        if self.path.rstrip("/") != "/mcp":
            self._responder(404, {"jsonrpc": "2.0", "id": None, "error": {"code": -32601, "message": "not found"}})
            return
        tamanho = int(self.headers.get("Content-Length", 0))
        bruto = self.rfile.read(tamanho) if tamanho else b"{}"
        try:
            corpo = json.loads(bruto)
        except json.JSONDecodeError:
            self._responder(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            return
        status, resposta = despachar(corpo, self.headers)
        self._responder(status, resposta)

    def do_GET(self):
        self._responder(404, {"error": "not found"})

    def _responder(self, status: int, corpo: dict) -> None:
        dados = json.dumps(corpo).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)


def main() -> None:
    global SEGREDO
    SEGREDO = _segredo()
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "7301"))
    servidor = ThreadingHTTPServer((host, port), Handler)
    print(f"[mcp] servidor Streamable HTTP em http://{host}:{port}/mcp (protocolo {PROTOCOLO})", file=sys.stderr, flush=True)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
