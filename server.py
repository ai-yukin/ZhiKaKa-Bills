"""
知卡卡账单 — 本地信用卡管理工具

功能：
1. 静态文件服务（前端 HTML/CSS/JS）
2. 数据持久化 API（本地 JSON 文件）
3. 邮件账单刷新 API（连接 QQ 邮箱 IMAP 抓取账单）
4. 邮箱配置 API

启动方式：
    python server.py

默认端口：5000
访问地址：http://localhost:5000
"""

import os
import sys
import json
import traceback
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

from bill_parser import parse_bill_email, html_to_text

app = Flask(__name__, static_folder='web')
CORS(app)

# ============================================================
# 全局异常包装：避免 5xx 裸堆栈，统一返回 JSON
# ============================================================

@app.errorhandler(404)
def not_found(e):
    return jsonify({"status": "error", "message": "接口不存在", "code": 404}), 404

@app.errorhandler(Exception)
def handle_exception(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return jsonify({"status": "error", "message": str(e.description), "code": e.code}), e.code
    traceback.print_exc()
    return jsonify({
        "status": "error",
        "message": f"服务器内部错误: {type(e).__name__}: {str(e)}",
        "code": 500
    }), 500

# 数据文件路径（放在 data 目录下，不提交到 Git）
BASE_DIR = Path(__file__).parent
DATA_FILE = BASE_DIR / "data" / "zhikaka_data.json"

# 邮箱 IMAP 默认配置（实际使用时在前端设置页配置）
DEFAULT_IMAP_SERVER = "imap.qq.com"
DEFAULT_IMAP_PORT = 993
SEARCH_DAYS = 60  # 搜索最近多少天的邮件


def load_data():
    """从本地 JSON 文件加载数据"""
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "cards": [],
        "bills": [],
        "messages": [],
        "settings": {
            "remindTime": "09:00",
            "remindOn": True,
            "email": "",
            "authCode": ""
        },
        "lastRefresh": None
    }


import threading
_save_lock = threading.Lock()

def save_data(data):
    """保存数据到本地 JSON 文件（原子写入，防止并发损坏）"""
    try:
        with _save_lock:
            DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
            # 先写入临时文件，再原子替换，防止并发写入导致文件损坏
            tmp_file = DATA_FILE.with_suffix('.json.tmp')
            with open(tmp_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            # 原子替换（Windows 上 os.replace 是原子操作）
            os.replace(tmp_file, DATA_FILE)
        return True
    except Exception as e:
        print(f"保存数据失败: {e}")
        return False


# ============================================================
# API 路由
# ============================================================

@app.route("/")
def index():
    """首页 — 返回前端 HTML（使用绝对路径，避免从其他目录启动时找不到文件）"""
    return send_from_directory(str(BASE_DIR / "web"), "index.html")


@app.route("/api/health", methods=["GET"])
def health():
    """健康检查"""
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})


@app.route("/api/data", methods=["GET"])
def get_data():
    """获取所有数据"""
    return jsonify(load_data())


@app.route("/api/data", methods=["POST"])
def post_data():
    """保存所有数据"""
    data = request.get_json(force=True, silent=True) or {}
    allowed_keys = {"cards", "bills", "messages", "settings", "lastRefresh"}
    existing = load_data()
    for key in allowed_keys:
        if key in data:
            existing[key] = data[key]
    if save_data(existing):
        return jsonify({"status": "ok"})
    return jsonify({"status": "error", "message": "保存失败"}), 500


@app.route("/api/config", methods=["GET"])
def get_config():
    """获取邮箱配置（不含敏感信息）"""
    data = load_data()
    settings = data.get("settings", {})
    return jsonify({
        "email": settings.get("email", ""),
        "hasAuthCode": bool(settings.get("authCode", ""))
    })


@app.route("/api/config", methods=["POST"])
def post_config():
    """保存邮箱配置"""
    payload = request.get_json(force=True, silent=True) or {}
    data = load_data()
    if "settings" not in data:
        data["settings"] = {}
    if "email" in payload:
        data["settings"]["email"] = payload["email"]
    if "authCode" in payload:
        data["settings"]["authCode"] = payload["authCode"]
    save_data(data)
    return jsonify({"status": "ok"})


