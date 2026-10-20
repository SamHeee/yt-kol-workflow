# 工作流可靠性修复与升级说明

本次修复基于 `7cdf577`，覆盖采集、断点续传、飞书同步、内容刷新、物流及提醒。
代码修复和离线测试不会自动补写已有 Base，也不会修改历史工作簿或发送提醒。

## 修复内容

| 模块 | 修复后的行为 |
| --- | --- |
| 飞书 CLI 列表 | 明确成功且 `items:null,total:0` 时作为空列表；未知结构仍报错。表、字段、视图完整读取 offset 分页。 |
| Workflow 权限 | 仅检查实际使用的 `bitable:app` 和 Workflow `create/read/update` 权限；移除使已授权账号被误判的多余 `base:workflow:write`。 |
| Workflow 回读 | 接受服务端对两个已知触发字段引用补充的、表名完全一致的 `extra_info`；错误表名、未知属性和其他引用仍严格报错。 |
| 飞书原生列表 | 表、字段、记录读取任意页失败都中止，不能用空数据或部分索引继续新增。校验分页游标及总数。 |
| 初始化与预检 | 补齐单选项时保留合法默认值；逐条确认测试记录删除结果。三个运营命令接受 `auto`，仍要求解析得到 CLI 身份。 |
| B/D 输出 | 采集完成和各目标写入完成分别记录；失败续跑补做未提交的飞书或 Excel 输出。 |
| D 采集 | 单频道结果与完成标记原子保存；续跑合并累计频道、视频。临时错误保持待重试。 |
| 去重 | 已发现、未完成和已完成分开。`--no-detail`、抓取失败、部分写入失败均不会永久阻止后续详情抓取。 |
| 恢复 | 保存筛选配置及原 Excel 目录；续跑恢复相同条件和完整四表。用户中断立即停止后续批量关键词。 |
| 人工字段 | 网红刷新前回读人工名称和来源；重放采集输出时保留已有人工名称、邮箱、开发状态等字段。 |
| 合作内容 | 在有效记录内选同视频的主记录，终态或合作无效记录不会阻断有效记录。 |
| 物流 | 成功缓存与失败的五分钟退避分开；批次失败保留其他批次成功结果，换运单立即重新核验。 |
| 提醒 | 计划、发送前回读和租约恢复都核对当前运单指纹；同包裹签收时间校正不重复发送已完成的送达提醒。 |
| 数据 | 搜索时间带 UTC 时区；邮箱状态由最终邮箱推导；断更评估按有效观察和最新发布日期更新。 |
| CLI | 预算按实际逐频道请求次数计算；CSV 缺少可选末尾列使用默认值；失败返回非零退出码。 |

## 已有 Base 的结构差异

样品跟踪表末尾新增三列，其他既有人工字段保留：

| 新增字段 | 类型 | 用途 |
| --- | --- | --- |
| 物流签收时间 | 日期时间 | 属于当前已同步运单的真实签收事件时间，供自动提醒使用。 |
| 签收日期核验状态 | 文本 | 区分物流填写、人工调整、历史日期待确认及物流时间缺失。 |
| 上次查询运单键 | 文本 | 失败退避关联当前查询运单，避免旧运单的错误阻止新运单查询。 |

完整七表 schema 现为 152 个字段；已有 Base 实际需要增加哪些字段，应以其
`feishu-setup --dry-run` 返回的差异为准。没有在线读取时不能推断已有表的实际状态。

推荐先指定同一个已有 Base 生成计划，避免误选其他项目的默认目标：

```powershell
.\.venv\Scripts\python.exe main.py feishu-setup --feishu-app-token "<已有 Base URL>" --dry-run --no-enable-automation
```

核对后可用相同目标执行初始化补列。`--no-enable-automation` 可保留为独立的工作流启用步骤。
物流模块要求新字段存在，因此升级顺序为：补列 → 刷新物流 → 提醒 dry-run → 核对提醒。
已有签收记录缺少新的物流签收时间时可以重新查询回填；接口未提供真实签收事件时间时保持待确认。
原“签收日期”不会因换运单被清空。更换承运商时，应同步更新或清空“物流商编码”；
仅修改物流商显示名称不会改变查询身份。

## 检查点、去重缓存与历史数据

