---
name: muninn-quote-check
description: Check whether a quote is verbatim in a recorded muninn event with `muninn quote-check REF QUOTE`. Use before `muninn know add`, or whenever someone claims "the owner said X" and you need to prove or refute it from the transcript.
---

# muninn quote-check

    muninn quote-check REF "exact words"

Returns whether the quote appears verbatim in that event.

- Copy the quote from `muninn open REF` output, not from a snippet or memory; quotes are matched verbatim, so changed whitespace or punctuation counts as a miss.
- A match confirms the quoted text only; assess the associated claim separately.
- Redacted or truncated events cannot confirm text inside the redacted or cut part.
- Check the quote before `muninn know add`, which requires a 12 to 300 character verbatim span.
