#!/usr/bin/env python3
"""MCP server: phone-call with local TTS, local LLM, and context isolation.

phone_ask() wraps speak+listen+ASR+LLM into one call.
Returns structured JSON WITH transcript for agent summarization.
Local TTS (espeak-ng) and local LLM (GGUF via llama-cpp) for sub-second latency.
Falls back to edge-tts and DeepSeek API when local backends unavailable.
"""

import subprocess, json, time, os, tempfile, asyncio, urllib.request
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Paths ────────────────────────────────────────────────────────────────────
ADB_BIN = os.environ.get("PHONE_ADB", os.path.expanduser("~/Android/Sdk/platform-tools/adb"))
ADB = [ADB_BIN]
BT_CARD = os.environ.get("PHONE_BT_CARD", "")
BT_SINK = os.environ.get("PHONE_BT_SINK", "")
BT_SOURCE = os.environ.get("PHONE_BT_SOURCE", "")

# ── TTS config ───────────────────────────────────────────────────────────────
ESPEAK_BIN = os.environ.get("PHONE_ESPEAK_BIN", os.path.expanduser("~/.local/bin/espeak-ng"))
TTS_BACKEND = os.environ.get("PHONE_TTS_BACKEND", "espeak")  # "espeak" or "edge"

# ── LLM config ───────────────────────────────────────────────────────────────
LLM_BACKEND = os.environ.get("PHONE_LLM_BACKEND", "local")
CONVERSE_BACKEND = os.environ.get("PHONE_CONVERSE_BACKEND", "api")
LOCAL_MODEL = os.environ.get("PHONE_LOCAL_MODEL",
    os.path.join(BASE_DIR, "qwen2.5-0.5b-instruct-q4_k_m.gguf"))
LLM_URL = os.environ.get("PHONE_LLM_URL", "https://api.deepseek.com/chat/completions")
LLM_KEY = os.environ.get("PHONE_LLM_KEY", "")
LLM_MODEL = os.environ.get("PHONE_LLM_MODEL", "deepseek-chat")
LLM_CONTEXT = os.environ.get("PHONE_LLM_CONTEXT", "")

# ── Lazy-loaded local LLM ────────────────────────────────────────────────────
_local_llm = None

def _get_local_llm():
    global _local_llm
    if _local_llm is not None:
        return _local_llm
    if not os.path.exists(LOCAL_MODEL):
        return None
    try:
        from llama_cpp import Llama
        _local_llm = Llama(model_path=LOCAL_MODEL, n_ctx=2048, n_threads=4, verbose=False)
        return _local_llm
    except Exception:
        return None

# ── Prompt template ──────────────────────────────────────────────────────────
EXTRACT_PROMPT = """从对话内容中提取指定字段的信息。

{context}

要提取的字段: {info_keys}

对话: {transcript}

严格按JSON格式返回，不要添加任何解释:
{{"info": {{"字段1": "提取到的值", "字段2": "提取到的值"}}, "done": true}}
如果某字段没有明确提到，填"未知"。"""

CONVERSE_PROMPT = """你在和真人通电话。按目标引导对话、收集信息。

{context}

目标: {goal}
待收集: {info_keys}
已收集: {collected}
当前第 {turn}/{max_turns} 轮

对方: {transcript}

决定下一句说什么，严格返回JSON:
{{"action": "ask", "text": "你要说的话"}}
{{"action": "done", "reason": "为什么结束"}}
对方说"无法回复""不太了解""会转告""稍后联系""帮你记下"等，立即返回done。轮次不重要，信息传达清楚就停。"""

server = Server("phone-call")


def _call_state() -> int:
    """Return highest call state: 0=idle, 1=ringing, 2=active."""
    out = adb("dumpsys telephony.registry | grep mCallState", timeout=3)
    highest = 0
    for line in out.split("\n"):
        if "mCallState=" in line:
            try:
                highest = max(highest, int(line.split("=")[1].strip()))
            except ValueError:
                pass
    return highest


def adb(cmd: str, timeout: int = 15) -> str:
    r = subprocess.run(ADB + ["shell", cmd], capture_output=True, text=True, timeout=timeout)
    return r.stdout + r.stderr


def _normalize_result(data: dict) -> dict:
    """Ensure result has {'info': {...}, 'done': bool} structure."""
    if not isinstance(data, dict):
        return {"info": {}, "done": False}
    if "info" not in data:
        # Model returned flat keys — wrap them
        done = data.pop("done", True)
        data = {"info": data, "done": done}
    data.setdefault("done", True)
    return data


