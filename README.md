# Home Assistant AI Tools

![项目图标](custom_components/ha_ai_tools/brand/icon.png)

通过现有 DeepSeek 和 Home Assistant 语音设备创建提醒、自动化，查询天气，并保留连续对话与永久记忆。界面为中文，业务时间统一为北京时间 `Asia/Shanghai`。

## 功能

- 提醒：一次、每天、工作日、每周；重启恢复，可查看、取消，并修改待执行提醒的播报设备。已结束记录可单条删除或批量清理。
- 播报：每条提醒独立选择一台或多台设备，到时仅向该条提醒选择的设备并行发送。默认设备只用于预选，修改默认设置不会改变已有提醒。
- 自动化：创建后可在 HA 自动化页面编辑、停用。采用 HA 原生验证、文件锁和写入器。未加载成功的操作保留原 ID，可重试加载。
- 天气：设置“家”“公司”等名称与现有天气实体映射，查询实况、每日和小时预报。
- 上下文：同设备最近15分钟的新唤醒延续对话，保留近期12轮，旧内容压缩为摘要。
- 记忆：自动提取明确稳定的偏好，支持“记住”“更正”“忘记”，页面可管理。HA 本地保存，按需提供给云端 DeepSeek。过滤明显的密码、密钥和临时状态，请勿口述凭据。

## 安装与使用

要求 Home Assistant **2026.10.1 或更高版本**，并预先配置 Extended OpenAI Conversation。当前客户端接口已在 Extended OpenAI Conversation **2.0.2** 与 DeepSeek 上验证；其他版本和提供方需自行确认兼容性。支持可播报的 `assist_satellite` 实体，以及由音箱集成提供的文本播报 `text` 实体。

### HACS

1. 打开 HACS，右上角菜单选择“自定义存储库”。
2. 添加 `https://github.com/q848899176/home-assistant-ai-tools`，类别选择 **Integration（集成）**。
3. 搜索 **Home Assistant AI Tools** 并下载，重启 Home Assistant。

[在 HACS 中打开本仓库](https://my.home-assistant.io/redirect/hacs_repository/?owner=q848899176&repository=home-assistant-ai-tools&category=integration)

HACS 自定义仓库安装不依赖默认商店收录；仓库可以安装并不代表已获 HACS 官方审核收录。

### 手动安装

从 [Releases](https://github.com/q848899176/home-assistant-ai-tools/releases) 下载 `ha_ai_tools.zip`，将 ZIP 内全部文件解压至 `/config/custom_components/ha_ai_tools/`，确保 `manifest.json` 位于该目录内，然后重启。

也可下载源码，将其中的 `custom_components/ha_ai_tools` 整个目录复制到 `/config/custom_components/`。

### 配置

1. 在“设置 → 设备与服务 → 添加集成”搜索 **Home Assistant AI Tools**。
2. 选择现有 Extended OpenAI Conversation 作为模型来源，无需重新输入密钥。
3. 在“设置 → 语音助手”打开正在使用的语音流水线，将对话代理选择为 **AI Tools**，保留已有 STT、TTS 设置。本集成本身不提供 STT 或 TTS。
4. 打开侧栏 **AI 工具**，选择默认播报设备并保存，按需测试播报。
5. 在“天气地点”设置名称与现有天气实体的映射；在“永久记忆”查看、修改或删除偏好。

集成依赖当前语音流水线提供的识别文字；如识别错误，需要单独调整 STT。天气地点映射不会自动创建天气服务或按经纬度查询外部天气。

创建提醒时，“本条提醒的播报设备”会直接列出支持播报的设备，可为喝水、吃药等不同提醒分别勾选。已有待执行提醒点击“修改播报设备”即可调整。取消全部勾选会提示至少选择一台，不会偷偷退回默认设备。

已完成、已取消、失败或错过的记录显示“删除记录”；“清理历史”会批量删除这些已结束记录。两种操作均会先确认，删除后不可恢复。待执行和正在播报的提醒受后端保护，不会被历史清理删除。

示例：

- “明天上午九点提醒我交电费。”
- “明天九点提醒我喝水，只在 Nabu 播报。”
- “把喝水提醒改为只在 Nabu 播报。”
- “每天晚上十一点关闭客厅灯。”
- “家里明天会下雨吗？”
- “记住我喜欢简短回答。” → “以后详细一点。”
- “把它关掉。”（承接同设备近期明确提及的设备）

## 边界

- 自动化目前支持明确实体的结构化配置；模板、任意事件动作及标签/楼层等间接控制目标会被拒绝。设备必须已向 Assist 公开。
- “同时播报”采用并行触发，不保证不同厂商音箱音频完全同步。界面显示的是 HA 服务调用结果，实际声音需通过设备试听验收。
- 24小时内过期的一次提醒补发一次；重启时过期重复提醒跳到下一未来时刻。重启中断的播报不自动重播，避免重复。
- 匿名语音使用家庭共享记忆；有可信用户身份时使用个人记忆。不根据音箱位置推断说话人。
- 初次加载会导入已有 `input_text.jarvis_long_memory` 的非敏感内容一次，保留原辅助实体。旧短期辅助实体不转换为永久事实。
- HA Store 文件：`.storage/ha_ai_tools.<配置条目ID>`。备份 HA 时一并备份；管理页面仅管理员可访问。

## 开发检查

使用 Python 3.14。安装 `homeassistant==2026.10.1 gazetteer-matcher==1.1.0 hassil==3.12.1 home-assistant-intents==2026.10.6 pytest pytest-asyncio ruff` 后运行：

```sh
python -m pytest -q
ruff check custom_components tests
node --check custom_components/ha_ai_tools/frontend/panel.js
```

首次公开版本的 93 项本地测试通过，另已验证真实 HA 管理接口、模型工具调用和 Home Assistant Voice 服务投递。服务投递成功不代表现场声音已经试听。

## 数据与隐私

- 仓库和发行包只含程序、测试及通用文档，不含家庭配置、提醒、记忆、凭据或实机截图。
- 语音文字、相关上下文、记忆及工具结果会发送给您配置的模型提供方；使用云端模型时会离开 HA 主机。密钥仍由原提供方集成管理，本集成不复制或显示密钥。
- 提醒和记忆保存在 HA 本地 Store 中，建议变更前备份。删除历史和删除记忆均不可通过本集成恢复。
- 需要回退时，在语音流水线中选择原来的对话代理，再停用本集成；保留 Store 文件可保留本地数据。

更新内容见 [CHANGELOG.md](CHANGELOG.md)，问题反馈请使用 [GitHub Issues](https://github.com/q848899176/home-assistant-ai-tools/issues)。

## 许可证

[MIT](LICENSE)。本项目为社区自定义集成。
