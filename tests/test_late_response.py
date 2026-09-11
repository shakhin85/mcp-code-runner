"""Late response: ответ на запрос, от которого вторая сторона уже отказалась.

SHA-129. Два направления, оба про один факт — «на брошенный id ответ не нужен»:

* upstream (демон как сервер): после TaskStop клиент шлёт notifications/cancelled,
  а SDK всё равно отвечает на отменённый id ошибкой "Request cancelled". Клиент
  уже удалил обработчик этого id → "unknown message ID" → канал рвётся,
  `mcp__code-runner__*` пропадают до ручного reconnect. Спека MCP: получатель
  отмены SHOULD NOT отвечать на отменённый запрос.
* downstream (демон как клиент): per-call таймаут бросает вызов, downstream
  отвечает позже на уже неизвестный id. Ответ отбрасывается с warn, сессия
  остаётся рабочей для следующего вызова.
"""

import asyncio
import logging

import anyio
from mcp.server.lowlevel import Server
from mcp.shared.message import SessionMessage
from mcp.types import (
    JSONRPCError,
    JSONRPCMessage,
    JSONRPCNotification,
    JSONRPCRequest,
    JSONRPCResponse,
)

from code_runner.client_pool import _new_session
from code_runner.late_response import drop_cancelled_responses

HANG_ID = 2
PING_ID = 3


def _request(request_id, method, params=None):
    return SessionMessage(
        message=JSONRPCMessage(
            JSONRPCRequest(jsonrpc="2.0", id=request_id, method=method, params=params)
        )
    )


def _cancelled(request_id):
    return SessionMessage(
        message=JSONRPCMessage(
            JSONRPCNotification(
                jsonrpc="2.0",
                method="notifications/cancelled",
                params={"requestId": request_id, "reason": "TaskStop"},
            )
        )
    )


async def _first_reply_after_cancel(*, filtered: bool):
    """Зависший tools/call → notifications/cancelled → ping.

    Возвращает первое, что сервер написал клиенту после отмены."""
    server = Server("late-response-test")
    started = anyio.Event()

    @server.call_tool(validate_input=False)
    async def hang(name, arguments):
        started.set()
        await anyio.sleep_forever()

    if filtered:
        drop_cancelled_responses(server)

    to_server, server_reads = anyio.create_memory_object_stream(16)
    server_writes, from_server = anyio.create_memory_object_stream(16)

    async with anyio.create_task_group() as tg:
        tg.start_soon(
            lambda: server.run(
                server_reads,
                server_writes,
                server.create_initialization_options(),
                stateless=True,
            )
        )
        with anyio.fail_after(5):
            await to_server.send(
                _request(HANG_ID, "tools/call", {"name": "hang", "arguments": {}})
            )
            # cancel() на ещё не вошедший в контекст responder — RuntimeError SDK,
            # поэтому отменяем только реально исполняющийся хэндлер.
            await started.wait()
            await to_server.send(_cancelled(HANG_ID))
            await to_server.send(_request(PING_ID, "ping"))
            reply = (await from_server.receive()).message.root
        tg.cancel_scope.cancel()
    return reply


def test_sdk_answers_cancelled_request_without_filter():
    """Предпосылка бага: голый SDK отвечает на отменённый id."""
    reply = asyncio.run(_first_reply_after_cancel(filtered=False))
    assert isinstance(reply, JSONRPCError)
    assert reply.id == HANG_ID


def test_late_response_to_cancelled_request_is_dropped(caplog):
    caplog.set_level(logging.WARNING, logger="code_runner.late_response")
    reply = asyncio.run(_first_reply_after_cancel(filtered=True))
    assert isinstance(reply, JSONRPCResponse), f"ответ на отменённый id ушёл клиенту: {reply}"
    assert reply.id == PING_ID, "транспорт жив: следующий запрос получил свой ответ"
    assert f"late_response: dropped reply to cancelled request id={HANG_ID}" in caplog.text


def test_daemon_server_runs_with_late_response_filter():
    """Боевой FastMCP (stdio и streamable-http идут через один _mcp_server.run)."""
    from code_runner.server import mcp

    assert mcp._mcp_server.run.__name__ == "run_without_cancelled_replies"


def test_downstream_late_response_on_unknown_id_keeps_session(caplog):
    """Per-call таймаут бросил запрос; downstream ответил позже на неизвестный id."""
    caplog.set_level(logging.WARNING, logger="code_runner.late_response")

    async def scenario():
        to_client, client_reads = anyio.create_memory_object_stream(16)
        client_writes, from_client = anyio.create_memory_object_stream(16)

        async def fake_downstream():
            abandoned = (await from_client.receive()).message.root
            answered = await from_client.receive()  # ретрай пошёл уже после таймаута
            for request in (abandoned, answered.message.root):
                await to_client.send(
                    SessionMessage(
                        message=JSONRPCMessage(
                            JSONRPCResponse(jsonrpc="2.0", id=request.id, result={})
                        )
                    )
                )

        async with anyio.create_task_group() as tg:
            tg.start_soon(fake_downstream)
            async with _new_session(client_reads, client_writes, "forgetful") as session:
                try:
                    async with asyncio.timeout(0.2):
                        await session.send_ping()
                except TimeoutError:
                    pass
                else:
                    raise AssertionError("первый ping не должен был дождаться ответа")
                with anyio.fail_after(5):
                    await session.send_ping()  # сессия жива после late response
            tg.cancel_scope.cancel()

    asyncio.run(scenario())
    assert "late_response: forgetful" in caplog.text
    assert "unknown request id" in caplog.text.lower()
