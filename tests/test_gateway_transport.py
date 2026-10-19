"""Tests for the Plejd gateway WebSocket transport."""

from __future__ import annotations

import asyncio
import base64
import json
from collections import deque

import aiohttp
import pytest
from plejd_cloud import gateway
from plejd_cloud.gateway_transport import PlejdGatewayConnection
from plejd_cloud.protocol import OutputState, set_output_state_and_level


def _text(obj: dict):
    return type("Msg", (), {"type": aiohttp.WSMsgType.TEXT, "data": json.dumps(obj)})()


def _state_reply(**addrs: str) -> dict:
    inner = {"controlType": "MeshStateReply", **addrs}
    return {"topic": "control.out", "op": "update", "data": base64.b64encode(json.dumps(inner).encode()).decode()}


def _push_frame(address: int, vector: bytes) -> dict:
    inner = {"raw": base64.b64encode(gateway.repackage_command_to_ws(vector)).decode(), "index": address}
    return {"topic": "mesh.out", "op": "update", "data": base64.b64encode(json.dumps(inner).encode()).decode()}


class _FakeWS:
    def __init__(self, incoming=()):
        self.closed = False
        self.sent: list[str] = []
        self._incoming = list(incoming)

    async def send_str(self, s):
        self.sent.append(s)

    async def close(self):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._incoming:
            return self._incoming.pop(0)
        self.closed = True
        raise StopAsyncIteration


class _FakeSession:
    def __init__(self, ws=None, error=None):
        self._ws = ws
        self._error = error
        self.connect_kwargs = None

    async def ws_connect(self, url, **kwargs):
        if self._error is not None:
            raise self._error
        self.connect_kwargs = (url, kwargs)
        return self._ws


def _conn(ws, on_state=None, on_disconnect=None, on_event=None):
    async def _token():
        return "tok"

    return PlejdGatewayConnection(
        _FakeSession(ws), "S1", "rs1", "inst-1", _token, on_state or (lambda: None), on_disconnect, on_event=on_event
    )


def _sent_publishes(ws, topic):
    out = []
    for raw in ws.sent:
        msg = json.loads(raw)
        if msg.get("topic") == [topic]:
            out.append(msg)
    return out


def test_set_state_records_output_state():
    from plejd_cloud.protocol import OutputState

    conn = PlejdGatewayConnection(object(), "S1", "rs1", "inst-1", None, lambda: None)
    assert conn.state_for(11) is None
    conn.set_state(11, OutputState(output=11, on=True, level=200))
    assert conn.state_for(11).on is True and conn.state_for(11).level == 200


async def test_connect_handshake_and_headers():
    ws = _FakeWS()
    session = _FakeSession(ws)

    async def _token():
        return "tok"

    conn = PlejdGatewayConnection(session, "S1", "rs1", "inst-1", _token, lambda: None)
    await conn.connect()
    await conn.disconnect()
    _, kwargs = session.connect_kwargs
    headers = kwargs["headers"]
    assert headers["Authorization"] == "Bearer tok"
    assert headers["Site-ID"] == "S1" and headers["Resource-Set-ID"] == "rs1" and headers["Client-ID"] == "inst-1"
    # subscribed to both out topics and requested initial state
    subs = [json.loads(s) for s in ws.sent if json.loads(s).get("op") == "subscribe"]
    assert {tuple(s["topic"]) for s in subs} == {(gateway.TOPIC_CONTROL_OUT,), (gateway.TOPIC_MESH_OUT,)}
    assert _sent_publishes(ws, gateway.TOPIC_CONTROL_IN)  # MeshStateRequest


def _b64json(obj: dict) -> str:
    return base64.b64encode(json.dumps(obj).encode()).decode()


def _ack_echo(data: str) -> dict:
    # The cloud's confirmation of our own acked publish (#70 capture): op "published"
    # on mesh.out echoing back the SAME `data`, with publisher=True.
    return {"op": "published", "topic": "mesh.out", "data": data, "publisher": True}


def _state_relay_published(address: int, vector: bytes) -> dict:
    # A `published` frame WITHOUT a publisher flag: the gateway's own state relay
    # (docs/gateway_protocol.md), which must be decoded like an `update`, not eaten as an ack.
    inner = {"raw": base64.b64encode(gateway.repackage_command_to_ws(vector)).decode(), "index": address}
    return {"op": "published", "topic": "mesh.out", "data": base64.b64encode(json.dumps(inner).encode()).decode()}


