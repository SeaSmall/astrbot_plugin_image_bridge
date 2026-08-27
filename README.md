# astrbot_plugin_image_bridge（图片问答桥接）

一个 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 插件：**用户发图片 → 插件调用免费 OCR 接口识别图片文字 → 用户再发文字提问时，把识别内容连同问题一起交给 AI 回答**。

核心规则（图片门控问答）：

- 用户**只发图片（不带文字）**：插件静默识别图片内容，不发送任何提示，**AI 不会回答**；
- 用户**随后发送文字问题**：插件把之前识别出的图片内容注入本次 LLM 请求，AI 结合图片内容回答；
- 用户**图片 + 文字一起发**：立即识别并随问题一起交给 AI；
- **表情/表情包**：QQ 官方把表情解析为 `[表情]`/`[表情:赞]` 文本 + 图片附件。插件提取表情语义（如"用户发送了一个表情：[赞]"）并入 OCR 结果——表情包图片（多为 **GIF 动图**）会自动**切第一帧转为静态图**再 OCR，识别出表情包上写的文字，让 AI 真正"看懂"用户发了什么表情包；`emoji_wait_pending` 开关可控制表情是否也参与"等待提问"。

适用于不支持多模态（图片直接进 LLM）的平台（如 QQ 个人号 aiocqhttp、Telegram 等），也适用于希望先识别图片文字再交给 LLM 的场景。

## 工作原理

```
用户发图片 ──► 插件调用 OCR.space 免费接口识别文字并静默挂起
                  │
                  ├─ 只发图片？──► 拦截本条消息（AI 不回答），后台等待用户提问
                  │
                  └─ 有文字？──► 文字消息继续进入 LLM 流程
                                    │
用户发文字问题 ──► on_llm_request 钩子把挂起的图片识别内容注入本次请求 ──► AI 一并回答
```

