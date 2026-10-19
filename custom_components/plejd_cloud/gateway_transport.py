"""Plejd gateway (remote / cloud) WebSocket transport.

Connects to the Plejd remote-control WebSocket (``wss://ws-ie.api.plejd.cloud``),
relays plaintext mesh commands, and tracks output state from ``MeshStateReply``
reports. The cloud/gateway holds the site crypto key, so this path is unencrypted —
it reuses ``protocol.encode_command`` and the codec in ``gateway.py``. See
docs/gateway_protocol.md.

An initial snapshot is requested on connect; after that, every state change (our own
commands and physical/off-app changes) arrives as a ``mesh.out`` push and is decoded
like a BLE LastChanged broadcast — no polling.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable

import aiohttp

from . import gateway, protocol
from .protocol import Command, OutputState

_LOGGER = logging.getLogger(__name__)

# App-level keep-alive: ping the gateway and reconnect if it doesn't pong. This
# catches a hung gateway behind a still-open WebSocket (the WS heartbeat only
# detects a dead socket).
GATEWAY_PING_INTERVAL = 60.0
GATEWAY_PONG_TIMEOUT = 10.0

# How long to wait for the cloud's `published` echo before proceeding anyway.
# The app's own acked publishes round-tripped in ~40-640ms over the cloud relay
# (#70); a lost ack past this window means the socket is in trouble and the ping
# loop will reconnect, so falling through here is safe.
GATEWAY_PUBLISH_ACK_TIMEOUT = 2.0


class PlejdGatewayConnection:
    """WebSocket client to the Plejd remote-control gateway."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        site_id: str,
        resource_set_id: str,
        installation_id: str,
        get_token: Callable[[], Awaitable[str]],
        on_state: Callable[[], None],
        on_disconnect: Callable[[], None] | None = None,
        on_event: Callable[[Command], None] | None = None,
        ws_url: str = gateway.GATEWAY_WS_URL,
    ) -> None:
        self._session = session
        self._site_id = site_id
        self._resource_set_id = resource_set_id
        self._installation_id = installation_id
        self._get_token = get_token
        self._on_state = on_state
        self._on_disconnect = on_disconnect
        self._ws_url = ws_url
        # Non-output-state pushes (NotifyEvents faults, motion, button presses) - same
        # decoded Command the BLE path routes through PlejdCoordinator._on_event.
        self._on_event = on_event
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._state: dict[int, OutputState] = {}
        self._recv_task: asyncio.Task | None = None
        self._ping_task: asyncio.Task | None = None
        self._pong = False
        self._closing = False
        # Futures awaiting a `published` ack, keyed by the publish's `data` string —
        # the cloud echoes that same `data` back on the ack, so we correlate each ack
        # to its own publish by content, not by arrival order. A deque per key handles
        # the (rare) case of two identical commands in flight. Content-keying keeps a
        # late/timed-out/cancelled ack from ever unblocking a different write().
        self._ack_waiters: dict[str, deque[asyncio.Future[None]]] = {}

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    @property
    def state(self) -> dict[int, OutputState]:
        return dict(self._state)

    def state_for(self, address: int) -> OutputState | None:
        return self._state.get(address)

    def set_state(self, address: int, state: OutputState) -> None:
        """Locally record an output's state, e.g. an optimistic update after a local write."""
        self._state[address] = state

    async def connect(self) -> None:
        """Open the WebSocket, authenticate, subscribe, and request initial state."""
        # A reconnect reuses this instance; cancel any leftover loops first so a stale
        # ping task can't close the fresh socket, and release any writes still waiting
        # on an ack from the dropped socket.
        self._cancel_tasks()
        self._release_ack_waiters()
        token = await self._get_token()
        headers = {
            "Client-Type": "app",
            "Authorization": f"Bearer {token}",
            "Site-ID": self._site_id,
            "Resource-Set-ID": self._resource_set_id,
            "Client-ID": self._installation_id,
        }
        self._ws = await self._session.ws_connect(self._ws_url, headers=headers, heartbeat=30)
        self._closing = False
        await self._subscribe(gateway.TOPIC_CONTROL_OUT)
        await self._subscribe(gateway.TOPIC_MESH_OUT)
        await self.async_request_state()
        self._recv_task = asyncio.ensure_future(self._receive_loop())
        self._ping_task = asyncio.ensure_future(self._ping_loop())

    async def write(self, vector: bytes) -> None:
        """Publish a plaintext mesh command and await the gateway's `published` ack.

        The app sets ack=true on every mesh command; over the cloud relay that path
        reliably delivers each command (round-trip ~40-640ms) where our old
        fire-and-forget publishes were mostly dropped — the cause of the chunky
        hold-to-dim in #70, confirmed by an A/B against the live relay. Awaiting the
        ack also paces a rapid command stream to the relay's real round-trip instead
        of flooding it. On timeout, cancellation, or a dropped socket we proceed: the
        command already went out, and state still reconciles via the mesh.out pushes
        and the periodic snapshot.
        """
        message = {**gateway.build_mesh_publish(vector, ack=True), "topic": [gateway.TOPIC_MESH_IN]}
        data = message["data"]
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._ack_waiters.setdefault(data, deque()).append(waiter)
        try:
            await self._send(message)
            await asyncio.wait_for(waiter, GATEWAY_PUBLISH_ACK_TIMEOUT)
        except TimeoutError:
            _LOGGER.debug("Plejd gateway publish ack timed out; proceeding without it")
        finally:
            # Covers success (already dequeued), timeout, cancellation, and send failure.
            self._discard_ack_waiter(data, waiter)

    async def async_request_state(self) -> None:
        """Ask the gateway for a full mesh-state snapshot."""
        await self._publish_control({"controlType": "MeshStateRequest"})

    async def _publish_control(self, inner: dict) -> None:
        data = base64.b64encode(json.dumps(inner).encode()).decode()
        await self._send({"op": "publish", "data": data, "topic": [gateway.TOPIC_CONTROL_IN]})

    async def _subscribe(self, topic: str) -> None:
        await self._send({"op": "subscribe", "topic": [topic]})

    async def _send(self, message: dict) -> None:
        if self._ws is None:
            raise RuntimeError("not connected")
        await self._ws.send_str(json.dumps(message))

    def _handle_frame(self, raw: str) -> None:
        try:
            frame = json.loads(raw)
        except ValueError:
            return
        if not isinstance(frame, dict):
            return
        if frame.get("op") == "published" and frame.get("publisher") is True:
            # Our own acked publish echoed back: release the matching write() by its
            # `data`, then fall through — the echo also carries the command's {raw,index},
            # so it doubles as the state relay for our own change (as it did before acks).
            data = frame.get("data")
            if isinstance(data, str):
                self._resolve_ack_waiter(data)
        # Everything below decodes state: the echo above, a `published` WITHOUT a publisher
        # flag (the gateway's relay of an off-app change, docs/gateway_protocol.md), and
        # `update` pushes — all share the {raw,index} shape.
        if not isinstance(frame.get("data"), str):
            return
        try:
            inner = json.loads(base64.b64decode(frame["data"]))
        except (ValueError, TypeError):
            return
        if not isinstance(inner, dict):
            return
        control_type = inner.get("controlType")
        if control_type == gateway.CONTROL_TYPE_MESH_STATE:
            self._state.update(gateway.parse_mesh_state_report(inner))
            self._on_state()
        elif control_type == gateway.CONTROL_TYPE_PONG:
            self._pong = True
        elif isinstance(inner.get("raw"), str) and inner.get("index") is not None:
            self._handle_push(inner["raw"], inner["index"])

    def _handle_push(self, raw_pkt: str, index: object) -> None:
        # A mesh.out update/push: a LastChanged Datavector relayed as {raw, index}.
        # Carries every command type (state, button, motion, NotifyEvents, ...), same
        # as BLE's LastChanged notifications - not just output state.
        try:
            vector = gateway.repackage_ws_to_command(base64.b64decode(raw_pkt), int(index))
            command = protocol.decode_command(vector)
        except (ValueError, TypeError):
            return
        state = protocol.decode_output_state(command)
        if state is not None:
            self._state[command.address] = state
        if self._on_event is not None:
            self._on_event(command)
        elif state is not None:
            self._on_state()

    async def _ping_loop(self) -> None:
        # Periodic app-level Ping; if the gateway misses a Pong, close to reconnect.
        # A failed send (a half-broken connection the receive loop hasn't noticed yet)
        # must be treated the same as a missed pong, not left to kill this loop silently.
        while not self._closing:
            await asyncio.sleep(GATEWAY_PING_INTERVAL)
            ws = self._ws  # tie this round to the socket we ping, not a later reconnect's
            if self._closing or ws is None or ws.closed:
                return
            self._pong = False
            try:
                await self._publish_control({"controlType": gateway.CONTROL_TYPE_PING})
                await asyncio.sleep(GATEWAY_PONG_TIMEOUT)
            except Exception:  # noqa: BLE001 - a failed ping is a missed pong, not a dead loop
                _LOGGER.warning("Plejd gateway ping failed, treating as a missed pong", exc_info=True)
            if not self._pong and not self._closing and self._ws is ws and not ws.closed:
                await ws.close()  # the receive loop will exit → owner reconnects
                return

    async def _receive_loop(self) -> None:
        # Any exit from this loop — clean close or an unexpected exception — must
        # notify the owner. Losing that notification silently means the coordinator
        # never learns the gateway dropped and never reconnects.
        assert self._ws is not None
        try:
            async for msg in self._ws:
                if msg.type is aiohttp.WSMsgType.TEXT:
                    self._handle_frame(msg.data)
        finally:
            if not self._closing and self._on_disconnect is not None:
                self._on_disconnect()

    def _resolve_ack_waiter(self, data: str) -> None:
        # Resolve the oldest pending write() for this exact publish; skip any already
        # timed out/cancelled. A stray ack with no matching publish is simply ignored.
        queue = self._ack_waiters.get(data)
        while queue:
            waiter = queue.popleft()
            if not queue:
                self._ack_waiters.pop(data, None)
            if not waiter.done():
                waiter.set_result(None)
                return

    def _discard_ack_waiter(self, data: str, waiter: asyncio.Future[None]) -> None:
        queue = self._ack_waiters.get(data)
        if queue is None:
            return  # already resolved+emptied, or never registered — nothing to do
        try:
            queue.remove(waiter)
        except ValueError:
            pass  # already dequeued by _resolve_ack_waiter
        if not queue:
            self._ack_waiters.pop(data, None)

    def _release_ack_waiters(self) -> None:
        # Unblock every in-flight write() so a dropped socket doesn't strand them
        # for the full ack timeout; their commands were already sent.
        for queue in self._ack_waiters.values():
            for waiter in queue:
                if not waiter.done():
                    waiter.set_result(None)
        self._ack_waiters.clear()

    def _cancel_tasks(self) -> None:
        for task in (self._recv_task, self._ping_task):
            if task is not None:
                task.cancel()
        self._recv_task = self._ping_task = None

    async def disconnect(self) -> None:
        self._closing = True
        self._cancel_tasks()
        self._release_ack_waiters()
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
