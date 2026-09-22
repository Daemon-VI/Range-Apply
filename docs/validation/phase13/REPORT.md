# Phase 13 — real-world operational validation (2026-09-12)

Everything below was produced by `tools/validation/phase13_validate.py`
against **real, public** ATS boards and pages, from this laptop, with AI
disabled, a scratch database and no submission of any kind. The JSON files
next to this report are the raw observations. Each item is tagged:

* **PROVEN BY TESTS** — asserted by the automated suite on local fixtures;
* **OBSERVED** — seen on real boards / pages during this validation;
* **NOT YET PROVEN** — could not be exercised here and is stated as such.

Boundary, on purpose: on employer pages the harness ran discovery
(`executor.prepare`), server-side mapping (`capture_form`), document
verification and the pre-submit gate. It **never called `execute`**: no
candidate data was typed into an employer form and no submit control was
pressed. Live submission stays behind `PLAYWRIGHT_DRY_RUN=false`, which only
a person sets.

## 1. Discovery on real boards (OBSERVED)

Eight boards, three ATS families, two full passes plus one after the Lever
fix (`01_discovery.json`, `02_discovery_second_pass.json`,
`02b_discovery_after_lever_fix.json`):

| Board | Source | Postings | Pass 1 | Pass 2 (unchanged) | Rejected | AI calls |
|---|---|---|---|---|---|---|
| Discord | Greenhouse | 46 | 46 new | 46 | 0 | 0 |
| Duolingo | Greenhouse | 80 | 80 new | 80 | 0 | 0 |
| Asana | Greenhouse | 103 | 103 new | 103 | 0 | 0 |
| Nium | Lever | 28 | 28 new | 28 | 0 | 0 |
| Spotify | Lever | 72 | 72 new | 72 | 0 | 0 |
| Linear | Ashby | 30 | 30 new | 30 | 0 | 0 |
| PostHog | Ashby | 11 | 11 new | 11 | 0 | 0 |
| Supabase | Ashby | 60 | 60 new | 60 | 0 | 0 |

430 postings → 429 jobs → 409 opportunities (one cross-board duplicate, 20
same-company / same-title / same-location collapses); 0 malformed records;
0 closures (nothing disappeared between passes); one request per board per
pass; no rate-limit hits; pass 2 converged to 100 % unchanged in 14 s total.
The third pass, after the raw-hash change (below), re-normalised every
posting once (0 unchanged, 429 duplicates, **2 Asana postings versioned by
real content edits in the meantime**, still 429 jobs / 409 opportunities).

## 2. Matching, sync, scheduling, preparation (OBSERVED)

429 jobs matched deterministically in 18.3 s (0 AI calls, 0 failures); 409
candidate opportunities synced (2.9 s): fit bands HIGH 25 / MEDIUM 131 / LOW
253 (median fit 43, range 15–91), eligibility LIKELY 382 / UNCERTAIN 27, all
admitted under the validation policy (daily cap 60). Scheduler run (window
40): 40 admitted, 40 prepared to READY_FOR_EXECUTION in 2.3 s (L1, REVIEW
lane), 0 NEEDS_USER_INPUT / NEEDS_REVIEW / FAILED preparations; 40 SUBMIT
queue items; one DUPLICATE_APPLICATION correctly blocked.

## 3. Real application pages — compatibility matrix

Final pass (`04_corpus.json`; the pre-fix pass is kept as
`04a_corpus_before_fixes.json`): 21 READY attempts, each on its real
application page, one page at a time, ≥ 3 s apart. "Discovered" means the
form was found, every field mapped server-side from the real preparation,
the documents verified and the pre-submit gate run (0 gate failures on all
21); nothing was typed or pressed.

