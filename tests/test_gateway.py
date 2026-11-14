import asyncio

import pytest

from bdh_supervisor import gateway as G


@pytest.mark.parametrize(("addr", "ok"), [
    ("1.1.1.1", True), ("2606:4700::1111", True),
    ("10.0.0.1", False), ("192.168.1.10", False), ("172.17.0.1", False), ("127.0.0.1", False),
    ("169.254.169.254", False), ("100.64.0.1", False), ("224.0.0.1", False), ("0.0.0.0", False),
    ("::1", False), ("fe80::1%eth0", False), ("fd00::1", False), ("::ffff:192.168.1.1", False),
    ("not-an-ip", False),
])
def test_public_address(addr, ok):
    assert G.public_address(addr) is ok


@pytest.mark.parametrize(("local", "ok"), [
    ("10.213.7.254", True), ("10.213.7.253", False), ("10.213.255.254", False), ("10.214.7.254", False),
    ("", False),
])
def test_proxy_only_on_outbound_addresses(local, ok):
    assert G.proxy_allowed_from(local, "10.213") is ok


def test_split_host_port():
    assert G.split_host_port("example.com:8443", 443) == ("example.com", 8443)
    assert G.split_host_port("example.com", 443) == ("example.com", 443)
    assert G.split_host_port("[2606:4700::1111]:443", 80) == ("2606:4700::1111", 443)
    with pytest.raises(G.Denied):
        G.split_host_port(":443", 80)
    with pytest.raises(G.Denied):
        G.split_host_port("x:y", 80)


def test_rewrite_absolute_strips_hop_headers():
    host, port, head = G.rewrite_absolute(
        "GET", "http://user@example.com:8080/a?b=1", "HTTP/1.1",
        ["Host: example.com", "Proxy-Authorization: x", "Proxy-Connection: keep-alive", "Accept: */*"])
    assert (host, port) == ("example.com", 8080)
    text = head.decode()
    assert text.startswith("GET /a?b=1 HTTP/1.1\r\n")
    assert "Proxy-" not in text and "Accept: */*" in text and text.endswith("Connection: close\r\n\r\n")
    with pytest.raises(G.Denied):
        G.rewrite_absolute("GET", "ftp://example.com/", "HTTP/1.1", [])


async def test_resolve_public_denies_private_names():
    with pytest.raises(G.Denied) as info:
        await G.resolve_public("localhost", 80)
    assert info.value.status == 403


async def test_open_vetted_checks_port_first():
    with pytest.raises(G.Denied, match="port 22"):
        await G.open_vetted("example.com", 22, frozenset({443}))


async def _request(port, payload):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(payload)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    return data


async def test_proxy_refuses_connections_not_on_outbound_address():
    gw = G.Gateway("127.0.0.1:1", "10.213", [443])
    server = await asyncio.start_server(gw.proxy, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        data = await _request(port, b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
    assert data.startswith(b"HTTP/1.1 403")
    assert b"no outbound access" in data


async def test_proxy_denies_private_destinations(monkeypatch):
    monkeypatch.setattr(G, "proxy_allowed_from", lambda local, prefix: True)
    gw = G.Gateway("127.0.0.1:1", "10.213", [80, 443])
    server = await asyncio.start_server(gw.proxy, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        data = await _request(port, b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n")
        assert data.startswith(b"HTTP/1.1 403")
        data = await _request(port, b"GET http://192.168.1.1/ HTTP/1.1\r\nHost: x\r\n\r\n")
        assert data.startswith(b"HTTP/1.1 403")
        data = await _request(port, b"CONNECT example.com:25 HTTP/1.1\r\n\r\n")
        assert data.startswith(b"HTTP/1.1 403")


async def test_relay_forwards_to_broker():
    async def echo(reader, writer):
        writer.write(b"broker:" + await reader.read(5))
        await writer.drain()
        writer.close()

    broker = await asyncio.start_server(echo, "127.0.0.1", 0)
    bport = broker.sockets[0].getsockname()[1]
    gw = G.Gateway(f"127.0.0.1:{bport}", "10.213", [443])
    relay = await asyncio.start_server(gw.relay, "127.0.0.1", 0)
    rport = relay.sockets[0].getsockname()[1]
    async with broker, relay:
        assert await _request(rport, b"hello") == b"broker:hello"
