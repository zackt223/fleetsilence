"""HTTP API (standard library only; nothing here touches the network beyond localhost)."""
import json
import re
import socket
import socketserver
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from . import config
from .jobcover import JobCover
from .refdata import RefData
from .status import Engine
from .store import Store
from .timeutil import Clock, iso, parse_ts

STATUS_PATH = re.compile(r"^/devices/([^/]+)/status/?$")
MAX_BODY = 64 * 1024 * 1024


class BadRequest(Exception):
    pass


class NotFound(Exception):
    pass


class App:
    """The service itself, independent of HTTP so tests can call it directly."""

    def __init__(self, data_dir, cfg=None):
        self.cfg = cfg or config.load()
        self.ref = RefData(data_dir)
        self.clock = Clock(self.cfg.depots, self._infer_time_zones())
        self.store = Store(self.cfg)
        self.engine = Engine(self.cfg, self.ref, self.store, self.clock)
        self.cover = JobCover(self.engine)
        self._seed_plan_changes()

    def _infer_time_zones(self):
        """Depots in the data that settings.toml has no time zone for get one estimated from the schedule."""
        missing = {d: self.ref.jobs_by_depot.get(d, []) for d in sorted(self.ref.depots) if d not in self.cfg.depots}
        if not missing:
            return {}
        hh, mm = (int(x) for x in str((self.cfg.get("clock") or {}).get("workday_start_local", "07:30")).split(":"))
        inferred = Clock.infer_offsets(missing, hh + mm / 60)
        for depot in missing:
            est = inferred.get(depot)
            if est:
                seen = sorted({o for _, o in est})
                print(f"[fleetsilence] warning: no time zone for depot {depot} in settings.toml; estimated UTC"
                      f"{'/'.join(f'{o:+d}' for o in seen)} from its schedule. Add [depots.{depot}] to be exact.",
                      file=sys.stderr)
            else:
                print(f"[fleetsilence] warning: no time zone for depot {depot} in settings.toml and no jobs to estimate"
                      f" one from; using UTC. Add [depots.{depot}] to settings.toml.", file=sys.stderr)
        return inferred

    def _seed_plan_changes(self):
        """plan_changes.csv is a copy of the history's plan_change events. Loading it means a paused
        plan is known even if those events are never streamed; when they are, they de-duplicate."""
        events = [{"event_id": r.get("event_id"), "serial": r.get("serial"), "type": "plan_change",
                   "device_time": r.get("time"), "received_at": r.get("time"),
                   "payload": {"plan": r.get("plan"), "status": r.get("status"), "reason": r.get("reason")}}
                  for r in self.ref.plan_changes]
        self.store.ingest(events)
        self.store.stats = {k: 0 for k in self.store.stats}

    # ------------------------------------------------------------ endpoints
    def ingest(self, body):
        if not isinstance(body, dict) or not isinstance(body.get("events"), list):
            raise BadRequest('body must be {"events": [...]}')
        return {"accepted": self.store.ingest(body["events"])}

    def device_status(self, serial, query):
        at = self._at(query)
        with self.store.lock:
            if not self.engine.known(serial):
                raise NotFound(f"unknown serial {serial!r}")
            return self.engine.snapshot(at).assess(serial).to_json(at)

    def job_cover(self, query):
        at = self._at(query)
        depot = self._one(query, "depot")
        if depot not in self.ref.depots and depot not in self.cfg.depots:
            known = sorted(set(self.ref.depots) | set(self.cfg.depots))
            raise BadRequest(f"depot must be one of {known}")
        raw = self._one(query, "hours")
        try:
            hours = int(raw)
        except ValueError:
            raise BadRequest("hours must be a whole number from 1 to 72") from None
        if not 1 <= hours <= 72:
            raise BadRequest("hours must be from 1 to 72")
        with self.store.lock:
            return self.cover.answer(depot, at, hours)

    @staticmethod
    def _one(query, name):
        vals = query.get(name)
        if not vals or not vals[-1].strip():
            raise BadRequest(f"missing parameter {name!r}")
        return vals[-1].strip()

    def _at(self, query):
        raw = self._one(query, "at")
        at = parse_ts(raw) if not raw.replace(".", "", 1).isdigit() else None
        if at is None:
            raise BadRequest("at must be a UTC time like 2026-10-12T14:05:12Z")
        return at


