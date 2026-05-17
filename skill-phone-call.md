---
name: phone-call
description: Make real phone calls through Android ADB + Bluetooth HSP. Use this skill whenever the user asks to make a phone call, dial a number, call someone, notify someone by phone, confirm information over the phone, or any task involving voice calls. Also use when you encounter phone_call MCP tools (phone_dial, phone_converse, phone_ask, phone_speak, phone_hangup, phone_check, phone_filler) — this skill explains when and how to use each correctly.
---

# Phone Agent — Real Phone Calls for OpenCode

## Overview

You can make **real voice calls** through a connected Android phone. The MCP server (phone-call) handles Bluetooth HSP audio routing, TTS, VAD-based recording, ASR transcription, and LLM-driven conversation steering. You orchestrate which tools to call and how to interpret the results.

**Platform target**: OpenCode Agent on a Linux machine with ADB + Bluetooth HSP connected to an Android phone.

## Tool Reference

| Tool | Purpose | When to Use |
|------|---------|-------------|
| `phone_dial` | Dial a number. Optionally pre-generates opening TTS to play on connect. | Every call starts here. |
| `phone_hangup` | End the current call. | Always call after conversation completes. |
| `phone_check` | Check call state (idle/ringing/active). | Before any call, or to verify hangup. |
| `phone_speak` | Speak TTS directly to the caller. | When you need to say something simple without ASR/recording. |
| `phone_ask` | Single turn: speak + record + ASR + extract. | One-off question with structured info extraction. |
| `phone_converse` | Multi-turn autonomous conversation driven by API LLM. | Full conversations: confirm attendance, notify, collect info. |
| `phone_filler` | Play a pre-generated filler phrase. | Rarely needed directly — `phone_converse` handles fillers automatically. |

### Tool Signatures

```
phone_dial(number: string, opening?: string)          → "connected <number>" or "bluetooth not connected"
phone_hangup()                                         → "hung up"
phone_check()                                          → "idle" | "ringing" | "active"
phone_speak(text: string)                              → (TTS plays over HSP)
phone_ask(question: string, info_keys: string, context?: string)
                                                       → {transcript, ...info_fields}
phone_converse(goal: string, info_keys: string, max_turns?: int, skip_opening?: bool, context?: string)
                                                       → {transcripts: [{agent, caller}], turns, status, done_reason}
phone_filler(type: "thinking" | "timeout" | "ack" | "repeat" | "bye")
                                                       → (pre-generated audio plays)
```

## Core Patterns

### Pattern 1: Two-Step Natural Conversation (RECOMMENDED)

**Use this for every phone_converse call.** The opening text is pre-generated during dialing and plays immediately on connect — zero latency between pickup and first words. The LLM then picks up the conversation from the caller's response.

```
// Step 1: Dial with opening. TTS generates in background during dialing.
phone_dial(
  number: "13800138000",
  opening: "您好，我是XX公司的AI助手，想跟您确认一下明天的活动您能否参加？"
)

// Step 2: Autonomous conversation. skip_opening=true because opening already played.
result = phone_converse(
  goal: "确认对方明天下午能否参加活动",
  info_keys: "出席",
  context: "活动时间是明天下午3点，地点在三号会议室。",
  max_turns: 4,
  skip_opening: true
)

// Step 3: Hang up
phone_hangup()
```

**Why this pattern matters:**
- Without `opening`, the first TTS is generated AFTER the call connects, creating a 2-5s silence before the caller hears anything. They may hang up.
- With `opening`, TTS is generated during the dialing ringtone. The caller hears you the instant they pick up.
- `skip_opening=true` tells phone_converse to skip its own first-turn TTS and go straight to recording the caller's response. The LLM then responds naturally based on what the caller said.

### Pattern 2: Single-Question Ask

**Use this when you only need one answer.** No multi-turn dialogue needed.

```
phone_dial(number: "13800138000")
result = phone_ask(
  question: "请问您的快递是放在前台还是送到楼上？",
  info_keys: "放置位置",
  context: "收件人张先生"
)
phone_hangup()
```

### Pattern 3: Direct Speak

**Use this for one-way announcements.** No recording, no ASR, just speak and hang up.

