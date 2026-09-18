"""Cliente MCP puro (host): fala Streamable HTTP com o servidor MCP por fora do
processo, sem importar nenhuma funcao de tool. Cada chamada carrega seus proprios
_meta e headers - nao ha sessao, nada e inferido de uma chamada anterior."""

from __future__ import annotations

import json
import secrets
import urllib.error
import urllib.request

PROTOCOLO = "2026-07-28"
CLIENT_INFO = {"name": "agente-central-de-salas", "version": "1.0.0"}
CAPABILITIES_ELICITATION_FORM = {"elicitation": {"form": {}}}


class ErroMcp(Exception):
    """Erro de protocolo: o servidor MCP respondeu com um objeto `error` JSON-RPC."""

    def __init__(self, code: int, message: str, data: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data or {}


class ErroTransporteMcp(Exception):
    """Falha de transporte ao falar com o servidor MCP: conexao recusada, timeout,
    queda no meio do voo, ou corpo de resposta que nao e JSON valido. Distinto de
    ErroMcp porque aqui o servidor nao chegou a responder um JSON-RPC - nao ha
    `error` para interpretar, so a chamada que nao completou."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ClienteMcp:
    def __init__(self, url_base: str):
        self.url = url_base.rstrip("/")

    def _chamar(self, metodo: str, params: dict, nome: str | None, traceparent: str | None) -> dict:
        meta = {
            "io.modelcontextprotocol/protocolVersion": PROTOCOLO,
            "io.modelcontextprotocol/clientInfo": CLIENT_INFO,
            "io.modelcontextprotocol/clientCapabilities": CAPABILITIES_ELICITATION_FORM,
        }
        if traceparent:
            meta["traceparent"] = traceparent
        corpo = {
            "jsonrpc": "2.0",
            "id": secrets.token_hex(8),
            "method": metodo,
            "params": {**params, "_meta": meta},
        }
        cabecalhos = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOLO,
            "Mcp-Method": metodo,
        }
        if nome:
            cabecalhos["Mcp-Name"] = nome
        req = urllib.request.Request(
            f"{self.url}/mcp", data=json.dumps(corpo).encode("utf-8"), headers=cabecalhos, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                bruto = r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            bruto = e.read().decode("utf-8")
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            # Conexao recusada, servidor caiu no meio do voo, timeout de 30s etc.
            # Nunca deixamos essa excecao de urllib subir crua para o chamador A2A.
            raise ErroTransporteMcp(f"falha de transporte chamando {metodo} no servidor MCP: {e}") from e
        try:
            resposta = json.loads(bruto)
        except json.JSONDecodeError as e:
            raise ErroTransporteMcp(f"resposta do servidor MCP para {metodo} nao e JSON valido: {e}") from e
        if "error" in resposta:
            erro = resposta["error"]
            raise ErroMcp(erro.get("code", -32000), erro.get("message", ""), erro.get("data"))
        return resposta.get("result") or {}

    def tools_list(self) -> list[dict]:
        resultado = self._chamar("tools/list", {}, None, None)
        return resultado.get("tools", [])

    def resources_read(self, uri: str) -> dict:
        resultado = self._chamar("resources/read", {"uri": uri}, uri, None)
        conteudo = (resultado.get("contents") or [{}])[0]
        return conteudo

    def reservar_sala(self, sala: str, inicio: str, fim: str, responsavel: str, traceparent: str | None) -> dict:
        params = {"name": "reservar_sala", "arguments": {"sala": sala, "inicio": inicio, "fim": fim, "responsavel": responsavel}}
        return self._chamar("tools/call", params, "reservar_sala", traceparent)

    def retomar_reservar_sala(self, chave: str, resposta_elicitation: dict, request_state: str, traceparent: str | None) -> dict:
        params = {
            "name": "reservar_sala",
            "arguments": {},
            "inputResponses": {chave: resposta_elicitation},
            "requestState": request_state,
        }
        return self._chamar("tools/call", params, "reservar_sala", traceparent)
