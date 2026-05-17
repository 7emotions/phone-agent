# Phone Agent — AI 打电话能力 for OpenCode

让 OpenCode Agent 获得**真实电话通话能力**。蓝牙 HSP + 本地 TTS + 本地 LLM + VAD + 上下文隔离。

## 架构

```
┌─────────────────────────────────────────────────┐
│               OpenCode Agent                    │
│  phone_dial / speak / ask(context) / filler     │
└──────────────────┬──────────────────────────────┘
                   │ MCP stdio
┌──────────────────▼──────────────────────────────┐
│           phone_call_mcp.py                     │
│  ├─ phone_dial    → ADB 拨号                    │
│  ├─ phone_hangup  → ADB 挂断                    │
│  ├─ phone_speak   → espeak-ng(本地TTS) + HSP   │
│  ├─ phone_ask     → speak+listen+VAD+ASR+LLM    │
│  │   ├─ 双层上下文 (系统预设 + 调用传入)         │
│  │   ├─ 本地 LLM (qwen2.5-1.5B GGUF)            │
│  │   ├─ 返回 transcript + info (agent 可总结)    │
│  │   └─ 回退: edge-tts / DeepSeek API           │
│  └─ phone_filler  → 预生成垫话（零延迟）         │
└──────┬───────────────────────┬──────────────────┘
       │                       │
   ┌───▼────┐           ┌──────▼──────┐
   │  ADB   │           │  蓝牙 HSP    │
   │ (拨号) │           │ paplay 上行  │
   └────────┘           │ parecord 下行│
                        │ 断连自动重连  │
                        │ 回声自动清理  │
                        └─────────────┘
```

## 性能

| 环节 | 之前 (云端) | 现在 (本地) |
|------|------------|------------|
| TTS 首句 | 2-5s (edge-tts) | **21ms** (espeak-ng) |
| LLM 提取 | 1-3s (DeepSeek API) | **500ms** (qwen2.5-1.5B) |
| phone_ask 全流程 | 5-10s | **1-2s** |

## 硬件要求

- Android 手机（已测试：Xiaomi 22041216C, Android 14, MTK Dimensity 8100）
- 电脑通过蓝牙 HSP 连接手机（电脑充当"蓝牙耳麦"）
- 开发者模式 + USB 调试已开启

## 依赖安装

### 电脑侧

```bash
# Python 依赖
pip install mcp edge-tts webrtcvad faster-whisper llama-cpp-python

# 系统依赖
apt install pulseaudio pulseaudio-module-bluetooth ffmpeg

# 本地 TTS（espeak-ng）
# 方式 1: apt install espeak-ng
# 方式 2: 从 deb 包提取二进制到 ~/.local/bin/
wget http://archive.ubuntu.com/ubuntu/pool/universe/e/espeak-ng/espeak-ng_1.50+dfsg-10ubuntu0.1_amd64.deb
dpkg-deb -x espeak-ng_*.deb /tmp/espeak-extract
cp /tmp/espeak-extract/usr/bin/espeak-ng ~/.local/bin/
```

### 本地 LLM 模型

```bash
pip install huggingface_hub

# 下载 qwen2.5-1.5B GGUF（~1GB）
python3 -c "
from huggingface_hub import hf_hub_download
hf_hub_download('Qwen/Qwen2.5-1.5B-Instruct-GGUF',
    filename='qwen2.5-1.5b-instruct-q4_k_m.gguf',
    local_dir='~/.local/share/phone-agent')
"
```

> 国内可用 `HF_ENDPOINT=https://hf-mirror.com` 加速下载。

### 手机侧（可选，调试用）

```bash
# Termux — 编译 tinyalsa
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
      // ── LLM: 本地 GGUF 优先，API 回退 ──
      "PHONE_LLM_BACKEND": "local",
      "PHONE_LOCAL_MODEL": "/path/to/qwen2.5-1.5b-instruct-q4_k_m.gguf",
      "PHONE_LLM_CONTEXT": "你是南京青赋驭境的AI助手。",
      // API 回退
      "PHONE_LLM_URL": "https://api.deepseek.com/chat/completions",
      "PHONE_LLM_KEY": "sk-xxx",
      "PHONE_LLM_MODEL": "deepseek-chat",
      // ── TTS: 本地 espeak-ng 优先，edge-tts 回退 ──
      "PHONE_TTS_BACKEND": "espeak",
      "PHONE_ESPEAK_BIN": "/path/to/espeak-ng",
      // ── 蓝牙 HSP ──
      "PHONE_BT_MAC": "F8:AB:82:92:08:76",
      "PHONE_BT_CARD": "bluez_card.F8_AB_82_92_08_76",
      "PHONE_BT_SINK": "bluez_sink.F8_AB_82_92_08_76.headset_audio_gateway",
      "PHONE_BT_SOURCE": "bluez_source.F8_AB_82_92_08_76.headset_audio_gateway",
      // ── ADB ──
      "PHONE_ADB": "/path/to/adb"
    }
  }
}
```

完整示例见 `opencode.jsonc.example`。

## 环境变量

