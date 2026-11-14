"""The shared app gateway (runs in its own container, ``bdh-gateway``).

It sits on the ``bdh-egress`` bridge and joins every app's internal network,
which otherwise has no route anywhere. It offers apps exactly two things:

- port 8099: a TCP relay to the hub broker (``BDH_BROKER_TARGET``), so the
  app's ``BDH_BROKER_URL`` is ``http://<gateway ip on its network>:8099/_broker``.
  The broker authenticates every call with the app's token.
- port 3128: an HTTP proxy (``CONNECT`` and absolute-form ``http://``) for
  ``outbound`` apps only, i.e. connections that arrived on a ``.254`` address.
  Destinations must resolve to public addresses (no private, loopback,
  link-local, CGNAT, multicast or reserved ranges) on an allowed port
  (``BDH_EGRESS_PORTS``, default 80,443). The proxy connects to the address it
  checked, so DNS rebinding can't redirect it.

The gateway holds no secrets and keeps no state.
"""
import asyncio
import ipaddress
import logging
import os
import socket
import sys

log = logging.getLogger("bdh.gateway")

MAX_HEAD = 16 * 1024
HEAD_TIMEOUT = 15
CONNECT_TIMEOUT = 15
MAX_CONNECTIONS = 512
HOP_HEADERS = {"proxy-connection", "proxy-authorization", "connection", "keep-alive"}


class Denied(Exception):
    def __init__(self, status, reason):
        super().__init__(reason)
        self.status = status
        self.reason = reason


def public_address(value):
    try:
        ip = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def proxy_allowed_from(local_ip, prefix):
    """Only connections that arrived on an outbound app's ``.254`` address."""
    parts = local_ip.split(".")
    return (len(parts) == 4 and ".".join(parts[:2]) == prefix and parts[3] == "254"
            and parts[2] != "255")


def split_host_port(value, default_port):
    value = value.strip()
    if value.startswith("["):
        host, _, rest = value[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
    elif value.count(":") == 1:
        host, port = value.split(":")
    else:
        host, port = value, ""
    if not host:
        raise Denied(400, "missing host")
    try:
        port = int(port) if port else default_port
    except ValueError as exc:
        raise Denied(400, "bad port") from exc
    return host, port


async def resolve_public(host, port, loop=None):
    """The public addresses ``host`` resolves to; Denied if any is private."""
    loop = loop or asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise Denied(502, f"cannot resolve {host}") from exc
    addrs = []
    for family, _type, _proto, _name, sockaddr in infos:
        if not public_address(sockaddr[0]):
            raise Denied(403, f"{host} resolves to a non-public address")
        addrs.append((family, sockaddr[0]))
    if not addrs:
        raise Denied(502, f"cannot resolve {host}")
    return addrs


async def open_vetted(host, port, allowed_ports):
    if port not in allowed_ports:
        raise Denied(403, f"port {port} is not allowed")
    last = None
    for family, addr in await resolve_public(host, port):
        try:
            return await asyncio.wait_for(asyncio.open_connection(addr, port, family=family),
                                          CONNECT_TIMEOUT)
        except (TimeoutError, OSError) as exc:
            last = exc
    raise Denied(502, f"cannot connect to {host}:{port}: {last}")


async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: S110 - closing a broken socket
            pass


async def splice(r1, w1, r2, w2):
    await asyncio.gather(pipe(r1, w2), pipe(r2, w1))


async def read_head(reader):
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT)
    if len(head) > MAX_HEAD:
        raise Denied(431, "request head too large")
    return head


def parse_head(head):
    lines = head.decode("latin-1").split("\r\n")
    try:
        method, target, version = lines[0].split(" ")
    except ValueError as exc:
        raise Denied(400, "bad request line") from exc
    headers = [line for line in lines[1:] if line]
    return method, target, version, headers


