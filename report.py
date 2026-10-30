#!/usr/bin/env python3
"""A 股收盘埋伏 + 复盘 云端报告生成器。

纯标准库 + 纯 HTTP 公开数据源，可在任意 Linux 环境（GitHub Actions）运行，
完全不依赖本机开机，也不依赖 westock CLI。

数据源：
  - 行情：腾讯 qt.gtimg.cn（实时快照）
  - 资金：新浪 MoneyFlow.ssl_bkzj_ssggzj（全市场批量，1 次请求）
  - 涨停/跌停/连板：东方财富 getTopicZTPool / getTopicDTPool（自带 lbc 连板数）
  - 股票清单/行业：data/industry.json（申万分类）

输出：一份自包含 HTML 报告，含：
  1) 情绪周期研判（五阶段：冰点/修复/发酵/高潮/退潮）
  2) 连板梯队（高连板 / 二板节点票 / 首板）
  3) 埋伏候选池（三层打分：辨识度 × 蓄势 × 涨幅位置）

不构成投资建议。
"""
import concurrent.futures
import datetime as dt
import json
import math
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import learner

BASE = Path(__file__).resolve().parent
TZ = dt.timezone(dt.timedelta(hours=8))


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (ValueError, TypeError):
        return None


def get(url, referer="https://gu.qq.com/", timeout=15):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": referer,
        "Accept": "application/json,text/plain,*/*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def now_local():
    return dt.datetime.now(TZ)


# --------------------------------------------------------------------------- #
# 股票清单 + 行业（复用 server.py 的 data/industry.json）
# --------------------------------------------------------------------------- #
def load_catalog():
    cat = json.loads((BASE / "data/industry.json").read_text(encoding="utf-8"))
    stocks = cat["stocks"]
    industries = {i["id"]: i for i in cat["industries"]}

    def board(exchange, code):
        if exchange == "BJ":
            return "BSE", "北交所"
        if exchange == "SH" and code.startswith(("688", "689")):
            return "STAR", "科创板"
        if exchange == "SH" and code.startswith("60"):
            return "SSE_MAIN", "沪市主板"
        if exchange == "SZ" and code.startswith("30"):
            return "CHINEXT", "创业板"
        if exchange == "SZ" and code.startswith("00"):
            return "SZSE_MAIN", "深市主板"
        return "UNKNOWN", "待核实"

    for s in stocks:
        s["board"], s["board_name"] = board(s["exchange"], s["code"])
    return stocks, industries


STOCKS_LIST, INDUSTRIES = load_catalog()
STOCKS = {s["code"]: s for s in STOCKS_LIST}


def industry_path_len(code):
    s = STOCKS.get(code)
    if not s:
        return 0
    node = INDUSTRIES.get(s["industry"])
    depth = 0
    seen = set()
    while node and node["id"] not in seen:
        seen.add(node["id"])
        depth += 1
        node = INDUSTRIES.get(node.get("parentId"))
    return depth


def industry_name(code):
    s = STOCKS.get(code)
    ind = INDUSTRIES.get(s["industry"]) if s else None
    return ind.get("name", "") if ind else ""


# --------------------------------------------------------------------------- #
# 行情（腾讯）
# --------------------------------------------------------------------------- #
def parse_tencent(payload, fetched_at):
    rows = {}
    text = payload.decode("gb18030", "replace") if isinstance(payload, bytes) else payload
    for market, code, raw in re.findall(r'v_(sh|sz|bj)(\d{6})="([^"\r\n]*)"', text):
        f = raw.split("~")
        if len(f) < 46 or f[2] != code:
            continue
        try:
            stamp = dt.datetime.strptime(f[30], "%Y%m%d%H%M%S").replace(tzinfo=TZ)
        except ValueError:
            continue
        price, previous, change = number(f[3]), number(f[4]), number(f[32])
        if price is None or price <= 0 or previous is None or previous <= 0:
            continue
        amount_wan = number(f[57]) if len(f) > 57 else None
        if amount_wan is None:
            amount_wan = number(f[37])
        volume = number(f[36])
        rows[code] = {
            "code": code, "name": f[1], "exchange": market.upper(),
            "price": price, "previous": previous, "pct": change,
            "amount": amount_wan * 10000 if amount_wan is not None else None,
            "volume": volume * 100 if volume is not None else None,
            "turnover": number(f[38]), "high": number(f[33]),
            "low": number(f[34]), "open": number(f[5]),
            "float_cap": number(f[44]) * 1e8 if number(f[44]) is not None else None,
            "total_cap": number(f[45]) * 1e8 if number(f[45]) is not None else None,
            "quote_time": stamp.isoformat(), "quote_date": stamp.date().isoformat(),
            "quote_fetched": fetched_at, "quote_source": "腾讯公开行情",
        }
    return rows