- 新检查点保存 `checkpoint_version=2`、各频道载荷、输出提交状态和原筛选参数。
- 旧检查点无法证明阶段 B/D 是否完整，会在实际续跑时重新计算 B/C/D。
  保留原任务待处理频道用于重新核验，避免被远端的部分结果误判为已完成。
  `checkpoint_migration` 记录需要重放的阶段；加载文件本身不写回磁盘。
- 旧检查点没有保存的参数无法还原，第一次升级续跑应重复传入原来的筛选参数。
  新检查点之后会使用保存的筛选条件。
- 旧版 seen 文件没有详情完成凭据，其条目保留但不再直接阻止详情采集。
  在实际执行时核验并补齐，只有成功输出的频道才转为完成。
- 原子替换 JSON 防止写入中断破坏已有有效状态。可重放输出仍按原业务唯一键更新。
- 历史错误时间、旧 Excel 的过期评估、旧邮箱状态不会被本次代码修改直接重写。
  处理这些数据前，应另外生成按记录 ID/唯一键对照的差异清单。

## 自动化退出码

| 退出码 | 含义 |
| --- | --- |
| 0 | 搜索或批量任务全部成功 |
| 1 | 失败、异常或配置错误 |
| 2 | 部分完成，需要查看错误并续跑 |
| 3 | 配额中断或预算不足 |
| 130 | 用户中断 |

即使只有搜索任务审计行或 Excel 补齐失败，也不会将执行结果报告为全部成功。
失败目的地本身不可写时，以本地检查点和命令退出状态为准；后续续跑会重试审计输出。

## 本地验证

本次在项目自身 `.venv`（Windows、Python 3.11.9、pytest 8.4.2）完成完整回归：
**573 项通过、2 项跳过**。两项跳过仅验证 POSIX 文件权限语义，由 POSIX 环境执行。
`main.py --help`、`main.py quota` 冒烟检查及 `git diff --check` 同时通过。

安装开发依赖并运行测试：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:YOUTUBE_API_KEY = ""
$env:TRACK17_API_KEY = ""
$env:FEISHU_AUTH_MODE = "cli"
$env:FEISHU_AUTO_SETUP = "false"
$env:FEISHU_AUTO_INSTALL = "false"
$env:FEISHU_OPEN_BROWSER = "false"
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
```

故障恢复测试使用临时目录与模拟 API，覆盖普通错误、配额中断、进程中断、
飞书部分写入、Excel 文件占用、历史缓存迁移和四张 Excel 的累计数据完整性。
飞书在线验收结果见 [2026-10-20 实测报告](live_feishu_validation_20261020.md)。
17TRACK/YouTube 联网行为未在本次飞书验收中测试。

## 2026-10-20 飞书实测发现的补充修复

通过已授权 CLI Profile 在独立验收 Base 执行完整初始化，额外复现并修复以下问题：

1. `FULL_WORKSPACE_SCOPES` 多要求了 `base:workflow:write`。官方 CLI
   [v1.0.88 启用实现](https://github.com/larksuite/cli/blob/v1.0.88/shortcuts/base/workflow_enable.go)
   和 [停用实现](https://github.com/larksuite/cli/blob/v1.0.88/shortcuts/base/workflow_disable.go)
   都使用 `base:workflow:update`；
   [v1.0.70 启用实现](https://github.com/larksuite/cli/blob/v1.0.70/shortcuts/base/workflow_enable.go)
   也相同。已移除多余权限，所需权限缺失时仍必须失败。
2. Workflow 创建成功后，实际回读为联系邮箱和 Channel ID 的字段引用补充
   `extra_info: {"table_name": "网红详情表"}`。这部分信息与已有引用重复，
   旧比较器却将其视为额外业务配置。现仅对这两个精确引用和精确表名做归一化；
   不接受附加未知键、错误源表、非 ref 类型或其他引用的注解。
3. 原生 Bitable 记录列表在空表时返回 `{"has_more": false, "total": 0}`，
   可完全省略 `items`。之前仅容忍 `items: null`，导致新表第一次 upsert 在建立
   去重索引时就失败。现仅在成功响应、明确整数总数为零且明确无后页时接受省略的
   列表；缺少总数、非零总数、分页矛盾或错误响应仍不能降级成空列表。

脱敏 Workflow 回读证据与正反例验证保存在
`output/live_validation/workflow_extra_info_metadata_redacted.json` 和
`output/live_validation/workflow_extra_info_validation_redacted.json`。
验收驱动和创建记录的清理凭据同样保存在 `output/live_validation/`，该目录按仓库规则忽略提交。
