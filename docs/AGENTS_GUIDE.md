# Rout Development Agents — Usage Guide

## The 4 Agents

| Agent | Skill Name | When to Call |
|-------|-----------|-------------|
| **Integration Scaffolder** | `rout-integration-scaffolder` | Adding any new service, API, or data source to Rout |
| **Pipeline Specialist** | `rout-pipeline-specialist` | Message delivery issues, BlueBubbles, group chat, formatting |
| **Deploy & Ops** | `rout-deploy-ops` | Deploying features, launchd management, config, releases |
| **Debug Tracer** | `rout-debug-tracer` | Any bug, error, or "it's not working" situation |

## How to Use These

### In Cowork / Claude Code

These skills live in your `OpenClaw Skills/` folder. To make them available to Claude:

1. **Install as skills** — Copy each folder into your active skills directory:
   ```bash
   cp -r "OpenClaw Skills/rout-integration-scaffolder" ~/.claude/skills/
   cp -r "OpenClaw Skills/rout-pipeline-specialist" ~/.claude/skills/
   cp -r "OpenClaw Skills/rout-deploy-ops" ~/.claude/skills/
   cp -r "OpenClaw Skills/rout-debug-tracer" ~/.claude/skills/
   ```

2. **Or reference in CLAUDE.md** — Add to your project's CLAUDE.md:
   ```markdown
   ## Available Rout Skills
   When working on Rout, read the appropriate skill before starting:
   - New integration: Read `OpenClaw Skills/rout-integration-scaffolder/SKILL.md`
   - Message pipeline: Read `OpenClaw Skills/rout-pipeline-specialist/SKILL.md`
   - Deployment/ops: Read `OpenClaw Skills/rout-deploy-ops/SKILL.md`
   - Debugging: Read `OpenClaw Skills/rout-debug-tracer/SKILL.md`
   ```

### Orchestration Pattern (Primary Agent Delegates)

The highest-leverage pattern: your primary coding agent reads the right skill BEFORE doing work. Here's how the delegation should flow:

**"Add Spotify to Rout"**
→ Primary reads `rout-integration-scaffolder/SKILL.md`
→ Scaffolds all 6 components following the pattern
→ Then reads `rout-deploy-ops/SKILL.md` for the deploy checklist

**"Messages aren't arriving in group chat"**
→ Primary reads `rout-debug-tracer/SKILL.md`
→ Runs the Layer Isolation diagnostic
→ If it's a pipeline issue, reads `rout-pipeline-specialist/SKILL.md` for deep context

**"Deploy the morning brief"**
→ Primary reads `rout-deploy-ops/SKILL.md`
→ Produces and executes the deploy checklist

**"Polymarket prices are wrong"**
→ Primary reads `rout-debug-tracer/SKILL.md`
→ Identifies it's Layer 4 (Execution)
→ Reads `rout-pipeline-specialist/SKILL.md` if it turns out to be a formatting/delivery issue instead

### Multi-Agent Chains (Common Workflows)

| Workflow | Agent Chain |
|---------|------------|
| Build + ship new integration | Scaffolder → Deploy & Ops |
| Fix a broken integration | Debug Tracer → (Pipeline Specialist if transport) → Deploy & Ops (restart) |
| Add morning brief section | Scaffolder (brief hook) → Deploy & Ops (restart brief service) |
| Proactive monitor for new service | Scaffolder (monitor code) → Deploy & Ops (new plist + deploy) |
| Release a new version | Deploy & Ops (tag + README + push) |
| "It worked yesterday, broke today" | Debug Tracer (layer isolation) → whatever layer is broken |

## Keeping Agents Current

These agents are trained on Rout's architecture as of v0.8. When you ship changes that affect the patterns, update the relevant skill:

- **New handler convention?** → Update Integration Scaffolder
- **Transport layer change?** → Update Pipeline Specialist
- **New service or config pattern?** → Update Deploy & Ops
- **New failure mode discovered?** → Update Debug Tracer's Known Gotchas

The agents compound in value as you add to them. Every bug you fix, every integration you ship — encode the lesson back into the skill.

## What These Agents Are NOT

- They are not autonomous runners. They're knowledge-loaded specialists that need a coding agent (or you) to execute.
- They don't have access to your Mac Mini. They produce the exact commands and code — you or your primary agent runs it.
- They don't replace understanding the code. They accelerate someone who already gets the architecture.