def fetch_quotes(stocks, fetched_at):
    """stocks: iterable of dict(code, exchange)。分批批量拉腾讯行情。"""
    result = {}
    items = list(stocks)
    batches = [items[i:i + 200] for i in range(0, len(items), 200)]

    def batch(b):
        symbols = ",".join(s["exchange"].lower() + s["code"] for s in b)
        return parse_tencent(get("https://qt.gtimg.cn/q=" + symbols), fetched_at)

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        for r in ex.map(batch, batches):
            result.update(r)
    return result


# --------------------------------------------------------------------------- #
# 资金（新浪批量）
# --------------------------------------------------------------------------- #
def fetch_flows(fetched_at):
    url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "MoneyFlow.ssl_bkzj_ssggzj?" + urllib.parse.urlencode(dict(num=7000)))
    data = json.loads(get(url, "https://finance.sina.com.cn/", 20))
    if not isinstance(data, list) or not data:
        raise RuntimeError("新浪批量资金接口未返回有效列表")
    result = {}
    for r in data:
        symbol = r.get("symbol", "")
        m = re.fullmatch(r"(sh|sz|bj)(\d{6})", symbol)
        if not m:
            continue
        code = m.group(2)
        main = number(r.get("netamount"))
        super_ = number(r.get("r0_net"))
        if main is None:
            continue
        large = (main - super_) if super_ is not None else None
        result[code] = dict(main=main, super=super_, large=large)
    return result


# --------------------------------------------------------------------------- #
# 涨停 / 跌停 / 连板（东方财富）
# --------------------------------------------------------------------------- #
def fetch_zt_pool(date_str):
    """返回涨停池 [{code,name,pct,lbc,zbc,fbt,hs,ltsz,fund,amount,hybk}] + total。
    date_str 传入 YYYY-MM-DD，东财接口要求 YYYYMMDD。"""
    date_compact = date_str.replace("-", "")
    out, page = [], 0
    while True:
        url = ("https://push2ex.eastmoney.com/getTopicZTPool?"
               "ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"
               f"&Pageindex={page}&pagesize=200&sort=fbt%3Aasc&date={date_compact}")
        d = json.loads(get(url, "https://quote.eastmoney.com/", 15))
        data = d.get("data")
        pool = data.get("pool") if data else []
        if not pool:
            break
        out.extend(pool)
        if len(pool) < 200:
            break
        page += 1
    rows = []
    for r in out:
        rows.append(dict(
            code=str(r.get("c", "")), name=r.get("n", ""),
            pct=number(r.get("zdp")), lbc=int(r.get("lbc") or 1),
            zbc=int(r.get("zbc") or 0), fbt=int(r.get("fbt") or 0),
            hs=number(r.get("hs")), ltsz=number(r.get("ltsz")),
            fund=number(r.get("fund")), amount=number(r.get("amount")),
            hybk=r.get("hybk", ""),
        ))
    total = data.get("tc") if data else len(rows)
    return rows, total


def fetch_dt_pool(date_str):
    date_compact = date_str.replace("-", "")
    url = ("https://push2ex.eastmoney.com/getTopicDTPool?"
           "ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"
           f"&Pageindex=0&pagesize=500&sort=fund%3Aasc&date={date_compact}")
    d = json.loads(get(url, "https://quote.eastmoney.com/", 15))
    data = d.get("data")
    pool = data.get("pool") if data else []
    return [dict(code=str(r.get("c", "")), name=r.get("n", "")) for r in pool], \
           (data.get("tc") if data else len(pool))


