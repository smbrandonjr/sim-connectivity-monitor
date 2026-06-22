"""Network probes for the scanner. Pure-Python / stdlib + subprocess `ping`.

Discovery and port scans use TCP connect: a SYN-ACK (open) OR an active refusal
(RST) both prove the host is up, so we find live hosts even when no probed port
is open. Traceroute uses raw sockets (root, Linux) and can be bound to a
specific interface to trace the cellular path.
"""

from __future__ import annotations

import re
import socket
import time

from sim_monitor.monitor.transport import make_session
from sim_monitor.system import proc

SO_BINDTODEVICE = getattr(socket, "SO_BINDTODEVICE", 25)


def tcp_probe(host: str, port: int, timeout: float = 0.5) -> str:
    """Return 'open', 'refused', 'timeout', or 'error'."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return "open"
    except ConnectionRefusedError:
        return "refused"
    except TimeoutError:
        return "timeout"
    except OSError:
        return "error"
    finally:
        s.close()


def scan_host(host: str, ports: list[int], timeout: float = 0.5) -> dict:
    """Probe a host across `ports`. up = any open/refused. Returns the result."""
    open_ports = []
    up = False
    for port in ports:
        result = tcp_probe(host, port, timeout)
        if result == "open":
            open_ports.append(port)
            up = True
        elif result == "refused":
            up = True
    return {"ip": host, "up": up, "open_ports": open_ports}


_PING_RX = re.compile(r"(\d+) packets transmitted, (\d+) (?:packets )?received")
# rtt min/avg/max/mdev = 10.1/12.4/15.8/1.2 ms — capture min, avg, max.
_RTT_RX = re.compile(r"=\s*([\d.]+)/([\d.]+)/([\d.]+)/")


def ping_host(host: str, interface: str | None = None,
              count: int = 4, timeout: int = 2, runner=proc.run) -> dict:
    """Run system ping (optionally bound to an interface). Returns loss/rtt.

    Keys: sent, received, loss_pct, avg_ms (back-compat) plus min_ms/max_ms.
    RTT fields are None when no packets came back (100% loss)."""
    args = ["ping", "-n", "-c", str(count), "-W", str(timeout)]
    if interface:
        args += ["-I", interface]
    args.append(host)
    try:
        out = runner(args, timeout=count * timeout + 5)
    except Exception as e:  # noqa: BLE001 - non-zero exit on 100% loss is normal
        out = str(e)
    sent = recv = 0
    m = _PING_RX.search(out)
    if m:
        sent, recv = int(m.group(1)), int(m.group(2))
    min_ms = avg_ms = max_ms = None
    r = _RTT_RX.search(out)
    if r:
        min_ms, avg_ms, max_ms = float(r.group(1)), float(r.group(2)), float(r.group(3))
    loss = round((1 - recv / sent) * 100, 1) if sent else 100.0
    return {
        "sent": sent, "received": recv, "loss_pct": loss,
        "avg_ms": avg_ms, "min_ms": min_ms, "max_ms": max_ms,
    }


def http_probe(url: str, interface: str | None = None, timeout: float = 8) -> dict:
    import requests

    started = time.monotonic()
    try:
        resp = make_session(interface).get(url, timeout=timeout, allow_redirects=True)
        return {"ok": True, "status": resp.status_code,
                "latency_ms": round((time.monotonic() - started) * 1000)}
    except requests.RequestException as e:
        return {"ok": False, "status": None,
                "latency_ms": round((time.monotonic() - started) * 1000), "error": str(e)}


def dns_resolve(hostname: str, timeout: float = 5) -> dict:
    """Resolve via the system resolver (not interface-bound)."""
    old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    started = time.monotonic()
    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        addrs = sorted({i[4][0] for i in infos})
        return {"ok": True, "addresses": addrs,
                "ms": round((time.monotonic() - started) * 1000)}
    except (OSError, socket.gaierror) as e:
        return {"ok": False, "addresses": [], "error": str(e),
                "ms": round((time.monotonic() - started) * 1000)}
    finally:
        socket.setdefaulttimeout(old)


def traceroute(dest: str, interface: str | None = None,
               max_hops: int = 30, timeout: float = 1.5):
    """Yield hops {ttl, ip, host, rtt_ms} as they're discovered. Raw sockets,
    so root + Linux; bind to an interface to trace the cellular path."""
    try:
        dest_ip = socket.gethostbyname(dest)
    except OSError as e:
        raise RuntimeError(f"cannot resolve {dest}: {e}") from e
    port = 33434
    for ttl in range(1, max_hops + 1):
        recv = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
        send = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        send.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, ttl)
        if interface:
            send.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE, interface.encode())
        recv.settimeout(timeout)
        started = time.monotonic()
        hop_ip = None
        try:
            send.sendto(b"", (dest_ip, port))
            _, addr = recv.recvfrom(512)
            hop_ip = addr[0]
        except TimeoutError:
            pass
        except OSError as e:
            raise RuntimeError(f"traceroute failed (needs root): {e}") from e
        finally:
            rtt = round((time.monotonic() - started) * 1000) if hop_ip else None
            send.close()
            recv.close()
        host = None
        if hop_ip:
            try:
                host = socket.gethostbyaddr(hop_ip)[0]
            except OSError:
                pass
        yield {"ttl": ttl, "ip": hop_ip, "host": host, "rtt_ms": rtt}
        if hop_ip == dest_ip:
            return
