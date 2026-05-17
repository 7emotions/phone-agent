# Phone Agent — AI 打电话能力 for OpenCode

让 OpenCode Agent 获得**真实电话通话能力**，通过蓝牙 HSP + 预生成 filler + VAD 实现自然对话。

## 架构

```
┌─────────────────────────────────────┐
│           OpenCode Agent            │
│  phone_dial / speak / listen / ...  │
└──────────────┬──────────────────────┘
               │ MCP stdio
┌──────────────▼──────────────────────┐
│       phone_call_mcp.py             │
│  ├─ phone_dial    → ADB 拨号        │
│  ├─ phone_hangup  → ADB 挂断        │
│  ├─ phone_speak   → edge-tts + HSP  │
│  ├─ phone_listen  → VAD + whisper   │
│  └─ phone_filler  → 预生成垫话       │
└──────┬───────────────────┬──────────┘
       │                   │
  ┌────▼────┐        ┌─────▼──────┐
  │  ADB    │        │ 蓝牙 HSP    │
  │ (拨号)  │        │ paplay 上行 │
  └─────────┘        │ parecord 下行│
                     └────────────┘
```

## 硬件要求

- Root Android 手机（已测试：Xiaomi 22041216C, Android 14）
- 电脑通过蓝牙 HSP 连接到手机（电脑充当"蓝牙耳麦"）
- 开发者模式 + USB 调试已开启

## 依赖安装

### 电脑侧

```bash
pip install edge-tts webrtcvad mcp faster-whisper
apt install pulseaudio pulseaudio-module-bluetooth ffmpeg adb
```

### 手机侧

```bash
# Termux
pkg install clang make cmake -y

# 编译 tinyalsa（用于调试 mixer）
cd ~/tinyalsa-push/build
cmake .. -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF
make tinymix tinycap tinyplay -j$(nproc)
```

### Java 音频工具（一次性编译）

```bash
javac -source 1.8 -target 1.8 \
  -bootclasspath $ANDROID_HOME/platforms/android-36.1/android.jar \
  -d classes CallRecorder2.java CallPlayerV2.java

d8 --lib $ANDROID_HOME/platforms/android-36.1/android.jar \
  --output . classes/CallRecorder2.class
adb push classes.dex /data/local/tmp/rec2.dex

d8 --lib $ANDROID_HOME/platforms/android-36.1/android.jar \
  --output . classes/CallPlayerV2.class
adb push classes.dex /data/local/tmp/player.dex
```

CallRecorder2 用 `AudioRecord(source=7 即 VOICE_COMMUNICATION)` 录制，CallPlayerV2 用 `AudioTrack(USAGE_VOICE_COMMUNICATION)` 播放。蓝牙 HSP 下不需要它们，保留作备用。

## 蓝牙 HSP 连接

```bash
# 配对后查看蓝牙卡
pactl list cards short | grep bluez

# 切换到 headset_audio_gateway 模式
pactl set-card-profile bluez_card.XX_XX_XX_XX_XX_XX headset_audio_gateway
```

此时电脑成为手机的"蓝牙耳麦"：
- `paplay --device=bluez_sink.XXX.headset_audio_gateway` → 对方听到（上行注入）
- `parecord --device=bluez_source.XXX.headset_audio_gateway` → 捕获对方（下行采集）

## 预生成 filler 音频

```bash
python3 gen_fillers.py
```

生成 5 个垫话：
| filler | 文本 | 用途 |
|---|---|---|
| thinking | 请稍等，让我记录一下 | LLM 思考中 |
| timeout | 喂，您还在吗？ | 对方超时未应答 |
| ack | 好的，明白了 | 确认信息 |
| repeat | 不好意思，我没听清楚… | ASR 置信度低 |
| bye | 好的，谢谢您，再见 | 结束语 |

filler 是 8kHz PCM WAV，直接播放无需 TTS 生成，零延迟。

## OpenCode 配置

```jsonc
"mcp": {
  "phone-call": {
    "type": "local",
    "command": ["python3", "/path/to/phone_call_mcp.py"],
    "enabled": true,
    "timeout": 120000
  }
}
```

## MCP 工具

| 工具 | 参数 | 说明 |
|---|---|---|
| `phone_dial` | `number` | 拨号，等待接通后返回 |
| `phone_hangup` | — | 挂断 |
| `phone_check` | — | 查通话状态 |
| `phone_speak` | `text` | TTS 生成 + 蓝牙注入（2-5s） |
| `phone_listen` | `max_sec=30`, `silence_sec=0.8` | VAD 录制 + ASR 转写 |
| `phone_filler` | `type`: thinking/timeout/ack/repeat/bye | 零延迟播预生成垫话 |

## Agent 对话模式

```
phone_dial("13800138000")

phone_speak("您好，确认一下周六出席吗？")
reply = phone_listen()

phone_filler("thinking")              # 零延迟，先稳住对方
# ... Agent 思考 ...
phone_speak("饮食有什么要求吗？")      # 根据 reply 动态追问

reply = phone_listen(max_sec=20)
if not reply:
    phone_filler("timeout")           # 对方沉默 → 主动问
    reply = phone_listen(max_sec=15)

phone_filler("ack")
phone_filler("bye")
phone_hangup()
```

## VAD 工作原理

`phone_listen_vad.py` 用 WebRTC VAD 实时检测语音活动：
1. `parecord --raw` 输出 8kHz PCM 到 stdout
2. 每 30ms 一帧送 VAD 判断 speech/silence
3. 首次检测到 speech → 开始累积
4. 连续 silence 达到 `silence_sec` → 停止
5. 写 WAV 文件返回

## 关键技术决策

- **蓝牙 HSP 而非扬声器**：扬声器耦合有回声、音量不稳定；HSP 是数字通路，干净可靠
- **8kHz 窄带**：HSP 协议限制，ASR 对同音字识别有偏差，但上下文可纠正
- **filler 预生成而非实时 TTS**：实时 TTS 有 2-5 秒延迟，filler 零延迟垫话消除对话间隔
- **VAD 而非固定时长**：对方说 0.5 秒就不再白等，silence_sec=0.8 在灵敏度和误中断间平衡
- **faster-whisper small**：比 OpenAI API 快且免费，预下载模型 + HF_HUB_OFFLINE 避免代理干扰
