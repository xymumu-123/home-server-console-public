# -*- coding: utf-8 -*-
"""Let's Encrypt 证书签发/续期脚本 (DNS-01, west.cn API 自动添删 TXT)

用法:
    python acme_issue.py --staging     # 测试签发 (LE staging, 不占生产限频)
    python acme_issue.py               # 生产签发/续期 (计划任务每天跑, 30 天内到期才真正签)

流程: 账户密钥(自动生成) -> new_order -> DNS-01 (west.cn add TXT -> 权威 NS 轮询 ->
answer) -> finalize(CSR) -> 证书落盘 -> remove TXT -> 续期成功时触发服务重启。
产物: D:\\Monitor\\certs\\pc2.jzlspace.top.fullchain.pem + .key.pem

日志: D:\\Monitor\\acme\\acme.log (追加, 256KB 自动截断)
"""
import base64
import datetime
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, r"D:\Monitor\app")

import requests
from acme import challenges, client, errors as acme_errors, messages
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID
from josepy import JWK

DOMAIN = "pc2.jzlspace.top"
ACME_DIR = r"D:\Monitor\acme"
CERT_DIR = r"D:\Monitor\certs"
ACCOUNT_KEY = os.path.join(ACME_DIR, "account.key")
LOG_PATH = os.path.join(ACME_DIR, "acme.log")
URLS = {
    "staging": "https://acme-staging-v02.api.letsencrypt.org/directory",
    "prod": "https://acme-v02.api.letsencrypt.org/directory",
}
RENEW_DAYS_LEFT = 30          # 剩余不足 30 天才重新签
PROPAGATION_TIMEOUT = 120     # 等权威 NS 出现 TXT 的上限 (秒)
AUTH_NS = ["ns1.myhostadmin.net", "ns2.myhostadmin.net"]