| 变量 | 说明 | 默认值 |
|---|---|---|
| **LLM** |||
| `PHONE_LLM_BACKEND` | 提取后端：`local` 或 `api` | `local` |
| `PHONE_LOCAL_MODEL` | GGUF 模型路径 | `./qwen2.5-1.5b-instruct-q4_k_m.gguf` |
| `PHONE_LLM_CONTEXT` | 系统预设上下文（身份+任务基调） | — |
| `PHONE_LLM_URL` | API 地址（回退用） | `api.deepseek.com/chat/completions` |
| `PHONE_LLM_KEY` | API 密钥 | — |
| `PHONE_LLM_MODEL` | API 模型名 | `deepseek-chat` |
| **TTS** |||
| `PHONE_TTS_BACKEND` | TTS 后端：`espeak` 或 `edge` | `espeak` |
| `PHONE_ESPEAK_BIN` | espeak-ng 二进制路径 | `~/.local/bin/espeak-ng` |
| **蓝牙** |||
| `PHONE_BT_MAC` | 手机蓝牙 MAC | — |
| `PHONE_BT_CARD` | PulseAudio 蓝牙卡名 | — |
| `PHONE_BT_SINK` | HSP 上行 sink | — |
| `PHONE_BT_SOURCE` | HSP 下行 source | — |
| **ADB** |||
| `PHONE_ADB` | adb 路径 | `~/Android/Sdk/platform-tools/adb` |

## MCP 工具

| 工具 | 参数 | 说明 |
|---|---|---|
| `phone_dial` | `number` | 拨号，阻塞直到接通 |
| `phone_hangup` | — | 挂断 |
| `phone_check` | — | 查通话状态：idle/ringing/active |
| `phone_speak` | `text` | TTS 生成 + 蓝牙注入（~21ms） |
| `phone_ask` | `question`, `info_keys`, `context?` | 问问题 → 录音(VAD) → ASR → **本地 LLM 提取** → 返回 JSON |
| `phone_filler` | `type`: thinking/timeout/ack/repeat/bye | 零延迟播放预生成垫话 |

### phone_ask — 双层上下文 + transcript 返回

`phone_ask` 接受可选的 `context` 参数，与系统预设 `PHONE_LLM_CONTEXT` 合并：

```
系统预设:  "你是南京青赋驭境的AI助手，正在电话确认活动出席"
调用传入:  "这是第三轮确认，对方之前说可能来"
合并结果:  "你是南京青赋驭境的AI助手，正在电话确认活动出席\n这是第三轮确认，对方之前说可能来"
```

返回格式：

```json
{
  "info": {"出席": "是", "饮食": "素菜"},
  "transcript": "我可以来，但是我不吃香菜，素菜就行",
  "done": true,
  "status": "ok"
}
```

`transcript` 字段让 Agent 可以直接基于原始通话内容做总结，不依赖 LLM 提取的准确率。

### 蓝牙自动重连 + 回声清理

每次 `ensure_hsp()` 激活 HSP 时：
1. 自动 `bluetoothctl connect` 重连
2. 卸载所有 PulseAudio `module-loopback`（防止 HSP sink→source 数字回声）
3. 静音 BT_SOURCE 防止本地回放

## Agent 对话模式

```
phone_dial("13800138000")

phone_speak("您好，确认一下周六出席吗？")

result = phone_ask("饮食有什么要求？", "出席,饮食",
    context="第三轮确认，对方之前表示可能来")
// 返回: {"info": {"出席": "是", "饮食": "素菜"}, "transcript": "..."}

// 基于 transcript 做总结
summary = f"用户{result['info']['出席']}参加，饮食要求{result['info']['饮食']}"

phone_filler("bye")
phone_hangup()
```

## VAD 工作原理

`phone_listen_vad.py` 使用 WebRTC VAD 实时检测语音活动：

1. `parecord --raw` 输出 8kHz PCM 到 stdout
2. 每 30ms 一帧送 VAD 判断 speech/silence
3. 首次检测到 speech → 开始累积
4. 连续 silence 达 `silence_sec`（默认 0.8s）→ 停止
5. 写 WAV 文件返回

## 关键技术决策

- **蓝牙 HSP 而非扬声器**：数字通路无回声，对方听到的是干净 TTS
- **本地 TTS 优先**：espeak-ng 21ms 生成，100x 快于云端 edge-tts
- **本地 LLM 优先**：qwen2.5-1.5B GGUF 本地推理，500ms 延迟，无网络依赖
- **双层上下文**：系统预设身份 + Agent 每次调用传入任务上下文，合并生效
- **transcript 透传**：Agent 可基于原始通话文本总结，不受 LLM 提取偏差影响
- **8kHz 窄带**：HSP 协议限制，ASR 对同音字有偏差但上下文可纠正
- **filler 预生成**：零延迟垫话，消除 TTS 生成的 2-5s 对话间隔
- **VAD 自适应收声**：对方说 0.5s 录 1.3s，不说就不白等
- **隔离 LLM 上下文**：来电文本不进入 Agent 上下文，防提示注入
- **faster-whisper small**：本地 ASR，HF_HUB_OFFLINE 避免代理干扰
- **回声自动清理**：HSP 激活时自动卸载 PulseAudio loopback 模块，消除数字回声
- **蓝牙断连自动重连**：`ensure_hsp()` 内部 `bluetoothctl connect` + 重设 profile
- **零硬编码路径**：所有路径通过环境变量注入，仓库可跨机器部署
