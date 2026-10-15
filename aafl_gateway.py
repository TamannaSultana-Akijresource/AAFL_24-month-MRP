"""
aafl_gateway.py
===============
Minimal MCP-over-HTTP client for the iBOS enterprise-api-gateway.

The old DWH direct login (pyodbc / mcp_user) is blocked. This module talks to
the gateway's JSON-RPC endpoint instead and runs read-only SELECT queries.

Credentials are read (in order) from:
  1. environment variables  AAFL_GATEWAY_TOKEN / AAFL_DEVICE_ID
  2. a local, git-ignored file  gateway_credentials.json  (token, device_id)
Never commit real credentials to the repository.
"""
import json
import os
import urllib.error
import urllib.request

DEFAULT_URL = "https://enterprise-api-gateway.ibos.agency/mcp"
_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_credentials():
    tok = os.environ.get("AAFL_GATEWAY_TOKEN")
    dev = os.environ.get("AAFL_DEVICE_ID")
    if tok and dev:
        return tok, dev
    for name in ("gateway_credentials.json",):
        path = os.path.join(_HERE, name)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            tok = tok or data.get("token")
            dev = dev or data.get("device_id")
    if not tok or not dev:
        raise RuntimeError(
            "Gateway credentials not found. Set AAFL_GATEWAY_TOKEN / AAFL_DEVICE_ID "
            "env vars or create gateway_credentials.json next to this script."
        )
    return tok, dev


class GatewayError(RuntimeError):
    pass


class Gateway:
    """Read-only gateway client. One HTTP session is reused across calls."""

    def __init__(self, url=None, timeout=60):
        self.url = (url or os.environ.get("AAFL_GATEWAY_URL") or DEFAULT_URL).rstrip("/")
        self.timeout = timeout
        self.token, self.device = _load_credentials()
        self.session_id = None
        self._msg_id = 0

    # -- low level -----------------------------------------------------------
    def _post(self, payload):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + self.token,
            "X-Device-Id": self.device,
        }
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
        )
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            raise GatewayError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:400]}")
        sid = resp.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid
        return self._parse(resp.read().decode("utf-8"))

    @staticmethod
    def _parse(body):
        body = body.strip()
        if not body:
            return None
        if body.startswith("{"):
            return json.loads(body)
        # SSE framing: collect the last `data:` payload
        data = None
        for line in body.splitlines():
            if line.startswith("data:"):
                data = line[5:].strip()
        if data is None:
            raise GatewayError("Unexpected gateway response: " + body[:200])
        return json.loads(data)

    def _rpc(self, method, params=None, notify=False):
        self._msg_id += 1
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            msg["id"] = self._msg_id
        out = self._post(msg)
        if notify:
            return None
        if out is None:
            return None
        if "error" in out:
            raise GatewayError(str(out["error"]))
        return out.get("result")

    def connect(self):
        if self.session_id is not None:
            return self
        self._rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "aafl-mrp", "version": "1.0"},
            },
        )
        self._rpc("notifications/initialized", {}, notify=True)
        return self

    # -- query helpers -------------------------------------------------------
    def query(self, sql, source="dwh", limit=500):
        """Run a read-only SELECT, return the parsed gateway payload
        {'rows': [...], 'count': n, 'truncated': bool}."""
        self.connect()
        result = self._rpc(
            "tools/call",
            {
                "name": "execute_readonly_query",
                "arguments": {"sql": sql, "source": source, "limit": limit},
            },
        )
        if result is None:
            return {"rows": [], "count": 0, "truncated": False}
        if result.get("isError"):
            raise GatewayError("Query failed: " + json.dumps(result)[:400])
        content = result.get("content") or []
        text = content[0].get("text", "{}") if content else "{}"
        payload = json.loads(text)
        if isinstance(payload, dict) and "error" in payload and "rows" not in payload:
            raise GatewayError(str(payload))
        return payload

    def rows(self, sql, source="dwh", limit=500):
        return self.query(sql, source=source, limit=limit).get("rows", [])


if __name__ == "__main__":
    gw = Gateway()
    print("connected:", gw.url)
    print(gw.rows(
        "SELECT TOP 3 strItemCode, strItemName FROM itm.tblItemArc WHERE intBusinesUnitId=232",
        source="dwh",
    ))
