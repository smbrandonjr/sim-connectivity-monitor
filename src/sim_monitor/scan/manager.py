"""Runs network scans in a background thread, off the daemon/modem path.

The web layer starts a scan and polls status; results accumulate live with a
progress count and can be cancelled. In --simulate mode the scans return quick
synthetic data so the UI is fully usable without real sockets/root.
"""

from __future__ import annotations

import copy
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from sim_monitor.scan import engine, net

DISCOVERY_WORKERS = 64


def _idle() -> dict:
    return {
        "running": False, "kind": None, "target": None, "interface": None,
        "started_at": None, "finished_at": None, "progress": 0, "total": 0,
        "results": [], "summary": None, "error": None,
    }


class ScanManager:
    def __init__(self, simulate: bool = False) -> None:
        self.simulate = simulate
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = _idle()

    # ── public API ───────────────────────────────────────────────────────
    def status(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._state)

    def stop(self) -> None:
        self._cancel.set()

    def interfaces(self) -> list[dict]:
        if self.simulate:
            return [{"name": "wwan0", "ip": "10.170.42.7", "cidr": "10.170.42.4/30", "hosts": 4},
                    {"name": "eth0", "ip": "192.168.1.50", "cidr": "192.168.1.0/24", "hosts": 256}]
        return net.list_interfaces()

    def start_discovery(self, cidr: str, ports: list[int]) -> None:
        hosts = net.expand_hosts(cidr)
        self._launch("discovery", cidr, None, len(hosts),
                     lambda: self._run_discovery(hosts, ports))

    def start_ports(self, host: str, ports: list[int]) -> None:
        self._launch("ports", host, None, len(ports),
                     lambda: self._run_ports(host, ports))

    def start_reachability(self, target: str, interface: str | None) -> None:
        self._launch("reachability", target, interface, 4,
                     lambda: self._run_reachability(target, interface))

    def start_traceroute(self, target: str, interface: str | None, max_hops: int = 30) -> None:
        self._launch("traceroute", target, interface, max_hops,
                     lambda: self._run_traceroute(target, interface, max_hops))

    # ── lifecycle ────────────────────────────────────────────────────────
    def _launch(self, kind, target, interface, total, worker) -> None:
        with self._lock:
            if self._state["running"]:
                raise RuntimeError("a scan is already running")
            self._cancel.clear()
            self._state = _idle()
            self._state.update(running=True, kind=kind, target=target,
                               interface=interface, started_at=time.time(), total=total)
        self._thread = threading.Thread(target=self._run, args=(worker,),
                                        name="scan", daemon=True)
        self._thread.start()

    def _run(self, worker) -> None:
        try:
            worker()
        except Exception as e:  # noqa: BLE001 - surface any failure to the UI
            with self._lock:
                self._state["error"] = str(e)
        finally:
            with self._lock:
                self._state["running"] = False
                self._state["finished_at"] = time.time()

    def _add(self, result: dict) -> None:
        with self._lock:
            self._state["results"].append(result)

    def _progress(self, n: int = 1) -> None:
        with self._lock:
            self._state["progress"] += n

    def _summary(self, summary: dict) -> None:
        with self._lock:
            self._state["summary"] = summary

    # ── workers ──────────────────────────────────────────────────────────
    def _run_discovery(self, hosts: list[str], ports: list[int]) -> None:
        if self.simulate:
            for i, host in enumerate(hosts[:6]):
                if self._cancel.is_set():
                    break
                self._progress(max(1, len(hosts) // 6))
                self._add({"ip": host, "host": f"device-{i}.local" if i % 2 else None,
                           "open_ports": [22, 80] if i % 2 else [443]})
                time.sleep(0.05)
            self._summary({"alive": min(6, len(hosts)), "scanned": len(hosts)})
            return
        alive = 0
        with ThreadPoolExecutor(max_workers=DISCOVERY_WORKERS) as pool:
            futures = {pool.submit(engine.scan_host, h, ports): h for h in hosts}
            for fut in as_completed(futures):
                if self._cancel.is_set():
                    pool.shutdown(cancel_futures=True)
                    break
                self._progress()
                res = fut.result()
                if res["up"]:
                    alive += 1
                    self._add({"ip": res["ip"], "host": net.reverse_dns(res["ip"]),
                               "open_ports": res["open_ports"]})
        self._summary({"alive": alive, "scanned": len(hosts)})

    def _run_ports(self, host: str, ports: list[int]) -> None:
        if self.simulate:
            for p in [22, 80, 443]:
                self._add({"port": p, "state": "open"})
            self._progress(len(ports))
            self._summary({"open": 3, "scanned": len(ports)})
            return
        open_count = 0
        with ThreadPoolExecutor(max_workers=DISCOVERY_WORKERS) as pool:
            futures = {pool.submit(engine.tcp_probe, host, p): p for p in ports}
            for fut in as_completed(futures):
                if self._cancel.is_set():
                    pool.shutdown(cancel_futures=True)
                    break
                self._progress()
                if fut.result() == "open":
                    open_count += 1
                    self._add({"port": futures[fut], "state": "open"})
        self._summary({"open": open_count, "scanned": len(ports)})

    def _run_reachability(self, target: str, interface: str | None) -> None:
        host = target.split("//")[-1].split("/")[0]
        url = target if target.startswith("http") else f"http://{target}"
        if self.simulate:
            self._summary({
                "ping": {"sent": 4, "received": 4, "loss_pct": 0.0, "avg_ms": 42.0},
                "dns": {"ok": True, "addresses": ["93.184.216.34"], "ms": 23},
                "http": {"ok": True, "status": 200, "latency_ms": 142},
                "tcp": {"443": "open", "80": "open"},
            })
            self._progress(4)
            return
        ping = engine.ping_host(host, interface)
        self._progress()
        dns = engine.dns_resolve(host)
        self._progress()
        http = engine.http_probe(url, interface)
        self._progress()
        tcp = {str(p): engine.tcp_probe(host, p) for p in (443, 80)}
        self._progress()
        self._summary({"ping": ping, "dns": dns, "http": http, "tcp": tcp})

    def _run_traceroute(self, target: str, interface: str | None, max_hops: int) -> None:
        if self.simulate:
            hops = [("10.170.42.8", 3), ("100.64.0.1", 28), ("203.0.113.1", 41),
                    ("93.184.216.34", 44)]
            for ttl, (ip, rtt) in enumerate(hops, start=1):
                if self._cancel.is_set():
                    break
                self._add({"ttl": ttl, "ip": ip, "host": None, "rtt_ms": rtt})
                self._progress()
                time.sleep(0.05)
            self._summary({"hops": len(hops), "reached": True})
            return
        try:
            dest_ip = socket.gethostbyname(target)
        except OSError:
            dest_ip = None
        reached = False
        count = 0
        for hop in engine.traceroute(target, interface, max_hops):
            if self._cancel.is_set():
                break
            count += 1
            self._add(hop)
            self._progress()
            if hop["ip"] and hop["ip"] == dest_ip:
                reached = True
        self._summary({"hops": count, "reached": reached})
