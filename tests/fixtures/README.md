# Shared test fixtures

Language-specific fixtures live with their suites:

- **Extension DOM fixtures** — `apps/chrome-extension/tests/fixtures/transcripts.ts`.
  Hand-written, sanitized approximations of the ChatGPT transcript structure.
  They contain no real employee content, no cookies and no tokens.
- **Backend fakes** — `services/backend/tests/fakes.py`. An in-memory object
  store implementing the same surface as the real S3 client, plus minimal PNG
  and EXIF-bearing JPEG byte fixtures.

No fixture is ever captured from a live ChatGPT account. When the product's
markup changes, add a *structural* fixture — element shape and attributes, with
invented text.
