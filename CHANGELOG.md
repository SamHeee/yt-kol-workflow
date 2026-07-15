# KOL Workflow 更新日志

## 2026-07-14 飞书自动 OAuth 与 Base 初始化

- 飞书认证默认改为 `FEISHU_AUTH_MODE=auto`：同时存在 Base URL/app_token、`FEISHU_APP_ID`、`FEISHU_APP_SECRET` 时兼容旧 App 模式，否则使用 lark-cli OAuth。
- CLI 模式支持自动检测或安装 lark-cli、创建 `FEISHU_CLI_PROFILE` 命名 profile、打开浏览器引导应用创建与 Base OAuth 授权；用户只需确认授权。
- 未提供 Base URL/app_token 时，自动创建名为 `FEISHU_BASE_NAME`（默认 `KOL网红开发工作流`）的 Base，初始化四张业务表、清理默认空表，并将目标保存到 `~/.kol_workflow/feishu_base_targets.json` 供后续复用。
- 新增 `feishu-setup` 子命令，可在正式搜索前独立完成飞书设置；`sync-workbook`、`clean-feishu-empty`、`refresh-influencers` 等维护命令允许省略 token 参数。
- 新增 `FEISHU_AUTO_SETUP`、`FEISHU_AUTO_INSTALL`、`FEISHU_OPEN_BROWSER`、`FEISHU_AUTH_TIMEOUT_SECONDS`、`FEISHU_LARK_CLI_PATH` 配置项。
- 自动安装要求本机已有 Node.js 与 `npx`。lark-cli OAuth 凭证保存在系统钥匙串，不写入 `.env`；企业策略仍可能要求管理员审批应用或权限。
- 显式使用 `--no-feishu` 时不触发 CLI 检测、安装、浏览器授权、Base 创建或飞书预检。

## 2026-07-14 删除 quickstart 交互式向导

小白用户不会用 PowerShell/Cmd，直接提供 CLI 参数更简单。

### 修改文件

| 文件 | 变更 |
|------|------|
| `quickstart.py` | 删除（交互式向导已移除） |
| `.quickstart_done` | 删除（运行时标记文件） |
| `main.py` | 删除 `quickstart` 子命令、`_ensure_quickstart_done()` 函数及 `.quickstart_done` 检查逻辑 |
| `README.md` | 删除 quickstart 相关说明，更新"运行"章节为直接调用 `batch` 命令 |

### 替代方式

用户直接使用 `batch` 命令配合 CLI 参数即可：

```bash
python main.py batch --keywords-file keywords.txt
```

## 2026-07-14 Excel 与飞书四表字段统一

- `feishu/schema.py` 成为四张表字段名称、顺序和类型的唯一来源；Excel 列定义从 Schema 派生，避免两套字段配置再次漂移。
- 四张表均显式保留飞书默认主字段 `多行文本`，新 Excel 将其作为第一列导出并保持为空；工作簿同步不会把该技术字段写回飞书。
- 新增搜索任务 Excel；单关键词、批量汇总和合并工作簿统一覆盖 `search_tasks`、`search_videos`、`influencers`、`influencer_videos` 四张业务表。
- 视频和网红写入飞书后回读真实 `CreatedTime`，分别写入本地 `视频记录日期`、`网红记录日期`；离线模式或回读失败时留空。
- 新文件只输出飞书标准字段名；读取旧批次时继续兼容旧中文表头。
- 汇总工作簿将 `来源批次`、`来源关键词目录`、`来源文件` 移至独立的 `来源信息` Sheet，不再污染业务表字段。
- `开发负责人`、`备注`等人工字段只增加本地列，不回读飞书历史人工值。

## 2026-07-14 视频与网红记录日期

- 视频数据表新增系统创建时间字段 `视频记录日期`，网红详情表新增系统创建时间字段 `网红记录日期`。
- 两个字段使用飞书 `CreatedTime`（type `1001`），表示当前飞书记录首次创建日期，不进入写入 payload，也不会被幂等 upsert 或详情刷新覆盖。
- 已有记录直接使用飞书保存的原始创建时间，无需逐行回填；记录删除后重新创建时日期会重置。
- 空行清理按字段元数据忽略 type `1001` 至 `1005` 的系统自动字段，避免创建时间使业务空行被误判为非空。
- 流程图移除已经下线的频道均播筛选，并更新网红详情表以 `Channel ID` 查重的说明。

## 2026-07-11 网红详情字段精简

