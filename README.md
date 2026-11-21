# AstrBot 表情包生成插件

基于 [meme-generator-rs](https://github.com/MemeCrafters/meme-generator-rs) 的 AstrBot 表情包插件。发送模板关键词即可生成表情，支持 QQ 头像、引用消息、外部表情库和 GIF 工具箱。

## 功能

- 关键词触发生成表情包，支持 `@用户`、QQ 号、图片、文本和引用消息。
- `/随机表情` 随机发一张表情，并附上可复用的模板指令。
- 支持触发前缀、用户冷却、头像缓存和生成超时。
- 管理员可禁用单个模板或停用整个插件。
- 内置 300 个模板，可按需安装外部库（如 `meme-emoji`）。
- GIF 工具箱：变速、倒放、分解、网格裁剪、线稿、做旧、多图合成、视频转动图。
- 戳一戳机器人时随机回一张表情（需开启）。

交流群：`771954725` · 问题反馈：[GitHub Issues](https://github.com/SodaSizzle/astrbot_plugin_meme_generator/issues)

## 安装

```bash
cd AstrBot/data/plugins
git clone https://github.com/SodaSizzle/astrbot_plugin_meme_generator
pip install -r astrbot_plugin_meme_generator/requirements.txt
```

安装或更新后重载插件。`Pillow` 与 `aiohttp` 通常已随 AstrBot 安装；`imageio` 和 `imageio-ffmpeg` 仅「视频转动图」需要，缺失时只影响该指令。

## 指令

### 生成表情

```text
摸头
摸头 @某人
摸头 2352938756
举牌 你好世界
```

QQ 号必须作为独立参数填写，例如 `摸头 2352938756`；填写机器人自己的 QQ 号同样有效。配置了 `trigger_prefix` 时需加前缀，例如前缀为 `#`：

```text
#摸头 2352938756
```

### 全部指令

| 命令 | 别名 | 说明 |
|---|---|---|
| `/表情帮助` | `/meme帮助`、`/meme菜单` | 查看帮助菜单 |
| `/随机表情` | — | 随机生成一张表情，并附带模板指令 |
| `/表情列表 [文字\|图片] [页码]` | `/meme列表` | 查看全部模板；文字模式按页合并转发 |
| `/表情信息 <关键词>` | `/meme信息 <关键词>` | 查看模板名称、别名、参数和标签 |
| `/表情包库 [安装\|卸载\|恢复\|重载] [库]` | `/meme库` | 查看、安装、卸载或重载外部库 |
| `/表情资源` | `/meme资源`、`/表情资源状态` | 查看资源初始化状态 |
| `/表情状态` | `/meme状态` | 查看运行状态和模板统计 |
| `/单表情禁用 <模板名>` | `/单meme禁用 <模板名>` | 禁用指定模板（管理员） |
| `/单表情启用 <模板名>` | `/单meme启用 <模板名>` | 重新启用模板（管理员） |
| `/禁用列表` | — | 查看已禁用模板（管理员） |
| `/表情启用` | `/meme启用` | 启用整个插件（管理员） |
| `/表情禁用` | `/meme禁用` | 停用整个插件（管理员） |
| `/清空表情` | `/清空表情缓存`、`/清空缓存` | 清空头像缓存（管理员） |

`/表情列表` 的 `auto` 模式在模板不超过 200 个时发卡片图，超过时改发纯文本；`文字` 和 `图片` 可强制指定。

`/表情包库` 支持 `owner/repo`、完整 GitHub 链接或简写 `meme-emoji`。需要指定 rustc 变体时写作 `anyliew/meme-emoji@1.93.1`。安装、卸载、恢复后会自动重载 worker，不需要重启 AstrBot；改动库开关后执行 `/表情包库 重载` 生效。

### GIF 工具箱

先发送或引用图片/动图，再发指令；结果只回图片，不带说明文字。

| 功能 | 写法示例 | 说明 |
|---|---|---|
| 变速 | `变速` `变速2x` `变速30fps` | 倍速或指定帧率，超过 50fps 自动抽帧 |
| 加速 | `加速` `加速5` `5倍加速` | 默认 2 倍速 |
| 减速 | `减速3` `3倍减速` | 默认放慢到 1/2 |
| 倒放 | `倒放` | 帧序与逐帧时长一起反转 |
| 分解 | `分解` | 拆成逐帧图片，合并转发，最多 100 帧 |
| 裁剪 | `裁剪3x4` `裁剪 3x4 边距上20` | 按网格切图，最多 20×20 |
| 线稿 | `线稿` | 本地算法转素描线稿 |
| 做旧 | `做旧10` | 模拟反复转发的失真，数值越大越重 |
| 合成 | `合成` `合成0.2` | 多张表情按发送顺序接成动图 |
| 视频转动图 | `视频转gif 1s-5s fps 10 0.5` | 需要 `imageio` 与 ffmpeg |

- 显式指令：带唤醒前缀或 @机器人，例如 `/加速 5`、`/倒放`。
- 自然语言：裸发 `加速5`、`倒放` 即可，由 `gif_enable_semantic` 控制。
- 裸发短词但没带素材时静默跳过；显式指令缺素材时才提示用法。
- 每个功能只保留一个规范词，参数跟在后面，例如 `加速5`、`裁剪3x4`。匹配只看消息开头，句中偶然出现不会误触。
- `变速`、`倒放`、`分解` 需要动图；`做旧` 静态图和动图都支持。
- `裁剪 2x3 边距上20 边距10` 表示上边裁 20、其余三边各裁 10，再切成 2×3。

## 配置

管理面板按「基础设置 / 表情包库 / 戳一戳 / GIF 工具箱」分组。

| 分组 | 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|---|
| 基础 | `enable_plugin` | `bool` | `true` | 是否响应表情生成请求 |
| 基础 | `trigger_prefix` | `string` | `""` | 表情关键词专用前缀 |
| 基础 | `cooldown_seconds` | `int` | `3` | 单用户冷却时间，0–60 秒 |
| 基础 | `generation_timeout` | `int` | `30` | 单次生成超时，5–120 秒 |
| 基础 | `enable_avatar_cache` | `bool` | `true` | 是否缓存 QQ 头像 |
| 基础 | `cache_expire_hours` | `int` | `24` | 头像缓存有效期，1–168 小时 |
| 基础 | `list_style` | `string` | `auto` | `/表情列表` 输出方式：`auto`、`text`、`image` |
| 表情包库 | `enable_builtin_library` | `bool` | `true` | 是否加载内置 300 个模板 |
| 表情包库 | `enable_external_libraries` | `bool` | `true` | 是否加载 `libraries/` 下的外部库 |
| 表情包库 | `disabled_templates` | `list` | `[]` | 被禁用的模板列表 |
| 表情包库 | `github_proxy` | `string` | `""` | 下载加速前缀或代理地址，可写多个，逗号分隔 |
| 戳一戳 | `enable_poke_response` | `bool` | `true` | 戳机器人时随机回复一张表情 |
| 戳一戳 | `poke_response_any_target` | `bool` | `false` | 任意戳一戳都响应，包括群成员互相戳 |
| GIF 工具箱 | `gif_enable_toolbox` | `bool` | `true` | GIF 工具箱总开关 |
| GIF 工具箱 | `gif_enable_semantic` | `bool` | `true` | 是否允许裸发短词触发 |
| GIF 工具箱 | `gif_output_format` | `string` | `GIF` | 输出格式：`GIF`、`APNG`、`WEBP` |
| GIF 工具箱 | `gif_default_scale` | `float` | `0.3` | 视频转动图默认缩放比例，0.1–1.0 |
| GIF 工具箱 | `gif_default_fps` | `int` | `10` | 视频转动图默认帧率 |
| GIF 工具箱 | `gif_max_duration` | `float` | `10.0` | 视频最大截取时长（秒） |
| GIF 工具箱 | `gif_max_video_size_mb` | `float` | `50.0` | 允许处理的视频最大体积（MB） |
| GIF 工具箱 | `gif_max_colors` | `int` | `256` | GIF 最大颜色数，2–256 |

库开关改完执行 `/表情包库 重载`；其他配置改完重载插件生效。

## 外部表情库

内置库编译在 `meme_generator` 里，常驻且不可卸载。外部库是独立的原生动态库，由插件的 worker 子进程加载。

- 安装时按当前平台和架构从 Release 挑选候选，逐个下载并在子进程预检，跳过无法加载的版本；最新 Release 缺当前平台资产时自动回退旧 Release。
- 安装新变体会清理同仓库旧 DLL；素材按文件增量下载，已有且大小一致的文件跳过，失败可续传。
- 下载优先使用 AstrBot `http_proxy`，没有时自动读取系统代理；`github_proxy` 可填加速前缀或代理地址（如 `http://127.0.0.1:7890`）。
- worker 启动前会写 `libraries/.loading` 标记，加载成功才清除；加载阶段崩溃时只隔离本次记录的文件，主进程以内置库继续运行。
- 不联网时也可以手动放：动态库放进数据目录的 `libraries/`，素材合并进 `resources/`，再执行 `/表情包库 重载`。
- 动态库需与主程序的 rustc / `meme_generator_core` 版本匹配；加载失败时会跳过，可用 `库@版本` 手动指定。
- 外部库关键词可能与内置模板重名（如 `睡觉`、`举牌`），按需加入 `disabled_templates`。
- 部分第三方库含成人或血腥内容，公开群聊不建议启用。

## 资源与排错

资源保存在 AstrBot 分配的插件数据目录：

```text
AstrBot/data/plugin_data/astrbot_plugin_meme_generator/
├── resources/
│   ├── fonts/
│   └── images/
├── libraries/
└── cache/meme_avatars/
```

自动下载失败时，可从 [Releases](https://github.com/SodaSizzle/astrbot_plugin_meme_generator/releases) 下载资源包，把完整的 `resources/` 解压到上述目录。

Linux / Docker 出现方块或缺字时安装中文字体：

```bash
apt update
apt install -y fontconfig fonts-noto-cjk
fc-cache -fv
```

参考资料：[加载其他表情](https://github.com/MemeCrafters/meme-generator-rs/wiki/%E5%8A%A0%E8%BD%BD%E5%85%B6%E4%BB%96%E8%A1%A8%E6%83%85) · [配置文件说明](https://github.com/MemeCrafters/meme-generator-rs/wiki/%E9%85%8D%E7%BD%AE%E6%96%87%E4%BB%B6)
