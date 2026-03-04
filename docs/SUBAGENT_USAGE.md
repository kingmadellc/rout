# Subagent Spawning via iMessage

## Overview

You can now spawn subagents directly from iMessage that will work in the background and text you when done.

## Commands

### Spawn a Subagent

```
spawn: <task description>
```

or

```
subagent: <task description>
```

**Examples:**

```
spawn: search the web for the latest AI policy news and summarize the key points

subagent: check my calendar for next week and find any scheduling conflicts

spawn: go through my recent emails and flag anything urgent
```

### List Active/Recent Subagents

```
subagents
```

Shows currently running and recently completed subagents.

## How It Works

1. **You send the command** via iMessage with a task description
2. **Watcher spawns the subagent** - creates an isolated session with your task
3. **Subagent works independently** - has full tool access (web search, file read, etc.)
4. **You get an iMessage when done** - subagent will text you with results
5. **10 minute timeout** - if the task takes longer, you'll get notified of timeout

## Technical Details

- **Timeout:** 10 minutes per subagent
- **Notification:** Automatic iMessage to chat_id 1 (your 1:1 with Clawd)
- **Tool access:** Full OpenClaw agent capabilities
- **Isolation:** Each subagent runs in its own session

## Example Flow

**You:** `spawn: find the latest news about the government shutdown and summarize`

**Clawd:** `✅ Subagent spawned! Task: find the latest news... You'll get an iMessage when it's done.`

*(5 minutes later, via iMessage)*

**Clawd:** `✅ Task complete: Shutdown continues, no deal reached. Dems holding firm on ICE reform. Latest talks scheduled for Feb 26. G30/G35 positions still favored to profit.`

## Testing

To test the functionality:

```
spawn: count to 10, then tell me you're done
```

Should spawn a simple subagent that completes quickly and texts you confirmation.

## Limitations

- One task per spawn (no multi-step workflows yet)
- 10 minute hard timeout
- iMessage delivery only (no other channels)
- No interactive back-and-forth during task execution

## Future Enhancements

- [ ] Custom timeout per task
- [ ] Multi-channel delivery (Discord, Telegram, etc.)
- [ ] Interactive mode with mid-task updates
- [ ] Scheduled subagents (spawn at specific time)
- [ ] Subagent chaining (one spawns another)