# ---------------------------------------------------------------- 日志
def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg)
    print(line, flush=True)
    try:
        old = io.open(LOG_PATH, encoding="utf-8", errors="ignore").read()[-250000:] \
            if os.path.exists(LOG_PATH) else ""
        with io.open(LOG_PATH, "w", encoding="utf-8") as f:
            f.write(old + line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- west.cn DNS API
import app_config
_DDNS = app_config.load()["ddns"]


def _west_api(extra_params):
    """调 west.cn DNS API; 返回 (ok, body_dict)"""
    base = _DDNS["api_key"].replace("#{domain}", _DDNS["domain"]).replace("#{ip}", "0.0.0.0")
    items = [[k, v] for k, v in (kv.split("=", 1) for kv in base.split("&"))]
    for k, v in extra_params.items():
        for it in items:
            if it[0] == k:
                it[1] = v
                break
        else:
            items.append([k, v])
    qs = "&".join(k + "=" + v for k, v in items)
    s = requests.Session()
    s.trust_env = False
    r = s.get(_DDNS["api_url"] + "?" + qs, timeout=20)
    return r.ok and r.json().get("code") == 200, r.json()


def dns_txt_add(record_name, value):
    ok, body = _west_api({"act": "dnsrec.add", "hostname": record_name,
                          "record_type": "TXT", "record_value": value})
    rid = (body.get("body") or {}).get("record_id")
    log(f"  TXT add {record_name}: ok={ok} id={rid}")
    return rid


def dns_txt_remove(record_name, record_id=None):
    params = {"act": "dnsrec.remove", "hostname": record_name, "record_type": "TXT"}
    if record_id:
        params["record_id"] = str(record_id)
    ok, body = _west_api(params)
    log(f"  TXT remove {record_name} id={record_id}: ok={ok}")
    return ok


def dns_txt_preclean(record_name):
    """清掉同名残留 TXT (异常中断可能遗留), 兼容多种字段名拼写"""
    ok, body = _west_api({"act": "dnsrec.list", "hostname": record_name})
    if not ok:
        return
    items = ((body.get("body") or {}).get("items")) or []
    for it in items:
        rid = it.get("record_id") or it.get("id") or it.get("rid")
        host = str(it.get("hostname") or it.get("host") or it.get("name") or "")
        rtype = str(it.get("record_type") or it.get("type") or "")
        if rid and rtype.upper() == "TXT" and host.rstrip(".") == record_name.rstrip("."):
            dns_txt_remove(record_name, rid)


def dns_propagated(record_name, value, timeout=PROPAGATION_TIMEOUT):
    """轮询权威 NS, 确认 TXT 已对外可见 (LE 会从多个观测点验证)"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for ns in AUTH_NS:
            try:
                out = subprocess.run(["nslookup", "-type=TXT", record_name, ns],
                                     capture_output=True, text=True, timeout=10).stdout
                if value in out:
                    log(f"  TXT 已在 {ns} 生效")
                    return True
            except (OSError, subprocess.TimeoutExpired):
                pass
        time.sleep(10)
    return False


# ---------------------------------------------------------------- ACME
def load_or_create_account_key():
    """账户密钥: RSA-2048 (acme ClientNetwork 默认 RS256 签名算法)"""
    if os.path.exists(ACCOUNT_KEY):
        with open(ACCOUNT_KEY, "rb") as f:
            return serialization.load_pem_private_key(f.read(), password=None)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with open(ACCOUNT_KEY, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    return key


def make_csr(key, domains):
    builder = x509.CertificateSigningRequestBuilder().subject_name(
        x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domains[0])]))
    builder = builder.add_extension(
        x509.SubjectAlternativeName([x509.DNSName(d) for d in domains]), critical=False)
    return builder.sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM)


def cert_days_left(cert_path):
    with open(cert_path, "rb") as f:
        cert = x509.load_pem_x509_certificate(f.read())
    return (cert.not_valid_after_utc - datetime.datetime.now(datetime.timezone.utc)).days


def issue(staging=False):
    os.makedirs(CERT_DIR, exist_ok=True)
    cert_path = os.path.join(CERT_DIR, DOMAIN + ".fullchain.pem")
    key_path = os.path.join(CERT_DIR, DOMAIN + ".key.pem")

    if os.path.exists(cert_path) and os.path.exists(key_path):
        left = cert_days_left(cert_path)
        if left > RENEW_DAYS_LEFT:
            log(f"证书剩余 {left} 天 (> {RENEW_DAYS_LEFT}), 无需续期")
            return "skip"

    mode = "staging" if staging else "prod"
    log(f"=== ACME 签发开始 ({mode}) ===")
    acc_key = load_or_create_account_key()
    acc_pem = acc_key.private_bytes(serialization.Encoding.PEM,
                                    serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
    jwk = JWK.load(acc_pem)                              # 签名必须用私钥 JWK
    net = client.ClientNetwork(jwk, user_agent="Monitor-home-server/1.0")
    directory = messages.Directory.from_json(net.get(URLS[mode]).json())
    acme = client.ClientV2(directory, net=net)

    reg = messages.NewRegistration.from_data(
        terms_of_service_agreed=bool(directory.meta.terms_of_service))
    try:
        regr = acme.new_account(reg)
    except acme_errors.ConflictError as e:                     # 账户已存在: 用 Location 绑定
        regr = messages.RegistrationResource(uri=e.location, body=reg)
        log("账户已存在, 复用")
    acme.net.account = regr                               # 后续请求用 KID 定位账户
    log(f"账户 OK: {regr.uri}")

    domain_key = ec.generate_private_key(ec.SECP256R1())
    csr_pem = make_csr(domain_key, [DOMAIN])

    order = acme.new_order(csr_pem)        # acme 4.x: 域名列表直接从 CSR SAN 解析
    dns_chall = None
    for authz in order.authorizations:
        for ch in authz.body.challenges:
            if isinstance(ch.chall, challenges.DNS01):
                dns_chall = ch
    if dns_chall is None:
        raise RuntimeError("订单里没有 DNS-01 挑战")
    resp, txt_value = dns_chall.chall.response_and_validation(
        net.key)  # type: (messages.ChallengeResponse, str)
    record_name = dns_chall.chall.validation_domain_name(DOMAIN)
    log(f"挑战: TXT {record_name} = {txt_value[:20]}...")

    dns_txt_preclean(record_name)
    rid = dns_txt_add(record_name, txt_value)
    try:
        if not dns_propagated(record_name, txt_value):
            raise RuntimeError("TXT 传播超时")
        acme.answer_challenge(dns_chall, resp)
        deadline = datetime.datetime.now() + datetime.timedelta(seconds=180)
        finalized = acme.finalize_order(order, deadline)   # CSR 已随订单携带
        fullchain = finalized.fullchain_pem
        log("订单完成, 证书已取回")
    finally:
        time.sleep(3)                    # 给 LE 多点验证观测点的余量
        dns_txt_remove(record_name, rid)

    with open(cert_path, "w", encoding="ascii") as f:
        f.write(fullchain)
    with open(key_path, "wb") as f:
        f.write(domain_key.private_bytes(serialization.Encoding.PEM,
                                         serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    left = cert_days_left(cert_path)
    log(f"=== 签发完成: {cert_path} (剩余 {left} 天) ===")
    return "renewed"


def restart_web():
    """新证书需要重启 uvicorn 才加载; 脚本在 SYSTEM 计划任务里跑, 直接控制任务"""
    log("触发服务重启以加载新证书...")
    for cmd in (["schtasks", "/End", "/TN", "MonitorWeb"],
                ["schtasks", "/Run", "/TN", "MonitorWeb"]):
        subprocess.run(cmd, capture_output=True, text=True, timeout=30)


if __name__ == "__main__":
    staging = "--staging" in sys.argv
    try:
        result = issue(staging=staging)
        if result == "renewed" and not staging:
            restart_web()
        sys.exit(0)
    except Exception as e:
        log(f"!!! 签发失败: {e!r}")
        sys.exit(1)
