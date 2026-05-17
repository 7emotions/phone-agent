# Phone Agent — AI 打电话能力 for OpenCode

让 OpenCode Agent 获得**真实电话通话能力**。蓝牙 HSP + TTS + VAD + ASR + LLM 对话引擎。

## 架构

```
┌──────────────────────────────────────────────────────────┐
│                    OpenCode Agent                        │
│  phone_dial / hangup / check / speak / ask / converse    │
└───────────────────────┬──────────────────────────────────┘
                        │ MCP stdio
┌───────────────────────▼──────────────────────────────────┐
│                 phone_call_mcp.py                        │
│  ├─ phone_dial       → ADB 拨号 + HSP 自检               │
│  ├─ phone_hangup     → ADB 挂断                          │
│  ├─ phone_check      → 通话状态                          │
│  ├─ phone_speak      → TTS + HSP 播放 + 回声清理         │
│  ├─ phone_ask        → 单轮: 问→录→ASR→提取→返回        │
│  ├─ phone_converse   → 多轮: API LLM 驱动自主对话        │
│  │   ├─ 四层停止机制 (model done > 关键词 > 重复 > max)   │
│  │   ├─ TTS 延迟2s才播 filler，等 filler 播完才说话       │
│  │   └─ 返回 {agent: 说的, caller: 回的}[]               │
│  └─ phone_filler     → 预生成垫话                        │
└──────┬──────────────────────────┬────────────────────────┘
       │                          │
   ┌───▼────┐              ┌──────▼──────┐
   │  ADB   │              │  蓝牙 HSP    │
   │ (拨号) │              │ paplay 上行  │
   └────────┘              │ parecord 下行│
                           │ 回声5次重试  │
                           │ 断连自动重连  │
                           └─────────────┘
```

## 性能

| 环节 | 延迟 |
|------|------|
| TTS (edge-tts) | 2-5s（后台并行生成，不阻塞） |
| ASR (whisper tiny) | 1-3s |
| LLM 对话决策 (DeepSeek API) | ~1s |
| phone_converse 单轮 | 5-10s |

## 硬件要求

- Android 手机（已测试：Xiaomi 22041216C, Android 14, MTK Dimensity 8100）
- 电脑通过蓝牙 HSP 连接手机（电脑充当"蓝牙耳麦"）
- 开发者模式 + USB 调试

## 依赖

```bash
pip install mcp edge-tts webrtcvad llama-cpp-python
apt install pulseaudio pulseaudio-module-bluetooth ffmpeg

# whisper（推荐 tiny 模型，快）
pip install faster-whisper

# 可选：本地 TTS
# apt install espeak-ng
```

## 蓝牙配对

```bash
bluetoothctl pair F8:AB:82:92:08:76
bluetoothctl trust F8:AB:82:92:08:76
pactl list cards short | grep bluez  # 确认识别
```

## 预生成 filler 音频

```bash
python3 gen_fillers.py
```

| filler | 文本 | 触发时机 |
|--------|------|----------|
| thinking | 请稍等，让我思考一下。 | TTS 生成超过 2s |
| timeout | 喂，您还在吗？ | 对方沉默超时 |
| ack | 好的，明白了。 | 确认信息 |
| repeat | 不好意思，我没听清楚… | ASR 低置信度 |
| bye | 好的，谢谢您，再见。 | 结束语 |

## OpenCode 配置

```jsonc
"phone-call": {
  "type": "local",
  "command": ["python3", "/path/to/phone_call_mcp.py"],
  "enabled": true,
  "timeout": 300000,
  "environment": {
    "PHONE_TTS_BACKEND": "edge",
    "PHONE_CONVERSE_BACKEND": "api",
    "PHONE_LLM_URL": "https://api.deepseek.com/chat/completions",
    "PHONE_LLM_KEY": "sk-xxx",
    "PHONE_LLM_MODEL": "deepseek-chat",
    "PHONE_LLM_CONTEXT": "你是南京青赋驭境的AI助手。",
    "PHONE_BT_MAC": "F8:AB:82:92:08:76",
    "PHONE_BT_CARD": "bluez_card.F8_AB_82_92_08_76",
    "PHONE_BT_SINK": "bluez_sink.F8_AB_82_92_08_76.headset_audio_gateway",
    "PHONE_BT_SOURCE": "bluez_source.F8_AB_82_92_08_76.headset_audio_gateway",
    "PHONE_ADB": "/path/to/adb"
  }
}
```

