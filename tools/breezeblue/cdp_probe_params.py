"""探测 /api/voices 的过滤参数：language/zh"""
import json, websocket, urllib.request

def find_target():
    data = json.load(urllib.request.urlopen("http://127.0.0.1:9222/json/list"))
    for t in data:
        if t.get("type") == "page" and "voice-library" in t.get("url", ""):
            return t
    return None

def cdp_eval(ws, expr):
    ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                        "params": {"expression": expr, "returnByValue": True, "awaitPromise": True}}))
    while True:
        msg = json.loads(ws.recv())
        if msg.get("id") == 1:
            r = msg.get("result", {}).get("result", {})
            if r.get("subtype") == "error":
                return {"__error__": r.get("description", "")[:500]}
            return r.get("value")

target = find_target()
ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=30)

for q in ["language=zh", "lang=zh", "locale=zh", "languages=zh", "language=zh-CN", "language=cmn"]:
    expr = f"fetch('/api/voices?page=1&page_size=5&{q}', {{credentials:'include'}}).then(r=>r.json())"
    d = cdp_eval(ws, expr)
    if isinstance(d, dict) and "items" in d:
        langs = set(i.get("language") for i in d["items"])
        print(f"{q:18} -> total={d.get('total')} langs={langs}")
    else:
        print(f"{q:18} -> BAD: {json.dumps(d, ensure_ascii=False)[:120]}")
ws.close()
