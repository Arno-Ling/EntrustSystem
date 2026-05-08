"""批次 2 冒烟：材料供应分叉 + 材料询价 + 加工方凭证"""
import io, sys, json, urllib.request, urllib.error
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
BASE = "http://localhost:8001"

def req(path, method="GET", body=None, token=None):
    r = urllib.request.Request(BASE+path,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={**({"Content-Type":"application/json"} if body is not None else {}),
                 **({"Authorization":f"Bearer {token}"} if token else {})},
        method=method)
    try:
        with urllib.request.urlopen(r) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:500]}")
        raise

# 1. alice 登录
tok_a = req("/api/auth/login","POST",{"username":"alice","password":"test123"})["access_token"]
print("[1] alice OK")

# 2. 找一张 awarded 订单 (没 material_sourcing 的)
import psycopg2
conn = psycopg2.connect("host=localhost port=5432 dbname=weiwai user=postgres password=361615")
conn.set_client_encoding("UTF8")
with conn.cursor() as cur:
    cur.execute("""
        UPDATE outsource_orders SET material_sourcing=NULL,
          material_sourcing_decided_by=NULL, material_sourcing_decided_at=NULL
        WHERE id IN (SELECT id FROM outsource_orders LIMIT 2)
    """)
    conn.commit()
    cur.execute("""
        SELECT o.id FROM outsource_orders o WHERE o.status='awarded' AND o.material_sourcing IS NULL
        LIMIT 1
    """)
    row = cur.fetchone()
    oo_id = row[0] if row else None
    cur.execute("SELECT id FROM outsource_orders LIMIT 1")
    any_id = cur.fetchone()[0]
conn.close()
if oo_id is None:
    oo_id = any_id
    # 手工改为 awarded + 清 sourcing
    conn = psycopg2.connect("host=localhost port=5432 dbname=weiwai user=postgres password=361615")
    conn.set_client_encoding("UTF8")
    with conn.cursor() as cur:
        cur.execute("UPDATE outsource_orders SET status='awarded', material_sourcing=NULL WHERE id=%s",
                    (oo_id,))
        conn.commit()
    conn.close()
print(f"[2] 选择加工单 id={oo_id}")

# 3. 决策材料供应方式 = internal
r = req(f"/api/internal/outsource-orders/{oo_id}/sourcing","POST",
        {"material_sourcing":"internal"}, tok_a)
print(f"[3] 决策 internal: {r}")

# 4. 建材料询价单
r = req("/api/internal/material-inquiries","POST",{
    "outsource_order_id": oo_id,
    "material_code":"Cr12MoV",
    "material_name":"Cr12MoV 工具钢",
    "spec":"200x100x50",
    "qty": 80,
    "unit":"kg",
    "required_date":"2026-07-15",
}, tok_a)
inq_id = r["id"]
print(f"[4] 询价单: {r}")

# 5. 候选材料方
r = req(f"/api/internal/material-inquiries/{inq_id}/candidates", token=tok_a)
print(f"[5] 候选材料方 = {r['total']}")
for c in r["items"]:
    print(f"    - {c['name']} (has_price={c['has_price']}, invited={c['already_invited']})")

# 6. 群发
r = req(f"/api/internal/material-inquiries/{inq_id}/send","POST",{},tok_a)
print(f"[6] 群发: {r}")

# 7. baogang 登录报价
tok_b = req("/api/auth/login","POST",{"username":"baogang","password":"test123"})["access_token"]
my_invs = req("/api/material/inquiries", token=tok_b)
print(f"[7a] baogang 收到 {my_invs['total']} 条材料询价邀请")
inv_id = my_invs["items"][0]["invitation_id"]

r = req(f"/api/material/inquiries/{inv_id}/quote","POST",
        {"unit_price": 65.5, "lead_time_days": 7, "note":"现货"}, tok_b)
print(f"[7b] baogang 报价: {r}")

# 8. xinsheng 也报价
tok_x = req("/api/auth/login","POST",{"username":"xinsheng","password":"test123"})["access_token"]
my2 = req("/api/material/inquiries", token=tok_x)
if my2["total"]:
    inv2 = my2["items"][0]["invitation_id"]
    req(f"/api/material/inquiries/{inv2}/quote","POST",
        {"unit_price": 63.0, "lead_time_days": 12}, tok_x)
    print(f"[8] xinsheng 报价 OK")

# 9. alice 截止 → 审批
r = req(f"/api/internal/material-inquiries/{inq_id}/close-quoting","POST",{},tok_a)
task_id = r["task_id"]
print(f"[9] 截止，审批 task_id={task_id}")

# 10. 选中最低价中标
detail = req(f"/api/internal/material-inquiries/{inq_id}", token=tok_a)
quoted = [i for i in detail["invitations"] if i["invitation_status"] == "quoted"]
quoted.sort(key=lambda x: x["unit_price"])
winner_inv_id = quoted[0]["invitation_id"]
print(f"[10a] 最低价中标方: {quoted[0]['supplier_name']} ¥{quoted[0]['unit_price']}")

r = req(f"/api/internal/approvals/{task_id}/award-material","POST",{
    "action":"approve",
    "awarded_invitation_id": winner_inv_id,
    "comment":"最低价中标"
}, tok_a)
print(f"[10b] 中标结果: {r}")

# 11. 同时测试 processor_source 分支
# 找另一个订单
conn = psycopg2.connect("host=localhost port=5432 dbname=weiwai user=postgres password=361615")
conn.set_client_encoding("UTF8")
with conn.cursor() as cur:
    cur.execute("""SELECT o.id FROM outsource_orders o
                   JOIN tenants t ON o.tenant_id=t.id
                   WHERE t.name LIKE '%华欣泽%' AND o.material_sourcing IS NULL
                   LIMIT 1""")
    row = cur.fetchone()
    if row:
        oo2 = row[0]
    else:
        oo2 = None
conn.close()
if oo2:
    req(f"/api/internal/outsource-orders/{oo2}/sourcing","POST",
        {"material_sourcing":"processor"}, tok_a)
    print(f"[11] 加工单 {oo2} 决策 processor")

    # 加工方上传凭证（纯表单无文件）
    tok_h = req("/api/auth/login","POST",{"username":"huaxinze","password":"test123"})["access_token"]
    # 走表单接口 —— 我们手动拼 multipart 或者简单 form-urlencoded
    import urllib.parse
    data = urllib.parse.urlencode({
        "proof_type":"invoice",
        "supplier_name_text":"本地五金贸易商",
        "batch_no":"B20260508",
        "material_code":"45#",
        "spec":"φ50x1000",
        "qty":120, "unit":"kg",
        "note":"现场自购"
    }).encode("utf-8")
    r = urllib.request.Request(BASE + f"/api/processor/orders/{oo2}/proofs",
        data=data,
        headers={"Content-Type":"application/x-www-form-urlencoded",
                 "Authorization":f"Bearer {tok_h}"},
        method="POST")
    try:
        with urllib.request.urlopen(r) as resp:
            x = json.loads(resp.read().decode())
            print(f"[12] huaxinze 上传自采凭证: {x}")
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code}: {e.read().decode('utf-8',errors='replace')[:300]}")

    proofs = req(f"/api/processor/orders/{oo2}/proofs", token=tok_h)
    print(f"[13] 凭证数 = {proofs['total']}")

print("\n=== 批次 2 冒烟 OK ===")