def _unload_loopbacks():
    r = subprocess.run(["pactl", "list", "short", "modules"], capture_output=True, text=True)
    for line in r.stdout.split("\n"):
        if "module-loopback" in line:
            mod_id = line.split()[0]
            subprocess.run(["pactl", "unload-module", mod_id], capture_output=True)

def _unload_loopbacks_aggressive():
    """Unload loopbacks with retry — catches modules auto-created by bluetooth-policy."""
    import time as _time
    for _ in range(5):
        mods_before = subprocess.run(
            ["pactl", "list", "short", "modules"], capture_output=True, text=True)
        count_before = mods_before.stdout.count("module-loopback")
        if count_before == 0:
            return
        _unload_loopbacks()
        _time.sleep(0.05)
        mods_after = subprocess.run(
            ["pactl", "list", "short", "modules"], capture_output=True, text=True)
        if mods_after.stdout.count("module-loopback") == 0:
            return


def ensure_hsp() -> bool:
    r = subprocess.run(["pactl", "list", "sinks", "short"], capture_output=True, text=True)
    if BT_SINK in r.stdout:
        _unload_loopbacks()
        return True

    subprocess.run(["bluetoothctl", "connect", os.environ.get("PHONE_BT_MAC", "")],
                   capture_output=True, timeout=10)
    time.sleep(2)

    r = subprocess.run(["pactl", "set-card-profile", BT_CARD, "headset_audio_gateway"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return False

    r2 = subprocess.run(["pactl", "list", "sinks", "short"], capture_output=True, text=True)
    if BT_SINK not in r2.stdout:
        return False

    _unload_loopbacks()
    return True


def clean_env():
    env = {}
    for k, v in os.environ.items():
        if 'proxy' not in k.lower() and 'PROXY' not in k:
            env[k] = v
    env['HF_HUB_OFFLINE'] = '1'
    env['TRANSFORMERS_OFFLINE'] = '1'
    return env


# ═══════════════════════════════════════════════════════════════════════════════
# TTS: local espeak-ng (primary) / edge-tts (fallback)
# ═══════════════════════════════════════════════════════════════════════════════

async def _espeak_tts(text: str) -> str:
    """Generate 8kHz PCM WAV using local espeak-ng. ~200-400ms."""
    raw = tempfile.mktemp(suffix=".wav")
    proc = await asyncio.create_subprocess_exec(
        ESPEAK_BIN, "-v", "cmn", "-s", "140", "-a", "100", "-w", raw, text,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await proc.wait()
    if proc.returncode != 0 or not os.path.exists(raw):
        return ""

    pcm = tempfile.mktemp(suffix=".wav")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", raw, "-ar", "8000", "-ac", "1", "-sample_fmt", "s16", pcm,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await proc.wait()
    os.remove(raw)
    return pcm if proc.returncode == 0 else ""


async def _edge_tts(text: str) -> str:
    """Generate 8kHz PCM WAV using cloud edge-tts. ~2-5s."""
    raw = tempfile.mktemp(suffix=".mp3")
    pcm = tempfile.mktemp(suffix=".wav")
    proc = await asyncio.create_subprocess_exec(
        "edge-tts", "--voice", "zh-CN-XiaoxiaoNeural",
        "--text", text, "--write-media", raw,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await proc.wait()
    if proc.returncode != 0:
        return ""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", raw, "-ar", "8000", "-ac", "1", "-sample_fmt", "s16", pcm,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await proc.wait()
    os.remove(raw)
    return pcm if proc.returncode == 0 else ""


async def tts_8khz(text: str) -> str:
    """Fast local TTS if available, edge-tts fallback."""
    if TTS_BACKEND == "espeak" and os.path.exists(ESPEAK_BIN):
        result = await _espeak_tts(text)
        if result:
            return result
    return await _edge_tts(text)


# ═══════════════════════════════════════════════════════════════════════════════
# LLM: local GGUF (primary) / API (fallback)
# ═══════════════════════════════════════════════════════════════════════════════

def _local_llm_extract(context: str, info_keys: str, transcript: str) -> dict:
    """Extract info using local GGUF model via llama-cpp. ~200-500ms."""
    llm = _get_local_llm()
    if llm is None:
        return None

    prompt = EXTRACT_PROMPT.replace("{context}", context).replace(
        "{info_keys}", info_keys).replace("{transcript}", transcript)
    try:
        resp = llm.create_chat_completion(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=128, temperature=0.1,
        )
        text = resp["choices"][0]["message"]["content"].strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        return _normalize_result(json.loads(text))
    except Exception:
        return None


def _api_llm_extract(context: str, info_keys: str, transcript: str) -> dict:
    """Extract info via DeepSeek API. ~1-3s."""
    prompt = EXTRACT_PROMPT.replace("{context}", context).replace(
        "{info_keys}", info_keys).replace("{transcript}", transcript)
    body = json.dumps({
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3, "max_tokens": 128
    }).encode()
    req = urllib.request.Request(LLM_URL, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LLM_KEY}"
    })
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read())
        text = data["choices"][0]["message"]["content"].strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        return _normalize_result(json.loads(text))
    except Exception:
        return {"info": {}, "done": False}


def extract_info(context: str, info_keys: str, transcript: str) -> dict:
    """Try local LLM first, fall back to API."""
    if LLM_BACKEND == "local" and os.path.exists(LOCAL_MODEL):
        result = _local_llm_extract(context, info_keys, transcript)
        if result is not None:
            return result
    return _api_llm_extract(context, info_keys, transcript)


def _converse_decide(context, goal, info_keys, collected, transcript, turn, max_turns) -> dict:
    """Decide next action. Uses API for quality, falls back to local."""
    if CONVERSE_BACKEND == "api":
        return _api_converse_decide(context, goal, info_keys, collected, transcript, turn, max_turns)

    prompt = CONVERSE_PROMPT.replace("{context}", context).replace(
        "{goal}", goal).replace("{info_keys}", info_keys).replace(
        "{collected}", json.dumps(collected, ensure_ascii=False)).replace(
        "{transcript}", transcript).replace("{turn}", str(turn)).replace(
        "{max_turns}", str(max_turns))

    llm = _get_local_llm()
    if llm is None:
        return {"action": "done", "reason": "no local model"}

    try:
        resp = llm.create_chat_completion(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=128, temperature=0.3,
        )
        text = resp["choices"][0]["message"]["content"].strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        return json.loads(text)
    except Exception:
        return {"action": "done", "reason": "llm error"}


def _api_converse_decide(context, goal, info_keys, collected, transcript, turn, max_turns) -> dict:
    """Use DeepSeek API for conversation steering."""
    if not LLM_KEY:
        return {"action": "done", "reason": "no api key"}

    prompt = CONVERSE_PROMPT.replace("{context}", context).replace(
        "{goal}", goal).replace("{info_keys}", info_keys).replace(
        "{collected}", json.dumps(collected, ensure_ascii=False)).replace(
        "{transcript}", transcript).replace("{turn}", str(turn)).replace(
        "{max_turns}", str(max_turns))

    body = json.dumps({
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3, "max_tokens": 128
    }).encode()
    req = urllib.request.Request(LLM_URL, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LLM_KEY}"
    })
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        text = data["choices"][0]["message"]["content"].strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        return json.loads(text)
    except Exception:
        return {"action": "done", "reason": "llm error"}


