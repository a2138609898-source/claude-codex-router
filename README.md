# claude-codex-router（codex-sota）

一个 Windows 本地推理路由器：在 `127.0.0.1` 上开一个环回网关，前面接 Claude Desktop 和
Codex CLI/App，后面接若干家 OpenAI / Anthropic 兼容的中转供应商，再配一个 Tkinter 管理器负责
增删供应商、存密钥、切档、看用量。

这是个人自用工具，不是产品。它只在 Windows 上跑，用到 DPAPI、`msvcrt` 文件锁和 Win32 进程 API，
没打算跨平台。放上来是因为里面有几个坑（Claude Desktop 的能力表怎么被模型名影响、staged 构建
怎么换、`/healthz` 为什么看不见代码改动）值得留个记录。

## 它解决什么问题

一家中转不够用：额度会掉、通道会挂、同一个模型在不同家的名字还不一样。但 Claude Desktop 和
Codex 各自只能填**一个**网关地址。所以在本地立一个收口：

- 对上：始终是同一个 `http://127.0.0.1:<port>`，配一次就不用再动。
- 对下：`providers.json` 里挂多家，每家自己的 base_url、密钥、模型清单和路径。
- 中间：按请求里的模型名决定发给谁，并把「客户端看到的名字」翻译成「上游真正认的名字」。

## 架构

```
  Claude Desktop                     Codex CLI / Codex App
  (configLibrary 第三方档)            (config.toml + model_catalog_json)
        │                                     │
        │  /v1/messages                       │  /responses
        ▼                                     ▼
  ┌───────────────────────┐           ┌───────────────────────┐
  │ 127.0.0.1:<claude>    │           │ 127.0.0.1:<codex>     │
  │ codex_sota_router.py  │           │ codex_sota_router.py  │
  │ workspace = claude    │           │ workspace = codex     │
  └───────────┬───────────┘           └───────────┬───────────┘
              │  ~/.claude-sota/providers.json    │  自己的 root/providers.json
              │  ~/.claude-sota/*.dpapi           │  自己的 secrets
              ▼                                   ▼
   ┌──────────┴──────────┬──────────┐   ┌─────────┴─────────┐
   ▼                     ▼          ▼   ▼                   ▼
 relay A (messages)  relay B    relay C  relay D (responses)  ...

  ┌──────────────────────────────────────────────────────────┐
  │ CodexSotaManager.py — 管理器 GUI                          │
  │ 改 providers.json / 存 DPAPI 密钥 / 起停路由器 / 写 Claude 档 │
  └──────────────────────────────────────────────────────────┘
```

两个 workspace 是两套完全独立的根目录，各自的 `providers.json`、`auth.json`、密钥目录、锁文件和
端口。这不是「一个文件加一列区分」，理由写在 `Workspace` 的 docstring 里：Codex 和 Claude 是两个
产品、两批账号、两套模型名，Claude 侧的任何一次编辑——包括写失败后的回滚——都不该碰到 Codex 的状态。

## 核心概念

这几个概念不看代码猜不出来，先说清楚。

**prefix（前缀）与默认供应商。** 多家可能都提供 `claude-opus-5`，所以每家非默认供应商都带一个
前缀，客户端看到的是 `<prefix><model_id>`。**默认供应商的 prefix 必须为空**（`validate_registry`
强制），它的模型以裸名出现。合法前缀有两种形状：`vendor--` 和 `vendor.anthropic.`，后者不允许
下划线，所以 id 里的 `_` 会被 `derive_model_prefix` 转成 `-`。

**published slug ≠ upstream model id。** 客户端选择用的 slug 由 `published_slug()` 决定：有
`publish_as` 就用它，否则是 `prefix + id`；而**发给上游的永远是 `model["id"]`**。这一层分开是为了
救 Claude Desktop 的能力表——它按规范化后的模型名去查思考档和 1M 上下文，`claude-opus-5-thinking`
规范化之后还是它自己、不在表里，滑杆就出不来。改个 `publish_as` 让它规范化到 `claude-opus-5`，
控件回来了，上游收到的仍是原来那个 `-thinking` 名字。同理，说 `responses` 协议的供应商禁止用
`publish_as`（Codex 侧不需要这套障眼法）。

