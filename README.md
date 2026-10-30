# A股收盘埋伏 · 云端复盘

云端定时生成 A 股收盘复盘报告，**完全不依赖本机开机**。由 GitHub Actions 在云端
（Linux 环境）每周一至五 15:30 北京时间自动运行，数据源全部为公开 HTTP 接口。

## 数据源（纯 HTTP，无 westock 依赖）

| 数据 | 来源 |
|------|------|
| 实时行情 | 腾讯 `qt.gtimg.cn` |
| 主力资金 | 新浪 `MoneyFlow.ssl_bkzj_ssggzj`（全市场批量） |
| 涨停/跌停/连板 | 东方财富 `getTopicZTPool` / `getTopicDTPool`（自带 `lbc` 连板数） |
| 股票清单/行业 | `data/industry.json`（申万分类，5572 只） |

## 报告内容

1. **情绪周期研判**：冰点 / 修复 / 发酵 / 高潮 / 退潮 五阶段，附涨停/跌停家数、最高连板、晋级率
2. **连板梯队**：高连板（≥3板）、二板（节点票）、首板
3. **埋伏候选池**：三层打分（辨识度 × 蓄势 × 涨幅位置），前 30 只

报告输出到 `reports/ambush-report-YYYY-MM-DD.html`，同时写一份 `reports/latest.html` 固定路径。

## 落地方式：GitHub Pages 全自动（手机浏览器直达）

报告生成后，workflow 会自动：

1. 把 `reports/latest.html` 复制为站点首页 `index.html`（历史报告一并保留）
2. 上传为 Pages artifact
3. 部署到 GitHub Pages

最终生成一个**固定网页链接**（形如 `https://<用户名>.github.io/a-share-ambush/`），
手机浏览器直接打开即可查看，无需本机开机、无需登录任何账号、无需手动操作。

## 定时触发

`.github/workflows/daily-report.yml`：

- `schedule`：`30 7 * * 1-5`（UTC），即北京时间周一至五 15:30
- `workflow_dispatch`：可手动触发测试

## 本地运行

```bash
python report.py
```

仅需 Python 3.9+ 标准库，无需第三方依赖。

> 免责声明：本报告仅供个人复盘参考，不构成任何投资建议。
