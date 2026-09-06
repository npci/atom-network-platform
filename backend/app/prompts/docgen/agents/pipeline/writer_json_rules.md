
═══════════════════════════════════════════════════════════
JSON OUTPUT RULES — MANDATORY
═══════════════════════════════════════════════════════════
- Return ONLY valid JSON — no explanation, no markdown fences, no preamble
- Start directly with {  End directly with }
- Escape all quotes inside strings with \"
- No trailing commas
- paragraphs: array of strings — each string is one full paragraph (never a list item inside a paragraph string)
  PARAGRAPH LENGTH: Each paragraph must be 2-4 sentences maximum.
  If content is longer, split it into multiple paragraph strings.
  NEVER write a paragraph longer than 4 sentences.
- bullet_points and numbered_items: [] when not applicable — never null
- code_blocks: array of raw code/XML strings (not escaped, just the raw code text)
  Use code_blocks for ALL XML samples, JSON samples, request/response examples, and code snippets.
  NEVER put XML tags, JSON objects, or code inside paragraphs strings — always use code_blocks.
  [] when no code samples are needed.
- table_data: null when not applicable; when present must have 3-5 realistic rows minimum
- Never output [TBD], placeholder sentences, or empty strings in paragraphs

