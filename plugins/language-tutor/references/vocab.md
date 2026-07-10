# Vocab — spaced-repetition review

Reviews due words and reschedules them. The SM-2-lite rules below are the single source of truth — chat mode and the flashcards artifact must apply them identically.

## Procedure

1. **Query due cards** (SQL on the Vocabulary data source): Course matches, Status in (Learning, Mature), `date:Next Review:start` ≤ today. Order by most overdue first; cap at ~20 per session (mention the remainder).
2. **Mode:**
   - ≤ 8 cards → quick chat drill, no artifact: prompt with Meaning → learner produces the word (or reverse), grade each Again/Hard/Good/Easy.
   - Otherwise → `assets/flashcards.html` with CONFIG: `payload.cards` = [{word, romanization, meaning, example, pageUrl, interval, ease, reps, lapses}], `notion.vocabDataSource`, all labels in the instruction language.
3. **Grade → update (SM-2-lite):**
   - **Again:** Reps = 0 · Lapses +1 · Interval = 1 · Ease = max(1.3, Ease − 0.20) · Status = Learning
   - **Hard:** Interval = max(1, round(Interval × 1.2)) · Ease = max(1.3, Ease − 0.15)
   - **Good:** Reps +1 · Interval = (Interval ≤ 1 ? 3 : round(Interval × Ease))
   - **Easy:** Reps +1 · Interval = round(max(Interval, 1) × Ease × 1.3) · Ease += 0.15
   - Always: `Next Review` = today + Interval. Interval ≥ 21 → Status = Mature.
4. **Writeback:** the artifact writes back at the end of the session via its in-artifact API call with the Notion MCP server, chunked into ~8–10 cards per call (in-artifact `max_tokens` is small); on any failed chunk it shows the recap screen for the learner to paste into chat, and the tutor applies the updates. In chat mode the tutor batches the updates directly.
5. **Struggling words** (Lapses ≥ 4): stop and help — build a mnemonic, a fresh personal example (artifact has a button for this via its Claude call), or offer to Suspend the card.
6. **Log:** Type Vocab, Summary = "N reviewed, X again/Y good…", Next Step unchanged from before unless reviews revealed a gap worth a consolidation note.

Never review words whose Next Review is empty — they haven't been taught yet.
