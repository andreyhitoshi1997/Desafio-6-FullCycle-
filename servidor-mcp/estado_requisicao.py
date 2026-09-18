"""requestState opaco e protegido por integridade (HMAC-SHA256).

O token carrega tudo que o servidor precisa para reconstruir o pedido original
de reserva. O servidor nao guarda nada em memoria entre o input_required e o
retry: toda a informacao viaja selada dentro do proprio token, e um retry
apresentado depois de o processo reiniciar continua valido, desde que
REQUEST_STATE_SECRET seja o mesmo.

Formato: "v1." + base64url(payload_json) + base64url(hmac_sha256)[43 chars fixos].
Qualquer byte adulterado no meio muda o HMAC recomputado e falha a verificacao
com hmac.compare_digest (comparacao em tempo constante).

Cada token carrega um "jti" (identificador unico, gerado em `selar`) que o
servidor usa para recusar um segundo redeem do mesmo requestState (replay):
ver o conjunto `_JTIS_CONSUMIDOS` em servidor.py. Esse rastreamento e em
memoria, pelo mesmo motivo que as reservas sao em memoria - o unico dado que
precisa sobreviver a um restart e a validade criptografica do token (assinatura
+ TTL), nao o registro de quais tokens ja foram gastos antes do restart.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

PREFIXO = "v1."
TAMANHO_ASSINATURA_B64 = 43  # base64url sem padding de um digest sha256 (32 bytes)
TTL_PADRAO_SEGUNDOS = 15 * 60  # 15 min, dentro da janela de 5 a 30 min exigida


class EstadoInvalido(Exception):
    """requestState ausente, adulterado, mal formado ou expirado."""


def _b64_sem_padding(dados: bytes) -> str:
    return base64.urlsafe_b64encode(dados).rstrip(b"=").decode("ascii")


def _b64_decode(txt: str) -> bytes:
    resto = len(txt) % 4
    if resto:
        txt += "=" * (4 - resto)
    return base64.urlsafe_b64decode(txt.encode("ascii"))


def selar(payload: dict, segredo: bytes, ttl_segundos: int = TTL_PADRAO_SEGUNDOS) -> str:
    corpo = dict(payload)
    corpo["iat"] = int(time.time())
    corpo["exp"] = corpo["iat"] + ttl_segundos
    # jti = identificador unico do token, usado pelo servidor para recusar um
    # segundo redeem do mesmo requestState (replay), mesmo com assinatura valida.
    corpo["jti"] = secrets.token_hex(16)
    payload_bruto = json.dumps(corpo, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_b64 = _b64_sem_padding(payload_bruto)
    assinatura = hmac.new(segredo, (PREFIXO + payload_b64).encode("ascii"), hashlib.sha256).digest()
    assinatura_b64 = _b64_sem_padding(assinatura)
    assert len(assinatura_b64) == TAMANHO_ASSINATURA_B64
    return PREFIXO + payload_b64 + assinatura_b64


def abrir(token: str, segredo: bytes) -> dict:
    if not isinstance(token, str) or not token.startswith(PREFIXO):
        raise EstadoInvalido("requestState sem o prefixo de versao esperado")
    corpo = token[len(PREFIXO):]
    if len(corpo) <= TAMANHO_ASSINATURA_B64:
        raise EstadoInvalido("requestState curto demais para conter payload e assinatura")
    payload_b64 = corpo[:-TAMANHO_ASSINATURA_B64]
    assinatura_b64 = corpo[-TAMANHO_ASSINATURA_B64:]
    try:
        assinatura_recebida = _b64_decode(assinatura_b64)
    except Exception as e:  # base64.binascii.Error, ValueError etc. entram aqui
        raise EstadoInvalido(f"assinatura do requestState mal formada: {e}") from e
    assinatura_esperada = hmac.new(segredo, (PREFIXO + payload_b64).encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(assinatura_recebida, assinatura_esperada):
        raise EstadoInvalido("assinatura do requestState nao confere: adulterado ou selado com outro segredo")
    try:
        payload_bruto = _b64_decode(payload_b64)
        payload = json.loads(payload_bruto)
    except Exception as e:
        raise EstadoInvalido(f"payload do requestState nao decodifica: {e}") from e
    if time.time() > payload.get("exp", 0):
        raise EstadoInvalido("requestState expirado")
    return payload
