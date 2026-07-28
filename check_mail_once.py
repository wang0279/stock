"""
check_mail_once.py
每天執行一次：檢查「當天」收到的富邦成交回報信 -> 解析 -> 推播 LINE。

Debug 模式（環境變數 DEBUG_MODE=true）：
- 不篩選信件、不要求解析成功，只做健檢：
  1) Gmail IMAP 能不能登入
  2) 印出今天所有信件的寄件者/主旨，方便確認寄件者關鍵字有沒有抓對
  3) 送一則固定的測試訊息到 LINE，確認 LINE 金鑰/User ID 有效

安裝套件：
    pip install requests
"""

import imaplib
import email
from email.header import decode_header
from datetime import date, timedelta
import re
import os
import sys
import requests

# ---------------- 設定區（用 GitHub Actions Secrets 帶入環境變數）----------------
GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
LINE_USER_ID = os.environ["LINE_USER_ID"]
DEBUG_MODE = os.environ.get("DEBUG_MODE", "false").lower() == "true"

SUBJECT_PATTERN = re.compile(r"富邦證券(\d{4})年(\d{1,2})月(\d{1,2})日證券成交回報")

# 先猜測的正則，等你提供實際信件內容後再校準
STOCK_NAME_PATTERN = re.compile(r"股票名稱[:：]\s*([^\s]+)")
PRICE_PATTERN = re.compile(r"成交價格?[:：]\s*([\d,.]+)")

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
# -----------------------------------------------------------------------------


def imap_date_str(d: date) -> str:
    """IMAP SINCE 要求的日期格式，例如 28-Jul-2026"""
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


def send_line_message(text: str) -> None:
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    payload = {"to": LINE_USER_ID, "messages": [{"type": "text", "text": text}]}
    resp = requests.post(url, headers=headers, json=payload, timeout=10)
    if resp.status_code != 200:
        raise RuntimeError(f"LINE 發送失敗: {resp.status_code} {resp.text}")


def decode_mime_words(s: str) -> str:
    decoded = decode_header(s)
    return "".join(
        str(t[0], t[1] or "utf-8") if isinstance(t[0], bytes) else t[0]
        for t in decoded
    )


def get_email_body(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                charset = part.get_content_charset() or "utf-8"
                return part.get_payload(decode=True).decode(charset, errors="ignore")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                charset = part.get_content_charset() or "utf-8"
                html = part.get_payload(decode=True).decode(charset, errors="ignore")
                return re.sub("<[^<]+?>", "", html)
    else:
        charset = msg.get_content_charset() or "utf-8"
        return msg.get_payload(decode=True).decode(charset, errors="ignore")
    return ""


def fetch_todays_messages(mail):
    """回傳今天（SINCE 今天日期）收到的所有信件的 email.message 物件"""
    today_str = imap_date_str(date.today())
    status, data = mail.search(None, "SINCE", today_str)
    ids = data[0].split()
    messages = []
    for eid in ids:
        status, msg_data = mail.fetch(eid, "(RFC822)")
        raw = msg_data[0][1]
        messages.append(email.message_from_bytes(raw))
    return today_str, messages


def check_mail_once() -> None:
    """正式模式：只處理今天、寄件者符合篩選、且解析成功的信件"""
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
    mail.select("inbox")

    today_str, messages = fetch_todays_messages(mail)
    print(f"今天（{today_str} 起）共 {len(messages)} 封信")

    sent_count = 0
    for msg in messages:
        subject = decode_mime_words(msg.get("Subject", ""))

        subject_match = SUBJECT_PATTERN.search(subject)
        if not subject_match:
            continue

        body = get_email_body(msg)
        name_match = STOCK_NAME_PATTERN.search(body)
        price_match = PRICE_PATTERN.search(body)

        if name_match and price_match:
            stock_name = name_match.group(1)
            price = price_match.group(1)
            text = f"📈 成交回報\n股票：{stock_name}\n成交價：{price}"
            send_line_message(text)
            sent_count += 1
            print("已發送:", text)
        else:
            print(f"主旨符合但未能解析內容，主旨: {subject}")
            print("---- 原始內文（前 500 字，用來校準正則）----")
            print(body[:500])
            print("--------------------------------------------")

    if sent_count == 0:
        print("今天沒有成交回報，改查詢過去7天...")
        rows = fetch_recent_trades(mail, days=7)
        mail.logout()
        send_line_message(build_no_trade_report(rows))
        print(f"已發送「今天沒交易」通知，附上過去7天 {len(rows)} 筆紀錄")
    else:
        mail.logout()
        print(f"完成，共推播 {sent_count} 則")


def fetch_recent_trades(mail, days: int):
    """查詢過去 N 天（含今天）主旨符合成交回報格式、且內文解析成功的紀錄
    回傳依交易日期排序的 ((year, month, day), 股票, 價格) list"""
    range_start = date.today() - timedelta(days=days - 1)
    status, data = mail.search(None, "SINCE", imap_date_str(range_start))
    ids = data[0].split()

    rows = []
    for eid in ids:
        status, msg_data = mail.fetch(eid, "(RFC822)")
        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)

        subject = decode_mime_words(msg.get("Subject", ""))
        subject_match = SUBJECT_PATTERN.search(subject)
        if not subject_match:
            continue

        body = get_email_body(msg)
        name_match = STOCK_NAME_PATTERN.search(body)
        price_match = PRICE_PATTERN.search(body)
        if not (name_match and price_match):
            continue

        y, m, d = (int(g) for g in subject_match.groups())
        rows.append(((y, m, d), name_match.group(1), price_match.group(1)))

    rows.sort(key=lambda r: r[0])
    return rows


def build_no_trade_report(rows) -> str:
    lines = ["📭 今天沒交易", "", "📊 過去7天成交紀錄："]
    if rows:
        lines.append("日期 | 股票 | 成交價")
        for (y, m, d), name, price in rows:
            lines.append(f"{m:02d}/{d:02d} | {name} | {price}")
    else:
        lines.append("（過去7天也沒有交易紀錄）")
    return "\n".join(lines)


def run_debug_check() -> None:
    """Debug 模式：健檢 Gmail 連線 + 列出今天所有信件 + 送一則 LINE 測試訊息"""
    print("=== DEBUG 模式 ===")

    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
    print("✅ Gmail IMAP 登入成功")
    mail.select("inbox")

    today_str, messages = fetch_todays_messages(mail)
    print(f"✅ 今天（{today_str} 起）共有 {len(messages)} 封信")

    matched = 0
    for msg in messages:
        from_addr = msg.get("From", "")
        subject = decode_mime_words(msg.get("Subject", ""))
        is_match = bool(SUBJECT_PATTERN.search(subject))
        matched += is_match
        flag = "✅符合主旨格式" if is_match else ""
        print(f"  - 寄件者: {from_addr} | 主旨: {subject} {flag}")

    print(f"✅ 主旨符合「富邦證券xxxx年xx月xx日證券成交回報」格式的信件數: {matched}")
    mail.logout()

    send_line_message(
        "✅ 測試訊息：Gmail 監控腳本 debug 模式執行成功！\n"
        "（這代表 Gmail 登入與 LINE 推播金鑰都設定正確）"
    )
    print("✅ LINE 測試訊息已送出，請檢查手機")


if __name__ == "__main__":
    try:
        if DEBUG_MODE:
            run_debug_check()
        else:
            check_mail_once()
    except Exception as e:
        print("發生錯誤:", e)
        sys.exit(1)