- 网红详情表删除与 `Channel ID` 重复的 `唯一键`，后续统一以 `Channel ID` 查重。
- 网红详情表删除未参与当前业务筛选的 `频道均播`，并同步移除飞书格式化、汇总工作簿和 Excel 输出映射。
- 搜索任务表、视频数据表和网红视频表继续保留各自的 `唯一键`。

## 2026-07-10 网红详情增强与历史刷新

### 新增字段

| 字段 | 说明 |
|------|------|
| `KOL Name` | 公开自述优先、频道名与邮箱弱证据互证；不确定输出 `手动确认` |
| `最新发布日期` | 最近可见已发布视频的 UTC 时间 |
| `断更评估` | `持续更新` / `有断更风险` / `待确认` 三态 |
| `频道初步判断` | 领域、内容、主体、自有品牌四段结构化文本 |
| `代表视频标题` | 代表视频当前默认语言标题 |

### 工作流变更

- 新增 `filter/kol_name_extractor.py`、`filter/channel_classifier.py`、`filter/activity_evaluator.py` 和可维护的 `channel_taxonomy.json`。
- 阶段 D 复用最近视频数据完成活跃度与频道判断，不增加新频道常规抓取的 API 请求。
- 飞书 Schema、Excel 导出、汇总工作簿同步及预检统一支持 5 个新字段。
- 新增 `refresh-influencers` 命令，对历史记录做部分字段回填，并支持周期性刷新活跃度。
- 姓名刷新默认保护非占位值；提供一次性 `--replace-kol-names` 迁移选项，用于姓名规则升级后的重新判定。
- 修复飞书批量更新记录错误使用 `PUT` 的问题，按官方接口改为 `POST`；新增单选选项 Schema 管理。
- 汇总旧、新批次时合并非空增强字段，保护已确认 KOL Name。
- 状态文件加入数据版本，旧断点会提示使用刷新命令补齐字段。
- 新增规则、输出、刷新、汇总和状态兼容测试。

## 2026-07-07 飞书预检模块统一

### 修改文件

| 文件 | 变更 |
|------|------|
| `feishu/preflight.py` | 重构为统一预检模块 `FeishuHealthCheck` 类，整合配置检查、连接检查、写入权限测试（测试表方式）、字段类型校验、4表测试数据写入清理 |
| `check_feishu.py` | 已删除，CLI 诊断功能不再保留 |
| `main.py` | 无需修改，`run_feishu_preflight()` 保持向后兼容 |

### 合并后的功能

| 方法 | 说明 |
|------|------|
| `check_config()` | 配置完整性检查 |
| `check_connection()` | 连接+bitable信息获取 |
| `check_permissions_write()` | 创建/删除测试表验证写入权限 |
| `check_schema()` | 确保4个表存在 |
| `check_fields()` | 字段类型校验 |
| `check_write_permission_records()` | 4表测试数据写入清理 |
| `run()` | 静默执行全部预检，返回 client 供 workflow 使用 |

## 2026-07-07 重构：消除重复代码与遗留脚本

### 修改文件

| 文件 | 变更 |
|------|------|
| `utils/logger.py` | 新增 `_to_rfc3339_boundary` 函数（原分散在 `main.py` 与 `quickstart.py`） |
| `main.py` | 移除本地 `_to_rfc3339_boundary` 定义，改为从 `utils.logger` 导入；`run_phase_c` 改为直接引用 `filter.channel_dedup.deduplicate_channels` |
| `quickstart.py` | 移除本地 `_to_rfc3339_boundary` 定义，改为从 `utils.logger` 导入 |
| `workflow/phase_c_dedup.py` | 删除（功能已由 `filter/channel_dedup.py` 直接提供，无需包装层） |
| `output/summary_20260623_merged/` | 删除遗留脚本 `build_summary_workbook.mjs`、`collect_merged_data.py`、`delete_empty_feishu_rows.py`、`sync_dedup_workbook_to_feishu.py`、`update_feishu_channel_descriptions.py`、`dedupe_influencers.mjs`、`summary_env.py` 及 `__pycache__` |
| `kol_workflow_file_inventory.csv` | 移除 `phase_c_dedup` 相关 3 条记录；`logger.py` 条目更新字节数及功能描述 |

## 2026-07-05 Quickstart 默认批量关键词

### 修改文件

| 文件 | 变更 |
|------|------|
| `quickstart.py` | 新增搜索结果数量确认，默认每个关键词获取前 100 个结果，可自定义 |
| `quickstart.py` | 删除搜索模式确认，Quickstart 固定为批量关键词搜索 |
| `quickstart.py` | 用户直接输入多个关键词，程序覆盖写入项目根目录 `keywords.txt` |
| `README.md` / `output/system_workflow.html` | 同步 Quickstart 链路说明 |

