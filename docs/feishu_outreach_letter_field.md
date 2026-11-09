# 飞书网红详情表开发信字段

## 定位

“开发信”是网红详情表中的公式字段，用已确认的 KOL 名称和代表视频标题生成赠品合作草稿。它不会自动发送邮件。“邮件主题”是另一个公式字段，内容为 `Collab idea: Brand × ` 加当前行的 Channel Name，频道名称为空时留空。

正文模板：

```text
Hello {KOL Name},

This is Sam from xxx, a brand that makes xxx. We came across your video "{代表视频标题}" and found it very helpful.

We'd like to propose a gifted collaboration: we'll send you a free [product] to keep, and in return you'd share your honest thoughts in a dedicated video or a short integration in an upcoming one, whichever works best for your channel.

If this sounds good, reply with your shipping address and a rough idea of when you could post.

Best,
Sam
www.brand.com
```

花括号位置表示动态字段值，不输出花括号。`Brand`、`xxx`、`[product]` 和网址暂为模板占位内容。

## 生成门禁

仅在以下条件同时成立时生成草稿：

- Channel ID 非空；
- KOL Name 非空且不是“手动确认”占位值；
- 代表视频标题非空。

任一条件不满足时公式返回空值，避免把猜测名称或空标题带入外联内容。

## 字段所有权

- KOL Name、代表视频标题由网红详情刷新逻辑补齐，但已人工确认的名称不会被常规刷新覆盖。
- 开发信内容由公式计算，不由同步程序写入。
- 使用者应在实际发送前人工核对称呼、视频、品牌信息、合作条件和适用的外联规则。

## 模板维护

公式定义以 `feishu/full_schema.py` 为唯一代码真源。新表使用 `feishu-setup`；已有表通过 `python main.py feishu-upgrade --feishu-app-token BASE_TOKEN_OR_URL --dry-run` 预览本次迁移，再用 `--apply` 执行。升级器只自动替换已知旧公式，自定义公式列为冲突。详见[升级与统计说明](recent_video_metrics_upgrade.md)。

## 验收

- 信息完整时生成草稿。
- 名称待确认或代表视频缺失时保持空白。
- setup 重跑不重复创建开发信字段。
- 文档、测试和示例不包含真实邮箱、频道、视频或业务记录。
