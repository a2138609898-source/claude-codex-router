# 开发与维护

## 开发环境

- **Windows**。整个项目依赖 DPAPI、`msvcrt` 文件锁和 Win32 进程 API，不考虑跨平台。
- **CPython 3.11**。`codex-sota.spec` 就是按它打的，打包用的 `.venv-build` 是 3.11.0。
- **PATH 上的 `python` 别信。** Windows 上它经常是 Microsoft Store 的占位符：能启动、什么都不跑、
  还返回 0。曾经因为这个，验证脚本「通过」了却根本没执行过任何测试。所以
  `Run-ThreeRoundValidation.ps1` 会自己去找解释器：先看 `CODEX_PYTHON` 环境变量，再看 Codex 自带
  运行时，再扫 `%LOCALAPPDATA%\Programs\Python\Python3*` 按版本号从新到旧，实在找不到才回落到 PATH。
  手工跑命令时请直接写解释器的完整路径。
- **运行时没有第三方依赖。** `.venv-build` 只装 PyInstaller，仅供打包。唯一的非标准库 import 是
  `cryptography`，只在 `sota_registry.py` 的 TLS 证书函数里延迟导入且有 `ImportError` 兜底。

## 怎么跑测试

`unittest` 套件（构建闸认这个）：

```
cd CodexSotaManager
<python311>\python.exe -m unittest -v test_codex_sota.py test_codex_sota_regressions.py test_sync_build_regressions.py
```

单个自检脚本：

```
cd CodexHistorySync
<python311>\python.exe check_router_messages.py
```

三轮验证（完整闸）：

```
powershell -NoProfile -ExecutionPolicy Bypass -File CodexSotaManager\Run-ThreeRoundValidation.ps1 -SkipArtifact
```

三轮各干什么：

1. **静态检查。** 对两个目录里所有 `.py` 跑 `py_compile`，并用 `[scriptblock]::Create` 把所有
   `.ps1` 解析一遍。语法错误在这里就断掉，不用等到测试。
2. **完整行为回归。** 三个 `unittest` 文件全跑。
3. **打包产物完整性 + 干净重跑。** 设好 `CODEX_SOTA_ARTIFACT_ROOT` 指向一个已打包的
   `codex-sota` 目录，再跑一遍与产物相关的测试子集——那些测试用这个环境变量找 exe，校验产物文件齐全、
   而且不比源码旧（见下）。

`-SkipArtifact` 会清掉 `CODEX_SOTA_ARTIFACT_ROOT`，第三轮就只是干净重跑一遍子集。没有已打包产物时
（比如刚 clone 下来）用这个开关。`-ArtifactRoot <路径>` 可以显式指定要检查哪份产物。

## 测试约定

树里并存两套，是刻意的，不要合并：

**`unittest`（`CodexSotaManager/test_*.py`）** —— 真正的回归网，`Build-Staged.ps1` 就卡在它上面。
新增的行为约束写这里。大量使用 `mock.patch.object` 隔离外部世界。

**独立自检脚本（`CodexHistorySync/check_*.py`）** —— 每个是一个可以直接 `python` 跑的程序，自带中文
分节标题（`=== n) ... ===`）、一个 `results: list[bool]`、末尾打印 `合计 N/M 项通过`，
`SystemExit(0 if passed == total else 1)`。它们的价值在于**排查时能一眼看出是哪个环节坏了**，输出是
给人读的而不是给 CI 读的。跑一个子系统的端到端行为、想看中间过程时用这套。

**端口一律用 `fixture_ports.py` 拿，不要写死。** 这不是风格洁癖。Windows 上 `SO_REUSEADDR`
（`ThreadingHTTPServer` 默认开）允许在已有监听者之上再 bind 一次，而连接落到哪一个是未定义的。
以前三个脚本各自钉死同样的 17896–17898，于是残留的影子路由器或并行跑的第二个脚本**不会报任何错**：
假上游根本收不到请求，命中列表是空的，所有场景以完全无关的理由失败，等对方进程退出又「自己好了」。
`reserve_port()` 给一个当前空闲的端口（交给子进程用，天生有竞态，调用方还得确认应答的是自己的孩子）；
`serve_on_free_port()` 自己 bind 到 0 号端口再报告落在哪，没有窗口期。

## 打包与发布

**`Build-Staged.ps1`** —— 造一份验证过的候选，成功了才放进 `dist-staging`：

1. 先跑 `Run-ThreeRoundValidation.ps1 -SkipArtifact` 做预检（`-SkipPreflight` 可跳）。
2. PyInstaller 打到一个带 GUID 的临时 `dist-candidate-*` / `build-candidate-*`。
3. 用 `-ArtifactRoot <候选>` 再跑一遍三轮验证——这次第三轮是针对**刚打出来的这份**。
4. 都过了才把旧的 `dist-staging` 改名成 `dist-staging.previous-<时间戳>`，候选移进 `dist-staging`。
5. `finally` 里清掉临时目录，无论成败。

