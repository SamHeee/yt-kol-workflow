# YouTube 网红开发工作流系统

基于网红营销SOP，自动化执行"关键词搜索→视频筛选→网红识别→联系方式获取→飞书录入"全流程。

## 快速开始

### 工作流使用教程（新手推荐）

> 如果你是首次使用，请按以下步骤操作：

1. **克隆项目** - 在 Agent 对话框输入：
   ```
   把 https://github.com/SamHeee/yt-kol-workflow 安装到 E:\（请自行填写你的文件夹路径）
   ```

2. **创建配置文件** - 在 Agent 对话框输入：
   ```
   参考项目文件创建 .env 和 keyword.txt 文件
   ```

3. **配置 YouTube API** - 手动打开 `.env` 文件，输入你的 YouTube API 密钥并保存。
   > 需要提前用 Gmail 邮箱自行获取免费 API 密钥（可咨询 AI 获取帮助）

4. **设置搜索关键词并启动** - 在 Agent 对话框输入：
   ```
   我的搜索关键词是 surefire/surefire flashlight，帮我更新到 keyword.txt。更新完成后启动工作流
   ```

5. **同步飞书（可选）** - 如需打通飞书，在 Agent 对话框输入：
   ```
   同步数据到飞书文档
   ```
   Agent 会自动进行飞书 CLI 授权、创建新多维表格，最后同步完整的数据。

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，至少填入 YOUTUBE_API_KEY
```

飞书认证默认使用 `FEISHU_AUTH_MODE=auto`。已有 `FEISHU_APP_TOKEN`、`FEISHU_APP_ID`、`FEISHU_APP_SECRET` 三项完整配置时继续使用旧 App 模式；否则转入 lark-cli OAuth 自动设置，不要求把 OAuth 凭证写进 `.env`。

### 3. 准备 Node.js / npx（飞书模式）

自动安装 `lark-cli` 的前提是本机已有 Node.js 和 `npx`。工作流会检测 CLI；默认可通过 `npx` 自动安装，并打开浏览器引导创建应用和完成 Base OAuth 授权。用户只需在浏览器中确认授权。企业管理员策略可能要求额外审批，此时需等待管理员放行后重试。

```bash
node --version
npx --version

# 可在正式搜索前单独完成飞书设置
python main.py feishu-setup
```

如果只保存本地 Excel，可直接使用 `--no-feishu`；该选项不会检测、安装或启动 lark-cli，也不会打开浏览器。

### 4. 配置品牌排除名单（可选）

```bash
python main.py exclusion --add-brand "ExampleBrand"
python main.py exclusion --add-brand "AnotherBrand"
python main.py exclusion --list
```

### 5. 运行

```bash
# 批量搜索（推荐主流程）
python main.py batch --keywords-file keywords.example.txt

# 单关键词搜索（兼容/调试入口）
python main.py search --keyword "wireless earbuds review"
```

## 工作流程

```
阶段A: 关键词搜索 → 阶段B: 数据提取+筛选 → 阶段C: 频道去重 → 阶段D: 网红详情
      ↓                    ↓                    ↓                  ↓
飞书·搜索任务表       飞书·视频数据表        飞书·网红详情表      飞书·网红视频表
```

默认筛选标准: 播放量 > 10,000 **或** 互动率 > 3%。也支持 AND、WEIGHTED 逻辑，以及订阅数、视频时长、字幕状态、清晰度、发布时间范围等过滤条件。品牌官方频道会按排除名单自动过滤。

## 命令参考

### search - 单关键词搜索（兼容/调试入口）

`search` 用于临时调试单个关键词、断点续传单个关键词，或快速验证某个关键词的输出。

```bash
python main.py search --keyword "关键词" [选项]

