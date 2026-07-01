# Twitter/X launch package

Two artifacts:
1. **Single-tweet version** — for one-shot posting / quote-retweet bait
2. **Thread version (6 tweets)** — for max engagement when you're online

Each tweet <= 280 characters, hand-counted.

---

## Single-tweet version

> "Context compacted" — the 2 words every Claude Code user dreads.
>
> vMem: persistent context that survives compaction.
> OS context management (kswapd, mlock, demand paging) for AI agents.
>
> Single SQLite file. MCP-native. Multi-agent shared. MIT.
>
> https://github.com/soolaugust/vMem

---

## Thread version (6 tweets)

### 1/6 — the hook (the pain everyone knows)

> If you use Claude Code, you know this screen:
>
> "⚠️ Auto-compact: conversation approaching context limit..."
>
> Hours of decisions, constraints, architectural context — gone.
> Next session? Start from zero.
>
> This is the #1 productivity killer in AI-assisted coding.

### 2/6 — why existing solutions don't work

> "Just use a vector store for memory" — that's what mem0, Letta, Zep do.
>
> They optimize for "find similar." But they can't:
> - Guarantee a constraint is NEVER evicted
> - Share state across multiple agents
> - Handle capacity pressure gracefully
>
> Memory isn't search. It's resource management.

### 3/6 — the insight

> OS engineers solved this 40 years ago.
>
> RAM ↔ context window
> Disk ↔ knowledge base
> Demand paging ↔ on-demand retrieval
> kswapd ↔ capacity-aware eviction
> mlock ↔ pin, never evict
>
> Same problem. Same solutions transfer.
> That's what vMem is.

### 4/6 — what it does

> vMem: OS-managed context continuity for Claude Code.
>
> - Memories persist OUTSIDE the context window
> - Compaction hits? Working set auto-restores in <100ms
> - Pin critical constraints — guaranteed never evicted
> - Multi-agent: one SQLite file, shared memory
> - MCP-native: works with Claude Code, Cursor, any agent
> - 3,500+ tests

### 5/6 — try it

> One-line install in Claude Code:
>
>     /install-plugin github:soolaugust/vMem
>
> Or:
>
>     git clone https://github.com/soolaugust/vMem
>     pip install -e .
>
> v0.1.0 just shipped. MIT licensed.

### 6/6 — the bet

> Context compaction is going away as a problem in 2026.
>
> Not because models get infinite context — but because memory
> infrastructure makes context windows feel infinite.
>
> Zero compact. Infinite memory.
>
> https://github.com/soolaugust/vMem

---

## Posting tactics

- **Pin the thread** to your X profile after posting.
- **Post Tuesday 09:00-11:00 ET** (peak dev-Twitter window).
- **Reply to your own tweet 30 min later** with the release link for a small
  algorithmic bump.
- **DM the thread to 3-5 people** in the Claude Code / AI agent space who
  might QT — especially people who've publicly complained about compaction.
- **Cross-post to LinkedIn** in long-form (paste tweets 1+3+4 stitched
  together).
- After Show HN goes up, **quote-tweet** your own thread with "now on HN: <link>".

## Hashtags (use sparingly — max 2)

Best signal-to-noise:
- `#ClaudeCode` — exact target audience
- `#AIAgents` — niche but precise
- `#buildinpublic` — gets community boosts

Avoid: `#AI` (too noisy), `#OpenSource` (too generic).

---

## Single-tweet alternates (for A/B'ing or reposting)

> "Context compacted" happens because critical knowledge lives only in
> the context window. Move it outside, and compaction becomes invisible.
>
> vMem — OS-grade persistent context for LLM agents.
>
> https://github.com/soolaugust/vMem

> Hot take: "infinite context window" is the wrong solution.
>
> The right solution: persistent context that makes window size irrelevant.
>
> vMem: demand paging, kswapd eviction, mlock pinning for AI.
>
> https://github.com/soolaugust/vMem

> I fixed Claude Code's compaction problem.
>
> Not by making the context bigger. By making memory persistent.
>
> vMem — one SQLite file, MCP-native, OS-managed context continuity.
>
> https://github.com/soolaugust/vMem
