"""Synthetic frozen-shell/demo checks, independent of production licensing."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import locale
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener
from xml.etree import ElementTree as ET
import zipfile


SAMPLES = ("synthetic-contract.docx", "synthetic-register.xlsx")
SYNTHETIC_ORG = "虚构星橙工程有限公司"


def log_contains_values(path, values):
    """Scan raw logs without discarding undecodable bytes or Windows text."""
    payload = Path(path).read_bytes()
    system_encoding = locale.getencoding() if hasattr(locale, "getencoding") else locale.getpreferredencoding(False)
    encodings = {"utf-8", "utf-16-le", "utf-16-be", "gbk", "gb18030", system_encoding}
    for value in values:
        if not value:
            continue
        for encoding in encodings:
            try:
                encoded = value.encode(encoding)
            except (UnicodeEncodeError, LookupError):
                continue
            if encoded in payload:
                return True
    return False


def demo_storage_isolated(settings, directory):
    """Windows keeps DPAPI even for disposable demos; other platforms use plaintext."""
    encrypted = settings.get("mapping_store", {}).get("encrypted_at_rest")
    mapping_dir = settings.get("mapping_dir")
    if not isinstance(mapping_dir, str) or not mapping_dir:
        return False
    return (encrypted is (os.name == "nt")
            and Path(mapping_dir).resolve().is_relative_to(Path(directory).resolve()))


def owner_private(path):
    """Check the actual Windows DACL; POSIX mode bits do not secure NTFS."""
    if os.name != "nt":
        return Path(path).stat().st_mode & 0o777 == 0o600
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if not powershell:
        return False
    script = r"""
