#!/usr/bin/env python3
"""Streaming think-strip proxy (threaded).
Strips <think>...</think> from every JSON string in any SSE frame or JSON response.
Per-item buffering for delta streams. Keepalive pings during prompt eval."""
import json, re, sys, socket, select
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse

TARGET = "http://127.0.0.1:8007"
tp = urlparse(TARGET)
THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)

def walk_strip(obj):
    if isinstance(obj, str): return THINK_RE.sub("", obj).strip()
    if isinstance(obj, dict): return {k: walk_strip(v) for k, v in obj.items()}
    if isinstance(obj, list): return [walk_strip(v) for v in obj]
    return obj

class THTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

class H(BaseHTTPRequestHandler):
    # Use HTTP/1.1 so chunked Transfer-Encoding works correctly with
    # strict clients like reqwest (codex). HTTP/1.0 + chunked is technically
    # invalid and causes reqwest to drop the connection after the first chunk.
    protocol_version = "HTTP/1.1"

    def log_message(self, *a): sys.stderr.write("PROXY: " + " ".join(str(x) for x in a) + "\n"); sys.stderr.flush()

    def end_headers(self):
        # Prevent intermediate proxies/buffers from buffering the SSE stream.
        self.send_header("X-Accel-Buffering", "no")
        super().end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        try:
            sock = socket.create_connection((tp.hostname, tp.port), timeout=900)
        except Exception as e:
            self.send_error(502, str(e)); return
        path = self.path or "/"
        req = (f"POST {path} HTTP/1.1\r\nHost: {tp.hostname}:{tp.port}\r\n"
               f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
               "Accept: text/event-stream\r\nConnection: close\r\n\r\n")
        sock.sendall(req.encode() + body)

        resp = b""
        while b"\r\n\r\n" not in resp:
            c = sock.recv(4096)
            if not c: break
            resp += c
        if b"\r\n\r\n" not in resp:
            self.send_error(502, "upstream no headers"); sock.close(); return
        hdr, rest = resp.split(b"\r\n\r\n", 1)
        st = int(hdr.decode().split("\r\n")[0].split()[1])
        ctype = ""
        for h in hdr.decode().split("\r\n")[1:]:
            if h.lower().startswith("content-type:"):
                ctype = h.split(":",1)[1].strip()

        self.send_response(st)
        self.send_header("Content-Type", ctype)
        self.send_header("Transfer-Encoding", "chunked")
        # Force connection close after the response so codex (reqwest) does
        # not try to reuse this connection — our handler only does one-shot
        # streaming responses.
        self.send_header("Connection", "close")
        self.end_headers()

        if "event-stream" not in ctype:
            # non-streaming: buffer, strip, forward
            while True:
                c = sock.recv(65536)
                if not c: break
                rest += c
            try:
                d = json.loads(rest)
                d = walk_strip(d)
                self._chunk(json.dumps(d).encode())
            except: pass
            sock.close(); self._end(); return

        # SSE streaming
        buf = rest
        got_data = False
        items = {}

        def emit(etype, ev):
            self._chunk((f"event: {etype}\ndata: {json.dumps(ev)}\n\n").encode())

        while True:
            while b"\n\n" not in buf:
                r = select.select([sock], [], [], 2.0)
                if not r[0]:
                    if got_data:
                        if buf.strip(): self._chunk(buf + b"\n\n")
                        break
                    self._chunk(b": ping\n\n")
                    continue
                c = sock.recv(65536)
                if not c:
                    if buf.strip(): self._chunk(buf + b"\n\n")
                    break
                buf += c
            if b"\n\n" not in buf: break
            frame, buf = buf.split(b"\n\n", 1)
            fs = frame.decode("utf-8", "replace")
            lines = fs.split("\n")
            dl = None; di = -1
            for i, l in enumerate(lines):
                if l.startswith("data:"):
                    dl = l[5:].strip(); di = i; break
            if dl is None:
                self._chunk((fs+"\n\n").encode()); continue
            try:
                ev = json.loads(dl)
            except json.JSONDecodeError:
                self._chunk((fs+"\n\n").encode()); continue
            if not got_data: got_data = True

            etype = ev.get("type", "")
            if etype == "response.output_text.delta":
                iid = ev.get("item_id")
                if iid not in items: items[iid] = ["clean", ""]
                mode, accum = items[iid]
                accum += ev.get("delta", "")
                if mode == "clean":
                    if "<think>" in accum:
                        before, _, after = accum.partition("<think>")
                        items[iid] = ["thinking", after]
                        if before.strip():
                            ne = dict(ev); ne["delta"] = before; emit(etype, ne)
                    elif len(accum) > 11:
                        emit_part = accum[:-11]
                        items[iid] = ["clean", accum[-11:]]
                        if emit_part.strip():
                            ne = dict(ev); ne["delta"] = emit_part; emit(etype, ne)
                else:
                    if "</think>" in accum:
                        _, _, after = accum.partition("</think>")
                        items[iid] = ["clean", after]
                        if after.strip():
                            ne = dict(ev); ne["delta"] = after; emit(etype, ne)
                            items[iid] = ["clean", ""]
                continue

            if etype == "response.completed":
                for iid, (mode, accum) in items.items():
                    clean = THINK_RE.sub("", accum).strip()
                    if clean:
                        emit("response.output_text.delta", {"type": "response.output_text.delta", "item_id": iid, "delta": clean})
                items.clear()

            ev = walk_strip(ev)
            emit(etype, ev)

        sock.close()
        self._end()

    def do_GET(self):
        # for /health and /v1/models - direct forward
        try:
            sock = socket.create_connection((tp.hostname, tp.port), timeout=30)
            path = self.path or "/"
            req = f"GET {path} HTTP/1.1\r\nHost: {tp.hostname}:{tp.port}\r\nConnection: close\r\n\r\n"
            sock.sendall(req.encode())
            resp = b""
            while True:
                c = sock.recv(65536)
                if not c: break
                resp += c
            sock.close()
            hdr, body = resp.split(b"\r\n\r\n", 1)
            st = int(hdr.decode().split("\r\n")[0].split()[1])
            self.send_response(st)
            for h in hdr.decode().split("\r\n")[1:]:
                if h.lower().startswith("content-type:"):
                    self.send_header("Content-Type", h.split(":",1)[1].strip())
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self.send_error(502, str(e))

    def _chunk(self, data):
        if not data: return
        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()

    def _end(self):
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8090
    print(f"threaded think-strip proxy: :{port} -> {TARGET}", flush=True)
    THTTPServer(("127.0.0.1", port), H).serve_forever()
