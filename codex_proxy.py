#!/usr/bin/env python3
"""Codex ↔ Qwythos (llama.cpp) 代理
解决的问题：
1. codex 发 developer role → 改为 system（匹配 Qwythos 模板）
2. codex 发顶级 instructions → 合并到 developer 消息内容里（避免双重 system 消息）
3. 正确处理 Content-Length
4. 透明转发响应（保持 HTTP/1.1 + chunked + X-Accel-Buffering: no）
5. 让 Codex 获知模型真实上下文窗口，以便在接近上限时自动压缩历史（auto-compact）
"""
import json, http.server, threading, sys, urllib.request

TARGET = "http://127.0.0.1:8007"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8090
CTX_SIZE = 262144

class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        cl = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(cl) if cl else b""
        req = json.loads(body)
        
        # 把顶级 instructions 合并到 developer 消息，统一转 system 并去重
        instructions = req.pop("instructions", None)
        first_system_done = False
        for m in req.get("input", []):
            if m.get("role") == "developer":
                # 把 instructions 合并到第一个 developer 里
                if instructions:
                    existing = m.get("content", [])
                    m["content"] = [{"type": "input_text", "text": instructions}] + existing
                    instructions = None  # 只合并一次
                m["role"] = "system"
        # 如果存在多个 system 消息，合并为一条（避免模板报错）
        system_msgs = []
        other_msgs = []
        for m in req.get("input", []):
            if m.get("role") == "system":
                # 将所有 system 消息的 content 收集起来
                system_msgs.append(m.get("content", []))
            else:
                other_msgs.append(m)
        if system_msgs:
            # 合并所有 system content 到一条
            merged_content = []
            for content_list in system_msgs:
                merged_content.extend(content_list)
            req["input"] = [{"role": "system", "content": merged_content}] + other_msgs
        
        new_body = json.dumps(req).encode()
        req2 = urllib.request.Request(TARGET + self.path, data=new_body, method="POST")
        for key in self.headers:
            if key.lower() not in ("content-length", "host", "transfer-encoding"):
                req2.add_header(key, self.headers[key])
        req2.add_header("Content-Length", str(len(new_body)))
        req2.add_header("Host", "127.0.0.1:8007")
        
        try:
            with urllib.request.urlopen(req2, timeout=900) as resp:
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    if k.lower() not in ("transfer-encoding", "connection"):
                        self.send_header(k, v)
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                while True:
                    chunk = resp.read(65536)
                    if not chunk: break
                    self.wfile.write(chunk)
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(e.read())
        except Exception as e:
            self.send_error(502, str(e))
    
    def do_GET(self):
        if self.path == "/v1/models":
            # Codex 通过 /v1/models 获取模型信息。llama.cpp 原生也提供这个
            # 接口，但响应里没有 context_length 字段；这里把真实上下文窗口
            # 注入进去，让 Codex 能在接近上限时自动触发 compact，避免硬错误。
            self._serve_models()
            return
        try:
            with urllib.request.urlopen(TARGET + self.path, timeout=30) as resp:
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    self.send_header(k, v)
                self.end_headers()
                raw = resp.read()
                # 顺手给 llama.cpp 原生 /v1/models 注入 context_length
                if self.path.rstrip("/") == "/v1/models":
                    try:
                        data = json.loads(raw)
                        for m in data.get("data", []):
                            m["context_length"] = CTX_SIZE
                        raw = json.dumps(data).encode()
                    except Exception:
                        pass
                self.wfile.write(raw)
        except Exception as e:
            self.send_error(502, str(e))

    def _serve_models(self):
        try:
            with urllib.request.urlopen(TARGET + "/v1/models", timeout=30) as resp:
                raw = resp.read()
            try:
                data = json.loads(raw)
                for m in data.get("data", []):
                    m["context_length"] = CTX_SIZE
            except Exception:
                # 解析失败就回退：构造一个最小模型列表
                data = {"data": [{"id": "qwythos", "context_length": CTX_SIZE}]}
            raw = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except Exception as e:
            self.send_error(502, str(e))
    
    def log_message(self, fmt, *a):
        sys.stderr.write("PROXY: " + (fmt % a) + "\n")
        sys.stderr.flush()

class ThreadedHTTPServer(http.server.HTTPServer):
    def process_request(self, request, ca):
        t = threading.Thread(target=self._handle, args=(request, ca))
        t.daemon = True; t.start()
    def _handle(self, request, ca):
        self.finish_request(request, ca)
        self.shutdown_request(request)

if __name__ == "__main__":
    srv = ThreadedHTTPServer(("127.0.0.1", PORT), H)
    print(f"codex proxy: :{PORT} -> {TARGET}", flush=True)
    srv.serve_forever()
