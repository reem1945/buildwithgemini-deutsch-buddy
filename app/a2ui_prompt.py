# A2UI v0.8 Basic Catalog System Prompt Definition

A2UI_SCHEMA_INSTRUCTION = """
## A2UI Output Rules:
When returning structured user interface cards (for vocabulary cards, grammar rules, review items, or image challenges), emit a valid A2UI v0.8 JSON array containing `beginRendering` and `surfaceUpdate` messages:
- Keep every surface tiny and flat: ONE Card > ONE Column > a few Text rows.
- Never nest a Card inside a Card.
- Use ONLY these components: Card, Column, Row, Text, and Image. Do not use Table or Heading, or Buttons, actions, or forms.
- You may include one Image component, but only when you have a public https URL for the image. Set the Image url to that exact https link: {"Image": {"url": {"literalString": "https://..."}}}. Never point an Image at a bare filename or non-http(s) path.
- No markdown in text; use the usageHint property ('h1', 'h2', 'body') for headings and emphasis.
- Output ONLY the raw A2UI JSON array — no prose wrapping.
"""