@app.route("/api/email-test", methods=["POST"])
def email_test():
    """轻量邮箱连通性测试：仅做 IMAP 登录验证，不拉取/解析邮件，秒级返回"""
    data = load_data()
    settings = data.get("settings", {})
    email_addr = settings.get("email", "")
    auth_code = settings.get("authCode", "")
    if not email_addr or not auth_code:
        return jsonify({
            "status": "error",
            "message": "邮箱配置不完整，请先在设置中填写邮箱账号和授权码"
        }), 400
    try:
        import imaplib
        mail = imaplib.IMAP4_SSL(DEFAULT_IMAP_SERVER, DEFAULT_IMAP_PORT, timeout=8)
        mail.login(email_addr, auth_code)
        mail.logout()
        return jsonify({"status": "ok", "message": "邮箱连接成功"})
    except imaplib.IMAP4.error as e:
        return jsonify({"status": "error", "message": f"邮箱连接失败: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": f"邮箱连接失败: {str(e)}"}), 500


@app.route("/api/refresh", methods=["POST"])
def refresh_bills():
    """
    刷新账单 — 连接邮箱拉取最新账单邮件
    从本地 settings 中读取邮箱配置
    """
    data = load_data()
    settings = data.get("settings", {})
    email_addr = settings.get("email", "")
    auth_code = settings.get("authCode", "")

    if not email_addr or not auth_code:
        return jsonify({
            "status": "error",
            "message": "邮箱配置不完整，请先在设置中填写邮箱账号和授权码"
        }), 400

    try:
        import imaplib
        import email
        import email.header
        from datetime import timedelta
        from email.utils import parsedate_to_datetime

        # 银行账单邮件关键词规则
        BANK_RULES = [
            {"bank": "招商银行", "sender_keywords": ["cmbchina.com", "95555"], "subject_keywords": ["信用卡", "账单", "还款", "对账单"]},
            {"bank": "工商银行", "sender_keywords": ["icbc.com.cn"], "subject_keywords": ["信用卡", "账单", "还款"]},
            {"bank": "建设银行", "sender_keywords": ["ccb.com"], "subject_keywords": ["信用卡", "账单", "还款"]},
            {"bank": "交通银行", "sender_keywords": ["bankcomm.com", "bocom.com.cn"], "subject_keywords": ["信用卡", "账单", "还款"]},
            {"bank": "平安银行", "sender_keywords": ["pingan.com"], "subject_keywords": ["信用卡", "账单", "还款"]},
            {"bank": "浦发银行", "sender_keywords": ["spdb.com.cn"], "subject_keywords": ["信用卡", "账单", "还款"]},
            {"bank": "中信银行", "sender_keywords": ["citiccard.com", "citic.com"], "subject_keywords": ["信用卡", "账单", "还款"]},
            {"bank": "光大银行", "sender_keywords": ["cebbank.com"], "subject_keywords": ["信用卡", "账单", "还款"]},
            {"bank": "民生银行", "sender_keywords": ["cmbc.com.cn", "minshengbank.com"], "subject_keywords": ["信用卡", "账单", "还款"]},
            {"bank": "广发银行", "sender_keywords": ["cgbchina.com.cn"], "subject_keywords": ["信用卡", "账单", "还款"]},
            {"bank": "农业银行", "sender_keywords": ["abchina.com", "95599"], "subject_keywords": ["信用卡", "账单", "还款"]},
        ]

        def decode_str(s):
            if s is None:
                return ""
            decoded_parts = email.header.decode_header(s)
            result = []
            for part, charset in decoded_parts:
                if isinstance(part, bytes):
                    try:
                        result.append(part.decode(charset or "utf-8", errors="replace"))
                    except Exception:
                        result.append(part.decode("gb18030", errors="replace"))
                else:
                    result.append(str(part))
            return "".join(result)

        def get_email_body(msg):
            plain_parts = []
            html_parts = []
            if msg.is_multipart():
                for part in msg.walk():
                    ctype = part.get_content_type()
                    if ctype == "text/plain":
                        payload = part.get_payload(decode=True)
                        if payload:
                            charset = part.get_content_charset() or "utf-8"
                            try:
                                plain_parts.append(payload.decode(charset, errors="replace"))
                            except Exception:
                                plain_parts.append(payload.decode("gb18030", errors="replace"))
                    elif ctype == "text/html":
                        payload = part.get_payload(decode=True)
                        if payload:
                            charset = part.get_content_charset() or "utf-8"
                            try:
                                html_parts.append(payload.decode(charset, errors="replace"))
                            except Exception:
                                html_parts.append(payload.decode("gb18030", errors="replace"))
            else:
                payload = msg.get_payload(decode=True)
                if payload:
                    charset = msg.get_content_charset() or "utf-8"
                    ctype = msg.get_content_type()
                    try:
                        decoded = payload.decode(charset, errors="replace")
                    except Exception:
                        decoded = payload.decode("gb18030", errors="replace")
                    if ctype == "text/plain":
                        plain_parts.append(decoded)
                    else:
                        html_parts.append(decoded)
            if plain_parts:
                return "\n".join(plain_parts)
            if html_parts:
                return html_to_text("\n".join(html_parts))
            return ""

        # 连接 IMAP（单次搜索 + 多银行规则匹配，避免对每家银行重复搜索）
        mail = imaplib.IMAP4_SSL(DEFAULT_IMAP_SERVER, DEFAULT_IMAP_PORT)
        mail.login(email_addr, auth_code)
        mail.select("INBOX")

        since_date = (datetime.today() - timedelta(days=SEARCH_DAYS)).strftime("%d-%b-%Y")
        found_bills = []
        seen_subjects = set()

        # 单次搜索获取最近邮件列表
        status, data_imap = mail.search(None, f'SINCE {since_date}')
        if status != "OK":
            mail.logout()
            return jsonify({"status": "error", "message": "搜索邮件失败"}), 500

        email_ids = data_imap[0].split()
        email_ids = email_ids[-200:]  # 最多处理最近200封，避免超时

        for eid in email_ids:
            try:
                status2, msg_data = mail.fetch(eid, "(RFC822)")
                if status2 != "OK":
                    continue
                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)

                subject = decode_str(msg.get("Subject", ""))
                sender = decode_str(msg.get("From", ""))

                # 匹配银行规则：遍历所有银行，找到发件人和主题都匹配的银行
                matched_bank = None
                for rule in BANK_RULES:
                    sender_match = any(kw.lower() in sender.lower() for kw in rule["sender_keywords"])
                    subject_match = any(kw in subject for kw in rule["subject_keywords"])
                    if sender_match and subject_match:
                        matched_bank = rule["bank"]
                        break

                if not matched_bank:
                    continue

                # 按银行+主题去重（同一银行同一期账单可能有多封通知）
                dedup_key = f"{matched_bank}_{subject}"
                if dedup_key in seen_subjects:
                    continue
                seen_subjects.add(dedup_key)

                body = get_email_body(msg)
                date_str = msg.get("Date", "")
                email_date = None
                try:
                    email_date = parsedate_to_datetime(date_str)
                except Exception:
                    pass

                bill = parse_bill_email(
                    subject=subject,
                    body=body,
                    bank_name=matched_bank,
                    sender=sender,
                    email_date=email_date,
                )
                found_bills.append(bill)
            except Exception:
                continue

        mail.logout()

        # 按银行去重，保留最新一期
        bank_latest = {}
        for bill in found_bills:
            bank = bill["bank"]
            if bank not in bank_latest:
                bank_latest[bank] = bill
            else:
                existing = bank_latest[bank]
                new_date = bill.get("email_date")
                old_date = existing.get("email_date")
                if new_date and old_date and new_date > old_date:
                    bank_latest[bank] = bill
                elif new_date and not old_date:
                    bank_latest[bank] = bill

        # 构建结果
        results = []
        for bank, bill in bank_latest.items():
            results.append({
                "bank": bank,
                "subject": bill["subject"],
                "amount_due": bill.get("amount_due"),
                "card_last4": bill.get("card_last4"),
                "email_date": bill["email_date"].isoformat() if bill.get("email_date") else None,
                "parse_ok": bill.get("parse_ok", False),
            })

        # 保存 lastRefresh
        data["lastRefresh"] = datetime.now().isoformat()
        save_data(data)

        return jsonify({
            "status": "ok",
            "count": len(results),
            "bills": results
        })

    except imaplib.IMAP4.error as e:
        return jsonify({
            "status": "error",
            "message": f"邮箱连接失败: {str(e)}"
        }), 500
    except Exception as e:
        traceback.print_exc()
        return jsonify({
            "status": "error",
            "message": f"刷新失败: {str(e)}"
        }), 500


# ============================================================
# 启动
# ============================================================

if __name__ == "__main__":
    print("=" * 50)
    print("  知卡卡账单 — 本地信用卡管理工具")
    print("=" * 50)
    print(f"\n数据文件: {DATA_FILE}")
    print(f"访问地址: http://localhost:5000")
    print("\n安全收口：仅监听 127.0.0.1（仅本机可访问）")
    print("按 Ctrl+C 停止服务\n")

    # 启动后自动打开浏览器（延迟1.5秒，等服务器就绪）
    import threading
    def _open_browser():
        import time
        time.sleep(1.5)
        try:
            import webbrowser
            webbrowser.open("http://localhost:5000")
        except Exception:
            pass
    threading.Thread(target=_open_browser, daemon=True).start()

    app.run(host="127.0.0.1", port=5000, debug=False)
