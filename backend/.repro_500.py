"""复现审批 500：拿个待办任务，调它的同意接口"""
import io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

import urllib.request, urllib.error, json

BASE = "http://127.0.0.1:8001"

def login(u, p):
    req = urllib.request.Request(f"{BASE}/api/auth/login",
        data=json.dumps({"username": u, "password": p}).encode(),
        headers={"Content-Type":"application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=5).read())

def api(token, path, method="GET", body=None):
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")

tok = login("alice", "test123")["access_token"]

# 1. 列待办
code, pending = api(tok, "/api/internal/approvals/pending")
print(f"待办数: {code} {len(pending.get('items', []))}")
if not pending.get("items"):
    print("没有待办任务，先创建一个决策项目或委外请求再试")
    sys.exit(0)

# 2. 取第一个任务看详情
task = pending["items"][0]
print(f"任务: id={task['id']} kind={task.get('kind')} subject_id={task.get('subject_id')}")

code, detail = api(tok, f"/api/internal/approvals/{task['id']}")
print(f"详情: {code}")

# 3. 尝试同意
print("\n>>> 触发 approve")
code, resp = api(tok, f"/api/internal/approvals/{task['id']}/action",
                 "POST", {"action": "approve", "comment": "smoke test"})
print(f"approve HTTP {code}")
print(f"返回: {resp}")