**`Apply-StagedBuild.ps1`** —— 把 staging 换成 live，三步移动 + 回滚：

1. 先把 `dist-staging\codex-sota` 移成 `dist\codex-sota.incoming-<id>`，这样一份坏的 staging
   目录根本碰不到 live。
2. 再把 live 移成 `dist\codex-sota.old-<id>` 退役。
3. 最后把 incoming 移成 live。

任何一步失败都会尽力把 live 和 staged 都放回去；如果连回滚都失败，它会明确抛出「回滚不完整」而不是
假装成功。提交之后的收尾（删空的 staging 父目录、写日志）是 best-effort，绝不触发回滚——日志被锁不该
让一次已经成功的发布失败。`-Wait` 会在遇到临时文件锁时每 5 秒重试，直到 `-TimeoutMinutes`。

为什么不原地覆盖：管理器自己可能正在运行，原地覆盖会撞上被占用的 exe。改名换目录是原子的、可回退的，
而且退役目录留在 `dist\` 里就是现成的回滚点。

**产物新鲜度。** `test_packaged_artifact_is_complete_and_not_older_than_sources` 会比较 exe 的 mtime
和几个关键源文件的 mtime，产物比源码旧就失败。这条专门防「改完源码忘了重新打包，却以为在测新代码」。
注意 PyInstaller 把模块压进 PYZ，所以**直接在 exe 里搜字符串搜不到**，别用那个当验证手段。

## 改动路由器代码时的陷阱

`Start-CodexSotaRouter.ps1` 是 **ensure-running**，不是 restart。它打 `/healthz`，把 `status`、
`version`、`registry_hash`、上游集合四项和期望值比一遍，全对就返回 `started: false` 并**不动那个
进程**。保存配置和各个启动按钮都依赖这个行为——重启一个本来就正确的路由器会把正在传输的请求打断。

问题在于：改 `codex_sota_router.py` 的代码，`ROUTER_VERSION` 不变，`registry_hash` 也不变（它算的是
配置，不是代码）。**于是健康检查照样匹配，「重启」变成了什么都没做，旧代码继续服务，而调用方收到
成功。** 这个坑非常安静。

不要试图通过改 `ROUTER_VERSION` 来绕：`router_health_matches_workspace` 把版本不一致当成
「没在跑」，一改就会让另一个 workspace 里健康的路由器显示成未运行。

正确做法是显式要求换进程：`restart_router(workspace, force=True)`，它会先 `stop_router` 再拉起，
并在结果里回 `stopped_process_ids` 说明换掉了哪些旧进程。管理器的「重启路由器」按钮走的就是这条；
启动 Claude Desktop 的路径也会强制重启一次，但刻意选在「已经关掉 Claude、还没拉起新的」那个空窗，
因为强制重启会打断在途请求。反过来，不关 Claude 的那些路径（比如单独重写档案）**不能**强制，会打断
用户正在进行的对话。

## 代码风格

照着周围的写就行，几条能看出来的约定：

- **注释解释「为什么」，而且经常直接点名它防的是哪个 bug。** 比如 `fixture_ports.py` 的模块
  docstring 讲的是 `SO_REUSEADDR` 那次两个下午。不要写「// 设置端口」这种复述代码的注释。
- 函数写 docstring，尤其是行为有反直觉之处的（`restart_router` 的 `force`、`published_slug` 的
  两个名字、`Workspace` 为什么不合并成一张表）。
- 全面用类型标注，`from __future__ import annotations`。
- 运行时不引第三方库。
- 中文注释和英文注释并存，看所在文件的习惯。用户可见的字符串（GUI、自检脚本输出）是中文。

## 不要提交什么

这个仓库是从一份本地工作树导出的，导出时会做清洗。往里加东西时请自觉遵守同样的规则：

- **任何真实供应商信息**：base_url、域名、供应商 id、商业中转的品牌名。测试夹具用中性名字和
  `example.com` / `.invalid` 域名。
- **任何凭据**：API key、`*.dpapi` 文件、DPAPI 熵常量、token。
- **写死的个人路径。** 不要 `sys.path.insert(0, r"C:\Users\<你>\...")`——用
  `os.path.dirname(os.path.abspath(__file__))`。`test_build_and_launcher_sources_are_portable`
  会检查启动器和 spec 里没有写死的家目录（它用 `Path.home()` 求值，所以对每个人都成立）。
- **运行时状态和日志**：`providers.json`、`active-profile.json`、`last-result.json`、`*.log`、
  `sync.lock`。`.gitignore` 里都拦了。
- **GUI 截图**，除非确认里面没有真实供应商清单、地址和用量数字。
