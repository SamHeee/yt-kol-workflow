# 最近视频统计与邮件字段升级

本版本的代码安装目录为 `E:\test`。新建 Base 使用 `feishu-setup`；已有 Base 使用下面的定向升级命令。

## 字段变化

| 表 | 字段 | 格式 |
|---|---|---|
| 视频数据表 | Views、Likes、Comments | 数字，千分位、0 位小数 |
| 网红详情表 | 订阅数、频道总播放量、视频总数、代表视频播放量 | 数字，千分位、0 位小数 |
| 网红详情表 | apify邮箱 | 普通文本，手动或外部导入；本版本不调用 Apify |
| 网红详情表 | 邮件主题 | 公式：`Collab idea: Brand × ` 加 Channel Name |
| 网红详情表 | 开发信 | 新的 gifted collaboration 模板，引用 KOL Name 和代表视频标题 |
| 网红详情表 | 最近视频平均播放量、最近视频中位播放量 | 数字，千分位、2 位小数；底层保留计算精度 |

`apify邮箱` 已有非空内容受 Excel 同步保护。两封邮件相关字段只生成草稿，不发送邮件。`Brand`、`xxx`、`[product]`、`www.brand.com` 为模板占位内容。

## 升级已有 Base

在 PowerShell 中运行，将 `BASE_TOKEN_OR_URL` 替换为明确的目标 Base token 或 `/base/` URL：

```powershell
cd E:\test
.\.venv\Scripts\python.exe main.py feishu-upgrade --feishu-app-token BASE_TOKEN_OR_URL --dry-run
```

默认只预览，并在 `output/schema_upgrade_*.json` 保存计划。确认内容后应用：

```powershell
.\.venv\Scripts\python.exe main.py feishu-upgrade --feishu-app-token BASE_TOKEN_OR_URL --apply
```

升级只涉及表中上述字段；保留字段 ID、默认值和描述，更新前复核配置，执行后读回核验。不创建 Base 或表，不启用工作流，不修改业务记录。旧开发信只有与仓库旧模板一致才自动升级；自定义公式、类型冲突、同名重复字段会阻止写入。报告保存升级前后定义及已完成步骤，失败后重新预览可以继续处理剩余字段。线上配置可能异步生效；若读回尚未一致，先检查报告，再重新预览。

普通 `feishu-setup` 保持严格校验。已有表先完成升级，再运行 setup。历史视频平均/中位播放量两列保持原样；本次新增的是“最近视频”两列。

## 新采集与已有频道刷新

正常 `search` / `batch` 的阶段 D 自动计算两项统计，写入飞书和 Excel，并随频道检查点保存。统计来自当前频道最近最多 10 个可读取视频，包含 Shorts。默认 uploads 采集器会在遇到不可读取条目时继续分页补足；分页达到上限、预算耗尽或请求失败按失败处理，不能把部分采集伪装成完整样本。旧的 search 采集方式仍兼容，其返回不足时按实际样本统计。

已有频道重新采集最近视频：

```powershell
.\.venv\Scripts\python.exe main.py refresh-influencers --feishu-app-token BASE_TOKEN_OR_URL --fields recent-views --recent-source youtube --limit 3 --dry-run
.\.venv\Scripts\python.exe main.py refresh-influencers --feishu-app-token BASE_TOKEN_OR_URL --fields recent-views --recent-source youtube --limit 3
```

核验后可移除 `--limit 3`；也可重复指定 `--channel-id` 定向处理。需要 YouTube API Key。默认的 `--fields all` 包含最近视频统计，并与活跃度/频道判断共用一次最近视频采集。刷新只处理已有 Base；不会自动新建 Base。

`--dry-run` 会读取飞书和调用 YouTube 生成计划，消耗 API 配额，但不写业务记录；不能同时使用 `--ensure-schema`。真实执行先按 Video ID 保存、核验视频明细，再写两项统计。已有重复视频行全部更新为本次观测值，保留人工字幕等非采集字段。重复频道共享同一次采集。

只利用现有「网红视频表」回填：

```powershell
.\.venv\Scripts\python.exe main.py refresh-influencers --feishu-app-token BASE_TOKEN_OR_URL --fields recent-views --recent-source table --dry-run
.\.venv\Scripts\python.exe main.py refresh-influencers --feishu-app-token BASE_TOKEN_OR_URL --fields recent-views --recent-source table
```

这个模式不需要 YouTube API Key，也不新增视频明细；先按 Channel ID 匹配，按 Video ID 去重，再按发布时间选最近 10 个。重复视频的存量播放量取最大值。缺少有效发布时间会报告该频道无法确定最近样本，保留旧指标。此模式代表表内已有数据快照，不保证已包含 YouTube 最新上传。

## 计算、空值与恢复

- 平均值为有效播放量之和除以有效样本数；中位数为排序后的中间值，偶数样本取中间两个值的平均数。
- 不足 10 个按实际有效样本数计算；真实 0 计入，缺失、负数和无效数值排除。
- 成功读取但无有效样本：两项指标为空；请求、保存或校验失败：保留此前指标，并在报告中列出失败。
- 每次刷新在 `output/refresh_influencers_*.json` 保存采集时间、样本数、Video ID、计算值和错误。部分失败返回非零退出码，已成功处理的数据保留；可用 `--channel-id` 重跑失败频道。
- 有并发人工修改或记录删除时跳过对应更新，查看报告中的 `concurrent_skips`、`missing_before_write`。不应同时运行多个任务更新同一组频道。
- 旧检查点的数据版本低于 3 时提示使用刷新命令补齐。已完成的旧阶段 D 不会自动重新请求，也不会把缺失统计当成 0。

## Excel 与导入

Excel 增加 apify邮箱和两项统计列，数字仍为数字单元格。统计采集时间与成功状态保存在平均播放量单元格的程序批注中，不增加业务列。合并时优先采用较新的完整成功统计，平均值和中位数成对替换；新的成功空样本可以清空旧值。

旧工作簿缺少新列，或只有一项统计时，不清空或部分覆盖已有统计。删除程序批注后，空白统计按“未知”处理，不再据此清空远端值；两项数值完整时仍可正常导入。

## 本地验收

```powershell
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
.\.venv\Scripts\python.exe main.py feishu-upgrade --help
.\.venv\Scripts\python.exe main.py refresh-influencers --help
```

上述自动化测试使用模拟接口，不访问实际 YouTube 或飞书。

2026-10-30 已通过飞书 CLI 在独立测试 Base 完成实测：3 个真实频道、30 条视频及合成边界样本，55 项线上检查全部通过。覆盖旧字段升级、公式实际计算、表内统计回填及重复升级。实测发现列表接口的数字字符串与批量读取接口的数值类型不同，已修复读取与比较兼容性并补充回归测试；完整本地测试为 619 passed、2 skipped。

本机详细报告为 `output/live-test-20261030/测试报告.md`，原始记录、字段快照及 CLI 回执保存在同目录。线上实测使用表内历史快照，不包含重新采集 YouTube 或调用 Apify。