选项:
  --sort-order {relevance,viewCount,date,rating,title}
                                            排序策略（不指定则交互询问）
  --max-results N                          最大结果数（默认100）
  --no-detail                              仅搜索+筛选，不抓取网红详情
  --feishu-app-token TOKEN                 可选：指定飞书 Base URL 或 app_token
  --no-feishu                              跳过飞书写入
  --no-excel                               跳过本地Excel输出
  --min-views N                            播放量阈值（默认10000，规则为 > N）
  --min-engagement N                       互动率阈值百分比（默认3.0，规则为 > N）
  --filter-mode {or,and,weighted}          视频筛选逻辑（默认 or）
  --min-subscribers N                      频道最低订阅数（默认0，不限制）
  --estimated-channels N                   配额预算估算: 每个关键词预计新增频道数
  --seen-channels-file FILE                跨运行持久频道库路径
  --video-duration {any,short,medium,long} YouTube 搜索过滤: 视频时长
  --video-caption {any,closedCaption,none} YouTube 搜索过滤: 字幕状态
  --video-definition {any,high,standard}   YouTube 搜索过滤: 清晰度
  --published-after YYYY-MM-DD             YouTube 搜索过滤: 发布时间不早于
  --published-before YYYY-MM-DD            YouTube 搜索过滤: 发布时间不晚于
  --resume STATE_FILE                      从断点续传
  --output-dir DIR                         输出目录（默认项目目录下 output）
  --region CODE                            搜索地区（默认 US）
  --lang CODE                              搜索语言（默认 en）
  --log-level {DEBUG,INFO,WARNING}
```

### batch - 批量关键词搜索

这是当前推荐的命令行主流程。

```bash
python main.py batch --keywords-file FILE [选项]

# 关键词文件格式:
# txt: 每行一个关键词
# csv: keyword,sort_order,max_results

# 常用选项:
# --sort-order relevance
# --min-views 10000
# --min-engagement 3
# --filter-mode or
# --min-subscribers 0
# --no-feishu / --no-excel
# --playlist / --search
# --yes
```

### exclusion - 品牌排除名单

```bash
python main.py exclusion --list
python main.py exclusion --add-brand "品牌名"
python main.py exclusion --add-channel "UCxxxxxxxx"
python main.py exclusion --add-keyword "频道名关键词"
python main.py exclusion --import-file exclusions.csv
```

### quota - 配额说明

```bash
python main.py quota
```

### feishu-setup - 飞书自动设置

```bash
python main.py feishu-setup
```

该命令可在搜索前独立完成 lark-cli 检测或自动安装、命名 profile、浏览器应用创建和 Base OAuth 授权。若未提供 Base URL 或 `FEISHU_APP_TOKEN`，设置流程会自动创建一个名为 `FEISHU_BASE_NAME`（默认 `KOL_CRM_`）的 Base，并在本地保存后续运行所需的目标信息。OAuth 凭证由 lark-cli 存入系统钥匙串，不写入 `.env`。

### 数据汇总与飞书维护

```bash
# 合并多个批次输出目录，生成 search_tasks / search_videos / influencers / influencer_videos 四张业务表
# 来源批次、关键词目录和源文件路径保存在独立的“来源信息”Sheet
python main.py merge-output output/20260621_211216_batch output/20260622_102003_batch --output output/summary/kol_summary_tables.xlsx

# 将汇总工作簿写入飞书；默认先测试 10 条并回读校验，再执行全量
python main.py sync-workbook --workbook output/summary/kol_summary_tables.xlsx --cleanup-empty-rows

# 同步 influencers 时调用 YouTube API 补空“频道描述”
python main.py sync-workbook --workbook output/summary/kol_summary_tables.xlsx --table influencers --fill-channel-descriptions

# 只补写飞书“频道描述”字段，不更新其他字段
python main.py sync-workbook --workbook output/summary/kol_summary_tables.xlsx --table influencers --fill-channel-descriptions --description-only

# 删除飞书多维表格中的全空行
python main.py clean-feishu-empty

# 首次补齐网红详情新增字段，并只生成前 10 条回填计划
# 注意：--ensure-schema 会修改表结构，即使同时使用 --dry-run
python main.py refresh-influencers --ensure-schema --limit 10 --dry-run

