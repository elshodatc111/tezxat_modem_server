"""Laravel gateway API ning soddalashtirilgan nusxasi (testlar uchun)."""
import json
import threading

import httpx


class FakeHosting:
    TOKEN = "gw_test"

    def __init__(self):
        self.lock = threading.Lock()
        self.messages = {}      # id -> dict(to,text,status,error,modem)
        self.heartbeats = []
        self.pulls = 0
        self.fail_requests = 0   # keyingi N so'rov 500 qaytaradi
        self.next_id = 1

    def add(self, to, text):
        with self.lock:
            mid = self.next_id
            self.next_id += 1
            self.messages[mid] = {"to": to, "text": text, "status": "queued", "error": None, "modem": None}
            return mid

    def status(self, mid):
        return self.messages[mid]["status"]

    def handler(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            if self.fail_requests > 0:
                self.fail_requests -= 1
                return httpx.Response(500, json={"success": False, "error": {"code": "server", "message": "boom"}})
            if request.headers.get("authorization") != f"Bearer {self.TOKEN}":
                return httpx.Response(401, json={"success": False, "error": {"code": "unauthorized", "message": "Invalid gateway token."}})
            path = request.url.path
            body = json.loads(request.content or b"{}")
            if path.endswith("/ping"):
                return httpx.Response(200, json={"success": True, "pong": True})
            if path.endswith("/pull"):
                self.pulls += 1
                out = []
                for mid, m in self.messages.items():
                    if m["status"] == "queued" and len(out) < body.get("limit", 5):
                        m["status"] = "processing"
                        out.append({"id": mid, "to": m["to"], "text": m["text"], "attempt": 1})
                return httpx.Response(200, json={"success": True, "messages": out})
            if path.endswith("/result"):
                applied = 0
                for r in body["results"]:
                    m = self.messages[r["id"]]
                    order = ["queued", "processing", "sent", "delivered"]
                    if r["status"] == "failed" and m["status"] in ("queued", "processing"):
                        m.update(status="failed", error=r.get("error"), modem=r.get("modem"))
                        applied += 1
                    elif r["status"] in ("sent", "delivered") and order.index(m["status"]) < order.index(r["status"]) if m["status"] in order else False:
                        m.update(status=r["status"], modem=r.get("modem"))
                        applied += 1
                return httpx.Response(200, json={"success": True, "applied": applied, "ignored": []})
            if path.endswith("/heartbeat"):
                self.heartbeats.append(body)
                return httpx.Response(200, json={"success": True})
            return httpx.Response(404, json={"success": False, "error": {"code": "not_found", "message": "Not found."}})

    def client_factory(self):
        from app.hosting import HostingClient

        def factory(url, token, timeout=30):
            return HostingClient(url, token, timeout, transport=httpx.MockTransport(self.handler))

        return factory
