# 全球 AI 订阅月价 · ChatGPT（7 档）+ Google AI Ultra（2 档）

纯云端架构（**Scheme B：GitHub Actions + Cloudflare Pages / Vercel**），合并了原 `18767`（ChatGPT 全球月价）与 `18771`（Google AI Ultra 全球月价），无须本机长期开启 HTTP 服务。

## 核心价格口径与精度机制

1. **官网月付锚点（住宅代理）**：
   * **ChatGPT（7 档）**：`GO`、`Plus`、`Business Standard`（团队）、`Pro 100`、`Business Premium`（团队）、`Pro 200`、`Pro 500`。仅采用官方直出 **不含税（`tax == "exclusive"`）** 月付订阅字段，绝不估算扣税，也不混用年付折算。
   * **Google AI Ultra（2 档）**：`5x (20 TB)`、`20x (30 TB)`。同 sticky 会话核验出口 IP、服务端定位、`COUNTRY_CODE` / `CURRENCY_CODE`，并保留 `5× >= 20×` 币种数值冲突拦截。
2. **App Store 全开发商扫描与新价优先**：
   * **Google AI Ultra**：动态拉取开发者 Google（`artistId=281956209`）全部官方软件，实测区分含内购（16 款）与无内购（64 款）清单；同档位多价时按苹果涨价机制**取新开用户最高有效月价**，排除老用户旧续订价（并拦截未标年付的 `>= 8.5×` 大跨度异常）。
   * **ChatGPT**：扫描 `id6448311069`，先隔离年付候选项（未明确标注月付且 `>= 8.5×` 最低月付），再从剩余有效月付候选中**取最高新价**。
3. **每日北京时间 04:00 定时直连巡检 Apple + 90 天历史账本**：
   * [`.github/workflows/daily-apple-patrol.yml`](.github/workflows/daily-apple-patrol.yml) 每天北京时间 `04:00`（`cron: '0 20 * * *'` UTC）零代理直连扫一次 App Store，结果写入 `public/data/apple-history.json` 和 `public/data/published.json`。
   * 若涨价新 SKU 曾短暂进入 Top 10 随后掉榜，90 天内账本自动保留该更高有效新价（标注 `历史新价`）；超过 90 天未再出现则标灰待复核并移出最低价看板。
4. **同币种跨渠道防老用户旧续订价（`< 65%` 标疑）**：
   * 当同币种下 `App Store < 官网 × 65%`（例如土耳其 `TRY` 官网为 `4,749.99 / 8,999.99`，苹果公开 Top 10 仅露出旧价 `1,989.99 / 2,949.99`），系统自动标黄为 **「疑似旧续订价」**，并自动从顶部 **最低月价看板** 排除。
   * 不同币种的正常价差（例如 `GH` / `IQ` / `KE` 官网用本币、App Store 收 USD）不会被误报。
5. **公网只读 + 管理员实测校准**：
   * 公网访客直接浏览全部比价与证据明细；点击右上角 **「管理员」** 并输入 `ADMIN_PASSWORD` 后，可派发 GitHub Actions 刷新、查看巡检记录及维护 `public/data/overrides.json` 人工实测校准价。住宅代理凭据仅存放于 GitHub Actions Encrypted Secrets，永不下发至浏览器。

---

## 云端部署步骤（约 5 分钟完成）

### 第 1 步：推送到你的 GitHub 仓库
```bash
cd /Users/zouchenlong/Downloads/定价/ai-subscription-prices
git add .
git commit -m "feat: unified ChatGPT & Google AI Ultra cloud pricing site"
# 在 GitHub 创建一个私有或公开仓库后关联并推送：
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git push -u origin main
```

### 第 2 步：配置 GitHub Actions 权限与住宅代理 Secrets
进入该 GitHub 仓库的 **Settings**：
1. **Actions → General → Workflow permissions**：选择 **Read and write permissions**，保存（这样每日 04:00 Apple 巡检与云端刷新才能把最新 JSON 自动提交回仓库）。
2. **Secrets and variables → Actions → Repository secrets**，新增以下 3 个 Secrets（官网刷新才会读取，Apple 每日巡检零消耗）：
   * `IPROYAL_PROXY_USER`
   * `IPROYAL_PROXY_PASS`
   * `IPROYAL_PROXY_ENDPOINT`（例如 `us.proxy.iproyal.com:12321` 或你的网关 `host:port`）

### 第 3 步：连接 Cloudflare Pages（或 Vercel）获得永久 HTTPS 网址

#### 方式 A：Cloudflare Pages（推荐，\$0/月，全球 CDN）
1. 登录 [Cloudflare Dashboard](https://dash.cloudflare.com/) → **Workers & Pages → Create → Pages → Connect to Git**，选择你的仓库。
2. 构建配置：
   * **Build command**：留空
   * **Build output directory**：`public`
   * Functions 会自动识别本仓库的 `functions/api/[[path]].js`。
3. 在 **Settings → Environment variables（Production & Preview）** 添加：
   * `ADMIN_PASSWORD`：你自己设定的管理员登录口令
   * `GITHUB_TOKEN`：一个具有该仓库 `Actions (Read & write)`、`Contents (Read & write)`、`Secrets (Read)` 权限的 GitHub Fine-grained PAT（仅用来从后台页面触发 Action 和写入 `overrides.json`）
   * `GITHUB_REPO`：`<你的用户名>/<仓库名>`
   * `GITHUB_BRANCH`：`main`（可选，默认 `main`）

#### 方式 B：Vercel（同样 \$0/月）
直接导入该仓库到 Vercel（`vercel.json` 与 `api/[...path].js` 已就绪），添加相同的 `ADMIN_PASSWORD`、`GITHUB_TOKEN`、`GITHUB_REPO` 环境变量即可。

---

## 上线验收后，停用本机旧服务与 18769 衔接

1. 先打开运行中的 `http://127.0.0.1:18769/`（**The X Subscription**），把原先复用 `18767` 的住宅代理用户名、密码、入口完整填进 `18769` 本站配置一次，确保 `18769` 完全独立。
2. 确认云端 `https://<你的站点>` 打开正常、ChatGPT / Google AI Ultra 数据及管理员登录均通过后，再执行以下命令彻底停用本机 `18767` 和 `18771` 后台服务：
```bash
launchctl bootout gui/$(id -u)/com.zouchenlong.chatgpt-pricing-tracker
launchctl bootout gui/$(id -u)/com.zouchenlong.google-ai-ultra-prices
```

---

## 本地测试
```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
node --test tests/test_api_core.mjs tests/test_frontend.mjs
python3 tests/verify_ui.py
```