# 全量回填姓名、活跃度、频道判断和代表视频标题
python main.py refresh-influencers

# 建议每周刷新一次最新发布日期和断更评估
python main.py refresh-influencers --fields activity

# 仅在确认现有 KOL Name 全部由程序生成时，重新计算并覆盖姓名
python main.py refresh-influencers --fields name --replace-kol-names
```

`sync-workbook`、`clean-feishu-empty`、`refresh-influencers` 等维护命令可以省略 token 参数，自动复用已保存的飞书目标和 profile；需要临时覆盖目标时仍可显式传入 Base URL 或 app_token。

除显式传入 `refresh-influencers --ensure-schema` 外，维护命令默认不改飞书字段结构。Excel 会保留 A 列主字段 `多行文本` 以匹配飞书物理字段，但该列固定为空，`sync-workbook` 不会把它写回飞书。频道描述补齐已并入 `sync-workbook`，统计结果会保存为 JSON，便于追踪新增、更新、重复源数据和空行清理情况。

### Excel 与飞书字段一致性

`feishu/schema.py` 是四张表字段名称、顺序和类型的唯一来源。新生成的 Excel 使用与 Schema 完全相同的表头和顺序，包括第一列空主字段 `多行文本`：搜索任务表 13 列、视频数据表 20 列、网红详情表 25 列、网红视频表 16 列。旧版 Excel 的中文别名仍可被汇总和同步命令读取，但所有新文件只输出飞书标准字段名。

汇总工作簿包含 `search_tasks`、`search_videos`、`influencers`、`influencer_videos` 四张业务 Sheet，以及独立的 `来源信息` Sheet。来源批次、来源关键词目录和来源文件不会混入业务表字段。

`开发负责人`、`备注`等人工字段会在 Excel 中保留对应列，但工作流不会回读飞书中的历史人工值；新记录没有本地值时保持空白。

### 记录日期字段

| 数据表 | 字段 | 规则 |
|------|------|------|
| 视频数据表 | 视频记录日期 | 飞书系统创建时间（`CreatedTime`，type `1001`），记录当前视频记录首次写入飞书的日期 |
| 网红详情表 | 网红记录日期 | 飞书系统创建时间（`CreatedTime`，type `1001`），记录当前网红记录首次写入飞书的日期 |

两个字段由飞书自动维护，不进入新增或更新 payload，因此重复搜索、幂等 upsert、详情刷新和工作簿同步都不会覆盖原日期。视频和网红记录写入飞书后，工作流会回读真实 `CreatedTime` 写入本地 Excel；未启用飞书或回读失败时对应 Excel 单元格留空，不用本地运行时间代替。删除后重新创建记录会生成新的日期。空行清理会忽略创建时间、最后更新时间、创建人、修改人和自动编号等系统自动字段，只按业务字段判断一行是否为空。

### 网红详情增强字段

网红详情表新增以下字段，本地 Excel 会按此顺序输出；已有飞书表中的新增列需要在飞书界面手工拖动到目标位置。

| 字段 | 规则 |
|------|------|
| KOL Name | 明确自我介绍可直接采用；否则仅在频道名称与非通用邮箱前缀相互印证时采用；弱证据或冲突为 `手动确认` |
| 最新发布日期 | YouTube API 可见的最新已发布视频时间，使用 UTC 计算 |
| 断更评估 | 30 天内为 `持续更新`；超过 30 天或确认无公开视频为 `有断更风险`；API/解析失败为 `待确认` |
| 频道初步判断 | 固定格式：`领域=...; 内容=...; 主体=...; 自有品牌=...` |
| 代表视频标题 | 代表视频在创作者默认语言下的当前标题，不代表历史首发标题 |

`refresh-influencers` 只更新上述增强字段，不覆盖开发状态、开发负责人、备注或已经人工确认的 KOL Name；占位值 `手动确认` 可以在后续识别成功时升级为真实姓名。
`--replace-kol-names` 是一次性迁移/规则修正选项，会覆盖现有姓名，人工开始维护后不要使用。

## 环境变量

| 变量 | 必填 | 说明 |
|------|------|------|
| YOUTUBE_API_KEY | 是 | YouTube Data API v3 密钥 |
| FEISHU_AUTH_MODE | 否 | `auto`（默认）/ `cli` / `app`；`auto` 优先兼容完整旧 App 配置，否则使用 CLI OAuth |
| FEISHU_APP_ID | App 模式 | 旧 App 模式应用 App ID |
| FEISHU_APP_SECRET | App 模式 | 旧 App 模式应用 App Secret |
| FEISHU_APP_TOKEN | 否 | Base URL 或 app_token；未提供时 CLI 模式自动创建 Base |
| FEISHU_CLI_PROFILE | 否 | lark-cli profile 名称，默认 `kol-workflow` |
| FEISHU_BASE_NAME | 否 | 自动创建 Base 的名称，默认 `KOL网红开发工作流` |
| FEISHU_AUTO_SETUP | 否 | 是否自动完成 CLI profile、授权和 Base 设置，默认 `true` |
| FEISHU_AUTO_INSTALL | 否 | 未发现 lark-cli 时是否通过 npx 自动安装，默认 `true` |
| FEISHU_OPEN_BROWSER | 否 | 设置和授权时是否自动打开浏览器，默认 `true` |
| FEISHU_AUTH_TIMEOUT_SECONDS | 否 | 等待浏览器授权的最长秒数，默认 `900` |
| FEISHU_LARK_CLI_PATH | 否 | 自定义 lark-cli 可执行文件路径；留空时从 PATH 查找 |
| OUTPUT_DIR | 否 | 输出目录（默认项目目录下 output） |
| LOG_LEVEL | 否 | 日志级别（默认 INFO） |
| REGION | 否 | 搜索地区（默认 US） |
| LANG_CODE | 否 | 搜索语言（默认 en） |
| SEEN_CHANNELS_FILE | 否 | 跨运行持久频道库路径（默认项目目录下 seen_channels.json） |
| BRAND_EXCLUSION_FILE | 否 | 品牌排除名单路径（默认 brand_exclusions.json） |

## 飞书模式说明

默认认证策略为 `FEISHU_AUTH_MODE=auto`：

1. 同时存在 Base URL/app_token、`FEISHU_APP_ID` 和 `FEISHU_APP_SECRET` 时，继续使用兼容的旧 App 模式。
2. 其余情况进入 lark-cli OAuth 模式。系统按 `FEISHU_CLI_PROFILE` 创建或复用命名 profile，必要时通过浏览器引导创建应用并申请 Base 权限；用户只需在浏览器中确认。
3. 没有 Base URL/app_token 时，系统自动创建名为 `FEISHU_BASE_NAME`（默认 `KOL网红开发工作流`）的 Base，初始化四张业务表并清理新 Base 的默认空表；目标信息保存在 `~/.kol_workflow/feishu_base_targets.json`，供主流程和维护命令复用。

可用 `FEISHU_AUTH_MODE=cli` 强制 CLI OAuth，或用 `FEISHU_AUTH_MODE=app` 强制旧 App 模式。`--no-feishu` 会完整跳过飞书准备、安装、授权和预检，不会触发浏览器。

### CLI 自动安装与授权

默认 `FEISHU_AUTO_INSTALL=true`、`FEISHU_AUTO_SETUP=true`。未检测到 lark-cli 时，系统会在 Node.js 与 `npx` 可用的前提下尝试自动安装；随后建立命名 profile，并按 `FEISHU_OPEN_BROWSER` 打开浏览器等待授权。等待上限由 `FEISHU_AUTH_TIMEOUT_SECONDS` 控制，自定义安装位置可通过 `FEISHU_LARK_CLI_PATH` 指定。

lark-cli 的 OAuth 凭证保存在操作系统钥匙串，不写入 `.env`；本地保存的 Base 目标不等同于 OAuth 凭证。企业安全策略可能要求管理员审批应用或权限，自动流程无法绕过组织审批。

维护命令使用 `--dry-run` 时不会新建 Base；如果既没有显式目标也没有本地已保存目标，程序会提示先运行 `python main.py feishu-setup`。

### 飞书预检

正式搜索开始前，系统会先执行飞书预检:

- 检查多维表格连接和写入权限
- 检查并补齐 `搜索任务表`、`视频数据表`、`网红详情表`、`网红视频表`
- 校验四张表的字段名称和字段类型，包括两张业务表的系统创建时间字段
- 向四张表分别写入测试记录
- 测试成功后立即删除测试记录

如果预检失败，程序会在调用 YouTube 搜索前停止，或询问是否切换为本地 Excel 保存继续。

如自动安装被关闭或失败，可参考 [larksuite/cli](https://github.com/larksuite/cli) 和 [飞书 CLI 安装指南](https://www.feishu.cn/content/article/7623291503305083853)。

## 输出文件

### 批量模式（当前主流程）

`batch` 会生成一个批次目录。批次根目录下是汇总文件，`per_keyword/` 下保存每个关键词的明细文件。

```
output/
└── {时间戳}_batch/
    ├── search_tasks_all.xlsx       # 全部关键词搜索任务汇总
    ├── search_videos_all.xlsx      # 全部关键词搜索视频汇总
    ├── influencers_all.xlsx        # 全部关键词网红详情汇总
    ├── influencer_videos_all.xlsx  # 全部关键词网红视频汇总
    └── per_keyword/
        └── {时间戳}_{关键词}/
            ├── search_tasks.xlsx
            ├── search_videos.xlsx
            ├── influencers.xlsx
            └── influencer_videos.xlsx
