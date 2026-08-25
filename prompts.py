"""System prompts for the one-shot (non-tool-loop) document flows:
policy PDF/photo summaries and receipt photo extraction."""

POLICY_SUMMARY_PROMPT = """You are helping an insurance agent turn a policy document into a
client-friendly summary they can paste into a message or read out on a call.

Given the policy document text below, produce a summary using this structure (use Telegram
"legacy Markdown" formatting — *bold* for section labels, no headers, no tables):

*Policy type & insurer*
*Policyholder / insured*
*Coverage highlights* (plain-language bullets using "-")
*Key limits & deductibles*
*Premium & payment schedule*
*Notable exclusions*
*Renewal / expiry date*
*Suggested next step* (one line — e.g. flag a gap, an upcoming renewal, or nothing needed)

If a field genuinely isn't in the document, write "Not stated in document" rather than
guessing. Keep it tight — this is read on a phone."""

RECEIPT_EXTRACTION_PROMPT = """Look at this receipt image and extract the following fields as
STRICT JSON only — no markdown fences, no commentary, just the JSON object:

{
  "vendor": string,
  "date": string in YYYY-MM-DD format (use your best guess if the year is missing, assuming
    the most recent plausible date; if truly unreadable, use null),
  "amount": number (the final/total amount paid, not a subtotal),
  "currency": 3-letter currency code (guess from symbols/context if not printed; default SGD),
  "category": one of "Meals", "Transport", "Client Entertainment", "Office Supplies",
    "Travel", "Other",
  "notes": short string with anything else useful (e.g. what was purchased), or null
}

If you cannot read the image well enough to extract a field, use null for that field rather
than guessing wildly."""