| ATS | Pages | Outcome | Fields (per page) | Required answered | Uploads mapped | Notes |
|---|---|---|---|---|---|---|
| Greenhouse, hosted board (Discord) | 3 | discovered (3/3) | 34 | 6 / 23 | resume + cover letter (`#resume`, `#cover_letter`, both labelled "Attach") | 0.9–3.6 s to discover; no CAPTCHA; unanswered required: Country, Location (City) (comboboxes, no profile fact), a "why Discord" essay, "located in the US" questions, EEO questions → `NEEDS_USER_INPUT` |
| Greenhouse, embedded on the employer site (Asana) | 2 | `CAPTCHA_REQUIRED` handoff after full discovery | 39, **inside the embed iframe** | 6 / 32 | resume | 23–26 s page load, form found in the child frame (`in_frame`); the embed carries a reCAPTCHA widget outside the form element → conservative handoff before anything is typed |
| Lever (Spotify) | 8 | `CAPTCHA_REQUIRED` handoff after full discovery in this pass; discovered after #11 (the hCaptcha is invisible-mode: zero-height container, hidden overlay iframes, measured on a real page) | 15–17 | 3 / 6–7 | resume | 2.1–4.0 s; unanswered required: Current location (typeahead), Current company, "previously employed by Spotify?" (`NEEDS_REVIEW`: unknown options), one location-eligibility question |
| Ashby (Linear, Supabase) | 6 | discovered (5/6), `CAPTCHA_REQUIRED` (1/6) | 12–14 | 3–4 / 7–9 | resume (both the "Resume" and the "Autofill from resume" controls) | 2.1–4.0 s incl. 0.5–0.6 s settle for client rendering; one Supabase page showed a challenge widget on this visit that it had not shown two hours earlier (intermittent bot protection: pacing matters); unanswered required: Passport Country, Country of Residence, GitHub / LinkedIn URLs, an async-work essay |
| Ashby (PostHog) | 2 | `AMBIGUOUS_FORM` handoff | 0 | — | — | the board renders a bare "Jobs" shell for these postings (no form after the 8 s settle window); apply by hand |

**After #11 (the last pass, what `04_corpus.json` now holds): 19/21
discovered — Greenhouse 5/5 (Discord hosted ×3 with resume + cover letter
mapped, Asana embed ×2 inside the iframe with 39 fields), Lever 8/8, Ashby
6/8 (the two PostHog shells remain `AMBIGUOUS_FORM`); 0 gate failures; the
invisible widgets are recorded (`captcha` true, `captcha_invisible` in the
diagnostics) on Discord (2/3 visits), both Asana pages, all 8 Lever pages
and 1 Supabase page.** The pass before it (overwritten; its per-page rows are quoted in
the table above) had 8/21 discovered with 11 `CAPTCHA_REQUIRED` handoffs on those same
invisible widgets.

Before the fixes (`04a`): Greenhouse 7/8 discovered (Duolingo embed 0
fields, Asana 1 field), Lever 0/8 (posting page instead of `/apply`, cookie
buttons counted as a form), Ashby 0/8 (nothing rendered at the load event).
After: every page that has a reachable form is discovered and fully mapped;
the only non-discoveries are two PostHog postings that render no form.