def _last_mesh_publish_data(ws) -> str:
    return _sent_publishes(ws, gateway.TOPIC_MESH_IN)[-1]["data"]


async def test_write_publishes_command_with_ack_and_awaits_echo():
    ws = _FakeWS()
    conn = _conn(ws)
    await conn.connect()
    vector = set_output_state_and_level(address=11, output=0, on=True, level=80)
    task = asyncio.ensure_future(conn.write(vector))
    await asyncio.sleep(0)  # let write() send and register its ack waiter
    conn._handle_frame(json.dumps(_ack_echo(_last_mesh_publish_data(ws))))  # matching echo unblocks write()
    await asyncio.wait_for(task, 1)
    await conn.disconnect()
    mesh_pubs = _sent_publishes(ws, gateway.TOPIC_MESH_IN)
    assert len(mesh_pubs) == 1
    assert mesh_pubs[0]["ack"] is True  # the app-matching flag that gets the reliable path
    inner = json.loads(base64.b64decode(mesh_pubs[0]["data"]))
    assert inner["index"] == 11
    assert base64.b64decode(inner["raw"]) == gateway.repackage_command_to_ws(vector)
    # the echo doubles as the state relay for our own change: brightness reflected, no polling
    assert conn.state_for(11) is not None and conn.state_for(11).level == 80
    # only the connect-time snapshot is requested; state changes arrive via mesh.out push
    assert len(_sent_publishes(ws, gateway.TOPIC_CONTROL_IN)) == 1


async def test_write_returns_when_ack_times_out(monkeypatch):
    from plejd_cloud import gateway_transport as gt

    monkeypatch.setattr(gt, "GATEWAY_PUBLISH_ACK_TIMEOUT", 0.01)
    ws = _FakeWS()
    conn = _conn(ws)
    await conn.connect()
    vector = set_output_state_and_level(address=11, output=0, on=True, level=80)
    await conn.write(vector)  # no echo ever arrives → returns after the timeout, no hang
    assert len(_sent_publishes(ws, gateway.TOPIC_MESH_IN)) == 1
    assert not conn._ack_waiters  # the timed-out waiter is cleaned up, not leaked
    await conn.disconnect()


async def test_publisher_echo_updates_state_even_without_waiter():
    fired = []
    conn = _conn(_FakeWS(), on_state=lambda: fired.append(1))
    vector = set_output_state_and_level(address=11, output=0, on=True, level=200)
    inner = {"raw": base64.b64encode(gateway.repackage_command_to_ws(vector)).decode(), "index": 11}
    frame = {"op": "published", "topic": "mesh.out", "publisher": True, "data": _b64json(inner)}
    # A publisher=True echo carries the command's {raw,index}: it resolves any ack AND
    # relays our own new state. With no waiter here, it still must update state (not be dropped).
    conn._handle_frame(json.dumps(frame))
    assert conn.state_for(11) is not None and conn.state_for(11).level == 200 and fired == [1]


async def test_published_without_publisher_decodes_as_state():
    fired = []
    conn = _conn(_FakeWS(), on_state=lambda: fired.append(1))
    vector = set_output_state_and_level(address=11, output=0, on=True, level=200)
    # No publisher flag → the gateway's state relay → must be decoded (regression: off-app changes).
    conn._handle_frame(json.dumps(_state_relay_published(11, vector)))
    assert conn.state_for(11) is not None and conn.state_for(11).level == 200 and fired == [1]


async def test_concurrent_writes_resolve_by_matching_data():
    ws = _FakeWS()
    conn = _conn(ws)
    await conn.connect()
    t1 = asyncio.ensure_future(conn.write(set_output_state_and_level(address=11, output=0, on=True, level=80)))
    await asyncio.sleep(0)
    d1 = _last_mesh_publish_data(ws)
    t2 = asyncio.ensure_future(conn.write(set_output_state_and_level(address=12, output=0, on=True, level=90)))
    await asyncio.sleep(0)
    d2 = _last_mesh_publish_data(ws)
    assert len(conn._ack_waiters) == 2  # two distinct publishes in flight
    # Ack the second BEFORE the first: content-keying resolves each write by its own echo,
    # so out-of-order acks can never unblock the wrong write.
    conn._handle_frame(json.dumps(_ack_echo(d2)))
    conn._handle_frame(json.dumps(_ack_echo(d1)))
    await asyncio.wait_for(asyncio.gather(t1, t2), 1)
    assert len(_sent_publishes(ws, gateway.TOPIC_MESH_IN)) == 2
    await conn.disconnect()


