"""在已登录页面上下文内调 /api/voices 拉全量音色数据"""
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
                return {"__error__": r.get("description", "")[:800]}
            return r.get("value")

target = find_target()
ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=30)

expr = """fetch('/api/voices', {credentials:'include'}).then(r=>r.json())"""
data = cdp_eval(ws, expr)
s = json.dumps(data, ensure_ascii=False)
print("TOTAL_LEN:", len(s))
print("HEAD:", s[:2000])
# 结构分析
if isinstance(data, dict):
    print("KEYS:", list(data.keys()))
    for k, v in data.items():
        if isinstance(v, list) and v:
            print(f"LIST[{k}] count={len(v)} first_item_keys={list(v[0].keys()) if isinstance(v[0], dict) else type(v[0])}")
            print("FIRST_ITEM:", json.dumps(v[0], ensure_ascii=False)[:1500])
elif isinstance(data, list) and data:
    print("TOP LIST count:", len(data))
    print("FIRST:", json.dumps(data[0], ensure_ascii=False)[:1500])
ws.close()