async def converse(goal: str, info_keys: str, max_turns: int = 5, call_context: str = "") -> dict:
    """Multi-turn autonomous conversation. API LLM drives, returns transcripts."""
    transcripts = []
    collected = {}
    merged_context = f"{LLM_CONTEXT}\n{call_context}" if call_context else LLM_CONTEXT

    for turn in range(1, max_turns + 1):
        if _call_state() != 2:
            return {"transcripts": transcripts, "turns": len(transcripts), "status": "call_ended"}

        last = transcripts[-1] if transcripts else ""
        if last or CONVERSE_BACKEND == "api":
            action = _converse_decide(merged_context, goal, info_keys, collected,
                last.get("caller", "") if isinstance(last, dict) else last,
                turn, max_turns)
        else:
            action = {"action": "ask", "text": "你好，我这边想确认一下信息，请问您现在方便吗？"}

        if action.get("action") == "done":
            break

        callers = [t.get("caller", "") for t in transcripts[-2:]]
        if any(kw in "".join(callers) for kw in ["无法回复", "不太了解", "会转告", "帮你记下", "稍后联系", "我会尽快"]):
            break
        if len(callers) >= 2 and callers[-1] == callers[-2]:
            break

        tts_task = asyncio.create_task(tts_8khz(action.get("text", "")))

        try:
            tts_wav = await asyncio.wait_for(asyncio.shield(tts_task), timeout=2.0)
        except asyncio.TimeoutError:
            filler_task = asyncio.create_task(call_tool("phone_filler", {"type": "thinking"}))
            tts_wav = await tts_task
            await filler_task

        if tts_wav:
            _unload_loopbacks_aggressive()
            proc = await asyncio.create_subprocess_exec(
                "paplay", tts_wav, "--device=" + BT_SINK,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await proc.wait()
            os.remove(tts_wav)

        if BT_SOURCE:
            subprocess.run(["pactl", "set-source-mute", BT_SOURCE, "0"], capture_output=True)
            subprocess.run(["pactl", "suspend-source", BT_SOURCE, "0"], capture_output=True)

        wav = await record_vad(20, 0.8)
        if not wav:
            continue

        # ASR with filler delay
        asr_task = asyncio.create_task(asr_16khz(wav))
        transcript = await _speak_filler_if_slow(asr_task)
        if transcript.strip():
            transcripts.append({"agent": action.get("text", ""), "caller": transcript})

    return {"transcripts": transcripts, "turns": len(transcripts), "status": "ok"}
async def _speak_filler_if_slow(asr_task, delay: float = 2.0):
    try:
        await asyncio.wait_for(asyncio.shield(asr_task), timeout=delay)
        return await asr_task
    except asyncio.TimeoutError:
        await call_tool("phone_filler", {"type": "thinking"})
        return await asr_task


# ASR and Recording
# ═══════════════════════════════════════════════════════════════════════════════

async def asr_16khz(wav_8khz: str) -> str:
    upsampled = tempfile.mktemp(suffix=".wav")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", wav_8khz, "-ar", "16000", upsampled,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await proc.wait()
    os.remove(wav_8khz)
    proc = await asyncio.create_subprocess_exec(
        "whisper", upsampled, "--model", "small", "--language", "zh",
        "--output_format", "txt", "--output_dir", os.path.dirname(upsampled),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        env=clean_env())
    await proc.wait()
    base = upsampled.rsplit(".", 1)[0]
    # whisper may output .json regardless of --output_format flag
    for ext in [".txt", ".json"]:
        path = base + ext
        if os.path.exists(path):
            content = open(path).read().strip()
            os.remove(path)
            if ext == ".json":
                try:
                    data = json.loads(content)
                    texts = [s["text"].strip() for s in data.get("segments", [])]
                    content = "".join(texts)
                except Exception:
                    content = ""
            os.remove(upsampled)
            return content
    os.remove(upsampled)
    return ""


async def record_vad(max_sec: int, silence_sec: float) -> str | None:
    wav = tempfile.mktemp(suffix=".wav")
    proc = await asyncio.create_subprocess_exec(
            "python3", os.path.join(BASE_DIR, "phone_listen_vad.py"),
        "--max-sec", str(max_sec), "--silence-sec", str(silence_sec),
        "--out", wav, stdout=asyncio.subprocess.DEVNULL)
    await proc.wait()
    return wav if proc.returncode == 0 and os.path.exists(wav) else None


# ═══════════════════════════════════════════════════════════════════════════════
# MCP Tools
# ═══════════════════════════════════════════════════════════════════════════════

@server.list_tools()
async def list_tools():
    return [
        Tool(name="phone_dial", description="Dial a phone number",
             inputSchema={"type": "object", "properties": {"number": {"type": "string"}}, "required": ["number"]}),
        Tool(name="phone_hangup", description="End current call",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="phone_check", description="Check call state",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="phone_speak", description="TTS to caller via HSP",
             inputSchema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}),
        Tool(name="phone_ask",
             description="Ask caller a question, record, transcribe, extract structured info. Returns transcript + extracted info. Per-call context merged with system preset.",
             inputSchema={"type": "object", "properties": {
                 "question": {"type": "string"},
                 "info_keys": {"type": "string", "description": "Comma-separated info fields, e.g. 出席,饮食"},
                 "context": {"type": "string", "description": "Per-call context. Merged with PHONE_LLM_CONTEXT system preset."}
             }, "required": ["question", "info_keys"]}),
        Tool(name="phone_converse",
             description="Autonomous multi-turn call. Local LLM drives the conversation, returns all transcripts for agent analysis. No caller text enters agent context.",
             inputSchema={"type": "object", "properties": {
                 "goal": {"type": "string", "description": "Conversation goal, e.g. 确认对方是否出席活动"},
                 "info_keys": {"type": "string", "description": "Comma-separated fields to collect, e.g. 出席,饮食"},
                 "max_turns": {"type": "integer", "description": "Max conversation turns (default 5)"},
                 "context": {"type": "string", "description": "Per-call context. Merged with PHONE_LLM_CONTEXT system preset."}
             }, "required": ["goal", "info_keys"]}),
        Tool(name="phone_filler", description="Play pre-generated filler audio",
             inputSchema={"type": "object", "properties": {
                 "type": {"type": "string", "enum": ["thinking", "timeout", "ack", "repeat", "bye"]}
             }, "required": ["type"]}),
    ]