- 图片识别使用 [OCR.space](https://ocr.space/) 免费 API（默认语言 `chs` 简体中文，可配置）。
- 挂起的识别内容按「会话 + 发送者」隔离（群聊中 A 的图片不会被 B 的问题消费），默认有效期 30 分钟（可配置）。
- 识别内容通过 `on_llm_request` 钩子以临时内容块（`mark_as_temp`）注入，不写入会话历史。

## 在 AstrBot 中安装

> 插件名：`astrbot_plugin_image_bridge`，仓库地址：
> `https://github.com/SeaSmall/astrbot_plugin_image_bridge.git`

### 方式一：WebUI 插件管理安装（推荐）

1. 打开 AstrBot WebUI，进入 **插件管理**；
2. 点击 **安装插件** → 选择 **通过 Git 地址安装**（或"安装本地插件"）；
3. 输入仓库地址并确认：

   ```text
   https://github.com/SeaSmall/astrbot_plugin_image_bridge.git
   ```

4. 安装完成后点击 **启用**；
5. 若提示依赖缺失，进入插件目录执行 `pip install -r requirements.txt` 后重启 AstrBot，或在 WebUI 插件管理中选择"重载插件"。

### 方式二：手动放置插件目录（pip / 源码方式安装的 AstrBot）

```bash
# 进入 AstrBot 的插件目录（源码安装默认为 AstrBot/data/plugins）
cd /path/to/AstrBot/data/plugins

# 克隆插件
git clone https://github.com/SeaSmall/astrbot_plugin_image_bridge.git

# 安装依赖
pip install -r astrbot_plugin_image_bridge/requirements.txt

# 回到 WebUI 插件管理，点击插件的 "..." → "重载插件"；或重启 AstrBot
```

### 方式三：Docker 部署的 AstrBot

```bash
# 将插件目录复制进容器（或使用 docker cp）
docker cp astrbot_plugin_image_bridge <容器名>:/AstrBot/data/plugins/astrbot_plugin_image_bridge

# 重启容器
docker restart <容器名>
```

或在 `docker-compose.yml` 中挂载插件目录：

```yaml
services:
  astrbot:
    image: soulter/astrbot:latest
    volumes:
      - ./astrbot_plugin_image_bridge:/AstrBot/data/plugins/astrbot_plugin_image_bridge
```

## 配置说明

在 WebUI 插件管理 → 本插件 → 配置 中可修改以下项：

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `ocr_api_url` | string | `https://api.ocr.space/parse/image` | OCR 接口地址 |
| `ocr_api_key` | string | `helloworld` | OCR.space API Key。默认值为其公开测试 key（免费、有限流），建议到 [ocr.space/ocrapi](https://ocr.space/ocrapi) 免费注册获取专属 key 后填入 |
| `ocr_language` | string | `chs` | 识别语言：`chs`=简体中文、`eng`=英文、`chinese_tra`=繁体中文 等 |
| `ocr_timeout` | int | `60` | OCR 请求超时（秒） |
| `pending_ttl` | int | `1800` | 图片识别内容有效期（秒），超时后需重新发送图片 |
| `emoji_wait_pending` | bool | `true` | 发送表情后是否也进入「等待提问」模式（挂起表情语义，用户提问时注入 LLM）。关闭时表情不挂起、不拦截，消息正常回复 |
| `prompt_template` | text | 见默认值 | 注入给 AI 的提示词模板，`{image_content}` 会被替换为识别文字 |

## 使用说明

1. 用户发送一张图片（机器人不发送任何提示，静默等待）；
2. 用户接着发送文字问题（默认 30 分钟内有效）；
3. 机器人将识别内容与问题一并交给 AI，AI 结合图片内容回答。

发送 `/picreset` 可清除本会话挂起的图片识别内容（例如想换一张图重问）。

## 更新日志

- **v1.0.4**：**表情包也能让 AI 看懂**——
  - GIF 动图/表情包自动**切第一帧转为 JPEG** 后进 OCR（Pillow），识别出表情包上写的文字（如"哈哈哈""无语"），AI 真正理解用户发了什么表情；
  - 表情图不再被跳过，全部进入 OCR；`[表情:赞]` 文本语义与 OCR 文字合并注入；
  - 新增依赖 `Pillow`（gif 切帧用）。
- **v1.0.3**：支持平台表情（QQ 官方 `[表情]/[表情:赞]` + 图片附件）——提取表情语义挂起/注入，新增 `emoji_wait_pending` 开关。
- **v1.0.2**：收到图片后不再发送提示消息，改为静默挂起识别内容、后台等待用户提问；移除 `wait_hint`、`show_ocr_preview` 配置项。
- **v1.0.1**：修复在 AstrBot v4.16~v4.27.x 上安装失败的问题（`EventMessageType` 改为从 `filter` 模块引用，兼容官方插件开发指南写法）。
- **v1.0.0**：初版发布。

## 常见问题

- **识别不准确 / 识别为空**：图片需文字清晰；可在配置中更换 `ocr_language`；免费额度有限流，频繁使用建议注册专属 key。
- **只发图片后没有任何回复**：这是插件设计——收到图片后静默等待，必须先发图片、再发文字问题，AI 才会回答。
- **群聊中误用他人图片**：挂起内容按发送者隔离，A 的图片不会被 B 的问题消费。
- **发了表情（尤其是自定义表情包 GIF）后机器人不回复**：表情也会进入「等待提问」模式（默认开启）——发表情后需再发文字问题，AI 才会结合表情包 OCR 文字回答；若希望表情立即被正常回复，将 `emoji_wait_pending` 关闭即可。
- **GIF 动图识别不了文字**：已自动处理——插件会把 GIF 切第一帧转成 JPEG 再 OCR，识别表情包/动图上写的文字（需安装 Pillow，`requirements.txt` 已包含）。

## 开源许可

本项目基于 [MIT License](LICENSE) 开源。图片识别能力来自 [OCR.space](https://ocr.space/) 免费 API，其服务条款与免费额度请以其官网为准。

