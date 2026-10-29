# Codex SOTA 维护地图

本文件保存架构和操作边界，便于后续维护者直接核对代码。真实账号、配置、日志和会话不属于仓库。

## 从入口到上游

```text
codex-sota.exe → manager_entrypoint → CodexSotaManager
                                   ├─ sota_registry → profile/providers.json + DPAPI
                                   ├─ Switch-CodexSota.ps1 → 官方 Codex App
                                   └─ claude_desktop → Claude 第三方配置库

Codex / Claude → 127.0.0.1 上的 codex_sota_router
                ├─ 模型命名空间和映射 → 指定供应商
                ├─ 原生 Responses / Messages 或 Chat 协议桥接
                ├─ sota_network → 按主机代理、重定向和凭据边界
                └─ sota_compaction → 本地加密摘要、后续历史还原

codex-sota-runtime.exe → 同一份打包运行环境 → 所有后台 Python 辅助脚本
```

## 文件职责

| 模块 | 责任与关键约束 |
| --- | --- |
| `CodexSotaManager.py` | 供应商、密钥、模型、协议、探测、延迟、备份恢复、用量和客户端启动界面。后台任务通过事件队列回主线程；切换工作区和关闭窗口需使过时回调失效。 |
| `sota_registry.py` | Schema、模型目录、DPAPI、配置读写锁、保存回滚、探测、路由健康。新建文件不能覆盖用户已有配置或登录；新机本地 token 不得替代上游密钥。 |
| `codex_sota_router.py` | HTTP 入口、模型路由、协议适配、流传输、压缩和计数、健康与用量。默认故障切换关闭；发布名和上游模型 ID 分开处理。 |
| `sota_network.py` | 系统代理和直连策略；本地控制端口始终直连；不允许重定向携带凭据到其他 origin。 |
| `sota_compaction.py` | 按 profile 保存 DPAPI 加密的本地压缩历史；摘要和工具调用必须可往返。 |
| `claude_desktop.py` | 维护自己的 Claude 配置库条目；共享 appliedId 使用带令牌的借还协议，不能覆盖其他管理器的所有权。 |
| `codex_launch_controller.py` / `codex_app_lifecycle.py` | 请求 ID、进程身份、启动租约、可取消等待，避免历史同步与新启动同时写入。 |
| `Switch-Codex*.ps1` | 配置预检、启动/重启、环境变量清理和结构化状态；精确识别进程，不能误关同名程序。 |
| `sync_codex_histories*.py` | 只同步历史文件与元数据；不复制配置、账号、密钥、日志和插件凭据。快照、校验、失败回滚和未完成恢复资料必须保留。 |
| `repair_*.py` | 离线修复。先计划和证据校验，变更前留备份；缺失可选 profile 时跳过。 |
| `archive_verified_duplicates.py` | 对明确选择且完整历史一致的冲突副本做事务归档，不移动或删除聊天内容。 |
| `Build-Release.ps1` / `codex-sota.spec` | 显式文件白名单、共享运行环境、源码指纹、包内容校验、离线运行探针、ZIP 和 Inno 安装包。 |

## 状态与迁移边界

默认 Codex 状态位于 `%USERPROFILE%\.codex-sota`，Claude 位于 `.claude-sota`，可通过环境变量分别覆盖。
两者注册表、密钥、端口、锁、缓存及恢复目录相互独立。`auth.json` 中的客户端登录和供应商自己的
DPAPI 密钥是不同的对象；不得为了修一家上游去覆盖其他账号或客户端登录。

模型发布名用于客户端选型，上游 ID 用于推理请求。既有 profile 的 `config.toml/model_provider`
是历史兼容标识；同步时从目的 profile 读取，不能用发行版的默认名字覆盖。DPAPI 密钥限当前 Windows
用户和机器使用，不能随安装包发布，也不能声称复制到新电脑后仍能解密。

源码目录中不保存私人运行状态。安装、升级和卸载不管理用户 profile。对正在使用的本机版本做改动前，
维护者必须先确认用户确实要求部署到该版本；构建、测试和发版可以只使用独立源码及临时目录。

状态展示使用 `claude_desktop.library_status_readonly()`，不能调用会迁移配置或借还共享槽位的写入路径。
后台状态结果带工作区和任务代次，过期回调直接丢弃。等待 Codex 窗口时按 PID 与创建身份复核已有进程，
只有身份确实不再匹配才重新进入原有启动检查，不能因窗口尚未出现重复启动。

上下文压缩和 token 计数不是同一操作：桥接压缩明确请求一次不超过 4096 输出 token、禁用工具的摘要生成，
成功后才封装可回放的本地加密 compaction 项；计数仍可在本地估算，不应触发生成。压缩响应缺少正常终止标记、
达到输出上限、被过滤、为空或包含工具调用时，不替换历史，也不重试可能已计费的请求。

## 验证与发布

在 Windows 运行 `python -B -m unittest discover -s CodexSotaManager -p 'test_*.py'`。
产品回归覆盖干净 profile、真实 Tk 编辑操作、失败回滚、代理边界、协议适配与打包运行环境。
`Build-Release.ps1` 在构建前跑回归，再检验打包内容和隔离运行时。源码在构建期间变化会使构建失败。

发布前运行 `CodexHistorySync/check_publish_scrub.py` 并查看 Git 差异。发布物只包含应用文件、公开示例、
许可证和校验值。完整功能需要用户自己的上游服务和官方客户端，离线测试不能证明第三方供应商一直可用。

Codex 配置格式参考：[官方配置参考](https://developers.openai.com/codex/config-reference/)。