Discovery cost per page (real network): median ≈ 2.8 s, worst 25.5 s
(Asana's page weight), settle wait 31 ms – 1.6 s where a form exists, the
full 8 s only on the PostHog shells.

Two things the final pass exposed and that were fixed after it:

* on the re-captured Discord forms both "Attach" rows carried the cover
  letter (the recapture path joined answers by question key, and both
  controls share the label "Attach"); the join is now by position (§8 #10);
  the first capture of each form (`04a`) had mapped them correctly;
* Asana's Greenhouse embed shows an invisible reCAPTCHA badge; this pass
  handed off on it. The soak then showed the same badge appearing on the
  hosted Discord pages on later visits (§8 #11); the executor now reports
  the badge and proceeds, and the pass was repeated (see the "after #11"
  line below).

## 4. Special pages (OBSERVED, `05_specials.json`)

| Page | Expected | Result |
|---|---|---|
| Google reCAPTCHA demo | CAPTCHA handoff | `CAPTCHA_REQUIRED`, nothing typed |
| hCaptcha demo | CAPTCHA handoff | `CAPTCHA_REQUIRED`, nothing typed |
| github.com/login | login wall | `AUTH_REQUIRED` |
| linkedin.com/login | login wall | `AUTH_REQUIRED` |
| Lever board index (no form) | ambiguous | `AMBIGUOUS_FORM` |
| Greenhouse posting id 1 (missing; redirects to the board index) | closed posting | first run: "discovered" 3 search-box fields; rerun after the fix: `AMBIGUOUS_FORM` (also PROVEN BY TESTS on `search_only.html`) |
| about:blank | malformed | `AMBIGUOUS_FORM` after the settle window |

## 5. Documents (OBSERVED, `07_documents.json`)

42 rendered artifacts (21 resumes + 21 cover letters, PDF) for the real
preparations: 42/42 open with pypdf, 42/42 SHA-256 match the stored hash,
42/42 contain the candidate's name and e-mail, 0 non-ASCII characters (the
seed is ASCII), 0 link annotations (URLs are plain text — P3 note), every
file passes `ArtifactStore.verify`. The Greenhouse "Attach" controls
(`#resume`, `#cover_letter`) and the Ashby / Lever resume controls all map to
the prepared artifacts after the file-name fix (§8).

## 6. Signal Inbox with realistic messages (OBSERVED, `08_signals.json`, `08b_signals_reprocessed.json`)

16 messages modelled on Greenhouse / Lever / recruiter / HackerRank /
Calendly / LinkedIn formats (no real employer text copied), ingested
against the real attempts: classification correct for **14/14** categorised
kinds (confirmation, received, rejection, interview invitation, threaded
reply via `In-Reply-To`, assessment, recruiter outreach, status update, job
alert → IGNORED, information request, withdrawal, HTML-only mail); the
duplicate re-delivery became an observation, not a second signal; the
company-only message with two live applications stayed AMBIGUOUS. First
pass attribution matched only the reference-bearing confirmation (1/13
title-bearing messages) — a real bug: titles like "Senior Product Manager -
Subscriptions" never matched the folded text. After the fix, reprocessing
the review queue attributed **10/13** (the remaining three are, by design,
ambiguous or unknown), review queue 13 → 3. The learning snapshot over the
12 "executed" attempts: dataset 12 rows, evidence STRONG 1 / NONE 11,
`responded` 1, 22 recommendations all "insufficient data" — no methodology
change.

## 7. Data chain, diagnostics (OBSERVED, `09_chain_audit.json`, `10_diagnostics.json`)

For three attempts the chain *why selected → what would be submitted →
evidence of submission → what happened afterward* reconstructs entirely
from stored rows: job (source, URL, content hash) → opportunity (identity
key) → candidate opportunity (eligibility decision + ruleset, priority
components + weights version, admission policy version, gate ruleset) →
preparation (version, level, lane, fingerprint, evidence keys, `ai_used`
false) → documents (SHA-256) → form snapshots → attempt / runs → signals →
outcome events → current outcome, plus the audit trail (pipeline-sync,
scheduler, preparation, validation actors). Diagnostics: queue 40 PENDING /
40 SUCCEEDED, 0 stale leases, caps day 40/60 week 40/300, one warning
("12 signals need review, 1 unmatched" before reprocessing); no automatic
policy change anywhere.

## 8. Bugs found and fixed (all reusable, none site-specific)

| # | Severity | Finding (real-world) | Fix | Proof |
|---|---|---|---|---|
| 1 | P1 | Lever's live API puts `hostedUrl` / `applyUrl` at the top level; the adapter read `urls.apply`, so every Lever attempt targeted the read-only posting page (0 fields on 8/8 pages). | `app/jobs/sources/lever.py` reads both shapes and derives `/apply`. | `test_lever_live_payload_shape_targets_the_apply_page`; corpus rerun (§3) |
| 2 | P1 | A moved apply link on an unchanged posting is never picked up (raw-hash fast path ignores URLs; the existing-job path never refreshes `application_url`). | URLs are part of the raw hash; same-source existing jobs follow a changed apply link (no content version). | `test_moved_apply_link_on_an_unchanged_posting_is_followed`; pass 3: 100/100 Lever jobs on `/apply` |
| 3 | P1 | Greenhouse embedded on employer sites (Duolingo) keeps the form in an iframe injected after load; Ashby renders the form client-side after the load event: 0 fields on 10/24 pages. | `_settled_scan`: re-scan main frame and child frames for up to `PLAYWRIGHT_SETTLE_MS` (8 s); fill / submit / rescan run in the frame that holds the form; frame loss after submit falls back to the page. | `test_form_inside_an_embedded_ats_iframe_is_discovered_and_filled`, `test_client_rendered_form_is_found_after_settling`, `test_page_that_never_renders_a_form_hands_off_after_the_settle_window`; corpus rerun |
| 4 | P1 | Attribution by company + title never matched real titles (dashes, commas, parentheses): the title key kept punctuation, the searched text was folded. | `_title_key` folds titles like the text. | `test_real_world_titles_with_punctuation_still_identify_the_application`; reprocess 10/13 |
| 5 | P2 | Greenhouse labels both uploads "Attach"; mapping only read the label, so neither resume nor cover letter was attached. | File controls also match on their name / id (`resume`, `cover_letter`). | `test_file_controls_named_resume_or_cover_letter_map_even_when_labelled_attach` |
| 6 | P2 | A closed Greenhouse posting redirects to the board index whose search form counted as "discovered"; live mode would have pressed *Search*. | `_looks_like_application_form`: a form without upload / e-mail / phone / applicant-named controls is an `AMBIGUOUS_FORM` handoff. | `test_a_listing_page_with_only_a_search_form_is_not_an_application`; specials rerun |
| 7 | P2 | Cookie-banner buttons ("accept" / "deny") counted as submit controls and masked the no-form handoff on Lever posting pages. | Only submit-looking buttons count. | `test_cookie_banner_buttons_do_not_count_as_a_form` |
| 8 | P2 | A challenge widget *inside* an application form never disappears once a person has completed it, so `_wait_for_person` and the extension's "press Fill again" could never clear (found on Lever, whose widget later turned out to be invisible-mode — #11; the fix still matters for every board with a visible checkbox widget). | Discovery reports `captcha_inline` and `captcha_solved` (the provider's response token exists — detected, never produced); the handoff clears once a person completed it. Nothing is typed before that; nothing solves it. | `test_inline_captcha_completed_by_the_person_lets_the_run_continue`, `test_captcha_widget_completed_by_the_person_is_not_a_wall` |
| 9 | P3 (docs) | `docs/DEVELOPMENT.md` / `README.md` started the dev server on `0.0.0.0` while read endpoints are open. | `--host 127.0.0.1`. | — |
| 11 | P1 | Greenhouse-hosted boards load an *invisible* reCAPTCHA badge lazily; on later (cached) visits it was present at scan time and every hosted Greenhouse page became a `CAPTCHA_REQUIRED` handoff (soak cycles 1–3: 9–10/10 handoffs), on first visits it was not (3/3 discovered). Lever's hCaptcha is invisible-mode too (zero-height container, hidden overlay iframes — measured on a real page at 0 / 2 / 5 s). A person cannot "complete" an invisible widget; the provider scores the submit itself. | Discovery distinguishes `captcha_visible` (a visible widget or challenge text: still a wall, nothing typed) from the badge alone (reported as `captcha_invisible`, run proceeds); a challenge that appears after the click hands off after the click, as before. Nothing solves anything. | `test_invisible_recaptcha_badge_is_not_a_wall_but_a_visible_challenge_is`, `test_invisible_recaptcha_badge_does_not_block_the_extension_fill`; soak rerun (§11) |
| 10 | P1 | Re-capturing a known form refreshed answers by *question key*; two controls with the same label ("Attach" × 2 on Greenhouse, "Country" × 2) received the last answer — the resume control would have been given the cover letter on a second visit. | `capture_form` joins by position (the fingerprint proves the same shape). | `test_recapturing_a_known_form_keeps_same_labelled_controls_apart` |

## 9. Browser failure modes (PROVEN BY TESTS, `tests/execution/test_playwright_failures.py`)

Fixtures served over loopback HTTP with Playwright route injection: slow
responses (still discovers), a hung load (TIMEOUT, retryable, before
submit), HTTP 500 (`AMBIGUOUS_FORM`, nothing typed), connection reset
(TRANSIENT_NETWORK, retryable), tab closed mid-fill (BROWSER_CRASH,
retryable, browser relaunched, 0 clicks), navigation away mid-fill and DOM
mutation between discovery and fill (FORM_CHANGED, retryable, 0 clicks), a
reload before fill (harmless), tab closed right after the click (UNKNOWN,
attempt UNCERTAIN, nothing reclaimable, 1 click), a confirmation the server
delays by 4 s (observed when it lands, exactly 1 click). Ten tests, all
green. Together with the Phase 7/12 suites: before the click every failure
is retryable and no submit is pressed; after it the outcome is UNKNOWN and
never resubmitted.

## 10. Extension (page scripts OBSERVED on real pages; runtime NOT YET PROVEN)

`06_extension_parity.json`: the extension's own page scripts
(`discover.js`, `fill.js`, `content.js`) were injected into six real
application pages the way `chrome.scripting.executeScript` does (page
evaluation, independent of the page's CSP) with a stub bridge, and their
discovery compared field by field with the Playwright scan of the same page
(which runs the same `discover.js`): Discord 34 = 34 (×3), Linear 12 = 12,
Supabase 14 = 14 and 12 = 12 — identical external ids, types and required
flags on all six; the content bridge answered `state` on every page (not
armed, no interception installed). What this does **not** prove: the
service worker inside a real Chrome (played by the Python driver on
fixtures only), sender validation at runtime, tab navigation / refresh /
SPA route changes while armed, and a different-origin confirmation page —
all NOT YET PROVEN. Known gap: the content script runs in the top frame
only, so a Greenhouse embed (Duolingo-style page) is invisible to it until
`all_frames` injection is added (P2). The extension's handling of inline
and invisible CAPTCHA widgets follows the same rules as the Playwright
executor (§8 #8, #11) and is PROVEN BY TESTS on fixtures.

## 11. Short soak (OBSERVED, `11_soak.json`)

Two runs of the discovery-only soak (the first ten corpus pages — 3
Discord, 2 Asana, 5 Spotify — visited once per cycle, ≥ 3 s apart, one
Chromium kept open, `11_soak_4_cycles.json` and `11_soak.json`):

| Run | Cycles | Seconds / cycle | Chromium processes | Python RSS | DB | Outcomes |
|---|---|---|---|---|---|---|
| before #11 | 4 | 75–78 | 6 (steady) | 85.5 → 91.6 MB | 17.14 MB (flat) | cycle 0: 1 discovered / 9 handoffs; cycles 1–2: 10/10 handoffs; cycle 3: 1 / 9 — every handoff `CAPTCHA_REQUIRED` on the lazily loaded invisible widgets |
| after #11 | 2 | 70–72 | 6 (steady) | 85.6 → 89.9 MB | 17.16 MB (flat) | 10/10 discovered in both cycles (34 / 34 / 34 Discord, 39 / 39 Asana in-frame, 15–17 Spotify), invisible widgets reported on 8/10 |

Browser launches 1 per run (no relaunch), 0 Chromium processes left after
`close()`, documents and AI cache unchanged (0.12 MB / 0 MB; AI off). This
is minutes, not days: it shows no leak per cycle and a stable process
count, nothing more.

## 12. Not yet proven

* Live submission on a real employer form (deliberately not attempted).
* The extension inside a real Chrome: service-worker restart, sender
  validation at runtime, tab navigation / refresh / SPA route changes while
  armed, a different-origin confirmation page. The page scripts were run on
  real pages through Playwright injection (same code path as
  `chrome.scripting.executeScript`); the service worker was played by the
  Python driver on fixtures only. The extension's content script runs in
  the top frame only: on Greenhouse embeds (Duolingo-style pages) it will
  not see the form until `all_frames` injection is added (P2 follow-up).
* Multi-day operation (the soak here is minutes, not days).
* PostgreSQL behaviour of the new code paths (CI only, as before).
* Combobox / typeahead controls (Greenhouse "Country" / "Location (City)",
  Lever "Current location") are discovered as text inputs; typing into them
  without picking an option will not satisfy the form — they surface as
  NEEDS_USER_INPUT today because the profile has no answer, so nothing wrong
  is typed, but answering them will need option selection (P2 follow-up).
