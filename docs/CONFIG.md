# providers.json 配置说明

字段与校验规则全部来自 `CodexHistorySync/sota_registry.py` 的 `validate_registry` /
`validate_provider` / `validate_model`。这三个函数是唯一的真相来源：管理器保存前会先跑一遍，
路由器热加载时也会再跑一遍，任何一条不过就整份拒绝。

## 文件在哪，密钥在哪

每个 workspace 一套独立的根目录（`Workspace` 数据类，`sota_registry.py`）：

| 路径 | 内容 |
| --- | --- |
| `<root>\providers.json` | 本文档描述的这份配置 |
| `<root>\auth.json` | 发往上游用的 Codex 认证文件（`auth_type = "codex_auth"` 时用） |
| `<root>\<secret_file>` | DPAPI 加密后的 API key，一家一个文件 |
| `<root>\sota-multi-vendor-model-catalog.json` | 生成给 Codex App 读的模型目录（Claude workspace 不生成） |

Claude workspace 的 root 是 `%USERPROFILE%\.claude-sota`。**API key 不在 `providers.json` 里**，
配置里只有文件名（`secret_file`）和熵（`entropy`）；真正的密文是旁边那个 `*.dpapi`，用 Windows
DPAPI 按「当前用户 + 当前机器」加密，拷到别处解不开。所以就算这份 JSON 泄了也拿不到 key——但它仍然
含有各家的 base_url 和供应商 id，所以照样不该进版本库。

## 顶层字段

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `version` | int | 否 | 格式版本，当前 `REGISTRY_VERSION = 1` |
| `updated_at` | string | 否 | 最后一次保存的时间戳，管理器写 |
| `providers` | array | 是 | 供应商列表；空数组是合法的「新建工作区」状态 |

`providers` 为空是刻意允许的：新工作区必须能先建出来，此时路由器会拒绝启动并由管理器给出提示。

## provider 对象

| 字段 | 类型 | 默认 | 校验 / 说明 |
| --- | --- | --- | --- |
| `id` | string | 无，必填 | `^[a-z][a-z0-9_]{1,39}$`（2–40 位小写字母、数字、下划线）。会被强制小写。也是派生 `prefix` / `secret_file` / `entropy` 的来源 |
| `name` | string | 无，必填 | 1–80 字符的显示名 |
| `base_url` | string | 无，必填 | 经 `normalize_base_url` 规范化 |
| `protocols` | array | `["responses"]` | 取值只能是 `"responses"` / `"messages"`。传字符串会被包成单元素数组；结果按 `PROTOCOLS` 的固定顺序重排。默认值是为了兼容早于 Claude 支持之前的老条目——那些都是 responses 网关 |
| `prefix` | string | 见下 | 模型前缀。默认供应商必须为空；其他家留空则由 `derive_model_prefix` 派生 |
| `is_default` | bool | `false` | 全表恰好一家为 `true` |
| `enabled` | bool | `true` | 关掉的供应商不参与路由，也不要求密钥存在 |
| `allow_failover` | bool | `false` | 允许这家在失败时被跳过换下一家。默认关，关掉时上游的状态码和原文直接透传 |
| `protected` | bool | `false` | 管理器里标记为不可误删 |
| `workspace` | string | `"codex"` | 必须是已知 workspace 名（`codex` / `claude`） |
| `auth_type` | string | `"dpapi"` | `"dpapi"`（读 `secret_file`）或 `"codex_auth"`（借 Codex App 自己的登录）。其它值直接报错 |
| `auth_header` | string | `"Authorization"` | 必须匹配 HTTP 头名字符集，且不含控制字符 |
| `auth_prefix` | string | `"Bearer "` | 拼在密钥前面，注意默认值**带一个尾随空格**。允许为空字符串 |
| `secret_file` | string | `<id>-api-key.dpapi` | 只在 `auth_type="dpapi"` 时用。文件名另有 `SECRET_FILE_PATTERN` 和 Windows 保留设备名的检查 |
| `entropy` | string | `CodexSota.Provider.<id>.v1` | DPAPI 的附加熵。改了就解不开旧密文，需要重新存一次 key |
| `models_path` | string | `/models` | 相对 `base_url` 的路径 |
| `responses_path` | string | `/responses` | 同上 |
| `messages_path` | string | `/v1/messages` | 同上 |
| `timeout_seconds` | int | `120` | 会被夹到 `[5, 900]`；非数字回落到 120 |
| `extra_headers` | object | `{}` | 额外请求头，键值都必须是字符串，键要过头名校验，值不能含控制字符 |
| `models` | array | `[]` | 见下节 |

`auth_type = "codex_auth"` 的一个特殊之处：它**不会**派生前缀。这家借的是 Codex App 自己的登录，
派生一个 id 前缀会把 App 已经看到的模型名改掉。

## model 对象

