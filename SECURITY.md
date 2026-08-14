# Security Policy

## Reporting a vulnerability

请优先打开仓库的 **Security** 页面，进入 **Advisories**，选择 **Report a vulnerability**，通过 GitHub Private Vulnerability Reporting 私密提交漏洞，并只提供定位问题所需的最少信息。

如果仓库暂未启用 Private Vulnerability Reporting，请只创建一条不含漏洞细节和敏感数据的公开 Issue，请求仓库所有者提供私下联系渠道；在转入私密沟通前不要发布复现步骤、凭证、业务数据或可利用细节。

任何情况下，都不要在公开 Issue 中提交 API Key、飞书 Base/表/字段 ID、用户 OpenID、运单号、业务数据或可复现的真实凭证。

## Local secrets

- 所有凭证写入未跟踪的 `.env`，不要写入源码、测试、文档或命令历史。
- 飞书 OAuth 凭证由 `lark-cli` 和操作系统凭证存储管理。
- 旧 App 兼容模式在 Windows 默认只做进程内 token 缓存；不要启用不安全的明文磁盘缓存兼容开关。
- 提交前检查 staged diff，确认没有 `output/`、日志、导出表格和本地状态文件。
- 如凭证曾进入 Git 历史，应立即在对应服务轮换，而不是只删除当前文件。

## Supported versions

安全修复以默认分支最新版本为准。
