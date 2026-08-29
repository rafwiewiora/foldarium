# Feature backlog

This is the durable running list of potential Foldarium features. Items remain
here until they are promoted into an implementation plan or explicitly declined.

Statuses: **idea**, **candidate**, **planned**, **in progress**, **shipped**, or
**declined**.

## In progress

### Retrospective Play for fun

- **Status:** in progress
- **Added:** 2026-08-28
- **Goal:** let a player launch any published Weekly from its retrospective
  detail page and replay the original blind choices without crystal overlays.
- **Leaderboard contract:** post-reveal answers remain physically separate from
  blind-week ballots and all-time rankings. The retrospective and in-quiz
  leaderboards display opted-in player scores in a separately labeled
  **For fun** group.

## Candidate features

### Apo-pocket structural similarity

- **Status:** candidate
- **Added:** 2026-08-28
- **Goal:** distinguish a genuinely new pocket geometry from a known apo pocket
  that has no prior ligand-bound training analogue.
- **Motivating example:** `37HF` has highly familiar PCSK9 structures and an
  AZD0780-compatible pocket across multiple apo structures, but no eligible
  pre-cutoff ligand occupied that pocket. The current ligand-bound scorer
  therefore reports no usable training analogue.
- **Possible approach:** compare the released query pocket with pre-cutoff
  ligand-free protein structures after structural alignment, calibrate an
  independent threshold, and report it alongside—not in place of—the canonical
  protein-frame overlap and RnP-style ligand/pocket metrics.
- **Important distinction:** report protein familiarity, apo-pocket familiarity,
  and ligand-bound-system familiarity separately.