$acl = Get-Acl -LiteralPath $env:TUOMIN_ACCEPTANCE_ACL_TARGET
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$rules = @($acl.Access)
$sids = @($rules | ForEach-Object { $_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value })
$valid = $acl.AreAccessRulesProtected -and $rules.Count -eq 2 -and ($sids -contains $sid) -and ($sids -contains 'S-1-5-18')
foreach ($rule in $rules) {
    $valid = $valid -and $rule.AccessControlType -eq 'Allow' -and -not $rule.IsInherited -and ($rule.FileSystemRights -band 2032127) -eq 2032127
}
if ($valid) { 'true' } else { 'false' }
"""
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
        env={**os.environ, "TUOMIN_ACCEPTANCE_ACL_TARGET": str(path)},
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=15,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def snapshot(directory):
    """Read only the isolated synthetic formal directory supplied by the caller."""
    result = {}
    for path in Path(directory).rglob("*"):
        relative = str(path.relative_to(directory))
        stat = path.stat()
        if path.is_file():
            result[relative] = (stat.st_mode & 0o777, stat.st_mtime_ns,
                                hashlib.sha256(path.read_bytes()).hexdigest())
        elif path.is_dir():
            result[relative] = (stat.st_mode & 0o777, "directory")
    return result


class Client:
    def __init__(self, origin, token="", timeout=120):
        self.origin, self.token, self.timeout = origin, token, timeout
        self.opener = build_opener(ProxyHandler({}))

    def request(self, method, path, payload=None, *, raw=None, capability=None):
        headers = {"X-Requested-With": "tuomin-acceptance"}
        if self.token:
            headers["x-tuomin-admin-token"] = self.token
        if capability:
            headers["x-tuomin-capability-token"] = capability
        body = raw
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        request = Request(self.origin + path, data=body, method=method, headers=headers)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return response.status, response.read()
        except HTTPError as error:
            return error.code, error.read()

    def json(self, method, path, payload=None, **kwargs):
        status, body = self.request(method, path, payload, **kwargs)
        return status, json.loads(body)


def office_text(data, file_format):
    """Compare all visible synthetic text nodes, without optional Office libraries."""
    prefix = "word/" if file_format == "docx" else "xl/"
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return tuple(
            (name, tuple(node.text or "" for node in ET.fromstring(archive.read(name)).iter()
                         if node.tag.rsplit("}", 1)[-1] == "t"))
            for name in sorted(archive.namelist())
            if name.startswith(prefix) and name.endswith(".xml")
        )


def _wait_demo(process, temporary, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and process.poll() is None:
        # Windows venv launchers may spawn a child whose PID differs from
        # Popen.pid. This temp root belongs only to this acceptance child.
        for directory in temporary.glob("tuomin-demo-*"):
            try:
                origin = (directory / "instance.url").read_text(encoding="utf-8").strip().removesuffix("/ui/")
                if Client(origin, timeout=2).json("GET", "/healthz")[0] == 200:
                    return directory, origin
            except (OSError, ValueError):
                pass
        time.sleep(.1)
    return None, None


def _stop(process):
    if process.poll() is not None:
        return True
    pipe = getattr(process, "stdin", None)
    if pipe is not None:
        pipe.close()
    else:
        process.terminate()
    try:
        process.wait(timeout=15)
        return pipe is not None or os.name != "nt"
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        return False


def _demo_text_roundtrip(client, check):
    # Create a capability-scoped test app in the disposable demo registry only.
    status, source = client.json("GET", "/admin/apps/demo_contract")
    check("演示合成词典配置可读取", status == 200)
    if status != 200:
        return []
    credentials = {name: secrets.token_urlsafe(32) for name in ("redact", "trusted_refill")}
    # The public response also includes derived read-only token metadata.
    entry = {key: source["entry"][key] for key in ("profile", "dictionary")}
    entry.update(capabilities=list(credentials),
                 capability_tokens={name: "sha256:" + hashlib.sha256(value.encode()).hexdigest()
                                    for name, value in credentials.items()},
                 mapping_scopes=["document"], refill_contracts=["trusted_display"])
    status, _ = client.json("PUT", "/admin/apps/accept_demo", entry)
    check("演示隔离 app 注册", status == 200)
    if status != 200:
        return list(credentials.values())
    text = f"{SYNTHETIC_ORG}与许星舟签订DEMO-2026-001，联系电话13900002222。"
    status, redacted = client.json("POST", "/api/v1/redact", {"app_id": "accept_demo", "text": text},
                                  capability=credentials["redact"])
    masked = redacted.get("masked_text", "")
    success = (status == 200 and redacted.get("egress_allowed") is True
               and all(value not in masked for value in (SYNTHETIC_ORG, "许星舟", "13900002222"))
               and "<" in masked)
    check("演示真实规则与合成词典脱敏", success)
    if success:
        status, restored = client.json("POST", "/api/v1/refill", {
            "app_id": "accept_demo", "contract": "trusted_display",
            "mapping_handle": redacted["mapping_handle"], "text": masked,
        }, capability=credentials["trusted_refill"])
        check("演示 trusted_display 文本往返", status == 200 and restored.get("text") == text)
    return list(credentials.values())


def _demo_office_roundtrip(client, samples, check, *, lite):
    passphrase = secrets.token_urlsafe(24)
    for name, raw in samples.items():
        file_format = Path(name).suffix.lstrip(".")
        suffix = "docx" if file_format == "docx" else "office"
        status, redacted = client.json("POST", f"/api/v1/documents/redact-{suffix}?filename={quote(name)}&app_id=demo_contract", raw=raw)
        redacted_key = f"redacted_{suffix}_b64"
        if lite:
            check(f"Lite {file_format.upper()} 缺 NER 时保持导出阻断",
                  status == 422 and redacted.get("egress_allowed") is False
                  and redacted_key not in redacted)
            continue
        success = status == 200 and redacted.get("egress", {}).get("egress_allowed") is True and bool(redacted.get(redacted_key))
        check(f"演示 {file_format.upper()} 原格式脱敏与出站闸门", success, f"http={status}")
        if not success:
            continue
        masked = base64.b64decode(redacted[redacted_key], validate=True)
        texts = repr(office_text(masked, file_format))
        check(f"演示 {file_format.upper()} 合成原值已替换", SYNTHETIC_ORG not in texts and "<" in texts)
        status, package = client.json("POST", f"/api/v1/documents/{redacted['task_id']}/package", {"passphrase": passphrase})
        check(f"演示 {file_format.upper()} 加密恢复包导出", status == 200 and bool(package.get("package")))
        if status != 200 or not package.get("package"):
            continue
        status, restored = client.json("POST", f"/api/v1/documents/refill-{suffix}", {
            "package": package["package"], "passphrase": passphrase,
            redacted_key: redacted[redacted_key], "file_format": file_format,
        })
        restored_data = base64.b64decode(restored.get(f"restored_{suffix}_b64", ""))
        success = status == 200 and restored.get("status") == "ok" and bool(restored_data)
        check(f"演示 {file_format.upper()} 文本完整往返", success and office_text(restored_data, file_format) == office_text(raw, file_format))
    return passphrase


def _desktop_check(binary, env, work, check):
    selftest_log = Path(work) / "shell-selftest.log"
    try:
        with selftest_log.open("w", encoding="utf-8") as output:
            completed = subprocess.run([str(binary), "--shell-selftest"], env=env,
                                       stdout=output, stderr=subprocess.STDOUT, timeout=60)
        check("冻结壳隐藏窗口与菜单栏自检", completed.returncode == 0
              and b"[shell-selftest] OK" in selftest_log.read_bytes())
    except (OSError, subprocess.TimeoutExpired):
        check("冻结壳隐藏窗口与菜单栏自检", False, "无法启动或超时")


def run_shell_checks(binary, *, env, formal_data_dir, formal_origin, work, check, lite=False,
                     desktop=True, skip=None):
    """Run even without an acceptance license; never touch normal user data."""
    binary, work, formal_data_dir = Path(binary), Path(work), Path(formal_data_dir)
    if desktop:
        _desktop_check(binary, env, work, check)
    elif skip is not None:
        skip("冻结壳隐藏窗口与菜单栏自检", "显式 --skip-desktop；尚未验证桌面显示与交互")
    else:
        raise ValueError("Skipping desktop requires an explicit skip reporter")

    temporary = work / "demo-temporary"
    temporary.mkdir(mode=0o700)
    before = snapshot(formal_data_dir)
    demo_log, directory = work / "demo.log", None
    sensitive = []
    with demo_log.open("w", encoding="utf-8") as output:
        process = subprocess.Popen([str(binary), "--headless", "--demo", "--shutdown-on-stdin-close"],
                                   env={**env, "TMPDIR": str(temporary), "TEMP": str(temporary), "TMP": str(temporary)},
                                   stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            directory, origin = _wait_demo(process, temporary)
            check("冻结 --demo --headless 启动", bool(origin))
            if not origin:
                return
            check("演示与正式实例端口隔离", origin != formal_origin)
            check("演示使用独立锁文件", (directory / "tuomin-demo.lock").is_file()
                  and not (directory / "tuomin.lock").exists())
            token_path = directory / "admin-token"
            token = next(line.strip() for line in token_path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#"))
            sensitive.append(token)
            check("演示管理凭证仅当前用户可访问", owner_private(token_path))
            client = Client(origin, token)
            status, mode = client.json("GET", "/api/v1/runtime-mode")
            check("演示模式标记", status == 200 and mode.get("demo") is True)
            status, settings = client.json("GET", "/admin/settings")
            check("演示临时存储隔离且符合平台加密策略", status == 200
                  and demo_storage_isolated(settings, directory))
            status, page = client.request("GET", "/ui/")
            check("演示页面包含演示模式与退出清理横幅", status == 200 and "演示模式" in page.decode()
                  and "退出即清除" in page.decode())
            samples = {}
            for name in SAMPLES:
                status, body = client.request("GET", "/demo/samples/" + quote(name))
                valid = status == 200 and zipfile.is_zipfile(io.BytesIO(body))
                check("演示样例下载 " + Path(name).suffix[1:].upper(), valid)
                if valid:
                    samples[name] = body
            sensitive.extend(_demo_text_roundtrip(client, check))
            sensitive.append(_demo_office_roundtrip(client, samples, check, lite=lite))
            check("演示运行期间正式目录无改动", snapshot(formal_data_dir) == before)
            check("演示期间正式实例仍健康", Client(formal_origin).json("GET", "/healthz")[0] == 200)
        except (OSError, ValueError, KeyError, zipfile.BadZipFile):
            check("演示验收流程完整执行", False, "读取隔离服务响应或合成样例失败")
        finally:
            graceful = _stop(process)
            check("演示进程正常退出", graceful and process.returncode == 0)
            check("演示退出清理临时目录", directory is not None and not directory.exists()
                  and not list(temporary.glob("tuomin-demo-*")))
            check("演示退出后正式目录无改动", snapshot(formal_data_dir) == before)
    check("演示日志无凭证和合成原值", not log_contains_values(
        demo_log, [*sensitive, SYNTHETIC_ORG, "许星舟"]))