def rewrite_absolute(method, target, version, headers):
    """``GET http://host/path`` -> (host, port, origin-form head bytes)."""
    if not target.lower().startswith("http://"):
        raise Denied(400, "only CONNECT and http:// requests are proxied")
    rest = target[len("http://"):]
    authority, slash, path = rest.partition("/")
    host, port = split_host_port(authority.rsplit("@", 1)[-1], 80)
    kept = [h for h in headers if h.split(":", 1)[0].strip().lower() not in HOP_HEADERS]
    out = [f"{method} /{path if slash else ''} {version}", *kept, "Connection: close", "", ""]
    return host, port, "\r\n".join(out).encode("latin-1")


class Gateway:
    def __init__(self, broker_target, prefix, egress_ports):
        host, port = split_host_port(broker_target, 8099)
        self.broker = (host, port)
        self.prefix = prefix
        self.egress_ports = frozenset(egress_ports)
        self.slots = asyncio.Semaphore(MAX_CONNECTIONS)

    async def relay(self, reader, writer):
        async with self.slots:
            try:
                ur, uw = await asyncio.wait_for(asyncio.open_connection(*self.broker), CONNECT_TIMEOUT)
            except (TimeoutError, OSError) as exc:
                log.warning("broker unreachable: %s", exc)
                writer.close()
                return
            await splice(reader, writer, ur, uw)

    async def proxy(self, reader, writer):
        async with self.slots:
            peer = writer.get_extra_info("peername") or ("?",)
            local = (writer.get_extra_info("sockname") or ("",))[0]
            try:
                # Read the request first so a refusal is a clean reply, not a reset.
                head = await read_head(reader)
                if not proxy_allowed_from(local, self.prefix):
                    raise Denied(403, "this network has no outbound access")
                method, target, version, headers = parse_head(head)
                if method == "CONNECT":
                    host, port = split_host_port(target, 443)
                    ur, uw = await open_vetted(host, port, self.egress_ports)
                    writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                    await writer.drain()
                else:
                    host, port, head = rewrite_absolute(method, target, version, headers)
                    ur, uw = await open_vetted(host, port, self.egress_ports)
                    uw.write(head)
                    await uw.drain()
                log.info("%s %s %s:%s", peer[0], method, host, port)
            except Denied as exc:
                log.info("%s denied: %s", peer[0], exc.reason)
                await self._reply(writer, exc.status, exc.reason)
                return
            except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
                writer.close()
                return
            await splice(reader, writer, ur, uw)

    @staticmethod
    async def _reply(writer, status, reason):
        body = (reason + "\n").encode()
        writer.write(f"HTTP/1.1 {status} Denied\r\nContent-Type: text/plain\r\n"
                     f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
        try:
            await writer.drain()
        finally:
            writer.close()


async def serve(relay_port, proxy_port, gateway):
    relay = await asyncio.start_server(gateway.relay, "0.0.0.0", relay_port)
    proxy = await asyncio.start_server(gateway.proxy, "0.0.0.0", proxy_port, limit=MAX_HEAD)
    log.info("gateway: relay :%s -> %s:%s, proxy :%s (ports %s)", relay_port, *gateway.broker,
             proxy_port, sorted(gateway.egress_ports))
    async with relay, proxy:
        await asyncio.gather(relay.serve_forever(), proxy.serve_forever())


def main():
    logging.basicConfig(level=os.environ.get("BDH_LOG_LEVEL", "INFO"), stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(message)s")
    gateway = Gateway(
        os.environ.get("BDH_BROKER_TARGET", "172.17.0.1:8099"),
        os.environ.get("BDH_SUBNET_PREFIX", "10.213"),
        [int(p) for p in os.environ.get("BDH_EGRESS_PORTS", "80,443").split(",") if p.strip()],
    )
    asyncio.run(serve(int(os.environ.get("BDH_GATEWAY_RELAY_PORT", "8099")),
                      int(os.environ.get("BDH_GATEWAY_PROXY_PORT", "3128")), gateway))


if __name__ == "__main__":
    main()