@server.call_tool()
async def call_tool(name: str, args: dict):
    if name == "phone_dial":
        if not ensure_hsp():
            return [TextContent(type="text", text="bluetooth not connected")]
        number = args["number"]
        adb(f"am start -a android.intent.action.CALL -d tel:{number}")
        for _ in range(20):
            if _call_state() == 2:
                return [TextContent(type="text", text=f"connected {number}")]
            await asyncio.sleep(1)
        return [TextContent(type="text", text=f"dialing {number}...")]

    elif name == "phone_hangup":
        adb("input keyevent KEYCODE_ENDCALL")
        return [TextContent(type="text", text="hung up")]

    elif name == "phone_check":
        s = _call_state()
        state_map = {0: "idle", 1: "ringing", 2: "active"}
        return [TextContent(type="text", text=state_map.get(s, str(s)))]

    elif name == "phone_speak":
        if not ensure_hsp():
            return [TextContent(type="text", text="bluetooth not connected")]
        wav = await tts_8khz(args["text"])
        if not wav:
            return [TextContent(type="text", text="TTS failed")]
        _unload_loopbacks_aggressive()
        proc = await asyncio.create_subprocess_exec(
            "paplay", wav, "--device=" + BT_SINK,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()
        os.remove(wav)
        return [TextContent(type="text", text="spoken")]

    elif name == "phone_ask":
        if not ensure_hsp():
            return [TextContent(type="text", text=json.dumps(
                {"info": {}, "transcript": "", "done": False, "status": "bluetooth_disconnected"},
                ensure_ascii=False))]
        question = args["question"]
        info_keys = args["info_keys"]
        call_context = args.get("context", "")

        merged_context = LLM_CONTEXT
        if call_context:
            merged_context = f"{LLM_CONTEXT}\n{call_context}" if LLM_CONTEXT else call_context

        # Pre-generate TTS while setting up HSP (parallelize)
        tts_task = asyncio.create_task(tts_8khz(question))

        if not ensure_hsp():
            return [TextContent(type="text", text=json.dumps(
                {"info": {}, "transcript": "", "done": False, "status": "bluetooth_disconnected"},
                ensure_ascii=False))]

        wav = await tts_task
        if not wav:
            return [TextContent(type="text", text=json.dumps(
                {"info": {}, "transcript": "", "done": False, "status": "tts_failed"},
                ensure_ascii=False))]

        _unload_loopbacks_aggressive()
        proc = await asyncio.create_subprocess_exec(
            "paplay", wav, "--device=" + BT_SINK,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()
        os.remove(wav)

        await asyncio.sleep(0.3)

        if _call_state() != 2:
            return [TextContent(type="text", text=json.dumps(
                {"info": {}, "transcript": "", "done": False, "status": "call_ended"},
                ensure_ascii=False))]

        if BT_SOURCE:
            subprocess.run(["pactl", "set-source-mute", BT_SOURCE, "0"], capture_output=True)
            subprocess.run(["pactl", "suspend-source", BT_SOURCE, "0"], capture_output=True)

        wav = await record_vad(20, 0.8)
        if not wav:
            return [TextContent(type="text", text=json.dumps(
                {"info": {}, "transcript": "", "done": False, "status": "no_speech"},
                ensure_ascii=False))]

        # Play filler during ASR processing (caller is waiting)
        asr_task = asyncio.create_task(asr_16khz(wav))
        transcript = await _speak_filler_if_slow(asr_task)

        if not transcript.strip():
            return [TextContent(type="text", text=json.dumps(
                {"info": {}, "transcript": "", "done": False, "status": "asr_empty"},
                ensure_ascii=False))]
        result = extract_info(merged_context, info_keys, transcript)
        result["transcript"] = transcript
        result["status"] = "ok"
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    elif name == "phone_converse":
        if not ensure_hsp():
            return [TextContent(type="text", text=json.dumps(
                {"transcripts": [], "turns": 0, "status": "bluetooth_disconnected"},
                ensure_ascii=False))]
        call_context = args.get("context", "")
        result = await converse(
            args["goal"], args["info_keys"],
            args.get("max_turns", 5),
            call_context)
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    elif name == "phone_filler":
        if not ensure_hsp():
            return [TextContent(type="text", text="bluetooth not connected")]
        ft = args["type"]
        wav = os.path.join(BASE_DIR, "phone_fillers", f"{ft}.wav")
        if not os.path.exists(wav):
            return [TextContent(type="text", text=f"filler {ft} not found")]
        _unload_loopbacks_aggressive()
        proc = await asyncio.create_subprocess_exec(
            "paplay", wav, "--device=" + BT_SINK,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()
        return [TextContent(type="text", text=f"filler:{ft}")]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