**model_routes 与 failover。** 校验通过的注册表会摊平成一张 `published_slug -> (vendor,
upstream_model)` 的表。请求进来先查表，查不到直接 400（不联网）。查到之后按 `failover_candidates`
拿一串候选，逐个试；`allow_failover` 默认关，关掉时候选只有一个，上游的错误状态和原文直接透传。

**热加载。** 路由器不监听文件事件，而是算一个「配置签名」（`providers.json` 加各密钥文件的
mtime/size），每次请求前比一下，变了就重建路由表。改完 `providers.json` 不用重启，`/healthz` 的
`registry_hash` 会跟着变；如果新文件校验不过，路由器**保留旧表继续服务**并把原因写进
`config_error`——所以 `config_error` 是空的才算改动真的生效了。

**ROUTER_VERSION 和 `/healthz` 的三元比较。** `/healthz` 回 `status` + `version` +
`registry_hash` + `upstreams`。启动脚本 `Start-CodexSotaRouter.ps1` 是**幂等的 ensure-running**：
四项全对就认为已经在跑，直接返回不动进程。这里有个坑值得单独记：改
`codex_sota_router.py` 的代码既不动 `ROUTER_VERSION` 也不动 `registry_hash`，所以健康检查照样匹配，
「重启」会变成什么都没做。真要换进程必须走 `-Stop`，或者调用方传 `force=True`。

## 目录结构

两个目录必须保持同级并且名字不能改：`Run-ThreeRoundValidation.ps1`、`codex-sota.spec` 和
`CodexSotaManager.py` 都按「同级目录，名字就叫 CodexHistorySync」去找对方。

### CodexHistorySync/ — 核心

| 文件 | 作用 |
| --- | --- |
| `codex_sota_router.py` | 路由器本体。HTTP 服务、模型分派、失败切换、SSE 透传、用量统计、`/healthz`、`/v1/models`、`count_tokens` 估算 |
| `sota_registry.py` | `providers.json` 的 schema 与校验、DPAPI 存取密钥、`Workspace` 定义、启停路由器、失败链计算 |
| `claude_desktop.py` | 往 Claude Desktop 的 `configLibrary` 写第三方推理档，包括 `supports1m` / 思考档 / `labelOverride`，以及和 cc-switch 共享 `appliedId` 槽的借还逻辑 |
| `Start-CodexSotaRouter.ps1` | 起停路由器（ensure-running 语义，带 `-Stop`）。`Start-ClaudeSotaRouter.ps1` 是 Claude workspace 的薄壳 |
| `Run-CodexHistorySync.ps1` | Codex 历史同步的入口，顺带清理会泄给子进程的 API key 环境变量 |
| `Switch-CodexSota.ps1` / `Switch-CodexProfile.ps1` | 切 Codex 档的启动器 |
| `sync_codex_histories*.py` | Codex 会话历史的两向 / 三向同步 |
| `release_claude_slot_after_exit.py` | 守 Claude Desktop 退出，把 `appliedId` 槽还给原主 |
| `check_*.py` / `check_*.ps1` | 独立自检脚本，见「测试」 |
| `fixture_ports.py` | 给测试发空闲端口 |

### CodexSotaManager/ — 管理器

| 文件 | 作用 |
| --- | --- |
| `CodexSotaManager.py` | Tkinter GUI 全部在这一个文件里 |
| `test_codex_sota.py`、`test_codex_sota_regressions.py`、`test_sync_build_regressions.py` | `unittest` 回归套件 |
| `Run-ThreeRoundValidation.ps1` | 三轮验证，构建的准入闸 |
| `Build-Staged.ps1` / `Apply-StagedBuild.ps1` | 先打到 staging，验证过再原子换上去 |
| `codex-sota.spec` | PyInstaller 配置 |