async def test_reconnect_releases_pending_ack_waiters():
    ws = _FakeWS()
    conn = _conn(ws)
    await conn.connect()
    vector = set_output_state_and_level(address=11, output=0, on=True, level=80)
    task = asyncio.ensure_future(conn.write(vector))
    await asyncio.sleep(0)  # write() is now blocked awaiting an ack
    await conn.connect()  # socket dropped + reconnect must not strand the pending write
    await asyncio.wait_for(task, 1)  # returns without waiting out the full timeout
    assert not conn._ack_waiters
    await conn.disconnect()


async def test_write_discards_waiter_when_send_fails():
    conn = _conn(_FakeWS())  # never connected → _send raises "not connected"
    vector = set_output_state_and_level(address=11, output=0, on=True, level=80)
    with pytest.raises(RuntimeError, match="not connected"):
        await conn.write(vector)
    assert not conn._ack_waiters  # a failed send must not leak its waiter


async def test_discard_ack_waiter_tolerates_absent_entries():
    # Cancellation/timeout/reconnect can clear a waiter before write()'s own finally runs;
    # discarding one that's no longer tracked must be a safe no-op for both a missing key
    # and a missing waiter, not an error.
    conn = _conn(_FakeWS())
    waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    conn._discard_ack_waiter("no-such-data", waiter)  # unknown key
    conn._ack_waiters["d"] = deque()  # key present but waiter absent
    conn._discard_ack_waiter("d", waiter)
    assert not conn._ack_waiters


async def test_ack_waiter_resolution_skips_already_finished_futures():
    conn = _conn(_FakeWS())
    loop = asyncio.get_running_loop()
    # resolve: a finished waiter ahead of a live one in the same data queue is skipped, not resolved twice.
    finished = loop.create_future()
    finished.cancel()
    live = loop.create_future()
    conn._ack_waiters["d"] = deque([finished, live])
    conn._resolve_ack_waiter("d")
    assert live.done() and live.result() is None and not conn._ack_waiters
    # release: an already-resolved waiter is left alone (no double set_result), dict cleared.
    resolved = loop.create_future()
    resolved.set_result(None)
    conn._ack_waiters["e"] = deque([resolved])
    conn._release_ack_waiters()
    assert not conn._ack_waiters


def test_handle_push_updates_state():
    fired = []
    conn = _conn(_FakeWS(), on_state=lambda: fired.append(1))
    vector = set_output_state_and_level(address=11, output=0, on=True, level=200)
    conn._handle_frame(json.dumps(_push_frame(11, vector)))
    assert conn.state_for(11) is not None and conn.state_for(11).level == 200 and fired == [1]


def test_set_state_records_locally():
    conn = _conn(_FakeWS())
    conn.set_state(9, OutputState(output=0, on=True, level=200))
    assert conn.state_for(9) == OutputState(output=0, on=True, level=200)


def test_handle_push_ignores_non_output_command():
    from plejd_cloud.protocol import encode_command

    fired = []
    conn = _conn(_FakeWS(), on_state=lambda: fired.append(1))
    # A button (0x0097) push decodes but isn't an output state -> no state, no notify.
    conn._handle_frame(json.dumps(_push_frame(10, encode_command(10, 0x0097, bytes([1])))))
    assert conn.state == {} and fired == []


def test_handle_push_forwards_every_decoded_command_to_on_event():
    from plejd_cloud.const import CMD_NOTIFY_EVENTS
    from plejd_cloud.protocol import encode_command

    events = []
    conn = _conn(_FakeWS(), on_event=lambda command: events.append(command))
    # NotifyEvents (fault) push - not an output-state command, but on_event still fires
    # with the decoded Command so the coordinator's own dispatch (faults, motion,
    # button) works the same as it does for BLE, not just output-state changes.
    conn._handle_frame(json.dumps(_push_frame(10, encode_command(10, CMD_NOTIFY_EVENTS, bytes([1])))))
    assert len(events) == 1 and events[0].address == 10 and events[0].command == CMD_NOTIFY_EVENTS