## 2026-07-05 飞书单账户模式

### 修改文件

| 文件 | 变更 |
|------|------|
| `config.py` | `FeishuConfig` 统一读取 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_APP_TOKEN`，移除 Sam/work 账号字段 |
| `feishu/auth.py` | `FeishuAuth` 移除账号后缀解析，只使用单套飞书应用凭证 |
| `quickstart.py` | 飞书同步不再询问账号，只确认多维表格 URL 或 app_token 及缺失凭证 |
| `main.py` | 移除 `--feishu-account` 参数，主流程、汇总同步、空行清理统一单账户 |
| `check_feishu.py` / `test_feishu_write.py` / `feishu/workbook_sync.py` / `feishu/cleanup.py` | 移除账号选择参数 |
| `.env.example` / `README.md` | 删除 Sam/work 后缀变量说明，补充单账户配置和迁移说明 |

### 配置迁移

历史后缀变量需要迁移为无后缀变量：

| 旧变量 | 新变量 |
|--------|--------|
| `FEISHU_APP_ID_Sam` / `FEISHU_APP_ID_work` | `FEISHU_APP_ID` |
| `FEISHU_APP_SECRET_Sam` / `FEISHU_APP_SECRET_work` | `FEISHU_APP_SECRET` |
| `FEISHU_APP_TOKEN_Sam` / `FEISHU_APP_TOKEN_work` | `FEISHU_APP_TOKEN` |

## 2026-06-25 飞书诊断工具合并

### 修改文件

| 文件 | 变更 |
|------|------|
| `check_feishu.py` | 增强为完整诊断工具，支持信息检查、权限检查、建表、连接诊断 |
| `check_and_setup_sam.py` | 改为包装器，重定向到 check_feishu.py，打印废弃提示 |

### 合并后的 check_feishu.py 功能

```bash
# 完整检查（信息 + 权限 + 建表 + 诊断）
python check_feishu.py

# 只做连接诊断
python check_feishu.py --diagnose-only

# 指定账号
python check_feishu.py --feishu-account work
```

## 2026-06-25 飞书认证模块重构：Token 持久化 + 多账号无缝切换

### 问题背景

之前切换飞书账号或多维表格 URL 后，经常报旧账号错误，需要频繁重新授权。根本原因：
1. `FeishuAuth` 的 token 仅存内存，程序重启后需重新认证
2. 多账号（Sam/work）的环境变量后缀支持不完整

### 修改文件

#### 1. `feishu/auth.py`

| 更新事项 | 更新原因 |
|---------|---------|
| 新增账号后缀支持 `FeishuAuth(account='Sam')` | 自动读取 `FEISHU_APP_ID_Sam` / `FEISHU_APP_SECRET_Sam` |
| 新增 Token 文件缓存（`~/.kol_workflow/.feishu_token_{app_id}`） | 跨进程复用 token，减少认证频率 |
| 新增 `invalidate()` 方法 | 清除缓存，强制重新认证 |
| 优化 token 获取优先级：内存 → 文件 → API | 确保 token 复用同时保证有效性 |

#### 2. `config.py`

| 更新事项 | 更新原因 |
|---------|---------|
| 新增 `FeishuConfig.create_auth()` 方法 | 标准化认证创建流程 |
| 新增 `FeishuConfig.invalidate_auth()` 方法 | 一行代码清除账号缓存 |

### 使用方式

```python
# 标准用法（推荐）
from config import FeishuConfig
cfg = FeishuConfig(account='Sam')
auth = cfg.create_auth()
client = BitableClient(cfg.app_token, auth)

# 切换账号前清除旧缓存
cfg.invalidate_auth()  # 清除 ~/.kol_workflow 中的缓存 token

