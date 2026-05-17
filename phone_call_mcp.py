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
LLM_BACKEND = os.environ.get("PHONE_LLM_BACKEND", "local")  # "local" or "api"
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
EXTRACT_PROMPT = """从对话中提取以下信息，只返回JSON，不要解释。

{context}

需要提取: {info_keys}

对话内容: {transcript}

返回格式:
{{"info": {{"出席": "是/否", "饮食": "具体内容"}}, "done": true}}"""

server = Server("phone-call")


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
    r = subprocess.run(["pactl", "list", "short", "modules"], capture_output=True, text=True)
    for line in r.stdout.split("\n"):
        if "module-loopback" in line:
            mod_id = line.split()[0]
            subprocess.run(["pactl", "unload-module", mod_id], capture_output=True)


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

    if BT_SOURCE:
        subprocess.run(["pactl", "set-source-mute", BT_SOURCE, "1"], capture_output=True)

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


# ═══════════════════════════════════════════════════════════════════════════════
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
    txt = upsampled.rsplit(".", 1)[0] + ".txt"
    result = open(txt).read().strip() if os.path.exists(txt) else ""
    os.remove(upsampled)
    if os.path.exists(txt):
        os.remove(txt)
    return result


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
        Tool(name="phone_filler", description="Play pre-generated filler audio",
             inputSchema={"type": "object", "properties": {
                 "type": {"type": "string", "enum": ["thinking", "timeout", "ack", "repeat", "bye"]}
             }, "required": ["type"]}),
    ]


@server.call_tool()
async def call_tool(name: str, args: dict):
    if name == "phone_dial":
        number = args["number"]
        adb(f"am start -a android.intent.action.CALL -d tel:{number}")
        for _ in range(20):
            state = adb("dumpsys telephony.registry | grep mCallState")
            if "mCallState=2" in state:
                return [TextContent(type="text", text=f"connected {number}")]
            await asyncio.sleep(1)
        return [TextContent(type="text", text=f"dialing {number}...")]

    elif name == "phone_hangup":
        adb("input keyevent KEYCODE_ENDCALL")
        return [TextContent(type="text", text="hung up")]

    elif name == "phone_check":
        out = adb("dumpsys telephony.registry | grep mCallState", timeout=5)
        for line in out.split("\n"):
            if "mCallState=" in line:
                s = line.split("=")[1].strip()
                return [TextContent(type="text", text={"0": "idle", "1": "ringing", "2": "active"}.get(s, s))]
        return [TextContent(type="text", text="unknown")]

    elif name == "phone_speak":
        if not ensure_hsp():
            return [TextContent(type="text", text="bluetooth not connected")]
        wav = await tts_8khz(args["text"])
        if not wav:
            return [TextContent(type="text", text="TTS failed")]
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

        await call_tool("phone_speak", {"text": question})
        await call_tool("phone_filler", {"type": "thinking"})

        await asyncio.sleep(0.5)
        wav = await record_vad(20, 0.8)
        if not wav:
            return [TextContent(type="text", text=json.dumps(
                {"info": {}, "transcript": "", "done": False, "status": "no_speech"},
                ensure_ascii=False))]

        transcript = await asr_16khz(wav)
        result = extract_info(merged_context, info_keys, transcript)
        result["transcript"] = transcript
        result["status"] = "ok"
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    elif name == "phone_filler":
        if not ensure_hsp():
            return [TextContent(type="text", text="bluetooth not connected")]
        ft = args["type"]
        wav = os.path.join(BASE_DIR, "phone_fillers", f"{ft}.wav")
        if not os.path.exists(wav):
            return [TextContent(type="text", text=f"filler {ft} not found")]
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
