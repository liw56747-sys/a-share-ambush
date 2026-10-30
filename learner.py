#!/usr/bin/env python3
"""自我学习模块：信号持久化 + 回测校验 + 自动调参。

设计思路（三层闭环）：
  1. persist_signal   每日收盘后，把当天信号（情绪阶段、埋伏候选 top、连板梯队）存成 JSON
                      到 history/YYYY-MM-DD.json，并 commit 回仓库 —— 这是"记忆"。
  2. evaluate_yesterday  下次运行时，对比"昨日埋伏候选 top30"今日实际涨跌幅，统计胜率/盈亏。
  3. auto_tune          根据近 N 日胜率反馈，微调打分权重（辨识度/蓄势/涨幅位置 三项主权重）。

关键原则：
  - 调参幅度保守（每次最多 ±5 分），避免因短期噪声剧烈抖动。
  - 学习需要时间：第一天只有记忆，第二天起才有回测，第五天起调参才趋稳。
  - 所有学习状态都落在仓库的 learn/ 目录，跨天累积，云端"有记忆"。
"""
import datetime as dt
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent
HISTORY_DIR = BASE / "history"
LEARN_DIR = BASE / "learn"
CONFIG_PATH = LEARN_DIR / "tuned_params.json"
TZ = dt.timezone(dt.timedelta(hours=8))

# 三项主权重（对应 ambush_screen 的三层打分），初始为原始设计值
DEFAULT_WEIGHTS = {
    "w_identify": 50.0,   # 辨识度（低价30 + 小盘20）
    "w_setup": 25.0,      # 蓄势形态（换手率）
    "w_timing": 25.0,     # 涨幅位置（时机）
}
WEIGHT_BOUNDS = {          # 每项权重的允许范围，防止调参跑飞
    "w_identify": (30.0, 65.0),
    "w_setup": (15.0, 40.0),
    "w_timing": (15.0, 40.0),
}
TUNE_STEP = 3.0            # 每次调整步长（分）
MIN_SAMPLES = 3            # 至少需要 3 天样本才开始调参


def _today():
    return dt.datetime.now(TZ).date().isoformat()


def load_tuned():
    """读取已调参的权重；无则返回默认。"""
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            w = data.get("weights", {})
            return {k: w.get(k, DEFAULT_WEIGHTS[k]) for k in DEFAULT_WEIGHTS}
        except (json.JSONDecodeError, OSError):
            pass
    return dict(DEFAULT_WEIGHTS)


def save_tuned(weights, meta):
    """保存调参结果 + 元信息（样本数、胜率等）。"""
    LEARN_DIR.mkdir(exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(
        {"weights": weights, "meta": meta, "updated_at": _today()},
        ensure_ascii=False, indent=2), encoding="utf-8")


