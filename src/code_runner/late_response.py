"""Late response: ответ на запрос, от которого вторая сторона уже отказалась.

SHA-129. После TaskStop клиент шлёт `notifications/cancelled` и удаляет обработчик
этого id, а MCP SDK (1.26, `RequestResponder.cancel`) всё равно отвечает на него
ошибкой "Request cancelled". Клиент получает ответ на неизвестный id и рвёт канал:
`mcp__code-runner__*` пропадали до ручного reconnect. Спека MCP: получатель отмены
SHOULD NOT отвечать на отменённый запрос.

Фильтр стоит на потоках сессии, а не в недрах SDK: ему нужны только два публичных
факта протокола — id из `notifications/cancelled` и id исходящего ответа.

Обратное направление (демон как клиент downstream-сервера): per-call таймаут
бросает вызов, downstream отвечает позже. SDK отдаёт такой ответ message_handler'у
как исключение и по умолчанию молча глотает — здесь он становится warn в журнале.
"""

import logging

import anyio
from mcp.client.session import MessageHandlerFnT
from mcp.server.lowlevel import Server
from mcp.types import JSONRPCError, JSONRPCNotification, JSONRPCResponse, RequestId

logger = logging.getLogger(__name__)

_CANCELLED = "notifications/cancelled"


class _CancelWatchingReader:
    """Входящий поток сессии: запоминает id из notifications/cancelled."""

    def __init__(self, inner, cancelled: set[RequestId]):
        self._inner = inner
        self._cancelled = cancelled

    async def __aenter__(self):
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, *exc_info):
        return await self._inner.__aexit__(*exc_info)

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self._inner.__anext__()
        root = getattr(getattr(message, "message", None), "root", None)
        if isinstance(root, JSONRPCNotification) and root.method == _CANCELLED:
            request_id = (root.params or {}).get("requestId")
            if request_id is not None:
                self._cancelled.add(request_id)
        return message

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _CancelledResponseFilter:
    """Исходящий поток сессии: не пропускает ответы на отменённые id.

    id остаётся в множестве: после отмены клиенту не нужен ни "Request cancelled",
    ни запоздалый результат хэндлера. Множество живёт ровно столько, сколько
    сессия, и растёт только на отменах."""

    def __init__(self, inner, cancelled: set[RequestId]):
        self._inner = inner
        self._cancelled = cancelled

    async def __aenter__(self):
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, *exc_info):
        return await self._inner.__aexit__(*exc_info)

    async def send(self, message) -> None:
        root = message.message.root
        if isinstance(root, JSONRPCResponse | JSONRPCError) and root.id in self._cancelled:
            logger.warning("late_response: dropped reply to cancelled request id=%s", root.id)
            return
        await self._inner.send(message)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def drop_cancelled_responses(server: Server) -> None:
    """Каждая сессия `server.run` получает фильтр ответов на отменённые запросы."""
    run = server.run

    async def run_without_cancelled_replies(read_stream, write_stream, *args, **kwargs):
        cancelled: set[RequestId] = set()
        return await run(
            _CancelWatchingReader(read_stream, cancelled),
            _CancelledResponseFilter(write_stream, cancelled),
            *args,
            **kwargs,
        )

    server.run = run_without_cancelled_replies  # type: ignore[method-assign]


def late_response_logger(server_name: str) -> MessageHandlerFnT:
    """message_handler для ClientSession downstream-сервера."""

    async def handle(message) -> None:
        if isinstance(message, Exception) and "unknown request id" in str(message).lower():
            logger.warning("late_response: %s answered an abandoned call: %s", server_name, message)
        await anyio.lowlevel.checkpoint()

    return handle