class Handler(BaseHTTPRequestHandler):
    app = None
    protocol_version = "HTTP/1.1"
    server_version = "fleetsilence"

    def log_message(self, fmt, *args):   # keep the console quiet; errors are still printed below
        pass

    def _send(self, code, body):
        data = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _run(self, fn):
        try:
            self._send(200, fn())
        except BadRequest as exc:
            self._send(400, {"error": str(exc)})
        except NotFound as exc:
            self._send(404, {"error": str(exc)})
        except (BrokenPipeError, ConnectionError):
            pass
        except Exception:   # noqa: BLE001 - never crash, never leak a stack trace to the caller
            traceback.print_exc(file=sys.stderr)
            try:
                self._send(500, {"error": "internal error"})
            except Exception:   # noqa: BLE001
                pass

    def do_GET(self):
        url = urlsplit(self.path)
        path = url.path.rstrip("/") or "/"
        query = parse_qs(url.query, keep_blank_values=True)
        m = STATUS_PATH.match(url.path)
        if path == "/health":
            self._run(lambda: {"status": "ok"})
        elif m:
            self._run(lambda: self.app.device_status(unquote(m.group(1)), query))
        elif path == "/job-cover":
            self._run(lambda: self.app.job_cover(query))
        else:
            self._send(404, {"error": f"no such endpoint: {url.path}"})

    def do_POST(self):
        path = urlsplit(self.path).path.rstrip("/")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self.close_connection = True
            return self._send(400, {"error": "missing or oversized body"})
        raw = self.rfile.read(length)
        if path != "/ingest":
            return self._send(404, {"error": f"no such endpoint: {path}"})

        def work():
            try:
                body = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise BadRequest("body is not valid JSON") from None
            return self.app.ingest(body)
        self._run(work)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def server_bind(self):
        # HTTPServer.server_bind looks the host up with socket.getfqdn(), a reverse-DNS query. With no network
        # that can stall for seconds, and the name is never used, so bind without it.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = str(host), port


class _Server6(_Server):
    address_family = socket.AF_INET6


def serve(data_dir, host=None, port=None):
    cfg = config.load()
    host = host or cfg.server["host"]
    port = int(port or cfg.server["port"])
    Handler.app = App(data_dir, cfg)
    ref = Handler.app.ref
    servers = [_Server((host, port), Handler)]
    if host == "127.0.0.1":
        # "localhost" can resolve to ::1 first; listen there too so callers never wait on a fallback.
        try:
            servers.append(_Server6(("::1", port), Handler))
        except OSError:
            pass
    print(f"[fleetsilence] {len(ref.assets)} assets, {len(ref.devices)} devices, {len(ref.jobs)} jobs loaded from {data_dir}",
          file=sys.stderr)
    print(f"[fleetsilence] listening on http://localhost:{port} (state is in memory and starts empty)", file=sys.stderr, flush=True)
    threads = [threading.Thread(target=s.serve_forever, name=f"listener-{i}") for i, s in enumerate(servers[1:], 1)]
    for t in threads:
        t.start()
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # Stop the other listeners' loops before closing their sockets, so no thread is still reading from a
        # closed socket (or writing to the console) while Python shuts down.
        for s in servers[1:]:
            s.shutdown()
        for t in threads:
            t.join(timeout=5)
        for s in servers:
            s.server_close()
        print("[fleetsilence] stopped", file=sys.stderr, flush=True)


__all__ = ["App", "BadRequest", "NotFound", "serve", "iso"]
