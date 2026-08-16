---
name: multimodal-capture
description: How to upload images/PDFs and embed them in notes so they become semantically searchable.
when_to_use: "When the user shares an image, diagram, screenshot, or PDF that should be stored in the second brain and be findable later — or when a note should reference such a file."
version: 1
---

# Multimodal Capture (images & PDFs)

The vault understands images and PDFs natively (via Gemini), not just text. Uploaded
media is vectorized into the `media` vault so `search_vault` can retrieve it by content,
and images embedded in a note are inlined into that note's embedding too.

## Uploading
Use `upload_media` with a `filename` (with extension) and base64-encoded `content_base64`.
- Allowed types only: `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.pdf`. Anything else is rejected.
- On success the tool returns the exact markdown snippet to link the file:
  - Image → `![alt text](../media/<filename>)`
  - PDF   → `[PDF](../media/<filename>)`
- The file is indexed on upload, so a standalone image/PDF is already searchable.

## Embedding in a note (recommended)
To tie an image to its context, reference it from a note body with the relative link:

    ![architecture diagram](../media/diagram.png)

When you `write_note`/`append_to_note`, the server loads that image from disk and embeds
it TOGETHER with the surrounding text — so a semantic `search_vault` on the note's topic
also surfaces the diagram. Prefer embedding over a bare upload when the image explains
something in a note.

## Searching
- `search_vault` with `vault: "media"` targets uploaded images/PDFs specifically.
- `search_vault` with `vault: "all"` (or the note vaults) also returns notes whose
  embedded images matched.

## Recovery
If the Qdrant index is ever lost, `reindex_vault` rebuilds it from disk — including every
image and PDF in the media directory, not just the markdown notes.
