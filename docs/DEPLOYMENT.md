# 可移植部署指南

本项目是 Windows 本地工具，不会把账号、供应商密钥或聊天记录打包进仓库。部署目录可以是任意路径；
用户数据应该放在部署目录之外，并通过环境变量指向它们。

## 前置条件

- Windows 10/11，PowerShell 5.1 或 PowerShell 7。
- CPython 3.11 或更新版本。Microsoft Store 的 `WindowsApps` Python 占位符不受支持。
- Codex App；非 Store 安装设置 `CODEX_APP_EXE` 为现有 `ChatGPT.exe` 或 `Codex.exe` 的完整绝对路径。
- 如果使用 Cockpit profile，安装 Cockpit Tools，或设置 `CODEX_COCKPIT_TOOLS_EXE` 和
  `CODEX_COCKPIT_API_PORT`。

## 首次安装

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\setup.ps1 -SkipBuildTools
. .\.runtime\codex-sota.env.ps1
& $env:CODEX_SOTA_PYTHON -B .\CodexSotaManager\CodexSotaManager.py
```

`setup.ps1` 检查带 tkinter 的 CPython 3.11+，生成私有环境文件；不加 `-SkipBuildTools` 时才会在
被 Git 忽略的 `.runtime` 中安装构建工具。运行时脚本只依赖 Python 标准库。需要 TLS 证书辅助 API 时，再在
你自己的环境中安装 `requirements-optional.txt`。

初始化时在管理器中选择对应 workspace，添加自己的供应商和密钥。`providers.example.json` 仅是
Claude/Messages 示例，不能原样复制成 Codex/Responses 配置。不要复制别人的 `auth.json`、`*.dpapi`、
数据库、session JSONL 或 `config.toml`。
DPAPI 密钥绑定 Windows 用户和机器，换电脑应重新录入。

## 路径与覆盖

环境变量优先于默认目录，请使用完整绝对路径；只作用于当前进程或显式设置的用户环境：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CODEX_SOTA_CORE_ROOT` | 仓库的 `CodexHistorySync` | 共享 Python/PowerShell 核心目录 |
| `CODEX_SOTA_CODEX_ROOT` | `%USERPROFILE%\\.codex-sota` | Codex workspace |
| `CODEX_SOTA_CLAUDE_ROOT` | `%USERPROFILE%\\.claude-sota` | Claude workspace |
| `CODEX_SOTA_COCKPIT_ROOT` | `%USERPROFILE%\\.codex-personal` | Cockpit profile |
| `CODEX_SOTA_PLUS_ROOT` | `%USERPROFILE%\\.codex-plus` | Plus profile |
| `CODEX_SOTA_PYTHON` / `CODEX_PYTHON` | 自动探测 | Python 解释器完整路径 |
| `CODEX_APP_EXE` | 自动探测 | Codex App 可执行文件 |
| `CODEX_COCKPIT_TOOLS_EXE` | LocalAppData 默认路径 | Cockpit Tools 可执行文件 |
| `CODEX_COCKPIT_API_PORT` | `56319` | Cockpit Tools 本地 API 端口 |

启动路由器时也可以使用 `-SotaRootOverride`、`-RouterPortOverride`、`-RouterScriptOverride` 和
`-PythonExecutableOverride`，适合 CI 或一次性诊断，不会持久化设置。

## 验证和升级

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File .\CodexSotaManager\Run-ThreeRoundValidation.ps1 -SkipArtifact
```

升级时先备份用户 profile，再拉取 GitHub 更新；不要用仓库内容覆盖任何 workspace 根目录。重新运行
`setup.ps1` 只会更新本地构建环境，不会迁移或删除用户数据。打包发布使用
`CodexSotaManager\Build-Staged.ps1`，它会先验证再生成 staging 目录。
自定义 `setup.ps1 -InstallRoot` 时，给构建脚本传 `-PythonExecutable` 指向该环境的
`.venv-build\Scripts\python.exe`；默认 `.runtime` 和旧版管理器旁的 `.venv-build` 会自动识别。

源码回归不依赖真实账号或聊天记录。CI 验证 Windows/CPython 3.11；未在每一种 Codex App 版本、
企业策略或新电脑上做过实际登录与 GUI 启动认证，因此不承诺所有环境零配置可用。升级前应保留自己的
配置备份，数据库 schema 不受支持时不要强行同步。

## 卸载

关闭管理器、路由器和 Codex App 后，删除仓库目录即可。用户 workspace 默认在 `%USERPROFILE%` 下，
不会由卸载脚本自动删除；如需删除，请先人工确认其中没有要保留的历史或认证数据。

## 安全边界

路由器默认只监听 `127.0.0.1`，没有入站鉴权，不要把端口暴露到局域网。仓库的 `.gitignore` 会拦截
`providers.json`、`auth.json`、`*.dpapi`、数据库、session JSONL、日志和运行状态；提交前仍应检查
`git status` 和 staged diff，不要 `git add -A` 把用户数据带入版本库。
