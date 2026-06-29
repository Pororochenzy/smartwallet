# Polymarket Smart Money Scorer

每天扫 Polymarket 公开数据，给钱包按 8 个指标打分，输出 Top 排行榜（网页 + Markdown）。

**线上排行榜**：https://YOUR_GITHUB_USERNAME.github.io/smartwallet/ ← 部署完替换这个

## 怎么运转

GitHub Actions 每天定时跑：
1. 拉 Polymarket leaderboard Top 500 + 增量拉每个钱包的交易历史
2. 算 8 个指标，横截面 percentile → 加权 → 星级
3. 导出 `docs/leaderboard.json`，自动 commit 回 repo
4. GitHub Pages 把 `docs/index.html` + JSON 当静态站点服务

**评分公式**

```
Score = 40% ROI + 20% Sharpe + 15% MaxDD + 15% AvgHold + 10% EarlyEntry
        × Confidence(log10(trades)) + MarketImpactBonus(0-5)
        → clamp(0, 100)

Followability = 50% FollowableROI(60s) + 20% (1-Slippage) + 15% Liquidity + 15% AvgHold
```

`Confidence` 扼杀 5-trade 30% ROI 假货。`Followability` 模拟延迟 60s/5min/15min 跟单的真实收益——这是给自动跟单看的，跟原始 ROI 完全两码事。

## 第一次部署（5 分钟）

1. **fork 或 clone 到你的 GitHub repo**（必须是公开 repo 才能用免费 Pages）

   ```bash
   gh repo create smartwallet --public --source=. --push
   ```

2. **开启 Pages**：repo 设置 → Pages → Source 选 `GitHub Actions`

3. **手动触发一次第一次跑**（不等明天的 cron）：

   ```bash
   gh workflow run daily.yml -f limit=30
   ```

   或在 GitHub 网页 Actions tab 点 "Daily Smart Money Update" → "Run workflow"。

   `limit=30` 是先用 30 个钱包跑通流水线；OK 之后再不带 `limit` 跑全量。

4. **跑完后访问**：`https://YOUR_USERNAME.github.io/smartwallet/`

之后每天 04:17 UTC 自动跑一次。

## 本地开发

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

smartmoney init-db
python -m smartmoney.tests   # 单元测试

# 如果你能直连 Polymarket：
smartmoney run-daily --limit 20
open docs/leaderboard.json
python3 -m http.server -d docs 8000
# 浏览器开 http://localhost:8000
```

### 中国大陆访问受限

GFW 对 `*.polymarket.com` 有 DNS 投毒 + SNI 阻断，本机直连不通。
**就让 GitHub Actions 跑就好**——GitHub 服务器在境外，没问题，你只是消费它产出的 `leaderboard.json` 和静态页。

## 命令

```bash
smartmoney init-db              # 建表
smartmoney ingest               # 拉数据
smartmoney ingest --wallet 0x.. # 只拉一个钱包，用于排查
smartmoney score                # 算分
smartmoney report               # 生成 Markdown 报告
smartmoney export-web           # 导出 docs/leaderboard.json
smartmoney run-daily            # 上面 4 个一起跑（GitHub Actions 入口）
smartmoney archive              # WebSocket 归档（长跑，本地用，actions 跑不动）
```

## 文件

```
.github/workflows/daily.yml     # 每日 cron + 部署 Pages
docs/index.html                 # 静态前端（vanilla JS，无依赖）
docs/leaderboard.json           # 数据，actions 每天覆盖
smartmoney/
  config.py                     # URL / 权重 / 阈值
  db.py                         # SQLite schema
  clients.py                    # Data/Gamma/CLOB/Subgraph API 封装
  ingest.py                     # leaderboard → trades → markets → positions
  pricing.py                    # 历史价查询（archived → CLOB history 兜底）
  metrics.py                    # 8 个 pure-function 指标
  score.py                      # 横截面 percentile → 加权 → 星级
  report.py                     # Jinja2 渲染 Markdown
  web_report.py                 # 导出 JSON 给前端
  archiver.py                   # WebSocket 归档（本地长跑）
  cli.py                        # click 命令
  dns_fix.py                    # DoH 兜底（GFW DNS 污染时用）
```

## 已知限制

- **Followable ROI 只能到分钟级**。要 5s/10s/30s 精度必须本地长期跑 `smartmoney archive`，actions 任务 6h 上限跑不动。除非你接受永远停在分钟级（对绝大多数策略够用了）。
- **首次跑很慢**。Top 500 钱包平均每个有数百笔交易，全量拉一次约 30-50 分钟。每天增量后只要几分钟。
- **页面 1 天 1 更**。不是实时。

## 下一步可加

- Followability 加权 √notional（大单跟单更现实）
- 每个钱包的"最近 5 笔交易"侧栏
- 排名变动 ≥ 5 时往 Telegram channel 推一条
- 接 PolyCop（要么 Telethon userbot 风险高，要么等他们出 partner API）
