#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""创业沙盒 · 本地 Web 应用

    python3 scripts/server.py            # 打开 http://127.0.0.1:8877
    python3 scripts/server.py --port 9000

零第三方依赖（只用标准库），因为这是要开源的东西：
别人的机器上不该为了点开一个页面先装一堆包。
数据全部落在本地 sessions/*.json，不上传任何地方。
"""
import argparse
import json
import os
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import api          # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(HERE, "web")

# 报告要跑 12 动作 × 120 路径 × 2 情形，约 40 秒。
# 单机版用「后台线程 + 轮询」，不阻塞页面，也不用引消息队列。
JOBS = {}
JOBS_LOCK = threading.Lock()


def _job_new(kind):
    jid = f"{kind}-{len(JOBS) + 1}-{threading.get_ident()}"
    with JOBS_LOCK:
        JOBS[jid] = {"status": "running", "result": None, "error": None}
    return jid


def _job_run(jid, fn, *args):
    try:
        r = fn(*args)
        with JOBS_LOCK:
            JOBS[jid].update(status="done", result=r)
    except Exception as e:
        traceback.print_exc()
        with JOBS_LOCK:
            JOBS[jid].update(status="error", error=f"{type(e).__name__}: {e}")


class Handler(BaseHTTPRequestHandler):
    server_version = "Sandbox/1.0"

    def log_message(self, fmt, *args):
        if "--quiet" not in sys.argv:
            sys.stderr.write("  %s\n" % (fmt % args))

    # ── 输出 ──
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, obj)

    def _err(self, msg, code=400):
        self._json({"error": str(msg)}, code)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    # ── 路由 ──
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path

        if p in ("/", "/index.html"):
            return self._static("index.html")
        if p.startswith("/web/"):
            return self._static(p[5:])

        try:
            if p == "/api/meta":
                return self._json(api.meta())
            if p == "/api/sessions":
                return self._json({"sessions": api.sessions()})
            if p.startswith("/api/session/"):
                sid = unquote(p.split("/")[3])
                st = api.S.load_session(sid)
                return self._json({"point": api.point(st), "ledger": api.ledger(st)})
            if p.startswith("/api/ledger/"):
                sid = unquote(p.split("/")[3])
                return self._json(api.ledger(api.S.load_session(sid)))
            if p.startswith("/api/forecast/"):
                sid = unquote(p.split("/")[3])
                n = int(q.get("n", ["300"])[0])
                return self._json(api.forecast(sid, n))
            if p.startswith("/api/audit/"):
                sid = unquote(p.split("/")[3])
                return self._json(api.audit(sid))
            if p.startswith("/api/job/"):
                jid = p.split("/")[3]
                with JOBS_LOCK:
                    j = dict(JOBS.get(jid) or {"status": "missing"})
                return self._json(j)
            return self._err("not found", 404)
        except FileNotFoundError:
            return self._err("会话不存在", 404)
        except ValueError as e:
            return self._err(str(e), 400)
        except Exception as e:
            traceback.print_exc()
            return self._err(f"{type(e).__name__}: {e}", 500)

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        try:
            b = self._body()

            if p == "/api/new":
                st = api.create(
                    b.get("pack") or "cafe",
                    int(b.get("seed") or 2026),
                    b.get("name"),
                    int(b.get("months") or 24),
                    intake=b.get("intake"),
                    snapshot_file=b.get("snapshot"),
                )
                return self._json({"sid": st["sid"], "point": api.point(st),
                                   "ledger": api.ledger(st)})

            if p.startswith("/api/choose/"):
                sid = unquote(p.split("/")[3])
                return self._json(api.choose(sid, b.get("choices") or []))

            if p.startswith("/api/end/"):
                sid = unquote(p.split("/")[3])
                return self._json(api.end(sid, b.get("reason")))

            if p.startswith("/api/resume/"):
                sid = unquote(p.split("/")[3])
                return self._json(api.resume(sid))

            if p == "/api/job/report":
                sid = b["sid"]
                jid = _job_new("report")
                threading.Thread(target=_job_run, args=(jid, api.report, sid),
                                 daemon=True).start()
                return self._json({"job": jid})

            if p == "/api/job/audit":
                sid = b["sid"]
                jid = _job_new("audit")
                threading.Thread(target=_job_run, args=(jid, api.audit, sid),
                                 daemon=True).start()
                return self._json({"job": jid})

            # L0 采集：6-8 次 Search + 1 次抽取，约 20-60 秒，走后台线程
            if p == "/api/job/collect":
                key = (b.get("key") or "").strip()
                if not key:
                    return self._err("没有填 API Key")
                intake = b.get("intake") or {}
                jid = _job_new("collect")
                threading.Thread(target=_job_run, args=(jid, api.collect, intake, key),
                                 daemon=True).start()
                return self._json({"job": jid})

            return self._err("not found", 404)
        except FileNotFoundError:
            return self._err("会话不存在", 404)
        except ValueError as e:
            # 业务层拒绝（已结束、动作不存在、不能继续…）不是服务器故障
            return self._err(str(e), 400)
        except Exception as e:
            traceback.print_exc()
            return self._err(f"{type(e).__name__}: {e}", 500)

    def _static(self, rel):
        path = os.path.join(WEB, rel)
        if not os.path.abspath(path).startswith(WEB) or not os.path.isfile(path):
            return self._err("not found", 404)
        ctype = {".html": "text/html; charset=utf-8",
                 ".js": "application/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8",
                 ".svg": "image/svg+xml"}.get(os.path.splitext(path)[1],
                                              "application/octet-stream")
        with open(path, "rb") as f:
            self._send(200, f.read(), ctype)


def main():
    ap = argparse.ArgumentParser()
    # 本地双击启动时绑 127.0.0.1（不暴露到局域网）；
    # 托管环境通常只注入 PORT，看到 PORT 就说明是部署，必须绑 0.0.0.0 才通。
    _host = os.environ.get("HOST") or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT") or 8877))
    ap.add_argument("--host", default=_host)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    shown = "127.0.0.1" if a.host in ("0.0.0.0", "::", "") else a.host
    print(f"\n  创业沙盒已启动  →  http://{shown}:{a.port}\n"
          f"  数据目录：{os.path.join(HERE, 'sessions')}\n"
          f"  按 Ctrl+C 停止\n", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止。会话都存好了。\n")


if __name__ == "__main__":
    main()
