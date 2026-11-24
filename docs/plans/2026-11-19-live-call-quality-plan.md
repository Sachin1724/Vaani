# Live call quality: latency, overlap, language, voice, names

Date: 2026-11-19. Scope: the voice-order CRM (`gateway/`), live mic path, Odia ASR, studio TTS.
Reference: the Fish Audio phone-ordering demo (53 s clip). Everything in "What the demo does" below that came from
a third-party write-up (architecture names, the on-screen latency numbers, a Spanish greeting) is **unverified** by us;
only the transcript was checked. Plan against behaviours, not against their stack.

## Where we are (measured today)

| Stage | Now | Source |
|---|---|---|
| End-of-speech wait (silence before we act) | **1.1 s fixed** (`END_SILENCE_S`) | `live_voice_crm_server.py` |
| Odia ASR (IndicConformer, beam) | 55-120 ms | server log `heard: ... (N ms)` |
| Order logic | 0.1-3.6 ms | server log `FSM=` |
| TTS, first sentence ready | 0.2-1.0 s (first call after start is slowest) | server log `time to first audio` |
| Female voice pace | 11.8 ch/s (human median 14.0, slowest quarter 11.6) | `score_pronunciation.py --sweep` |
| Pronunciation error (female) | CER 0.95-1.2 % | same |

Time from the caller stopping to hearing Mira is therefore about **1.1 s waiting + 0.1 s ASR + 0.3-1.0 s TTS = 1.5-2.2 s**.
The demo shows ~0.3-0.4 s. The wait for silence is two thirds of our gap, and it is not a model problem.

Modules that already exist in the repo but are **not wired into the live path** (and whose real-world accuracy has never been measured):
`engine/blind_source_separation.py`, `engine/acoustic_lid_router.py`, `engine/semantic_turn_taking.py`,
`engine/conversational_cues.py`, `engine/enrollment.py` (speaker embeddings). `PROGRESS_REPORT.md` already records earlier
claims that did not hold up, so each phase below starts by measuring, and nothing is called done on a self-report.

## What the demo does (from our own transcript)

Switches language when asked; one- or two-sentence replies; reads back a multi-person order; handles "change X to large and add five cokes" in one turn;
asks a spelling question for the name; says "give me a second" while working; ends with a delivery time.
Already built here: edit-in-one-turn, read-back, name confirm, delivery time, barge-in, generation ids.

## Phase 1 - Latency (do first; biggest win, no new models)

Goal: time from end of speech to first audible sound **under 0.8 s typical, under 1.0 s worst** (a hard 0.35 s is not reachable with this TTS; first sentence alone is 0.2-0.3 s).

1. **Measure first.** Add one timeline log per turn: speech-end, ASR done, reply built, first chunk sent, first chunk playing (browser reports back). Acceptance: a per-turn table in the log; we stop guessing.
2. **Stop waiting for silence when the thought is done** (optimal stopping, simple form). Run the existing greedy ASR (`beam=False`, ~5 ms) on the growing buffer every 200 ms. End the turn after **0.45 s** of silence if the partial text parses as a complete order or answer (item+quantity, yes/no, digits of the expected length); keep **1.1 s** if it ends on a conjunction or looks unfinished (`semantic_turn_taking.py` already lists these). Acceptance: median wait drops from 1.1 s to about 0.5 s; no increase in cut-off sentences on the 20-utterance recording set (Phase 0 below).
3. **Pre-rendered acknowledgements.** Render once at startup, from the female voice, a small set of short lead-ins ("ହଁ", "ଠିକ୍ ଅଛି", "ଆଜ୍ଞା", "ଏକ ମିନିଟ୍") and play one immediately when the turn ends while the real reply renders. Perceived response becomes ~0.1 s. Acceptance: reply never starts with the same filler twice in a row (already true for text; extend to audio).
4. **Warm the models** at server start (TTS dummy synth, ASR dummy decode) so the first customer is not the slow one (first TTS call is currently ~1 s vs ~0.25 s).

Risk: shorter endpointing cuts people off. Mitigation: the partial-text completeness check, and a single "continue" window: if speech resumes within 0.6 s after we ended, merge it into the same turn.