# 或命令行清除
python -c "from config import FeishuConfig; FeishuConfig(account='Sam').invalidate_auth()"
```

### 功能验证

| 测试项 | 结果 |
|--------|------|
| 账号后缀读取（Sam/work/默认） | 通过 |
| Token 文件缓存写入/读取 | 通过 |
| 跨进程 token 复用 | 通过 |
| `invalidate()` 缓存清除 | 通过 |
| `FeishuConfig.create_auth()` | 通过 |

## 2026-06-24 汇总同步流程产品化

### 新增文件

| 文件名 | 说明 |
|--------|------|
| `export/summary_builder.py` | 合并多个批次输出目录，生成 `influencer_videos`、`influencers`、`search_videos` 三张汇总表，并默认按业务 ID 去重 |
| `feishu/value_utils.py` | 统一飞书同步所需的空值判断、文本提取、数字、日期、链接、复选框格式转换 |
| `feishu/workbook_sync.py` | 将汇总工作簿同步到飞书多维表格，默认先测试 10 条、回读校验，再执行全量 |
| `feishu/cleanup.py` | 删除飞书多维表格中的全空行，支持按表名选择和 dry-run |

### 修改文件

| 更新事项 | 更新原因 |
|---------|---------|
| `export/excel_exporter.py` 的网红详情导出新增 `频道描述` 列 | 修复频道描述已采集但本地 Excel 未导出的问题 |
| `main.py` 新增 `merge-output`、`sync-workbook`、`clean-feishu-empty` 子命令 | 将本次人工处理沉淀为可复用流程，频道描述补齐并入 `sync-workbook` |
| 汇总默认对 `influencers` 按 `频道ID` 去重，对两个视频表按 `Video ID` 去重 | 减少重复视频和重复频道写入飞书 |
| 飞书同步遵守现有字段结构，按飞书字段类型转换，并跳过 A 列主字段 `多行文本` | 避免字段结构漂移和主字段被误写 |
| `sync-workbook` 新增 `--fill-channel-descriptions` 和 `--description-only` | 将频道描述补齐并入现有同步流程，不再维护独立回填工具 |
| 飞书同步支持测试 10 条、回读校验、全量执行、统计 JSON | 降低批量写入风险，保留可审计结果 |
| `delete_empty_records.py` 改为调用新的清理模块 | 去除硬编码凭证，避免旧脚本误删或扩散密钥 |
| `check_feishu.py`、`test_feishu_write.py`、`check_and_setup_sam.py` 改为读取环境变量/参数 | 移除源码中的飞书凭证；测试写入后自动清理测试记录 |
| `README.md` 补充汇总、同步内补频道描述和空行清理命令 | 让后续操作不依赖临时脚本或对话记录 |

### 功能验证

| 测试项 | 结果 |
|--------|------|
| Python 语法检查 | 通过 |
| 新增 CLI 帮助命令 | 通过 |
| 使用 2026-06-21 / 2026-06-22 三个批次目录实跑本地汇总 | 通过，生成 3 张去重汇总表 |
| 源码范围硬编码飞书凭证扫描 | 通过 |

## 2026-06-23 Quickstart 启动向导与流程配置改造

### 新增文件

| 文件名 | 说明 |
|--------|------|
| `quickstart.py` | 新增交互式启动向导，统一收集搜索模式、排序、筛选、保存方式和飞书前置设置 |

---

### 修改文件

#### 1. `main.py`

| 更新事项 | 更新原因 |
|---------|---------|
| 新增 `quickstart` 子命令，并在不传子命令时默认进入 quickstart | 启动后先完成前置配置确认，避免任务执行到中途才报错 |
| lark-cli 未安装、未授权或授权失败时输出参考链接 | 让用户能直接找到安装和授权操作说明 |
| 飞书保存前新增多维表格预检 | 在正式搜索前检查表、字段、写入权限，并写入/删除测试记录 |
| 扩展 `--sort-order` 支持 `rating/title/videoCount` | 对齐 YouTube Data API `search.list` 可选排序参数 |
| 新增 `--min-views`、`--min-engagement` | 支持自定义筛选阈值，默认播放量 > 10000 或互动率 > 3% |
| 新增 `--video-duration`、`--video-caption`、`--video-definition`、`--published-after`、`--published-before` | 支持 YouTube 搜索阶段过滤 |
| 批量模式接入 `--no-excel` / `--no-feishu` | 让“本地保存”与“飞书保存”选择真实生效 |
| 修复批量汇总 Excel 数据累积 | 恢复 `search_videos_all.xlsx`、`influencers_all.xlsx`、`influencer_videos_all.xlsx` 输出 |
| 修复低配额判断调用 | 正确调用 `quota_tracker.remaining()` |

#### 2. `config.py`

| 更新事项 | 更新原因 |
|---------|---------|
| 新增 `min_views`、`min_engagement`、`search_filters` 配置字段 | 将 quickstart/CLI 参数贯穿到实际搜索与筛选流程 |
| 飞书账号变量读取支持默认变量兜底 | `FEISHU_APP_ID_Sam/work` 优先，`FEISHU_APP_ID` 作为兜底 |

#### 3. `youtube/search.py` 与 `workflow/phase_a_search.py`

| 更新事项 | 更新原因 |
|---------|---------|
| `search.list` 调用支持透传搜索过滤参数 | 支持时长、字幕、清晰度、发布时间范围等过滤 |

#### 4. `workflow/phase_b_filter.py` 与 `filter/video_filter.py`

| 更新事项 | 更新原因 |
|---------|---------|
| Phase B 默认筛选阈值统一引用 `video_filter` 默认值 | 避免 2% / 3% 默认值不一致 |
| 未达标原因与 `>` 规则保持一致 | 播放量等于阈值时正确显示为未超过阈值 |

#### 5. `.env.example`

| 更新事项 | 更新原因 |
|---------|---------|
| 新增 Sam/work 飞书账号变量样例 | 配合 quickstart 飞书账号选择 |
| 更新输出目录说明 | 默认输出为项目目录下 `output/` |

#### 6. `feishu/bitable.py` 与 `feishu/preflight.py`

| 更新事项 | 更新原因 |
|---------|---------|
| 新增测试写入后返回 record_id 的创建方法 | 支持预检后精准清理测试数据 |
| 新增批量删除记录方法 | 支持删除飞书预检测试记录 |
| 新增飞书预检流程 | 检查表结构、字段类型、写入权限和测试数据清理 |

---

### 功能验证

| 测试项 | 结果 |
|--------|------|
| Python 语法检查 | 通过 |
| `main.py --help` | 通过 |
| `search --help` / `batch --help` | 通过 |
| quickstart 单关键词取消路径 | 通过 |
| quickstart 批量关键词取消路径 | 通过 |
| 飞书预检: work 配置 | 通过 |
| 飞书预检: Sam 配置 | 未通过，接口返回 Forbidden，已能在任务开始前拦截 |
| `quota` 命令 | 通过 |

---

### 安全更新

| 更新事项 | 更新原因 |
|---------|---------|
| 文档中的飞书凭证示例改为占位符 | 避免真实 App Secret / app_token 在文档中扩散 |

## 2026-06-01 项目初始化与配置

### 新增文件

| 文件名 | 说明 |
|--------|------|
| `keywords.txt` | 关键词测试文件（5个关键词） |

---

### 修改文件

#### 1. `main.py`

| 更新事项 | 更新原因 |
|---------|---------|
| 新增 `--yes/-y` 参数，批量模式跳过交互确认 | 自动化执行时无法响应 input() 提示 |
| 新增 `--playlist/--search` 参数，指定视频获取方式 | 避免交互询问，适配自动化执行 |
| `run_batch()` 新增 `skip_confirm`、`use_playlist` 参数 | 支持 CLI 传入跳过确认和获取方式 |
| Emoji `✅⚠️❌` 替换为 `[OK]/[WARN]/[FAIL]` | Windows GBK 控制台无法显示 emoji |
| `run_batch()` 默认使用 playlistItems 方案 | playlistItems 消耗配额更少（2 units vs 101 units） |

#### 2. `youtube/quota.py`

| 更新事项 | 更新原因 |
|---------|---------|
| Emoji `✅⚠️❌` 替换为 `[OK]/[WARN]/[FAIL]` | Windows GBK 控制台无法显示 emoji |

#### 3. `config.py`

| 更新事项 | 更新原因 |
|---------|---------|
| `output_dir` 默认使用项目目录下 `output/`（自动解析为绝对路径） | 迁移到其他电脑时路径自动适配 |
| `feishu.app_token` 支持从环境变量 `FEISHU_APP_TOKEN` 读取 | 支持通过 .env 文件配置 |
| `validate()` 的 `require_feishu` 默认为 `False` | 允许跳过飞书验证，仅本地 Excel 输出 |

#### 4. `.env`

| 更新事项 | 更新原因 |
|---------|---------|
| 填写 `FEISHU_APP_TOKEN=你的飞书多维表格app_token` | 配置飞书多维表格 |

---

### 功能验证

| 测试项 | 结果 |
|--------|------|
| YouTube API 搜索 | ✅ 通过 |
| 频道去重 | ✅ 通过 |
| 网红详情抓取 | ✅ 通过 |
| Excel 本地输出 | ✅ 通过 |
| 批量处理 | ✅ 通过 |
| 飞书写入 | ✅ 通过 |
| 迁移路径适配 | ✅ 通过 |

---

### 迁移说明

复制项目到新电脑后，只需修改 `.env` 中的 `YOUTUBE_API_KEY`，其他配置（飞书凭证、输出目录）均可复用。

```env
YOUTUBE_API_KEY=你的API密钥
FEISHU_APP_ID=你的飞书App_ID
FEISHU_APP_SECRET=你的飞书App_Secret
FEISHU_APP_TOKEN=你的飞书多维表格app_token
```
