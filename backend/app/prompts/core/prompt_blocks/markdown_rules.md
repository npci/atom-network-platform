Markdown formatting rules (STRICT — prevents broken rendering):
- Do NOT use `**Label**` decoration as a section prelude or pseudo-heading. Use proper `##` / `###` markdown headings for structure. The frontend renders headings; bold-as-heading is malformed.
- Do NOT wrap entire paragraphs in italics (`*…*`) or bold-italic (`***…***`). Use those markers only for short inline emphasis on a single word or two.
- NEVER emit unbalanced asterisks: `***Word**` (three open, two close) or `**Word***` (two open, three close) cause the renderer to display literal `**` characters. If you need bold, use exactly two asterisks on each side.
- Do NOT prepend a "**Feature**" or similar label before any section. The required section structure (## 1, ## 2, ...) is the only allowed heading scaffold.
