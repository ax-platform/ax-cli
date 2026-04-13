# ATTACHMENT-FLOW-001: CLI Context Attachment Flow

**Status:** Draft  
**Owner:** @madtank  
**Date:** 2026-04-12  
**Related:** ax-backend ATTACHMENTS-001, LISTENER-001

## Purpose

Define the CLI contract for sharing files through aX context and messages.
The CLI must not create split-brain attachment state where the uploaded bytes
land in one space, the context pointer lands in another, and the message lands
in a third.

## Flow

`axctl upload file` performs one logical operation:

1. Resolve the target `space_id`.
2. Upload bytes to `POST /api/v1/uploads/` with that `space_id`.
3. Store a context pointer under that same `space_id`.
4. Send a message in that same `space_id` with attachment metadata containing
   `id`, `filename`, `content_type`, `size_bytes`, `url`, and `context_key`.

`axctl context download <key>` performs the inverse:

1. Resolve the target `space_id`.
2. Read the context pointer from that `space_id`.
3. Follow the stored upload URL while passing the same `space_id`.

## File Type Policy

The backend owns the canonical allowlist. The CLI should set accurate MIME
types for common artifact files so the backend can make an explicit decision.

Code and active-document formats may be accepted for collaboration, but they
should not be treated as inline-safe previews unless the backend explicitly
marks them safe.

## Acceptance Criteria

- Upload API, context API, and message API receive the same resolved space id.
- Message metadata contains both `url` and `context_key`.
- `axctl context download <key>` can retrieve a file uploaded by an agent when
  the active profile has access to the target space.
- Unsupported file types fail with a clear backend error.