# --------------------------------------------------------------------------- #
# 埋伏候选池（三层打分，与 server.py AMBUSH_RULES 一致）
# --------------------------------------------------------------------------- #
AMBUSH_RULES = {
    "max_price": 15.0, "max_float_cap": 8e9,
    "max_pct": 9.5, "min_pct": -7.0,
    "min_turnover": 0.3, "max_turnover": 20.0,
    "gentle_low": 3.0, "gentle_high": 10.0, "chill_low": 0.5,
    "exclude_boards": ("STAR", "BSE", "CHINEXT"),
}


def ambush_screen(quotes, flows, weights=None):
    rules = AMBUSH_RULES
    # 可调权重：learner 学出来的主权重（辨识度/蓄势/时机），默认为原始设计比例
    if weights is None:
        weights = {"w_identify": 50.0, "w_setup": 25.0, "w_timing": 25.0}
    # 把"原始满分"缩放为"调参后的满分"：辨识度原 50 分、蓄势原 25 分、时机原 25 分
    k_id = weights["w_identify"] / 50.0
    k_setup = weights["w_setup"] / 25.0
    k_timing = weights["w_timing"] / 25.0
    result = []
    for code, q in quotes.items():
        stock = STOCKS.get(code)
        if not stock:
            continue
        if q.get("quote_date") is None:
            continue
        if stock.get("board", "") in rules["exclude_boards"]:
            continue
        name = q.get("name") or stock.get("name", "")
        if "ST" in name.upper() or "退" in name:
            continue
        price, pct = q.get("price"), q.get("pct")
        float_cap, turnover = q.get("float_cap"), q.get("turnover")
        if price is None or pct is None:
            continue
        if not (rules["min_pct"] <= pct <= rules["max_pct"]):
            continue
        if price > rules["max_price"]:
            continue
        if float_cap is not None and float_cap > rules["max_float_cap"]:
            continue
        if turnover is not None and not (rules["min_turnover"] <= turnover <= rules["max_turnover"]):
            continue

        score, tags = 0.0, []
        price_score = max(0.0, 1.0 - price / rules["max_price"]) * 30.0 * k_id
        cap_score = 0.0
        if float_cap is not None and float_cap > 0:
            cap_score = max(0.0, 1.0 - float_cap / rules["max_float_cap"]) * 20.0 * k_id
        score += price_score + cap_score
        if price <= 10.0:
            tags.append("低价")
        if float_cap is not None and float_cap <= 5e9:
            tags.append("小盘")

        if turnover is not None:
            if pct > 0.5 and rules["gentle_low"] <= turnover <= rules["gentle_high"]:
                score += 25.0 * k_setup
                tags.append("温和放量")
            elif rules["chill_low"] <= turnover <= rules["gentle_low"]:
                score += 20.0 * k_setup
                tags.append("缩量蓄势")
            else:
                dist = min(abs(turnover - rules["gentle_low"]), abs(turnover - rules["gentle_high"]))
                score += max(0.0, 15.0 - dist * 1.5) * k_setup

        if 0.0 <= pct <= 4.0:
            score += 25.0 * k_timing
            if pct >= 1.0:
                tags.append("温和上涨")
        elif 4.0 < pct <= rules["max_pct"]:
            score += (25.0 - (pct - 4.0) * 3.0) * k_timing
            tags.append("涨幅偏高")
        else:
            score += (18.0 + max(pct, -4.0)) * k_timing
            tags.append("低位潜伏")

        main = flows.get(code, {}).get("main")
        if main is not None and main > 0:
            score += 5.0
            tags.append("主力净流入")

        path_len = industry_path_len(code)
        if path_len >= 3:
            score += 3.0
            if path_len >= 4:
                tags.append("多题材")

        result.append(dict(
            code=code, name=name, exchange=stock.get("exchange", ""),
            board_name=stock.get("board_name", ""),
            industry=industry_name(code), price=price, pct=pct,
            float_cap=float_cap, turnover=turnover, main=main,
            score=round(score, 1), tags=tags,
        ))
    result.sort(key=lambda r: r["score"], reverse=True)
    return result


