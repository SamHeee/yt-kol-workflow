# YouTube KOL 开发工作流

从 YouTube 关键词发现 KOL，并把采集、合作、内容刷新、样品物流和提醒串成一套飞书多维表格工作流。

本版本包含完整自动化：首次交互运行可自动安装 `lark-cli`、打开浏览器完成 OAuth、创建 Base 和七张表；后续可按小时刷新合作内容，并按日刷新 17TRACK 物流及发送飞书提醒。

## 功能范围

飞书工作区由七张表组成：

1. 搜索任务表
2. 视频数据表
3. 网红详情表
4. 网红视频表
5. 合作红人表
6. 样品跟踪表
7. 合作内容数据表

项目配套的飞书多维表格：[KOL网红开发工作流](https://my.feishu.cn/base/TABNbzI76aCG4VsmLutcr82inPd)。查看和编辑权限由该 Base 的飞书权限设置决定。

完整设置会幂等创建或补齐字段、查找引用和“首次进入合作自动建档”工作流。再次运行不会重复创建已有对象；遇到同名但类型冲突的字段时会停止并报告，不会破坏性覆盖。

已有项目升级请先阅读[可靠性修复与升级说明](docs/reliability_fixes.md)，其中列出了新增物流字段、旧检查点处理和自动化退出码变化。

## 环境要求

- Python 3.10 或更高版本
- Node.js 与 `npm`（飞书 CLI 自动安装需要）
- 用户自己的 YouTube Data API Key
- 用户自己的 17TRACK API Key（物流刷新需要）

## 快速开始

```bash
git clone https://github.com/SamHeee/yt-kol-workflow.git
cd yt-kol-workflow
python -m venv .venv
```

Windows PowerShell：

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
Copy-Item keywords.example.txt keywords.txt
```

macOS / Linux：

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
cp keywords.example.txt keywords.txt
```

编辑 `.env`，至少填写：

```dotenv
YOUTUBE_API_KEY=
```

然后执行完整飞书设置：

```bash
python main.py feishu-setup
```

默认配置有意保持以下交互体验：

```dotenv
FEISHU_AUTH_MODE=auto
FEISHU_AUTO_SETUP=true
FEISHU_AUTO_INSTALL=true
FEISHU_OPEN_BROWSER=true
```

没有可用 CLI 时，程序会通过 `npm` 全局安装仓库已验证的精确版本 `@larksuite/cli@1.0.70`，不会运行 CLI 的安装向导或额外安装 Skills；需要授权时会打开浏览器；没有指定或保存过 Base 时会创建名为 `KOL网红开发工作流` 的 Base。OAuth 凭证由 `lark-cli`/操作系统保存，不写入 `.env`。如需完全禁止自动安装，可设置 `FEISHU_AUTO_INSTALL=false`，或在相关命令中使用 `--no-feishu-auto-install`。

只查看建表计划、不修改远端：

```bash
python main.py feishu-setup --dry-run
```

仅使用本地 Excel、完全跳过飞书准备：

```bash
python main.py batch --keywords-file keywords.txt --no-feishu
```

## 搜索与采集

```bash
# 批量关键词主流程
python main.py batch --keywords-file keywords.txt

# 单关键词调试
python main.py search --keyword "wireless earbuds review"

# 品牌排除名单
python main.py exclusion --add-brand "ExampleBrand"
python main.py exclusion --list

# 查看 API 配额估算
python main.py quota
```

默认筛选规则为播放量或互动率达到配置阈值，也支持 `or`、`and`、`weighted` 模式，以及订阅数、时长、字幕、清晰度和发布时间过滤。

## 飞书运营自动化

### 首次进入合作

当网红详情进入合作状态后，飞书工作流按规范化邮箱查重并创建合作记录。重复触发不会重复建档。合作红人表继续关联样品跟踪和合作内容数据。

运营人员可在合作红人表记录合作形式、合作费用和专属code，并在样品跟踪表记录样品成本与运费。金额字段保留两位小数，不预设币种符号。

### 合作内容刷新

合作内容刷新只更新已有记录，校验视频 ID、视频所属频道和重复视频，并更新标题、发布时间、播放、点赞、评论、互动率和同步状态。失败时保留最后一次成功快照。

归因点击、归因GMV、归因订单量和归因销量由运营人员填写；YouTube 刷新不会覆盖这些字段。

```bash
# 预览，不写飞书
python main.py refresh-cooperation-content --dry-run --limit 10

# 刷新到期记录，默认成功记录 24 小时后再次刷新
python main.py refresh-cooperation-content

# 强制刷新指定记录；参数可重复
python main.py refresh-cooperation-content --content-record-id RECORD_ID --force
```

### 样品物流刷新

物流模块使用 17TRACK 查询已有样品记录。请在 `.env` 配置：

```dotenv
LOGISTICS_PROVIDER=17track
TRACK17_API_KEY=
SAMPLE_LOGISTICS_TIMEZONE=Asia/Shanghai
SAMPLE_LOGISTICS_STALL_HOURS=72
SAMPLE_REMINDER_USER_OPEN_ID=
```

```bash
# 完整 dry-run：不调用 17TRACK、不注册运单、不写飞书
python main.py refresh-sample-logistics --dry-run

# 刷新已寄出或异常且有运单号的样品
python main.py refresh-sample-logistics
```

程序会归一化承运商、物流状态、最新节点、签收时间和异常信息。运单号变化时，旧物流与提醒状态会按合同重置。

### 飞书提醒

提醒包括签收次日跟进和物流连续 72 小时无新进展。当天事件合并成一张飞书卡片，并通过事件键与发送状态避免重复发送。

```bash
# 只生成提醒计划
python main.py dispatch-sample-reminders --dry-run

# 发送当天提醒摘要
python main.py dispatch-sample-reminders
```

## 其他维护命令

```bash
python main.py merge-output OUTPUT_DIR_A OUTPUT_DIR_B --output output/summary/kol_summary_tables.xlsx
python main.py sync-workbook --workbook output/summary/kol_summary_tables.xlsx
python main.py clean-feishu-empty
python main.py refresh-influencers
```

字段合同详见：

- [合作红人表](docs/feishu_cooperation_table.md)
- [合作内容数据表](docs/feishu_cooperation_content_table.md)
- [样品跟踪表](docs/feishu_sample_tracking_table.md)
- [开发信字段](docs/feishu_outreach_letter_field.md)

## 开发与测试

```bash
python -m pip install -r requirements-dev.txt
python -m compileall -q .
python -m pytest -q -p no:cacheprovider
```

测试和 CI 使用模拟客户端，不应访问真实 YouTube、飞书或 17TRACK 服务。

## 安全说明

- 不要提交 `.env`、API Key、OpenID、运单号或业务导出；其他 Base/表/字段 ID 也不要提交，除非维护者明确指定写入文档。
- 调试日志和报错不得包含完整认证信息、完整运单号或飞书记录 payload。
- `output/`、本地状态、日志、Excel 和 CSV 已加入 `.gitignore`。
- 在真实工作区启用自动化前先运行 `feishu-setup --dry-run` 检查差异。

## Disclaimer / 免责声明

This tool uses the official YouTube Data API v3 and accesses publicly available data. Users must supply their own API credentials and are solely responsible for their use of YouTube, Feishu, 17TRACK and any creator contact information, including compliance with applicable terms, anti-spam rules and privacy laws. The author assumes no liability for third-party use of this software.

本工具使用用户自行提供的 YouTube、飞书和 17TRACK 凭证，仅处理工作流所需的数据。使用者应自行遵守相关服务条款、反垃圾信息规定和适用的隐私法律。第三方使用本软件产生的后果由使用者自行承担，作者不承担连带责任。
