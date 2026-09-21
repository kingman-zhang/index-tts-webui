"""分页拉取 /api/voices 全量数据并保存"""
import json, websocket, urllib.request, sys

OUT = "/Users/zhangjianwen/Documents/Kingman/workbuddy/index-tts/podcast-webui/tools/breezeblue/voices_all.json"

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
ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=60)

all_items = []
page = 1
total = None
while True:
    expr = f"fetch('/api/voices?page={page}&page_size=50', {{credentials:'include'}}).then(r=>r.json())"
    data = cdp_eval(ws, expr)
    if not isinstance(data, dict) or "items" not in data:
        print(f"page {page}: BAD RESPONSE {json.dumps(data, ensure_ascii=False)[:200]}")
        break
    items = data["items"]
    total = data.get("total")
    all_items.extend(items)
    print(f"page {page}: +{len(items)} (cum {len(all_items)}, total~{total}, has_more={data.get('has_more')})")
    if not data.get("has_more") or not items or page >= 40:
        break
    page += 1

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(all_items, f, ensure_ascii=False, indent=1)
print(f"SAVED {len(all_items)} voices -> {OUT}")

# 统计
from collections import Counter
langs = Counter(v.get("language") for v in all_items)
print("语言分布:", dict(langs.most_common()))
zh = [v for v in all_items if (v.get("language") or "").startswith("zh")]
print(f"中文音色: {len(zh)}")
cats = Counter((v.get("primary_category") or {}).get("name") for v in all_items)
print("分类分布:", dict(cats.most_common(15)))
ws.close()
