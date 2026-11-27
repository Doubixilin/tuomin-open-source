"""Seed the isolated screenshot instance with synthetic demo data only."""
import json, os, re, sys, urllib.request
from pathlib import Path
sys.path.insert(0, "src")
N = Path(sys.argv[1]); data = N / "manual-data"
base = (data / "instance.url").read_text().strip().removesuffix("/ui/")
token = (data / "admin-token").read_text().strip()

def call(method, path, body=None, raw=None):
    payload = raw if raw is not None else (json.dumps(body, ensure_ascii=False).encode() if body is not None else None)
    req = urllib.request.Request(base + path, data=payload, method=method, headers={"x-tuomin-admin-token": token, "Content-Type": "application/octet-stream" if raw is not None else "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r: return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read() or b"{}")

entries = json.load(open("demo/synthetic-dictionary.json")) + [
    {"canonical_value": "虚构星橙工程有限公司", "label": "ORG", "aliases": ["星橙工程", "星橙公司"]},
    {"canonical_value": "滨江示范园区改造项目", "label": "PROJECT", "aliases": ["滨江项目"]},
    {"canonical_value": "虚构海岳建材有限公司", "label": "SUPPLIER", "aliases": ["海岳建材"]},
]
print("import", call("POST", "/admin/apps/workbench/dictionary/import", {"format": "json", "content": json.dumps(entries, ensure_ascii=False), "dry_run": False})[1].get("counts"))
(data / "dictionaries" / "review.json").write_text("[]", encoding="utf-8")
print("app", call("PUT", "/admin/apps/contract_review", {"name": "合同评审助手", "profile": "contract_review", "dictionary": str(data / "dictionaries" / "review.json")})[0])
from tuomin_gateway.file_redact import http_post, redact_file
post = http_post(base, token)
out = N / "manual-out"; out.mkdir(exist_ok=True)
tasks = {}
for name in ("synthetic-register.xlsx", "synthetic-contract.docx"):
    r = redact_file(Path("demo/samples") / name, post=post, app_id="workbench", out_dir=out)
    print("redact", name, r["status"], r["message"], r["label_counts"]); tasks[name] = r["task_id"]
from tuomin_gateway.agent_setup import load_agent_tokens
from tuomin_gateway.mcp_stdio import AgentGateway
t = load_agent_tokens(data)
g = AgentGateway(base, t["redact"], t["namespace"], session=True)
a = g.redact_text("agent", "请审阅：虚构星橙工程有限公司与虚构云帆技术有限公司签订的合同 DEMO-2026-001，联系人许星舟，电话 13800138000。")
b = g.redact_text("agent", "补充：星橙工程的付款节点延后 30 天，由林月禾确认。")
print("agent", a["status"], a["masked_text"]); print("agent2", b["masked_text"])
print("codex install", call("POST", "/admin/agents/codex/install", {"with_skill": True})[0])
st, m = call("POST", f"/api/v1/documents/{tasks['synthetic-contract.docx']}/mapping", {})
mapping = {e["placeholder"]: (e["label"], e["original_value"]) for e in m.get("entries", [])}
print("contract placeholders", list(mapping.items())[:12])
json.dump({"tasks": tasks, "agent_masked": [a["masked_text"], b["masked_text"]], "session": a["session_id"], "mapping": mapping}, open(N / "manual-seed.json", "w"), ensure_ascii=False, indent=1)
