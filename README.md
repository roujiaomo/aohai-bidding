# 遨海商机雷达（本地版）

面向遨海科技的轻量招投标机会发现与跟进工具。当前版本以本地 SQLite、Python 标准库和静态管理看板运行，不依赖 Docker、Redis 或浏览器自动化。

## 本地启动

```powershell
python app\radar.py init
python app\radar.py seed-demo
python app\radar.py serve
```

浏览器打开 `http://127.0.0.1:8787`。

## 配置与数据

首次使用可复制 `config.example.json` 为 `config.json`，再按需调整。配置文件、SQLite 数据库、抓取结果和本地调试文件均已被 Git 忽略，不会提交到仓库。

```powershell
Copy-Item config.example.json config.json
```

## 常用命令

```powershell
# 抓取所有已接入来源的最新公告
python app\radar.py fetch

# 只抓取某个来源
python app\radar.py fetch csg

# 天眼查关键词搜索（默认关键词：航道/航标/海事；可用环境变量覆盖）
# PowerShell: $env:TYC_KEYWORDS="疏浚,智慧港口"; python app\radar.py fetch tianyancha
set TYC_KEYWORDS=疏浚,智慧港口 && python app\radar.py fetch tianyancha

# 导入符合 docs/sample_tenders.json 格式的公告
python app\radar.py import-json docs\sample_tenders.json

# 查看本地公告
python app\radar.py list --min-score 60

# 查看已登记来源与接入状态（已接入/待接入分组显示）
python app\radar.py sources

# 采购单位画像增强（天眼查 tyc CLI，消耗账号额度）
python app\radar.py enrich                    # 自动挑选评分>=80的采购单位
python app\radar.py enrich "某公司名称"         # 直接指定单位
python app\radar.py enrich --min-score 60 --limit 10 --with-risk
```

## 天眼查画像增强（tyc CLI）

`enrich` 命令通过天眼查官方 `tyc` CLI 对高分采购单位做工商画像增强，结果存入 `buyer_profiles` 表。

**前置条件**（每台机器一次性配置）：

```bash
npm install -g tyc-cli
tyc login --no-open --no-block   # 浏览器完成 OAuth 授权后
tyc login --resume
```

**命令定位说明**：
- 每次查询消耗天眼查账号额度，默认只查工商登记（每家 1 次），`--with-risk` 会额外查询风险总览
- 已画像的单位默认跳过，`--refresh` 强制重查
- 机关/事业单位（如海事局、航道局）通常不在天眼查企业库，会如实标记 `no_match`
- tyc 命令查找顺序：`TYC_CMD` 环境变量 → 本机 node 直调路径 → PATH 中的 `tyc`
- 服务器部署：装好 node + tyc-cli 后在服务器上跑一次 `tyc login` 即可，无需从本机搬运凭证

## 来源接入状态

### 已接入（5 个）

| 来源 | 适配器 | 说明 |
|------|--------|------|
| ccgp 中国政府采购网 | fetch_ccgp | 中央公开招标公告列表页静态 HTML，抓取前 3 页（约 60 条/次） |
| cjhdj 长江航道局 | fetch_cjhdj | 招标公告列表页静态 HTML |
| csg 南方电网供应链平台 | fetch_csg | 采购公告列表页静态 HTML |
| crc 华润守正电子招标 | fetch_crc | 招标公告列表页静态 HTML（需 SSL legacy 兼容）|
| tianyancha 天眼查 | fetch_tianyancha / enrich | 天眼AI官方 OAuth 通道。fetch：按业务关键词（默认 航道/航标/海事，可用 `TYC_KEYWORDS` 环境变量覆盖）搜索近 90 天全国招标公告入库，每个关键词消耗 1 次额度；enrich：高分采购单位工商画像 |

### 无法接入（11 个）

| 来源 | 原因 |
|------|------|
| ln_ggzy 辽宁公共资源 | 交易信息页 JS 动态渲染 |
| sd_ggzy 山东公共资源 | HTTP 连接超时 |
| bj_ggzy 北京公共资源 | SSL BAD_ECPOINT 不兼容 |
| msa 海事局 | 采购栏目 404/403，无结构化公告列表 |
| cmcc 中国移动 | SPA 单页应用，需 JS 渲染 |
| ceb 招标投标公共服务平台 | API 反爬挑战 + 403 |
| ggzy 全国公共资源 | SSL 握手超时 |
| chnenergy 国能e招 | 151 字节 JS 跳转页 |
| caizhao 采招网 | 需会员账号 |
| sgcc 国家电网 ECP | SPA hash 路由，需 JS 渲染 |
| unicom 中国联通 | HTTP 412 反爬拦截 |

数据库默认位于 `data/radar.db`。账号、Cookie 和任何访问凭据均不写入数据库或代码；登录型来源目前只登记为待授权/人工核验来源。

## 当前范围

- 公告导入、去重、业务匹配、优先级评分、跟进状态与本地看板。
- 登记公开、低风险来源及其接入等级。
- 不下载标书、不执行报名或投标、不规避登录、验证码和反自动化措施。

后续服务器部署时，可继续运行本项目，或将 SQLite 迁至 PostgreSQL、将 CLI 任务交给 systemd timer / cron。
