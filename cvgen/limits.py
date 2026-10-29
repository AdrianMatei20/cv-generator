"""Length limits for a tailored CV. Tune here; cvgen.schema enforces them.

Measured on templates/cv_template_with_summary.docx (Letter, 0.5in margins,
7.5in usable width, Calibri 11pt, bullets indented 0.15in) by reading the
lines LibreOffice actually rendered: full bullet lines held 102-122
characters, median 113. Summary and skills lines (no indent) hold ~112-120.
"""

# Characters that fit on one full bullet line. Use the conservative end so
# estimates of line count err towards "more lines".
CHARS_PER_LINE = 110

# The whole CV must fit on this many pages.
MAX_PAGES = 2

# Bullets: at most two full lines. Avoid targeting ~115-135 characters: that
# wraps onto a second line holding only a word or two (the orphan step 6 fixes).
MAX_BULLET_CHARS = 2 * CHARS_PER_LINE  # 220
MAX_BULLETS_PER_ROLE = 6
MAX_BULLETS_PER_PROJECT = 4
MAX_PROJECTS = 3

# Header and summary.
MAX_HEADLINE_CHARS = CHARS_PER_LINE  # one centred line
MAX_SUMMARY_CHARS = 5 * CHARS_PER_LINE  # 550, about five lines

# Skills: each category is one "Category: a, b, c" line in the template.
MAX_SKILL_CATEGORIES = 9
MAX_SKILLS_TOTAL = 60  # comma-separated items across all categories
MAX_SKILL_LINE_CHARS = 2 * CHARS_PER_LINE  # category name + items, two lines

# Layout pass (pipeline step 6).
SHORT_LAST_LINE_WORDS = 4  # a bullet whose last line has this many words or fewer is rephrased
MAX_LAYOUT_ROUNDS = 2