def test_handle_push_without_on_event_does_not_crash():
    from plejd_cloud.const import CMD_NOTIFY_EVENTS
    from plejd_cloud.protocol import encode_command

    conn = _conn(_FakeWS())  # on_event defaults to None
    conn._handle_frame(json.dumps(_push_frame(10, encode_command(10, CMD_NOTIFY_EVENTS, bytes([1])))))


def test_handle_push_routes_state_command_to_on_event_instead_of_on_state():
    state_fired = []
    events = []
    conn = _conn(_FakeWS(), on_state=lambda: state_fired.append(1), on_event=lambda cmd: events.append(cmd))
    vector = set_output_state_and_level(address=11, output=0, on=True, level=200)
    conn._handle_frame(json.dumps(_push_frame(11, vector)))
    # on_event takes over dispatch when provided; on_state (the old path) is not also called.
    assert conn.state_for(11) is not None and conn.state_for(11).level == 200
    assert state_fired == []
    assert len(events) == 1 and events[0].address == 11


def test_handle_push_routes_non_output_command_to_on_event():
    from plejd_cloud.protocol import encode_command

    events = []
    conn = _conn(_FakeWS(), on_event=lambda cmd: events.append(cmd))
    # A button (0x0097) push isn't an output state, but on_event still sees it
    # (needed for NotifyEvents/motion/button on gateway-backed sites).
    conn._handle_frame(json.dumps(_push_frame(10, encode_command(10, 0x0097, bytes([1])))))
    assert conn.state == {}
    assert len(events) == 1 and events[0].command == 0x0097


def test_handle_push_ignores_undecodable_packet():
    conn = _conn(_FakeWS())
    inner = {"raw": base64.b64encode(b"too-short").decode(), "index": 11}  # not a 23-byte packet
    frame = {"topic": "mesh.out", "op": "update", "data": base64.b64encode(json.dumps(inner).encode()).decode()}
    conn._handle_frame(json.dumps(frame))  # repackage raises -> ignored, no crash
    assert conn.state == {}


def test_handle_frame_pong_sets_flag():
    conn = _conn(_FakeWS())
    frame = {"data": base64.b64encode(json.dumps({"controlType": "Pong"}).encode()).decode()}
    conn._handle_frame(json.dumps(frame))
    assert conn._pong is True


def _ping_sent(ws) -> bool:
    for s in ws.sent:
        msg = json.loads(s)
        data = msg.get("data")
        if isinstance(data, str) and json.loads(base64.b64decode(data)).get("controlType") == "Ping":
            return True
    return False


async def test_ping_loop_survives_on_pong(monkeypatch):
    from plejd_cloud import gateway_transport as gt

    ws = _FakeWS()
    conn = _conn(ws)
    conn._ws = ws

    async def fake_sleep(delay):
        if delay == gt.GATEWAY_PONG_TIMEOUT:
            conn._pong = True  # gateway answered in time
            conn._closing = True  # stop the loop after this cycle

    monkeypatch.setattr(gt.asyncio, "sleep", fake_sleep)
    await conn._ping_loop()
    assert _ping_sent(ws) and not ws.closed


async def test_ping_loop_closes_on_missed_pong(monkeypatch):
    from plejd_cloud import gateway_transport as gt

    ws = _FakeWS()
    conn = _conn(ws)
    conn._ws = ws

    async def fake_sleep(_delay):
        pass  # never set _pong → missed pong

    monkeypatch.setattr(gt.asyncio, "sleep", fake_sleep)
    await conn._ping_loop()
    assert _ping_sent(ws) and ws.closed  # missed pong → closed so the owner reconnects


async def test_ping_loop_does_not_close_reconnected_socket(monkeypatch):
    from plejd_cloud import gateway_transport as gt

    old, new = _FakeWS(), _FakeWS()
    conn = _conn(old)
    conn._ws = old
    calls = {"n": 0}

    async def fake_sleep(_delay):
        calls["n"] += 1
        if calls["n"] == 2:  # pong wait of round one: socket drops + owner reconnects
            old.closed = True
            conn._ws = new  # fresh socket, not yet ponged
            conn._pong = False
        elif calls["n"] >= 3:  # next round's ping interval: stop the loop
            conn._closing = True

    monkeypatch.setattr(gt.asyncio, "sleep", fake_sleep)
    await conn._ping_loop()
    assert not new.closed  # the stale loop must NOT close the freshly reconnected socket