## 安装与运行

需要 Windows 和 **CPython 3.11**（`.venv-build` 就是 3.11.0，`codex-sota.spec` 按它打包）。
注意 PATH 上的 `python` 在 Windows 上很可能是 Microsoft Store 的占位符，什么都不会跑；用真实
解释器的完整路径。

**运行时没有第三方依赖。** 全树唯一的非标准库 import 是 `cryptography`，只在
`sota_registry.py` 里那两个 TLS 证书函数内部延迟导入，并且 `ImportError` 有兜底。不用可选的 TLS
监听端口就不需要装任何东西。

从源码起管理器：

```
<python311>\python.exe CodexSotaManager\CodexSotaManager.py
```

单独起路由器（一般由管理器或启动脚本调用）：

```
powershell -NoProfile -ExecutionPolicy Bypass -File CodexHistorySync\Start-CodexSotaRouter.ps1
```

## 配置

`providers.example.json` 是模板，字段逐个解释在 [docs/CONFIG.md](docs/CONFIG.md)。

**真实配置不在仓库里。** 它住在 `%USERPROFILE%\.claude-sota\providers.json`（Claude workspace，
Codex workspace 有自己的根目录），API key 根本不写在这个文件里——它们是旁边的 `*.dpapi` 文件，
用 Windows DPAPI 按用户+机器加密，换个账号或换台机器都解不开。仓库的 `.gitignore` 里也拦了
`providers.json` 和 `*.dpapi`，算第二道防线。

## 测试

树里并存两套约定，各有用处。

**`unittest` 套件（CodexSotaManager/）**，构建闸认的就是这个：

```
cd CodexSotaManager
<python311>\python.exe -m unittest test_codex_sota.py test_codex_sota_regressions.py test_sync_build_regressions.py
```

**独立自检脚本（CodexHistorySync/`check_*.py`）**，每个直接 `python <脚本>` 跑，自己打中文分节标题、
末尾输出 `合计 N/M 项通过`，全过退 0 否则退 1。适合手工排查单个子系统。

**`Run-ThreeRoundValidation.ps1`** 是三轮合一的准入闸：第一轮 `py_compile` 全树 `.py` 并用
`[scriptblock]::Create` 解析全树 `.ps1`；第二轮跑完整 `unittest`；第三轮带
`CODEX_SOTA_ARTIFACT_ROOT` 再跑一遍打包产物相关的子集。加 `-SkipArtifact` 可以跳过对已打包
产物的依赖。细节见 [docs/DEVELOPING.md](docs/DEVELOPING.md)。

## 打包

PyInstaller 走 `codex-sota.spec`（onedir、`console=False`、图标内嵌）。发布不是原地覆盖：
`Build-Staged.ps1` 先打到 `dist-staging` 并跑完三轮验证，`Apply-StagedBuild.ps1` 再把旧的
`dist\codex-sota` 改名退役、把 staging 换上去。这样做是因为管理器自己可能正在运行——原地覆盖会
撞上被占用的 exe，而改名换目录是可回退的。

## 安全说明

- 路由器默认只监听 `127.0.0.1`（`--host` 的默认值），不对外。
- **环回监听没有鉴权。** 它不检查任何进来的 `Authorization` 头（只读 `Transfer-Encoding` 和
  `Content-Length`），也就是说这台机器上任何进程只要能连到那个端口，就能用你配置的全部供应商和额度。
  `--auth` 参数指的是**发往上游**用的 Codex 认证文件，不是入口的门禁。单机自用可以接受，但别把这个
  端口暴露到局域网，也别在多用户机器上跑。
- 仓库里刻意没有：真实 `providers.json`、任何密钥或 `.dpapi` 文件、运行时状态、构建日志、构建产物、
  以及会露出真实供应商清单的 GUI 截图。测试夹具里的供应商 id 和域名都是中性占位（`example.com`、
  `.invalid`）。

## 许可证

MIT，见 [LICENSE](LICENSE)。
