# Mac 本地语音服务

此可选组件与 HA 集成分开安装，使用 Apple Silicon、Python 3.12 和 MLX Audio。不要在 HA Python 环境安装 MLX 或模型。

安装前按实施计划检查系统、空闲端口、磁盘和旧服务；把配置存放在私有目录，权限为 600。复制 `config.example.json` 后填写指定本机接口、HA 允许来源和新生成的独立密钥。模型必须事先下载，`models` 每项包含本地 path、固定 revision 和 stt/tts capabilities；推理期间禁用下载。密钥不能出现在命令参数、浏览器快照、日志和 Git 中。

```sh
uv sync --project local_speech --extra inference --group dev --locked
local_speech/.venv/bin/python local_speech/scripts/manage.py preflight --config private/speech/config.json
local_speech/.venv/bin/python local_speech/scripts/manage.py start --config private/speech/config.json
local_speech/.venv/bin/python local_speech/scripts/manage.py status --config private/speech/config.json
local_speech/.venv/bin/python local_speech/scripts/manage.py stop --config private/speech/config.json
```

部署副本应保存在长期保留的位置，不让 launchd 引用准备归档的开发工作树。start 只管理 `com.ha-ai-tools.speech`；stop 不删除程序、模型或缓存，也不停止其他 AI 服务。Mac 睡眠或退出登录后服务可能不可用，先使用 AI Tools 中的恢复点切回原语音引擎。不自动修改系统睡眠设置，不自动把本地音频发送到云端。

首轮接口为完整 WAV；只有验证过真正的有效音频首段和 Nabu 边收边播，才声明流式能力。支持中文预设 Vivian、Serena、Uncle_Fu、Dylan、Eric；实际音质由用户试听验收。初始语速固定 1.0，不接受声音克隆、文件路径和未知模型。

性能测量：`python -m ha_ai_tools_speech.benchmark --config <私有配置> --corpus <私有样本JSON> --output <私有结果JSON>`。corpus 的 tts 为 text/voice 对象数组，stt 为 file/reference 对象数组；file 相对 corpus 所在目录。结果仅记录字符数量、音色、首个可解码音频、完整耗时和 CER，不记录转写正文。P50/P95 区分冷启动、常驻及缓存；API 耗时不等于 Nabu 实际首声。所有业务时间为北京时间。

实机测试只用 Nabu，不调用小爱；多设备逻辑使用模拟服务。模型安装和服务启动不自动改变任何 HA 流水线。

## 部署与回退

1. 将本目录复制到长期目录，在该目录建立独立环境。下载固定 revision 的模型后，核对官方文件尺寸及 SHA256，再填写私有配置。不要加载截断权重。
2. 使用长期目录的 `.venv/bin/python` 运行管理脚本。launchd 会记录此 Python 路径，不能引用临时开发工作树。运行命令无需再次使用 `uv run`，避免未指定 inference extra 时移除推理依赖。
3. 在 HA 保存语音连接，验证服务为 ready。保存不改变流水线，也不会发声。
4. 创建并绑定 Nabu 测试流水线，明确勾选该流水线，预览后应用识别与合成。生产流水线保留原引擎，直到客观指标和音色试听验收通过。
5. 回退时恢复语音字段，结束测试恢复 Nabu 原绑定，确认旧引擎可用后再停止 Mac 服务。stop 保留程序、模型和缓存，不停止其他服务。
6. 降级集成前，恢复所有流水线和设备音色，结束所有测试，通过管理页面删除未引用的语音连接，再还原旧程序并重启。不要手工改写 `.storage`。

HA 2026.10.2 没有单个 TTS 实体关闭全局磁盘缓存的选项。AI Tools 音色试听及使用自定义音色的提醒使用不落盘请求；Assist 沿用原全局策略，不修改全局配置。