def persist_signal(trade_date, emotion, limitup, ambush):
    """把当日信号落盘为 history/YYYY-MM-DD.json。返回写入的文件路径。"""
    HISTORY_DIR.mkdir(exist_ok=True)
    payload = {
        "date": trade_date,
        "emotion": emotion,
        "limitup_counts": {k: len(v) for k, v in limitup.items()},
        # 埋伏候选只存 top30（含 code/name/price/pct/score/tags），足够回测用
        "ambush_top": [
            {k: r.get(k) for k in ("code", "name", "price", "pct", "score", "tags")}
            for r in ambush[:30]
        ],
    }
    path = HISTORY_DIR / f"{trade_date}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_history():
    """加载所有历史信号，按日期排序。返回 [{date, emotion, ambush_top, ...}]。"""
    if not HISTORY_DIR.exists():
        return []
    rows = []
    for p in sorted(HISTORY_DIR.glob("*.json")):
        try:
            rows.append(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    rows.sort(key=lambda r: r.get("date", ""))
    return rows


def evaluate_yesterday(quotes, prev_signal):
    """用今日行情，评估昨日埋伏候选 top30 的实际表现。

    返回 dict:
      win_rate: 上涨（pct>0）比例
      avg_pct: 平均涨跌幅
      evaluated: 实际评估到的样本数
      top_gainers: 涨幅最大的 3 只（供报告展示"昨日信号今日验证"）
    """
    cands = prev_signal.get("ambush_top", [])
    if not cands:
        return None
    pcts = []
    gainers = []
    for c in cands:
        code = c.get("code")
        q = quotes.get(code)
        if not q or q.get("pct") is None:
            continue
        pct = q["pct"]
        pcts.append(pct)
        gainers.append({"code": code, "name": q.get("name") or c.get("name", ""),
                        "pct": pct, "score": c.get("score")})
    if not pcts:
        return None
    gainers.sort(key=lambda r: -r["pct"])
    win_rate = sum(1 for p in pcts if p > 0) / len(pcts)
    return {
        "win_rate": round(win_rate * 100, 1),
        "avg_pct": round(sum(pcts) / len(pcts), 2),
        "evaluated": len(pcts),
        "total": len(cands),
        "top_gainers": gainers[:3],
    }


def auto_tune(history):
    """根据历史信号的'次日验证结果'，微调三项权重。返回 (new_weights, tuning_meta)。

    核心思想（朴素贝叶斯式反馈）：
      - 若某层权重的"高分候选"次日胜率高 → 该维度有效 → 上调该权重；
      - 反之若高分候选次日普遍跌 → 该维度可能失效 → 下调。
    这里用简化近似：统计每日 top10（分数最高）候选的平均胜率作为整体信号质量，
    再结合各维度标签（低价/温和放量/温和上涨等）的次日表现做细分微调。
    """
    weights = load_tuned()
    if len(history) < MIN_SAMPLES:
        return weights, {"status": "样本不足", "samples": len(history),
                         "need": MIN_SAMPLES}

    # 聚合：每个标签维度的次日表现
    tag_stats = {}  # tag -> [pcts]
    total_pcts = []
    for sig in history:
        cands = sig.get("ambush_top", [])
        # 只用 top10（分数最高）作为"系统最看好"的样本
        for c in cands[:10]:
            pct = c.get("next_pct")  # 由 evaluate 阶段回填
            if pct is None:
                continue
            total_pcts.append(pct)
            for t in c.get("tags", []):
                tag_stats.setdefault(t, []).append(pct)

    if len(total_pcts) < MIN_SAMPLES * 3:
        return weights, {"status": "验证样本不足", "samples": len(total_pcts)}

    overall_win = sum(1 for p in total_pcts if p > 0) / len(total_pcts)
    overall_avg = sum(total_pcts) / len(total_pcts)

    def tag_avg(tag):
        vals = tag_stats.get(tag, [])
        return (sum(vals) / len(vals)) if vals else None

    # 维度 -> 相关标签
    # 辨识度：低价、小盘
    # 蓄势：温和放量、缩量蓄势
    # 时机：温和上涨、低位潜伏
    dims = {
        "w_identify": ["低价", "小盘"],
        "w_setup": ["温和放量", "缩量蓄势"],
        "w_timing": ["温和上涨", "低位潜伏"],
    }

    tuning_meta = {"status": "已调参", "overall_win": round(overall_win * 100, 1),
                   "overall_avg": round(overall_avg, 2), "samples": len(total_pcts),
                   "changes": {}}

    new_weights = dict(weights)
    for dim, tags in dims.items():
        # 该维度各标签的平均涨幅（对比整体平均）
        dim_avgs = [tag_avg(t) for t in tags if tag_avg(t) is not None]
        if not dim_avgs:
            continue
        dim_avg = sum(dim_avgs) / len(dim_avgs)
        delta = dim_avg - overall_avg  # 该维度相对整体是否更优
        old = new_weights[dim]
        lo, hi = WEIGHT_BOUNDS[dim]
        if delta > 0.3:  # 该维度标签显著跑赢 → 加权重
            new_val = min(hi, old + TUNE_STEP)
        elif delta < -0.3:  # 显著跑输 → 减权重
            new_val = max(lo, old - TUNE_STEP)
        else:
            new_val = old  # 中性，不动
        if abs(new_val - old) > 0.01:
            tuning_meta["changes"][dim] = {"from": round(old, 1), "to": round(new_val, 1),
                                           "delta_avg": round(delta, 2)}
        new_weights[dim] = round(new_val, 1)

    # 归一化到 100 分（三项权重总和固定 100）
    total_w = sum(new_weights.values())
    if total_w > 0:
        new_weights = {k: round(v * 100.0 / total_w, 1) for k, v in new_weights.items()}

    return new_weights, tuning_meta
