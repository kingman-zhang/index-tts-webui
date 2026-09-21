"""探测音色库的数据接口：__NEXT_DATA__、API 端点、试听按钮结构"""
import json, sys, websocket, urllib.request

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
ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=20)

print("== NEXT_DATA? ==")
print(json.dumps(cdp_eval(ws, "JSON.stringify(!!document.getElementById('__NEXT_DATA__'))")))
print("== fetch /api/voices 试探 ==")
expr = """fetch('/api/voices', {credentials:'include'}).then(r=>r.status+' '+r.status.toString()+' :: '+r.headers.get('content-type')).catch(e=>'ERR '+e.message)"""
print(json.dumps(cdp_eval(ws, expr)))
print("== 音色卡片按钮结构 ==")
expr2 = """JSON.stringify((()=>{
  const btns=[...document.querySelectorAll('button')].map(b=>b.getAttribute('aria-label')).filter(Boolean);
  return {btnCount:btns.length, labels:[...new Set(btns)].slice(0,30)};
})())"""
print(json.dumps(cdp_eval(ws, expr2)))
print("== 音频相关元素与事件线索 ==")
expr3 = """JSON.stringify((()=>{
  const els=[...document.querySelectorAll('[class*=play],[data-play],[aria-label*=播放],[aria-label*=试听]')];
  return els.slice(0,10).map(e=>({tag:e.tagName, cls:(e.className||'').toString().slice(0,80), aria:e.getAttribute('aria-label')}));
})())"""
print(json.dumps(cdp_eval(ws, expr3)))
ws.close()