# --------------------------------------------------------------------------- #
# 情绪周期研判
# --------------------------------------------------------------------------- #
def judge_emotion(zt_rows, zt_total, dt_total, max_lbc):
    """依据涨停/跌停家数、连板高度，映射到五阶段之一。"""
    zt_n, dt_n = zt_total, dt_total
    # 晋级率近似：二板及以上 / 涨停总数
    second_up = sum(1 for r in zt_rows if r["lbc"] >= 2)
    promo = second_up / zt_n if zt_n else 0.0

    if zt_n >= 100:
        stage, advice = "高潮期", "情绪极热，普涨，随时可能退潮。**不埋伏**，观望或只做主线接力。"
    elif dt_n > zt_n and zt_n < 30:
        stage, advice = "冰点期", "情绪跌到极值，是最佳埋伏窗口。可左侧潜伏低位有辨识度票，分批、留子弹、设逻辑止损。"
    elif zt_n < 30:
        stage, advice = "冰点期", "涨停家数稀少，情绪偏冷。埋伏需承受时间成本，轻仓试探。"
    elif 30 <= zt_n <= 80 and dt_n < zt_n:
        stage, advice = "修复期", "赚钱效应回暖，次佳埋伏窗口。可埋伏刚启动未大涨的票，或低位二板节点票。"
    elif zt_n > 80 and max_lbc >= 5:
        stage, advice = "发酵期", "梯队完整、有空间板。埋伏已晚，应接力强势票而非埋伏低位。"
    elif dt_n > zt_n:
        stage, advice = "退潮期", "亏钱效应显现。**禁止埋伏**，空仓观望，等情绪重新跌到冰点。"
    else:
        stage, advice = "修复期", "情绪中性偏暖，可小仓埋伏试错。"
    return dict(stage=stage, advice=advice, zt_n=zt_n, dt_n=dt_n,
                max_lbc=max_lbc, promo=round(promo * 100, 1))


# --------------------------------------------------------------------------- #
# HTML 报告
# --------------------------------------------------------------------------- #
def fmt_cap(v):
    if v is None:
        return "—"
    return f"{v / 1e8:.1f}亿"


def fmt_money(v):
    if v is None:
        return "—"
    if v >= 1e8:
        return f"{v / 1e8:.2f}亿"
    if v >= 1e4:
        return f"{v / 1e4:.0f}万"
    return f"{v:.0f}"


def pct_cls(p):
    if p is None:
        return ""
    return "up" if p > 0 else ("down" if p < 0 else "")