```
phone_dial(number: "13800138000")
phone_speak("您的快递已到达一楼前台，请及时领取。")
phone_hangup()
```

### Pattern 4: phone_ask → phone_converse escalation

If phone_ask gets an ambiguous answer that needs follow-up, escalate to phone_converse:

```
result = phone_ask(question: "...", info_keys: "确认", context: "...")
// result.确认 is ambiguous → need more context
phone_converse(goal: "...", info_keys: "...", skip_opening: true, ...)
```

Note: Don't hang up between phone_ask and phone_converse — the call is still active.

## Conversation Design

### Writing the Opening Text

The opening is the caller's first impression. Make it count:

1. **Identify yourself** ("您好，我是XX公司的AI助手")
2. **State the purpose** ("想跟您确认一下...")
3. **End with a clear question** that invites a response

**Good openings:**
- "您好，我是南京青赋驭境的AI助手，想跟您确认一下明天下午的评审会您这边能参加吗？"
- "您好，这边是快递驿站，您的包裹到了，请问放前台可以吗？"

**Bad openings:**
- "你好" — too brief, caller confused about who's calling
- "您好我是XX公司的客服代表请问您现在方便接听电话吗" — too long, overwhelming

### Setting goal, info_keys, context

- **`goal`**: One sentence in Chinese describing what you want to accomplish. This goes to the LLM that steers the conversation. Be specific.
  - ✅ "确认王经理是否收到周五会议延期通知"
  - ❌ "问一下"
- **`info_keys`**: Comma-separated field names (Chinese). These are what you want to extract from the conversation.
  - ✅ "出席,饮食,人数"
  - ❌ "info" — too vague
- **`context`**: Additional background that helps the LLM handle edge cases. Merged with the system `PHONE_LLM_CONTEXT` preset.
  - ✅ "对方之前表示可能无法参加，请确认最终决定。备用时间：周六下午。"

### Choosing max_turns

| Scenario | Recommended max_turns |
|----------|----------------------|
| Simple notification (yes/no) | 2 |
| Confirmation with one follow-up | 3 |
| Collecting multiple pieces of info | 4-5 |
| Sensitive/negotiation topics | 1 (phone_ask instead) |

The stop mechanism will typically end the call before max_turns is reached. Setting it too high is fine — it's a safety cap, not a target.

## Stop Mechanism

phone_converse has a **4-layer stop mechanism**. The conversation ends when ANY layer triggers:

| Layer | Trigger | Example |
|-------|---------|---------|
| 1. Model `done` | LLM decides conversation is complete | "好的，我会参加。" → model outputs `done` |
| 2. Keyword | Caller uses a deflection/phrase that signals unwillingness to continue | "帮你记一下", "打错了", "稍后联系", "尽快", "马上" |
| 3. Dedup | Same caller response twice in a row | "喂？" → "喂？" (caller not engaging) |
| 4. max_turns | Hard safety limit reached | More turns than expected |

**Interpreting results:**

```
{
  "transcripts": [
    {"agent": "您好，请问明天...", "caller": "好的，我去。"},
  ],
  "turns": 1,
  "status": "ok"
}
```

- `status: "ok"` → conversation completed naturally (layer 1 or 2)
- `status: "call_ended"` → caller hung up mid-conversation
- `done_reason: "callback: ..."` → model couldn't answer something and offered to call back. Read the reason for what needs confirming, then tell the user to research it before redialing.
- `turns: 1` with single transcript → short answer, stop triggered quickly
- `agent: "(opening from phone_dial)"` → skip_opening was used (opening not shown in transcript)

**After the call, always synthesize:** Read the transcripts, extract the relevant fields, and tell the user what happened in natural language. Don't just dump the raw JSON.

**Callback pattern:** When `done_reason` starts with `"callback:"`, the model encountered something it didn't know. Tell the user: "对话中遇到了未确认的信息，建议确认后回电。" Then show the transcribed conversation so the user can see what needs research.

## Callback Flow

When the model hits unknown information, it will say "这个我不确定，我确认后再回复您" and return `done_reason: "callback: ..."`. As the orchestrating agent, you should:

