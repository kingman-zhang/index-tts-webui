"""通过 CDP 连接已登录的音色库页面，探测页面结构和登录态"""
import json, sys, websocket

def find_target():
    import urllib.request
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
if not target:
    print("NO_TARGET"); sys.exit(1)
print("TARGET:", target["url"], "|", target.get("title", ""))
ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=15)

# 1. 登录态 / localStorage
print("== localStorage keys ==")
print(json.dumps(cdp_eval(ws, "JSON.stringify(Object.keys(localStorage))"), ensure_ascii=False))
# 2. 页面概况
print("== body text head ==")
print(json.dumps(cdp_eval(ws, "document.body.innerText.slice(0, 3000)"), ensure_ascii=False))
# 3. 卡片/音色元素试探
print("== audio elements ==")
print(json.dumps(cdp_eval(ws, "JSON.stringify([...document.querySelectorAll('audio')].map(a=>({src:a.currentSrc||a.src,paused:a.paused})))"), ensure_ascii=False))
ws.close()