def build_html(report):
    d = report
    date_str = d["date"]
    emotion = d["emotion"]
    limitup = d["limitup"]
    ambush = d["ambush"]
    weights = d.get("weights") or {"w_identify": 50.0, "w_setup": 25.0, "w_timing": 25.0}
    tuning = d.get("tuning")
    eval_result = d.get("evaluation")

    # 情绪五阶段指示条
    stages = ["冰点期", "修复期", "发酵期", "高潮期", "退潮期"]
    stage_idx = stages.index(emotion["stage"]) if emotion["stage"] in stages else -1

    def stage_bar():
        cells = []
        for i, s in enumerate(stages):
            cls = "on" if i == stage_idx else ""
            cells.append(f'<div class="stg {cls}">{s}</div>')
        return '<div class="stagebar">' + "".join(cells) + "</div>"

    def tier_table(title, rows, fields):
        if not rows:
            return f'<h3>{title} <span class="muted">（无）</span></h3>'
        head = "".join(f"<th>{h}</th>" for h in fields)
        body = []
        for r in rows:
            tds = []
            for h in fields:
                if h == "名称":
                    tds.append(f'<td>{r["name"]} <span class="muted">{r["code"]}</span></td>')
                elif h == "连板":
                    tds.append(f'<td class="strong">{r["days"]}板</td>')
                elif h == "涨跌幅":
                    v = r.get("pct")
                    tds.append(f'<td class="{pct_cls(v)}">{v if v is not None else "—"}</td>')
                elif h == "换手":
                    v = r.get("hs")
                    tds.append(f'<td>{v:.1f}%</td>' if v is not None else "<td>—</td>")
                elif h == "封单":
                    tds.append(f'<td>{fmt_money(r.get("fund"))}</td>')
                elif h == "成交额":
                    tds.append(f'<td>{fmt_money(r.get("amount"))}</td>')
                elif h == "炸板":
                    tds.append(f'<td>{r.get("zbc", 0)}</td>')
                elif h == "行业":
                    tds.append(f'<td class="muted">{r.get("hybk", "")}</td>')
                else:
                    tds.append("<td></td>")
            body.append("<tr>" + "".join(tds) + "</tr>")
        return (f'<h3>{title} <span class="muted">（{len(rows)} 只）</span></h3>'
                f'<table><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table>')

    def ambush_table(rows):
        head = "<th>#</th><th>名称</th><th>行业</th><th>现价</th><th>涨跌幅</th><th>换手</th><th>流通市值</th><th>主力净额</th><th>评分</th><th>标签</th>"
        body = []
        for i, r in enumerate(rows, 1):
            tags = " ".join(f'<span class="tag">{t}</span>' for t in r["tags"])
            body.append(
                f'<tr><td>{i}</td>'
                f'<td>{r["name"]} <span class="muted">{r["code"]}</span></td>'
                f'<td class="muted">{r["industry"]}</td>'
                f'<td>{r["price"]:.2f}</td>'
                f'<td class="{pct_cls(r["pct"])}">{r["pct"]:+.2f}%</td>'
                f'<td>{r["turnover"]:.1f}%</td>'
                f'<td>{fmt_cap(r["float_cap"])}</td>'
                f'<td class="{pct_cls(r["main"])}">{fmt_money(r["main"])}</td>'
                f'<td class="score">{r["score"]:.1f}</td>'
                f'<td>{tags}</td></tr>')
        return f'<table><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table>'

    ambush_top = ambush[:30]

    # 自我学习状态卡片
    def learn_card():
        w = weights
        wl = [
            ("辨识度(低价+小盘)", w.get("w_identify", 50)),
            ("蓄势(换手形态)", w.get("w_setup", 25)),
            ("时机(涨幅位置)", w.get("w_timing", 25)),
        ]
        bars = "".join(
            f'<div class="lrow"><span class="lname">{name}</span>'
            f'<div class="lbar"><div class="lfill" style="width:{min(v,100):.0f}%"></div></div>'
            f'<span class="lval">{v:.0f}</span></div>'
            for name, v in wl
        )
        tuning_html = ""
        if tuning and tuning.get("status") == "已调参":
            changes = tuning.get("changes", {})
            ch = "".join(
                f'<li>{k}：{c["from"]} → {c["to"]}（该维度相对均值 {c["delta_avg"]:+.2f}%）</li>'
                for k, c in changes.items()
            ) if changes else "<li>本轮无需调整（各维度表现接近均值）</li>"
            tuning_html = (f'<div class="lnote">📈 本轮调参：<ul>{ch}</ul>'
                           f'整体胜率 {tuning.get("overall_win")}%，'
                           f'样本 {tuning.get("samples")} 个</div>')
        elif tuning:
            tuning_html = (f'<div class="lnote">📚 学习积累中：{tuning.get("status")}'
                           f'（已有 {tuning.get("samples", 0)} 个样本，'
                           f'需 {tuning.get("need", 3)} 天起开始自动调参）</div>')

        eval_html = ""
        if eval_result:
            g = "".join(
                f'<span class="tag">{r["name"]} {r["pct"]:+.1f}%</span>'
                for r in eval_result.get("top_gainers", [])
            )
            eval_html = (f'<div class="lnote">🔍 昨日埋伏候选今日验证：'
                         f'胜率 <b>{eval_result["win_rate"]}%</b>，'
                         f'平均 <b>{eval_result["avg_pct"]:+.2f}%</b>'
                         f'（{eval_result["evaluated"]}/{eval_result["total"]} 只）<br>'
                         f'涨幅居前：{g}</div>')
        elif weights.get("w_identify") == 50.0 and weights.get("w_setup") == 25.0:
            eval_html = '<div class="lnote">📚 今天是首个交易日，尚未积累历史信号，学习能力从明天起逐步生效。</div>'

        return (f'<div class="card"><h2>④ 自我学习状态 <span class="muted">（信号追踪 + 自动调参）</span></h2>'
                f'{bars}{tuning_html}{eval_html}</div>')

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>A股收盘埋伏复盘 · {date_str}</title>
<style>
:root {{ --bg:#0f1420; --card:#1a2130; --line:#2a3346; --tx:#e8edf7; --muted:#8a94a8;
  --up:#e5484d; --down:#22c55e; --accent:#4f7cff; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
  background:var(--bg); color:var(--tx); line-height:1.6; }}