1. Report the reason to the user
2. Offer to research the answer (look up database, check calendar, ask user)
3. When ready, redial with enhanced context:
```
phone_dial(number, opening: "您好，刚才关于XXX的问题，我已经确认了...")
phone_converse(goal: "补充确认XXX", info_keys: "...", context: "刚才回电，补充信息：YYY。")
```

## Filler and Timing

phone_converse automatically handles filler phrases — you rarely need to use `phone_filler` directly.

**How it works:**
- After recording ends, TTS generation starts. If TTS takes >2 seconds, a "请稍等，让我思考一下。" filler plays to bridge the gap.
- The first turn NEVER plays a filler (caller just picked up, filler would be jarring).
- If TTS finishes while filler is playing, the system waits for filler to complete before speaking — no audio overlap.

**Filler types (for manual use only):**

| Type | Text | Trigger |
|------|------|---------|
| thinking | 请稍等，让我思考一下。 | Auto: TTS >2s delay |
| timeout | 喂，您还在吗？ | Manual: caller silent too long |
| ack | 好的，明白了。 | Manual: confirmation |
| repeat | 不好意思，我没听清楚… | Manual: need replay |
| bye | 好的，谢谢您，再见。 | Manual: graceful exit |

## Error Handling

### Bluetooth not connected
`phone_dial` returns `"bluetooth not connected"`. The HSP connection is checked before every dial. If this happens, the phone's Bluetooth needs to be reconnected. Tell the user: "Phone Bluetooth is disconnected. HSP connection required — check the phone's Bluetooth is on and paired."

### Call dropped mid-conversation
phone_converse returns `status: "call_ended"` with partial transcripts. Report what was gathered before the drop:
```
"Call disconnected after 2 turns. I collected: 出席=待确认. The caller hung up before giving a final answer."
```

### Empty transcript / hallucination
ASR can produce empty results or hallucinated text (e.g., "字幕by索兰娅"). phone_converse filters these automatically. If transcripts come back empty, the call likely failed. Don't fabricate results — report honestly.

### Max turns reached without stop
This means the LLM couldn't extract the needed info. Review the transcripts to understand why:
- Caller repeatedly deflected? → Goal might be too ambitious for this call
- ASR mis-transcribing? → Try repeating the question differently
- Model confused by context? → Simplify the goal and context

## Common Mistakes

### Skipping the `opening` parameter
Without pre-generated opening, the caller hears 2-5 seconds of silence after picking up. They may think it's a spam call and hang up. Always use `phone_dial(opening: "...")` for outbound calls.

### Forgetting to set `skip_opening=true`
If you pass `opening` to phone_dial but don't set `skip_opening` in phone_converse, the first turn will generate ANOTHER opening and speak it — the caller hears the opening twice.

### Using phone_ask instead of phone_converse for multi-turn
phone_ask is single-turn only. If the caller's answer needs clarification, you can't follow up. Use phone_converse for anything that might need back-and-forth.

### Not hanging up
`phone_converse` does NOT hang up automatically. Always call `phone_hangup()` after the conversation completes, even if the status is "done" or "call_ended". Leaving a call active wastes resources.

### Maxing out max_turns
Setting `max_turns: 10` is unnecessary — the stop mechanism will naturally end most conversations within 3-4 turns. Higher values just delay the safety cap if something goes wrong.

### Not checking transcripts before claiming success
`status: "ok"` means the conversation ended cleanly, NOT that you got the information you wanted. Always read the `transcripts` array and verify the caller actually confirmed/committed.

## Red Flags

**Never:**
- Pass `opening` to `phone_dial` without setting `skip_opening=true` in the subsequent `phone_converse`
- Call `phone_converse` without first dialing (except when escalating from phone_ask)
- Hang up between `phone_ask` and a follow-up `phone_converse` — the call is still active
- Fabricate results when transcripts are empty or hallucinated
- Use `phone_speak` for multi-turn conversations — the caller can't respond
- Assume `status: "ok"` means "got the info" — always read the transcripts

**Always:**
- Use the two-step pattern: `phone_dial(opening)` → `phone_converse(skip_opening=true)` → `phone_hangup()`
- Check `phone_check()` before dialing if unsure about state
- Synthesize transcripts into natural language summary for the user
- Handle `status: "call_ended"` gracefully — partial results are still valuable
- Set `max_turns` based on conversation complexity (2-5 is the sweet spot)
