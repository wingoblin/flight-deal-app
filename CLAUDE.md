# Project Rules

Reference: https://github.com/drona23/claude-token-efficient

## Approach
- Read existing files before writing. Don't re-read unchanged files.
- Thorough in reasoning, concise in output.
- Skip files over 100KB unless required.
- No sycophantic openers or closing fluff.
- No emojis or em-dashes.
- Do not guess APIs, versions, flags, commit SHAs, or package names. Verify by reading code or docs first.

## Core Rules
- Short sentences (8-10 words max in English prose).
- No filler, no preamble, no pleasantries.
- Tool first. Result first. Explain only if asked.
- Code stays normal. English gets compressed.

## Output
- Return code first. Explanation after, only if non-obvious.
- No inline prose padding. Comments sparingly, only where logic is unclear.
- No boilerplate unless explicitly requested.

## Code Rules
- Simplest working solution. No over-engineering.
- No abstractions for single-use operations.
- No speculative features or "you might also want...".
- Read the file before modifying it. Never edit blind.
- No docstrings or type annotations on code not being changed.
- No error handling for scenarios that cannot happen.
- Three similar lines is better than a premature abstraction.

## Review Rules
- State the bug. Show the fix. Stop.
- No suggestions beyond the scope of the review.
- No compliments before or after.

## Debugging Rules
- Never speculate without reading the relevant code first.
- State what you found, where, and the fix. One pass.
- If cause is unclear: say so. Do not guess.

## Formatting
- No em-dashes, smart quotes, or decorative Unicode.
- Plain hyphens and straight quotes only.
- Natural language characters (Korean, accented letters, CJK) are fine.
- Code output must be copy-paste safe.
