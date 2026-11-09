# Local Intent Classifier: Calibration

How the thresholds in `portfolio_agent/intent_classifier.py` were chosen, and what they achieve. All numbers
come from `scripts/calibrate_intents.py` run with the **real** local model (FastEmbed 0.9.1, ONNX Runtime,
`sentence-transformers/all-MiniLM-L6-v2`, CPU). No Gemini call or hosted API was involved.

## Method

- **Prototypes** (`portfolio_agent/intent_examples.py`): about 25 to 100 example messages per intent, including
  informal wording, typos and **hard negatives** (analytical questions phrased like help requests, such as
  "What can you tell me about Growth Equity Fund holdings?", are examples of the analytical intent).
- **Score** for an intent = the highest cosine similarity between the message and any example of that intent.
- **Decision**: the winner needs its own minimum score, must beat the best *unrelated* intent by `min_margin`,
  and a conversational or "unsupported" winner is overridden when analytical content is also clearly present
  (`mixed_floor` and `mixed_gap`), so a real question is never swallowed. Anything else is `UNKNOWN`.
- **Validation set** (`tests/intent_validation_set.py`, `VALIDATION`, 109 messages): positives for every
  intent, hard negatives, mixed messages, ambiguous messages and the ten official questions (Q1-Q10).
  Thresholds were chosen on it by a per-intent search that first minimises *wrong-class* decisions (an
  `UNKNOWN` is a safe miss, a wrong class is not) and then total errors, taking the middle of the best
  plateau of each threshold.
- **Holdout set** (`HOLDOUT`, 62 messages): written after the thresholds were chosen and evaluated afterwards,
  as an estimate of generalisation. Two items that duplicated prototypes were replaced and the `mixed_gap`
  guard was added after inspecting score gaps of both sets (accuracy did not change), so treat it as a
  good, not perfectly pristine, estimate.
- A unit test enforces that no validation or holdout message (other than the official Q1-Q10) is also a
  prototype.

## Chosen parameters

| Parameter | Value |
|---|---|
| `min_score` GREETING / THANKS / GOODBYE | 0.55 / 0.45 / 0.50 |
| `min_score` IDENTITY / CAPABILITIES | 0.50 / 0.50 |
| `min_score` PORTFOLIO_ANALYTICS / SECTOR_EXPOSURE | 0.50 / 0.50 |
| `min_score` UNSUPPORTED / UNKNOWN (vague) | 0.40 / 0.55 |
| `min_margin` (winner over best unrelated intent) | 0.04 |
| `mixed_floor` (analytical score that signals analytical content) | 0.50 |
| `mixed_gap` (max distance below a conversational winner) | 0.20 |
| `analytic_pair_margin` (SECTOR_EXPOSURE over PORTFOLIO_ANALYTICS) | 0.02 |

`UNSUPPORTED` has a lower minimum because "everything else" is an open class with weak similarity to any
fixed example list; the margin and the mixed-message override keep it from blocking real questions.

## Results

| Set | Category | Correct |
|---|---|---|
| VALIDATION | positive | 65 / 68 (96%) |
| | hard negative | 12 / 13 (92%) |
| | mixed greeting/help + analytics | 10 / 10 (100%) |
| | ambiguous (expect clarification) | 8 / 8 (100%) |
| | official Q1-Q10 | 10 / 10 (100%) |
| | **all** | **105 / 109 (96%)** |
| HOLDOUT | positive | 36 / 42 (86%) |
| | hard negative | 6 / 7 (86%) |
| | mixed | 6 / 6 (100%) |
| | ambiguous | 7 / 7 (100%) |
| | **all** | **55 / 62 (89%)** |

For analytical expectations either analytical intent counts as correct: the agent's keyword rules still
decide between the SQL tool and the exposure calculator, and Q9/Q10 are checked end to end.

### Remaining misses and why they are acceptable

Every miss is a *safe* one: none sends an analytical question to a conversational reply or a local
rejection (a smoke test asserts that for every analytical item in both sets).

| Message | Outcome | Effect |
|---|---|---|
| "hallo there!", "until next time", "Write a short story about a dragon", "Who are you voting for?" | `UNKNOWN` | asks the user to rephrase |
| "what type of portfolio questions are supported", "tell me what you are good at" | `UNKNOWN` | asks the user to rephrase |
| "What is your purpose?" | IDENTITY instead of CAPABILITIES | still a helpful local reply |
| "how many funds do we manage" | `UNKNOWN` | clarification; rephrasing with "portfolios" works |
| "Who are you comparing the portfolios against?" | `UNKNOWN` | clarification |
| "Remove the Apple holding from the Growth Equity Fund" | analytical | reaches the existing pipeline, which is read-only and rejects it |

## Limits

- Small, hand-written sets (109 and 62 messages): treat percentages as indicative.
- English only; MiniLM was trained on short sentences and is weakest on very short or typo-heavy text.
- Scores are similarities, not probabilities. Re-run `python scripts/calibrate_intents.py` (add `--tune`
  for a per-intent search, `--holdout` for the holdout set) after changing the model, the prototypes or the
  thresholds.
- Real-model checks are opt-in: `RUN_INTENT_MODEL_SMOKE=1` then
  `python -m unittest tests.smoke_intent_model`. The normal test suite uses a mocked embedder and never
  loads or downloads the model.
