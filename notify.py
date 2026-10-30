#!/usr/bin/env python3
"""Server酱微信推送：把报告摘要推送到个人微信。

用法：
  python notify.py <markdown摘要文件路径>

SendKey 从环境变量 SERVERCHAN_SENDKEY 读取（GitHub Secrets 注入），
不硬编码、不打印、不提交。免费额度每天 5 条，本项目每天仅推 1 条。

接口（Server酱³/Turbo 通用）：
  POST https://sctapi.ftqq.com/{SendKey}.send
  参数 title（必填，不能含换行）、desp（正文，支持 Markdown）
  成功返回 JSON code == 0
"""
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path


def send_wechat(title, desp):
    sendkey = os.environ.get("SERVERCHAN_SENDKEY", "").strip()
    if not sendkey:
        print("[notify] 未配置 SERVERCHAN_SENDKEY，跳过推送", file=sys.stderr)
        return False

    # Server酱³（sctp 开头）用新域名；经典（SCT 开头）用 sctapi
    # 官方 uid 提取正则：/^sctp(\d+)t/
    import re as _re
    m = _re.match(r"^sctp(\d+)t", sendkey)
    if m:
        uid = m.group(1)
        url = f"https://{uid}.push.ft07.com/send/{sendkey}.send"
    else:
        url = f"https://sctapi.ftqq.com/{sendkey}.send"

    data = urllib.parse.urlencode({"title": title, "desp": desp}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Mozilla/5.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", "replace")
    except Exception as e:
        print(f"[notify] 推送失败：{type(e).__name__} {e}", file=sys.stderr)
        return False

    # 成功返回 code 0
    if '"code":0' in body or '"code": 0' in body or '"errno":0' in body:
        print("[notify] 推送成功")
        return True
    print(f"[notify] 推送返回异常：{body[:200]}", file=sys.stderr)
    return False


def main():
    if len(sys.argv) < 2:
        print("用法：python notify.py <markdown摘要文件>", file=sys.stderr)
        sys.exit(1)

    md_path = Path(sys.argv[1])
    if not md_path.exists():
        print(f"[notify] 摘要文件不存在：{md_path}", file=sys.stderr)
        sys.exit(1)

    desp = md_path.read_text(encoding="utf-8")
    # title 取第一行（### A股收盘复盘 · 日期），去掉 markdown 标记
    first_line = desp.splitlines()[0].lstrip("# ").strip() if desp else "A股收盘复盘"
    title = first_line[:60]  # title 不能含换行，截断保护

    send_wechat(title, desp)


if __name__ == "__main__":
    main()