```

### 单关键词模式（兼容/调试入口）
```
output/
└── {时间戳}_{关键词}/
    ├── search_tasks.xlsx       # 本次搜索任务状态与统计
    ├── search_videos.xlsx      # 搜索到的视频（含筛选标记）
    ├── influencers.xlsx        # 网红详情（含频道描述）
    └── influencer_videos.xlsx  # 网红最近视频
```

## 配额说明

YouTube Data API 每日免费配额 10,000 units:
- search.list: 100 units/次
- videos.list / channels.list / playlistItems.list: 1 unit/次

推荐使用 playlistItems 方案获取网红最近视频（2 units/频道 vs 101 units/频道），每日可处理约 45 个关键词。

## 安全注意事项

- 所有密钥存放在 `.env` 文件中，已加入 `.gitignore`
- 日志中 API Key 自动脱敏
- 切勿将 `.env` 提交到代码仓库

## Disclaimer / 免责声明

This tool uses the official YouTube Data API v3 and only accesses
publicly available data. Contact emails are extracted solely from
information that creators have voluntarily published in their public
channel descriptions.

By using this tool, you agree that:

- You will use your own YouTube API key and comply with the
  [YouTube API Services Terms of Service](https://developers.google.com/youtube/terms/api-services-terms-of-service)
  and [Developer Policies](https://developers.google.com/youtube/terms/developer-policies),
  including data retention and refresh requirements.
- You are solely responsible for complying with applicable anti-spam
  and privacy laws (e.g., CAN-SPAM, GDPR, PIPL) when contacting any
  creator whose information is processed by this tool.
- Extracted data must be used only for legitimate business outreach.
  Any misuse is the sole responsibility of the user.

The author assumes no liability for how third parties use this software.

本工具基于 YouTube 官方 Data API v3，仅访问公开数据；联系邮箱仅提取自
创作者在频道简介中主动公示的信息。使用者需自备 API Key 并自行遵守
YouTube API 服务条款、开发者政策及所在地反垃圾邮件与隐私法规
（如 CAN-SPAM、GDPR、个人信息保护法）。提取的数据仅限合法商务合作
用途，任何滥用行为由使用者自行承担责任，作者不承担任何连带责任。
