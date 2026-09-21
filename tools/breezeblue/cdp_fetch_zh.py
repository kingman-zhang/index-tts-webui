"""分页拉取全部中文音色 + 下载 preview wav"""
import json, websocket, urllib.request, os, time, sys

OUT_DIR = "/Users/zhangjianwen/Documents/Kingman/workbuddy/index-tts/podcast-webui/tools/breezeblue"
JSON_OUT = os.path.join(OUT_DIR, "voices_zh.json")
AUDIO_DIR = os.path.join(OUT_DIR, "zh_audio")
os.makedirs(AUDIO_DIR, exist_ok=True)

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
ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=60)

# 探测 page_size 上限
expr = "fetch('/api/voices?page=1&page_size=100&language=zh', {credentials:'include'}).then(r=>r.json())"
d = cdp_eval(ws, expr)
ps = len(d.get("items", [])) if isinstance(d, dict) else 0
print(f"page_size=100 实际返回 {ps} 条, total={d.get('total')}")
page_size = 100 if ps > 50 else 50

all_items = []
page = 1
while True:
    expr = f"fetch('/api/voices?page={page}&page_size={page_size}&language=zh', {{credentials:'include'}}).then(r=>r.json())"
    data = cdp_eval(ws, expr)
    if not isinstance(data, dict) or "items" not in data:
        print(f"page {page}: BAD {json.dumps(data, ensure_ascii=False)[:150]}"); break
    items = data["items"]
    all_items.extend(items)
    print(f"page {page}: +{len(items)} (cum {len(all_items)}/{data.get('total')})")
    if not data.get("has_more") or not items or page >= 20:
        break
    page += 1

with open(JSON_OUT, "w", encoding="utf-8") as f:
    json.dump(all_items, f, ensure_ascii=False, indent=1)
print(f"SAVED {len(all_items)} zh voices -> {JSON_OUT}")

from collections import Counter
print("性别:", dict(Counter(v.get("gender") for v in all_items)))
print("分类:", dict(Counter((v.get('primary_category') or {}).get('name') for v in all_items).most_common()))
print("age:", dict(Counter(v.get("age") for v in all_items).most_common()))

# 下载 preview wav（带引用头，跳过已存在）
import urllib.error
ok, fail = 0, 0
for i, v in enumerate(all_items):
    url = v.get("preview_audio_url")
    if not url:
        continue
    fn = os.path.join(AUDIO_DIR, f"{v['public_id']}.wav")
    if os.path.exists(fn) and os.path.getsize(fn) > 1000:
        ok += 1
        continue
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://breeze.blue/"})
        with urllib.request.urlopen(req, timeout=30) as r, open(fn, "wb") as f:
            f.write(r.read())
        ok += 1
    except Exception as e:
        fail += 1
        print(f"FAIL {v['public_id']}: {e}")
    if (i+1) % 20 == 0:
        print(f"... {i+1}/{len(all_items)} ok={ok} fail={fail}")
        time.sleep(0.3)
print(f"下载完成 ok={ok} fail={fail}")
total_size = sum(os.path.getsize(os.path.join(AUDIO_DIR,f)) for f in os.listdir(AUDIO_DIR))
print(f"音频总大小: {total_size/1024/1024:.1f} MB")
ws.close()