## Phase 0 - Test set (small, do alongside Phase 1)

Record **20 real utterances** from you (orders, edits, yes/no, OTP digits, a name, two overlapping speakers, one English sentence) into `tests/fixtures/live/` with the expected text. Every later acceptance number is computed on this set, not on synthetic audio. Without it, "better" is an opinion.

## Phase 2 - Overlap and "one by one"

Goal: when two people talk at once, Mira says so instead of mis-ordering, then (stretch) takes both.

1. **Detect overlap** cheaply: `blind_source_separation.py`'s overlap-energy detector on live audio, plus a speaker-embedding change within one utterance (`enrollment.py` ERes2Net model is already on disk). Acceptance: on the two-speaker fixtures, flagged overlap >= 90 %; on single-speaker fixtures, false flags <= 5 %.
2. **Respond**: "ଦୟାକରି ଗୋଟିଏ ଗୋଟିଏ କୁହନ୍ତୁ" and wait. Acceptance: no cart change from an overlapped utterance.
3. **Stretch, only if step 1 passes**: run separation, transcribe each stream, merge into the cart. Treat as research: classical harmonic masking rarely separates two speakers of similar pitch on one laptop mic. Accept dropping this if CER on separated tracks is worse than 2x single-speaker CER.

## Phase 3 - Language switching

Goal: caller says "speak English" (or Hindi) and the call continues in that language without losing the cart.

1. **Wire `acoustic_lid_router.py`** per utterance (it is script/lexical today). Check what it really does on the recordings before trusting it. Acceptance: >= 95 % correct on the 20-utterance set including 3 English ones.
2. **ASR routing**: Odia utterances -> IndicConformer (as now); English -> Whisper small (already installed, cached).
3. **The gap that decides the scope: we have no English or Hindi voice.** The studio TTS is Odia only. Options, to decide after a 1-hour spike: (a) an offline English voice such as Piper (small, CPU, free), (b) Windows built-in voices (zero setup, lowest quality), (c) none: reply in English text on the dashboard only. Recommendation: (a), English first, Hindi later.
4. Reply language follows the caller's last utterance; cart and stage are language independent (they already are).

## Phase 4 - Voice naturalness (measure, do not retrain)

We measured that retraining the rhythm model on podcast audio was **6x worse** before; do not retry that. What can still help, each scored with `score_pronunciation.py` plus a blind A/B with you:
1. Pace is set (female 1.15). Add a **speed slider** on the dashboard (no restart), range 1.0-1.35, so you pick the setting.
2. **Short human backchannels**: cut 6-10 real "ହଁ / ଆଚ୍ଛା / ଓହୋ" clips from the female harvest (`data/podcasts/female_harvest`) as the Phase 1 acknowledgements. Real audio is more natural than synthesized filler.
3. Vary reply wording (2-3 phrasings per intent, picked at random, never the same twice in a row). Cheap, removes the "scripted" feel more than any acoustic change.
4. Optional later: temperature 0.45 -> 0.6 for slightly more variation; accept only if CER stays within +0.5 points.

## Phase 5 - Name capture and spelling

Goal: the kitchen ticket has a name the caller agreed to.

1. Odia script name from ASR is shown with a **romanized form** (`phonetic_translit.odia_to_roman`) on the dashboard ticket.
2. If ASR confidence is low or the romanization has known variants (Sachin/Sachine, Smita/Smitha), Mira offers two choices and the caller says "ପ୍ରଥମଟି / ଦ୍ୱିତୀୟଟି" (the demo's "the first one"). Acceptance: on 10 recorded names, the confirmed spelling matches the intended one >= 8 times.
3. Keep the current yes/no confirmation as the fallback.

## Order of work

Phase 0 and Phase 1 first (a week at most; gives a measurable half-second-plus gain), then Phase 4 items 1-3 (cheap, you judge by ear), then Phase 5, then Phase 3, then Phase 2 (hardest, least certain).

## Not in this plan

SMS provider and real telephony (Twilio/Exotel), kitchen/rider ETA integration, multi-merchant menus, persisting orders to a database. Each is its own phase after the call quality is acceptable.
