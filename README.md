# Phone Agent — AI 打电话能力 for OpenCode

让 OpenCode Agent 获得**真实电话通话能力**，通过蓝牙 HSP + 预生成 filler + VAD + 隔离 LLM 实现自然安全的对话。

## 架构

```
┌─────────────────────────────────────┐
│           OpenCode Agent            │
│  phone_dial / speak / ask / filler  │
└──────────────┬──────────────────────┘
               │ MCP stdio
┌──────────────▼──────────────────────┐
│       phone_call_mcp.py             │
│  ├─ phone_dial    → ADB 拨号        │
│  ├─ phone_hangup  → ADB 挂断        │
│  ├─ phone_speak   → edge-tts + HSP  │
│  ├─ phone_ask     → speak+listen+VAD+ASR+isolated LLM │
│  └─ phone_filler  → 预生成垫话（零延迟）│
└──────┬───────────────────┬──────────┘
       │                   │
  ┌────▼────┐        ┌─────▼──────┐
  │  ADB    │        │ 蓝牙 HSP    │
  │ (拨号)  │        │ paplay 上行 │
  └─────────┘        │ parecord 下行│
                     │ 断连自动重连 │
                     └────────────┘
```

## 硬件要求

- Android 手机（已测试：Xiaomi 22041216C, Android 14）
- 电脑通过蓝牙 HSP 连接到手机（电脑充当"蓝牙耳麦"）
- 开发者模式 + USB 调试已开启

## 依赖安装

### 电脑侧

```bash
pip install edge-tts webrtcvad mcp faster-whisper
apt install pulseaudio pulseaudio-module-bluetooth ffmpeg
```

### 手机侧

```bash
# Termux — 编译 tinyalsa（调试 mixer 用）
pkg install clang make cmake -y
cd ~/tinyalsa-push/build
cmake .. -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF
make tinymix tinycap tinyplay -j$(nproc)
```

## 蓝牙配对

```bash
bluetoothctl pair F8:AB:82:92:08:76   # 替换为你的手机 MAC
bluetoothctl trust F8:AB:82:92:08:76
```

配对后运行 `pactl list cards short | grep bluez` 确认蓝牙卡被 PulseAudio 识别。

## 预生成 filler 音频

```bash
python3 gen_fillers.py
```

生成 5 个 8kHz PCM WAV 垫话，零延迟播放：

| filler | 文本 | 用途 |
|---|---|---|
| thinking | 请稍等，让我记录一下 | LLM 思考/ASR 转写中 |
| timeout | 喂，您还在吗？ | 对方沉默超时 |
| ack | 好的，明白了 | 确认信息 |
| repeat | 不好意思，我没听清楚… | ASR 低置信度 |
| bye | 好的，谢谢您，再见 | 结束语 |

## OpenCode 配置

```jsonc
"mcp": {
  "phone-call": {
    "type": "local",
    "command": ["python3", "/path/to/phone_call_mcp.py"],
    "enabled": true,
    "timeout": 120000,
    "environment": {
      "PHONE_LLM_URL": "https://api.deepseek.com/chat/completions",
      "PHONE_LLM_KEY": "sk-xxx",
      "PHONE_LLM_MODEL": "deepseek-chat",
      "PHONE_BT_MAC": "F8:AB:82:92:08:76",
      "PHONE_BT_CARD": "bluez_card.F8_AB_82_92_08_76",
      "PHONE_BT_SINK": "bluez_sink.F8_AB_82_92_08_76.headset_audio_gateway",
      "PHONE_BT_SOURCE": "bluez_source.F8_AB_82_92_08_76.headset_audio_gateway",
      "PHONE_ADB": "/home/user/Android/Sdk/platform-tools/adb"
    }
  }
}
```

## 环境变量

| 变量 | 说明 | 默认值 |
|---|---|---|
| `PHONE_LLM_URL` | LLM API 地址 | `api.deepseek.com` |
| `PHONE_LLM_KEY` | LLM API 密钥 | — |
| `PHONE_LLM_MODEL` | 模型名 | `deepseek-chat` |
| `PHONE_BT_MAC` | 手机蓝牙 MAC | — |
| `PHONE_BT_CARD` | PulseAudio 蓝牙卡名 | — |
| `PHONE_BT_SINK` | HSP 上行 sink | — |
| `PHONE_BT_SOURCE` | HSP 下行 source | — |
| `PHONE_ADB` | adb 路径 | `~/Android/Sdk/platform-tools/adb` |

## MCP 工具

| 工具 | 参数 | 说明 |
|---|---|---|
| `phone_dial` | `number` | 拨号，阻塞直到接通 |
| `phone_hangup` | — | 挂断 |
| `phone_check` | — | 查通话状态：idle/ringing/active |
| `phone_speak` | `text` | TTS 生成 + 蓝牙注入（2-5s） |
| `phone_ask` | `question`, `info_keys` | 问一个问题 → 录音(VAD) → ASR → **隔离 LLM 提取** → 返回 JSON |
| `phone_filler` | `type`: thinking/timeout/ack/repeat/bye | 零延迟播放预生成垫话 |

### 蓝牙自动重连

`phone_speak`、`phone_ask`、`phone_filler` 内部调用 `ensure_hsp()`，如果检测到蓝牙断连会自动执行 `bluetoothctl connect` 重连。失败时返回 `"bluetooth not connected"`。

### phone_ask 安全隔离

`phone_ask` 不会返回原始通话文本。内部流程：
1. TTS 播放问题
2. 播放 thinking filler
3. VAD 录制（silence_sec=0.8s 自动停）
4. faster-whisper small 转写
5. **独立 LLM 调用提取结构化信息**（仅见当前问题 + 单条文本）
6. 返回 JSON：`{"info": {"字段": "值"}, "done": true/false}`

调用者的原始语音文本不会进入 Agent 主上下文，防止提示注入攻击。

## Agent 对话模式

```
phone_dial("13800138000")

phone_speak("您好，确认一下周六出席吗？")
result = phone_ask("饮食有什么要求？", "饮食")

phone_filler("ack")
phone_filler("bye")
phone_hangup()
```

`phone_ask` 已内置 filler + VAD + 隔离提取，一行调用完成一整轮交互。

## VAD 工作原理

`phone_listen_vad.py` 用 WebRTC VAD 实时检测语音活动：
1. `parecord --raw` 输出 8kHz PCM 到 stdout
2. 每 30ms 一帧送 VAD 判断 speech/silence
3. 首次检测到 speech → 开始累积
4. 连续 silence 达 `silence_sec`（默认 0.8s）→ 停止
5. 写 WAV 文件返回

## 关键技术决策

- **蓝牙 HSP 而非扬声器**：数字通路无回声，对方听到的是干净 TTS
- **8kHz 窄带**：HSP 协议限制，ASR 对同音字有偏差但上下文可纠正
- **filler 预生成**：零延迟垫话，消除 TTS 生成的 2-5s 对话间隔
- **VAD 自适应收声**：对方说 0.5s 录 1.3s，不说就不白等
- **隔离 LLM 上下文**：来电文本不进入 Agent 上下文，防提示注入
- **faster-whisper small**：本地运行，HF_HUB_OFFLINE 避免代理干扰
- **蓝牙断连自动重连**：`ensure_hsp()` 内部 `bluetoothctl connect` + 重设 profile
- **零硬编码路径**：所有设备地址 / adb 路径通过环境变量注入