async def test_ping_loop_stops_when_disconnected(monkeypatch):
    from plejd_cloud import gateway_transport as gt

    ws = _FakeWS()
    ws.closed = True  # socket already gone
    conn = _conn(ws)
    conn._ws = ws

    async def fake_sleep(_delay):
        pass

    monkeypatch.setattr(gt.asyncio, "sleep", fake_sleep)
    await conn._ping_loop()
    assert ws.sent == []  # bailed before sending a ping


async def test_receive_updates_state_and_signals_disconnect_on_close():
    fired = []
    dropped = []
    ws = _FakeWS([_text(_state_reply(**{"11": "1,65535", "24": "0,0"}))])
    conn = _conn(ws, on_state=lambda: fired.append(1), on_disconnect=lambda: dropped.append(1))
    await conn.connect()
    await conn._recv_task  # drain the (finite) incoming frames, then the socket "closes"
    assert conn.state_for(11).on is True and conn.state_for(11).level == 255
    assert conn.state_for(24).on is False
    assert fired == [1] and dropped == [1]  # state pushed once, disconnect signalled on close


async def test_receive_loop_silent_when_closing():
    dropped = []
    ws = _FakeWS()
    conn = _conn(ws, on_disconnect=lambda: dropped.append(1))
    conn._ws = ws
    conn._closing = True
    await conn._receive_loop()  # exhausts immediately; closing → no disconnect callback
    assert dropped == []


async def test_receive_loop_signals_disconnect_on_unexpected_error():
    class _RaisingWS(_FakeWS):
        async def __anext__(self):
            raise RuntimeError("connection reset")

    dropped = []
    ws = _RaisingWS()
    conn = _conn(ws, on_disconnect=lambda: dropped.append(1))
    conn._ws = ws
    with pytest.raises(RuntimeError):
        await conn._receive_loop()
    assert dropped == [1]  # an unexpected error must still notify the owner, not die silently


async def test_ping_loop_treats_send_failure_as_missed_pong(monkeypatch):
    from plejd_cloud import gateway_transport as gt

    ws = _FakeWS()
    conn = _conn(ws)
    conn._ws = ws

    async def _fail_send(_s):
        raise ConnectionResetError("broken pipe")

    ws.send_str = _fail_send

    async def fake_sleep(_delay):
        pass

    monkeypatch.setattr(gt.asyncio, "sleep", fake_sleep)
    await conn._ping_loop()
    # the failed send is caught and treated like a missed pong: close so the owner
    # reconnects, and the loop ends on its own (no second cycle needed).
    assert ws.closed


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        json.dumps(123),  # not a dict
        json.dumps({"op": "subscribed", "topic": ["control.out"]}),  # no data
        json.dumps({"data": "!!!not-base64!!!"}),  # undecodable
        json.dumps({"data": base64.b64encode(b"123").decode()}),  # decodes to a non-dict
        json.dumps({"data": base64.b64encode(json.dumps({"controlType": "Pong"}).encode()).decode()}),  # other type
    ],
)
def test_handle_frame_ignores_non_state(raw):
    fired = []
    conn = _conn(_FakeWS(), on_state=lambda: fired.append(1))
    conn._handle_frame(raw)
    assert conn.state == {} and fired == []


async def test_connect_failure_propagates():
    async def _token():
        return "tok"

    conn = PlejdGatewayConnection(_FakeSession(error=OSError("boom")), "S1", "rs1", "i", _token, lambda: None)
    with pytest.raises(OSError, match="boom"):
        await conn.connect()


async def test_send_without_connection_raises():
    conn = _conn(_FakeWS())  # never connected
    with pytest.raises(RuntimeError, match="not connected"):
        await conn.async_request_state()


async def test_disconnect_is_idempotent():
    ws = _FakeWS()
    conn = _conn(ws)
    await conn.connect()
    await conn.disconnect()
    assert ws.closed and not conn.connected
    await conn.disconnect()  # no error second time


async def test_connect_cancels_leftover_tasks():
    conn = _conn(_FakeWS())

    async def _sleeper():
        await asyncio.sleep(100)

    leftover = asyncio.ensure_future(_sleeper())
    conn._ping_task = leftover  # a stale loop from a prior session
    await conn.connect()
    await asyncio.sleep(0)
    assert leftover.cancelled()  # reconnect cancelled it, so it can't close the fresh socket
    await conn.disconnect()