| 字段 | 类型 | 默认 | 校验 / 说明 |
| --- | --- | --- | --- |
| `id` | string | 无，必填 | 上游真正认的模型名。≤160 字符，不能含 `\r` `\n` `\t`。**发给上游的永远是这个值** |
| `enabled` | bool | `false` | 注意默认是关的：新加的模型要显式打开。只有开着的模型才进可选 slug 和唯一性检查 |
| `publish_as` | string | `""` | 覆盖对外可选的 slug，见下节。需匹配 `PUBLISH_AS_PATTERN`，≤160 字符 |
| `display_name` | string | `""` | 显示名 |
| `description` | string | `""` | 备注 |
| `last_test_status` | string | `"untested"` | 只接受 `ready` / `failed` / `untested`，其它值被改写成 `untested` |
| `fast_tier_status` | string | `"unknown"` | 只接受 `supported` / `unsupported` / `unknown` |
| `fast_tier_effect` | string | `"untested"` | 只接受 `faster` / `none` / `untested` |
| `fast_tier_forced` | bool | `false` | 非布尔值一律当 `false` |

## 前缀的两种形状

`MODEL_PREFIX_PATTERN` 只认两种，都是自带分隔符的，保证 `prefix + model id` 拼起来无歧义：

```
vendor--              原始形式，凡是说 responses 的供应商都用它
vendor.anthropic.     Bedrock 风格，messages-only 的供应商用它
```

为什么要有点号那种：Claude Desktop 判断一个模型有没有「思考档」滑杆，是把模型名规范化之后去查一张
硬编码表。它的规范化器会剥掉开头的 `<label>.anthropic.`（Bedrock 模型 id 的形状），但完全不认识
`vendor--`。所以只有点号形式能让 `tango.anthropic.claude-opus-5` 落到表里 `claude-opus-5` 那一项上。

点号形式里的 label 比 `id` 更严：Claude 自己的正则是 `^(?:[a-z][a-z0-9-]*\.)?anthropic\.`，
不接受下划线。所以 `derive_model_prefix` 会把 id 里的 `_` 换成 `-`——`alt_relay` 派生出来是
`alt-relay.anthropic.`。

## publish_as：可选 slug 与上游 id 分离

`published_slug()` 的规则是：有 `publish_as` 就用它，否则 `prefix + id`。**上游收到的永远是
`model["id"]`。**

存在的理由只有一个：前缀救不了「把档位烧进模型名」的供应商。Claude 的规范化器会剥掉
`<label>.anthropic.`、`[...]` 后缀、`-vN`、`@date` 和 `-date`，但**不会**剥 `-thinking`。所以
`claude-opus-5-thinking` 不管怎么加前缀都进不了能力表，滑杆和 1M 变体都拿不到。把它
`publish_as` 成 `claude-opus-5`（或 `<prefix>claude-opus-5`）就能落到表里，而上游仍然收到
`-thinking` 那个原名。

两条限制：

- **说 `responses` 的供应商禁止用 `publish_as`。** Codex 侧的 slug 由 `config.toml` 的
  `model = ...` 钉住，不需要这套障眼法，混进来只会让两边对不上。
- `publish_as` 恰好等于 `prefix + id` 时会被自动清空——两种写法结果一样，留着只会让摘要不稳定、
  文件更难读。

## 四条会在实践中咬人的不变式

1. **恰好一个默认供应商。** `providers` 非空时 `is_default` 为真的必须正好一家，否则整份拒绝。
2. **默认供应商的 `prefix` 必须为空。** 校验器对默认家不派生前缀，因为新工作区里第一个存进去的
   供应商必然是默认家，派生前缀会让它永远校验不过。
3. **所有启用模型的可选 slug 全局唯一。** 冲突时报错会把两个claim方都点名（并标出哪个是
   `publish_as`），因为有了 `publish_as` 之后最常见的冲突是「覆盖名撞上刚被打开的兄弟条目」，
   光说「重复」会让人找不到该改哪边。
4. **启用中的 `dpapi` 供应商必须有密钥文件。** 关掉的供应商即使 key 文件已被删也仍可编辑/删除。

第 2 和第 3 条会联动：把默认供应商换成另一家时，新默认家要清空前缀，而**老默认家那条
`publish_as`（如果是裸名）也必须一起改**，否则两家同时claim同一个裸 slug，整份文件被拒、改动等于
没生效。

## 改完之后怎么生效

路由器不监听文件事件。它算一个「配置签名」（`providers.json` 加各密钥文件的 mtime/size），每次请求
之前比一次，变了就重建路由表——**改完不用重启**。

确认是否真的生效看 `/healthz`：

```
curl.exe http://127.0.0.1:<port>/healthz
```

`registry_hash` 变了、并且 `config_error` 是空字符串，才算新配置被接受了。如果新文件校验不过，
路由器会**保留旧表继续服务**并把原因写进 `config_error`——这时候 `registry_hash` 不变，看起来
「没反应」，其实是被拒了。

一个例外：改 `codex_sota_router.py` 的**代码**不会改 `ROUTER_VERSION` 也不会改 `registry_hash`，
所以健康检查照样匹配，ensure-running 的启动脚本会认为不用动。详见
[DEVELOPING.md](DEVELOPING.md) 的「改动路由器代码时的陷阱」。

## 模板

`providers.example.json` 在仓库根目录，可以直接拷成 `%USERPROFILE%\.claude-sota\providers.json`
再改。它有意做成两家：一家默认、空前缀；一家带 `alt-relay.anthropic.` 前缀并演示 `publish_as`。
拷过去之后还要用管理器存一次 API key，`*.dpapi` 文件不能手写。