完整示例见 `opencode.jsonc.example`。

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `PHONE_CONVERSE_BACKEND` | 多轮对话后端：`api` / `local` | `api` |
| `PHONE_LLM_BACKEND` | 提取后端：`local` / `api` | `local` |
| `PHONE_LLM_CONTEXT` | 系统预设上下文（身份+任务） | — |
| `PHONE_LLM_URL` | API 地址 | `api.deepseek.com/chat/completions` |
| `PHONE_LLM_KEY` | API 密钥 | — |
| `PHONE_LLM_MODEL` | API 模型名 | `deepseek-chat` |
| `PHONE_TTS_BACKEND` | TTS：`espeak` / `edge` | `edge` |
| `PHONE_ESPEAK_BIN` | espeak-ng 路径 | `~/.local/bin/espeak-ng` |
| `PHONE_BT_MAC` | 手机蓝牙 MAC | — |
| `PHONE_BT_CARD` | PulseAudio 蓝牙卡名 | — |
| `PHONE_BT_SINK` | HSP 上行 sink | — |
| `PHONE_BT_SOURCE` | HSP 下行 source | — |
| `PHONE_ADB` | adb 路径 | `~/Android/Sdk/platform-tools/adb` |

## MCP 工具

| 工具 | 参数 | 说明 |
|------|------|------|
| `phone_dial` | `number` | 拨号（自动检查 HSP） |
| `phone_hangup` | — | 挂断 |
| `phone_check` | — | idle / ringing / active |
| `phone_speak` | `text` | TTS → HSP |
| `phone_ask` | `question`, `info_keys`, `context?` | 单轮：问→录→ASR→提取 |
| **`phone_converse`** | `goal`, `info_keys`, `context?`, `max_turns?` | **多轮自主对话** |
| `phone_filler` | `type` | 播放垫话 |

### phone_converse — 自主多轮对话

Agent 只需要描述目标，MCP 内部自动完成整轮对话：

```json
phone_converse({
  "goal": "通知王经理周五下午3点会议延期到下周一上午10点",
  "info_keys": "收到通知",
  "context": "地点三号会议室不变，确认王经理收到通知即可。",
  "max_turns": 4
})
```

**四层停止机制**：

| 层 | 机制 | 触发条件 |
|----|------|----------|
| 1 | API 模型判 done | 信息收集完毕 / 对方拒绝 / 对话完成 |
| 2 | 关键词触发 | 推脱语（帮你记、打错、稍后联系等） |
| 3 | 重复检测 | 连续两轮相同回复 |
| 4 | max_turns | 安全上限 |

**TTS 延迟填补**：录音结束后开始计时，超过 2s 才播 "请稍等" filler。如果 TTS 在 filler 期间完成，等 filler 播完再说话。

**返回格式**：

```json
{
  "transcripts": [
    {"agent": "请问是王经理吗？会议延期到下周一...", "caller": "好的收到。"}
  ],
  "turns": 1,
  "status": "ok"
}
```

## Agent 调用模式

```
// 模式 1: 简单通知（1-2 轮自动完成）
result = phone_converse({
  goal: "通知快递明天到",
  info_keys: "收到通知",
  context: "SF12345，收件人刘女士"
})

// 模式 2: 确认收集（多轮对话）
result = phone_converse({
  goal: "确认周六活动出席和饮食",
  info_keys: "出席,饮食",
  context: "活动下午3点开始，对方之前表示可能来"
})

// Agent 拿到 transcript 自己总结
for (const t of result.transcripts) {
  console.log(`问: ${t.agent}\n答: ${t.caller}`)
}
```

## VAD 工作原理

`phone_listen_vad.py` + WebRTC VAD：

1. `parecord --raw` 输出 8kHz PCM
2. 每 30ms 一帧送 VAD
3. 检测到 speech → 开始累积
4. 连续 silence 0.8s → 停止
5. 写 WAV 返回

## 关键技术决策

- **蓝牙 HSP**：数字通路无回声
- **API 驱动对话**：DeepSeek 做方向盘，1.5B 本地做简单提取，各司其职
- **双层上下文**：系统预设 + 每次调用传入，模型自动合并
- **四层停止**：模型判停 + 关键词 + 重复检测 + max，防止死循环
- **TTS 后台生成**：录音结束立即后台生成 TTS，不阻塞
- **filler 智能播放**：超 2s 才播，播完才说话，不打断
- **回声 5 次重试清理**：module-bluetooth-policy 自动创建 loopback，每次 paplay 前强制清除
- **蓝牙断连自动重连**：`ensure_hsp()` 内部 `bluetoothctl connect` + 重设 profile
- **HSP 拨号前检查**：`phone_dial` 先走 `ensure_hsp()`，连不上不拨号
- **通话状态取最高值**：`dumpsys` 多状态行时取最大，避免误判
- **transcript 双端记录**：返回 `{agent, caller}` 对，Agent 看到完整对话
- **whisper tiny**：速度优先，1-3s 转写
- **零硬编码路径**：全部通过环境变量注入
- **98% 停止准确率**：184 条多场景对话测试验证

## Agent Skill

`skill-phone-call.md` — OpenCode agent skill for orchestrating phone calls. Teaches agents tool selection, two-step conversation flow, stop mechanism interpretation, and error recovery. Install to `~/.agents/skills/phone-call/SKILL.md`.
