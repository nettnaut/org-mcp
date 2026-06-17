# Using org-mcp — how to ask Claude to work with your notes & tasks

This is a guide for *humans*. Once org-mcp is installed (see the
[README](README.md)), you don't call any tools or type commands — you just
describe what you want in plain language, and Claude picks the right action and
runs it against your **live Emacs**. The notes and tasks it touches are your
real ones: same files, same agenda, same TODO keywords.

## The one thing to know

Talk to Claude the way you'd ask a capable assistant who can see your notes:

> "Find my note on the deployment runbook and remind me what the rollback step
> was."

That single sentence is enough — Claude searches, opens the right note, and
answers. You don't need to name files or IDs (though you can; see *Being
precise* below).

## What you can ask for, with examples

### Find a note
When you remember roughly what it's *called* or *tagged*:
- "Do I have a note about Kubernetes retries?"
- "Show me my notes tagged 'devops'."
- "Find the note titled something like 'onboarding checklist'."

When you only remember *a phrase that's in the text*:
- "Which of my notes mentions 'retry budget'?"
- "Search my notes for anywhere I wrote about the staging database."

Tip: if a title/tag search comes up empty, say "try the full text" and Claude
searches note bodies instead.

### Read or summarise a note
- "Open my 'Architecture decisions' note and summarise it."
- "What does my note on the release process say about hotfixes?"

You can refer to a note by its title — no ID needed.

### Capture a task
- "Add a task: email the vendor about pricing."
- "Capture a to-do to review the contract before Friday, tag it work."
- "Throw 'book flights' into my someday list."

New tasks land in your inbox by default; name a bucket (inbox / agenda / notes /
someday) to put it elsewhere.

### Update or schedule a task
- "Mark the vendor email task as NEXT and schedule it for Friday."
- "Set a deadline of next Monday on the contract review."
- "That task is blocked — set it to WAITING."
- "Move 'book flights' into my agenda file."

If several tasks match, ask Claude to list candidates first.

### See what's on your plate
- "What's on my agenda?"
- "Show me everything marked NEXT."
- "Run my weekly review."

Claude reads your own custom agenda views, so this matches what you'd see in
Emacs. From there: "mark the second one done" works.

### Create a note
- "Make a note titled 'Vendor comparison' with these three options: …"
- "Start a note capturing what we just decided, tag it 'project'."

### Link notes / find what links where
- "Link my 'Vendor comparison' note to the 'Procurement process' note."
- "What other notes point to my 'Architecture decisions' note?"

### Log to your journal
- "Add to today's journal: decided to go with the wrapper-script approach."
- "Log in my journal for June 20 that the client signed."

## First-time setup tip: backfill IDs

If you have **existing org tasks created before installing org-mcp**, they won't
have the per-heading ID that task updates rely on. Ask once:

> "Back-fill IDs on all my tasks — do a dry run first."

Claude runs `org_ensure_todo_ids` (dry run = count only), then for real. After
that, every task can be updated by Claude; new tasks it captures get an ID
automatically.

## Chaining goals in one ask

- "Find my note on vendor pricing, capture a task to follow up next week, and
  link it back to that note."
- "Summarise yesterday's journal and add a NEXT task for anything unfinished."

## What Claude will and won't do on its own

- **Reading** (search, open, summarise, agenda) is safe and immediate.
- **Writing** (new notes/tasks, journal entries, links, ID backfill) happens
  when you ask. If a request is ambiguous, Claude checks rather than guesses.
- Everything stays inside your org / org-roam directories — the server cannot
  reach files elsewhere on your machine.

## Being precise (optional)

Plain language is enough, but to remove all doubt you can:
- Quote the **exact note title**: "open the note titled 'Release process'".
- Name a **tag** to narrow a search: "search my 'work' notes for …".
- Give a **specific date**: "log this in my 2026-06-20 journal", "schedule it
  for 2026-06-20".
- Name the **task state**: NEXT, TODO, WAITING, SOMEDAY, DONE, CANCELLED.

## If something doesn't work

Everything runs through your live Emacs, so it only works while Emacs is running
(server / daemon started). If Claude says it can't reach your notes, make sure
Emacs is up and ask again.