.wrap {{ max-width:1000px; margin:0 auto; padding:24px 16px 60px; }}
h1 {{ font-size:22px; margin:0 0 4px; }}
h2 {{ font-size:17px; margin:32px 0 12px; border-left:4px solid var(--accent); padding-left:10px; }}
h3 {{ font-size:15px; margin:20px 0 8px; }}
.muted {{ color:var(--muted); font-size:12px; }}
.sub {{ color:var(--muted); font-size:13px; margin-bottom:20px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px; margin:12px 0; }}
.stagebar {{ display:flex; gap:6px; margin:14px 0; }}
.stg {{ flex:1; text-align:center; padding:8px 4px; border-radius:8px; background:#141b2b;
  border:1px solid var(--line); color:var(--muted); font-size:13px; }}
.stg.on {{ background:var(--accent); color:#fff; border-color:var(--accent); font-weight:600; }}
.kpis {{ display:flex; gap:10px; flex-wrap:wrap; }}
.kpi {{ flex:1; min-width:130px; background:#141b2b; border:1px solid var(--line);
  border-radius:10px; padding:12px; text-align:center; }}
.kpi .v {{ font-size:22px; font-weight:700; }}
.kpi .k {{ font-size:12px; color:var(--muted); }}
.advice {{ background:#1b2536; border:1px solid var(--accent); border-radius:10px;
  padding:12px 14px; font-size:14px; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; background:var(--card);
  border:1px solid var(--line); border-radius:12px; overflow:hidden; }}
th,td {{ padding:8px 10px; text-align:left; border-bottom:1px solid var(--line); }}
th {{ background:#141b2b; color:var(--muted); font-weight:600; white-space:nowrap; }}
tr:last-child td {{ border-bottom:none; }}
.up {{ color:var(--up); }} .down {{ color:var(--down); }}
.score {{ color:var(--accent); font-weight:700; }}
.strong {{ font-weight:700; }}
.tag {{ display:inline-block; background:#22304a; color:#aebdd9; border-radius:4px;
  padding:1px 6px; margin:1px 2px; font-size:11px; white-space:nowrap; }}
.lrow {{ display:flex; align-items:center; gap:8px; margin:6px 0; }}
.lname {{ width:150px; font-size:13px; color:var(--muted); }}
.lbar {{ flex:1; height:8px; background:#141b2b; border-radius:4px; overflow:hidden; }}
.lfill {{ height:100%; background:linear-gradient(90deg,var(--accent),#7c9cff); border-radius:4px; }}
.lval {{ width:36px; text-align:right; font-size:13px; font-weight:700; }}
.lnote {{ font-size:12px; color:var(--muted); margin-top:8px; line-height:1.7; }}
.lnote b {{ color:var(--tx); }}
.lnote ul {{ margin:4px 0 4px 18px; padding:0; }}
.footer {{ margin-top:36px; color:var(--muted); font-size:12px; text-align:center; }}
</style></head><body><div class="wrap">
<h1>📊 A股收盘埋伏 · 每日复盘</h1>
<div class="sub">{date_str} · 由云端定时任务自动生成 · 数据源：腾讯行情 / 新浪资金 / 东方财富涨停池</div>

<div class="card">
<h2>① 情绪周期研判</h2>
{stage_bar()}
<div class="kpis">
  <div class="kpi"><div class="v">{emotion["zt_n"]}</div><div class="k">涨停家数</div></div>
  <div class="kpi"><div class="v">{emotion["dt_n"]}</div><div class="k">跌停家数</div></div>
  <div class="kpi"><div class="v">{emotion["max_lbc"]}</div><div class="k">最高连板</div></div>
  <div class="kpi"><div class="v">{emotion["promo"]}%</div><div class="k">晋级率(2板+/涨停)</div></div>
</div>
<div class="advice"><b>阶段判定：{emotion["stage"]}</b><br>{emotion["advice"]}</div>
</div>

<div class="card">
<h2>② 连板梯队</h2>
{tier_table("🔺 高连板（≥3板 · 空间板）", limitup["high"], ["名称","连板","涨跌幅","换手","封单","成交额","行业"])}
{tier_table("🥈 二板（节点票 · 承上启下）", limitup["second"], ["名称","连板","涨跌幅","换手","封单","成交额","行业"])}
{tier_table("🥉 首板（一进二观察池）", limitup["first"], ["名称","连板","涨跌幅","换手","封单","炸板","行业"])}
</div>

<div class="card">
<h2>③ 埋伏候选池 <span class="muted">（三层打分 · 前 30 只）</span></h2>
{ambush_table(ambush_top)}
</div>

{learn_card()}

<div class="footer">本报告由云端定时任务自动生成，仅供个人复盘参考，不构成任何投资建议。<br>股市有风险，投资需谨慎。</div>
</div></body></html>"""
    return html


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main():
    # 交易日判断：用行情日期而非本地日期（周末/节假日行情日期会停留在最近交易日）
    fetched_at = time_now = now_local().timestamp()
    # 先小批量拉一次行情，探明"最近交易日"
    probe = [s for s in STOCKS_LIST if s["exchange"] == "SH"][:5]
    q_probe = fetch_quotes(probe, fetched_at)
    trade_date = None
    for q in q_probe.values():
        trade_date = q.get("quote_date")
        break
    if not trade_date:
        print("无法确定交易日，终止。", file=sys.stderr)
        sys.exit(1)

    print(f"[report] 交易日 = {trade_date}", flush=True)

    # 1. 全市场行情
    print("[report] 拉取全市场行情 ...", flush=True)
    quotes = fetch_quotes(STOCKS_LIST, fetched_at)
    print(f"[report] 行情 {len(quotes)} 只", flush=True)

    # 2. 资金
    flows = {}
    try:
        print("[report] 拉取新浪批量资金 ...", flush=True)
        flows = fetch_flows(fetched_at)
        print(f"[report] 资金 {len(flows)} 只", flush=True)
    except Exception as e:
        print(f"[report] 资金源失败（降级为空）：{type(e).__name__} {str(e)[:100]}", flush=True)

    # 3. 涨停/跌停
    print("[report] 拉取涨停/跌停池 ...", flush=True)
    zt_rows, zt_total = fetch_zt_pool(trade_date)
    dt_rows, dt_total = fetch_dt_pool(trade_date)
    max_lbc = max((r["lbc"] for r in zt_rows), default=0)
    print(f"[report] 涨停 {zt_total} 家，跌停 {dt_total} 家，最高 {max_lbc} 板", flush=True)

    # 4. 情绪周期
    emotion = judge_emotion(zt_rows, zt_total, dt_total, max_lbc)

    # ---- 自我学习：加载历史 → 回测昨日 → 自动调参 → 用新权重打分 ----
    print("[learn] 加载历史信号 ...", flush=True)
    history = learner.load_history()
    tuning = None
    eval_result = None
    # 4a. 回测昨日信号（若有前一日信号）
    if history:
        prev = history[-1]
        prev_date = prev.get("date")
        # 若昨日信号还没被评估过，用今日行情回填 next_pct 并评估
        if prev_date != trade_date and any(c.get("next_pct") is None for c in prev.get("ambush_top", [])):
            eval_result = learner.evaluate_yesterday(quotes, prev)
            # 把 next_pct 回填到 history（供 auto_tune 用）
            qmap = {c["code"]: c for c in prev.get("ambush_top", [])}
            for c in prev.get("ambush_top", []):
                q = quotes.get(c.get("code"))
                if q and q.get("pct") is not None:
                    c["next_pct"] = q["pct"]
            if eval_result:
                print(f"[learn] 昨日信号验证：胜率 {eval_result['win_rate']}%，"
                      f"平均 {eval_result['avg_pct']}%，样本 {eval_result['evaluated']}/{eval_result['total']}", flush=True)
    # 4b. 自动调参（基于历史 + next_pct）
    weights, tuning = learner.auto_tune(history)
    if tuning and tuning.get("status") == "已调参":
        print(f"[learn] 已调参：{tuning.get('changes')}", flush=True)
    elif tuning:
        print(f"[learn] 调参状态：{tuning.get('status')}（样本 {tuning.get('samples')}）", flush=True)

    # 5. 连板梯队分组
    high = [r for r in zt_rows if r["lbc"] >= 3]
    second = [r for r in zt_rows if r["lbc"] == 2]
    first = [r for r in zt_rows if r["lbc"] == 1]
    for tier in (high, second, first):
        tier.sort(key=lambda r: (-r["lbc"], -(r["fund"] or 0)))
    limitup = dict(
        high=[dict(r, days=r["lbc"]) for r in high],
        second=[dict(r, days=r["lbc"]) for r in second],
        first=[dict(r, days=1) for r in first],
    )

    # 6. 埋伏候选池（用调参后的权重）
    print("[report] 运行埋伏候选池 ...", flush=True)
    ambush = ambush_screen(quotes, flows, weights)
    print(f"[report] 埋伏候选 {len(ambush)} 只", flush=True)

    # 7. 持久化当日信号（写入历史，供后续学习）
    learner.persist_signal(trade_date, emotion, limitup, ambush)

    report = dict(date=trade_date, generated=now_local().isoformat(),
                  emotion=emotion, limitup=limitup, ambush=ambush,
                  weights=weights, tuning=tuning, evaluation=eval_result)

    html = build_html(report)
    out = BASE / "reports" / f"ambush-report-{trade_date}.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"[report] 已生成 {out}", flush=True)

    # 同时写一份 latest.html 便于固定链接
    latest = BASE / "reports" / "latest.html"
    latest.write_text(html, encoding="utf-8")
    print(f"[report] 已生成 {latest}", flush=True)

    # 输出 JSON 摘要（供后续上传云盘用）
    summary = dict(date=trade_date, emotion=emotion,
                   limitup_counts={k: len(v) for k, v in limitup.items()},
                   ambush_count=len(ambush),
                   top_ambush=ambush[:10])
    print("===SUMMARY_JSON===")
    print(json.dumps(summary, ensure_ascii=False))

    # 生成微信推送摘要（Markdown），供 Server酱 推送用
    wechat_md = build_wechat_summary(trade_date, emotion, limitup, ambush)
    md_path = BASE / "reports" / f"wechat-summary-{trade_date}.md"
    md_path.write_text(wechat_md, encoding="utf-8")
    print(f"[report] 已生成微信摘要 {md_path}", flush=True)


def build_wechat_summary(date_str, emotion, limitup, ambush):
    """生成微信推送用的 Markdown 摘要（Server酱 desp 字段）。"""
    lines = []
    lines.append(f"### A股收盘复盘 · {date_str}")
    lines.append("")
    lines.append(f"**情绪阶段：{emotion['stage']}**")
    lines.append(f"> {emotion['advice']}")
    lines.append("")
    lines.append(f"- 涨停 {emotion['zt_n']} 家 / 跌停 {emotion['dt_n']} 家")
    lines.append(f"- 最高连板 {emotion['max_lbc']} 板 / 晋级率 {emotion['promo']}%")
    lines.append("")

    # 连板梯队摘要
    high = limitup.get("high", [])
    second = limitup.get("second", [])
    if high or second:
        lines.append("**连板梯队**")
        if high:
            names = "、".join(r["name"] for r in high[:5])
            lines.append(f"- 高连板：{names}")
        if second:
            names = "、".join(r["name"] for r in second[:8])
            lines.append(f"- 二板节点票：{names}")
        lines.append("")

    # 埋伏候选 top5
    lines.append("**埋伏候选 Top5**")
    for i, r in enumerate(ambush[:5], 1):
        tags = " ".join(r["tags"][:3])
        lines.append(f"{i}. {r['name']}（{r['code']}）{r['price']:.2f}元 "
                     f"{r['pct']:+.2f}% · 评分{r['score']:.0f} · {tags}")
    lines.append("")
    lines.append(f"[查看完整报告](https://liw56747-sys.github.io/a-share-ambush/)")
    lines.append("")
    lines.append("---")
    lines.append("仅供参考，不构成投资建议。")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
