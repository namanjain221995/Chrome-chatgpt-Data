#!/usr/bin/env python3
"""Generate the shared JSON Schemas from the backend Pydantic models.

The Pydantic models in `services/backend/app/schemas` are the single source of
truth for the wire contract. This script renders them to
`packages/schemas/schemas/*.json`, which the Chrome extension validates its
payloads against in CI. `make schema-check` regenerates and diffs, so a change
on either side that is not mirrored fails the build instead of drifting into a
production mismatch.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / "services" / "backend"
OUTPUT = REPO_ROOT / "packages" / "schemas" / "schemas"

sys.path.insert(0, str(BACKEND))


def main() -> int:
    from app.schemas.attachments import (  # noqa: PLC0415
        AttachmentCompleteIn,
        AttachmentCompleteOut,
        AttachmentInitIn,
        AttachmentInitOut,
        ExportCreateIn,
        ExportOut,
    )
    from app.schemas.auth import (  # noqa: PLC0415
        AuthExchangeIn,
        AuthTokensOut,
        DeviceRegisterIn,
        DeviceRegisterOut,
        SignedRuntimeConfig,
    )
    from app.schemas.common import BatchResponse, ErrorResponse  # noqa: PLC0415
    from app.schemas.ingest import (  # noqa: PLC0415
        CaptureEventBatchIn,
        ConversationUpsertIn,
        ConversationUpsertOut,
        FeedbackIn,
        MessageBatchIn,
        SyncStatusOut,
    )

    models = {
        "conversation-upsert-request": ConversationUpsertIn,
        "conversation-upsert-response": ConversationUpsertOut,
        "message-batch-request": MessageBatchIn,
        "capture-event-batch-request": CaptureEventBatchIn,
        "batch-response": BatchResponse,
        "attachment-init-request": AttachmentInitIn,
        "attachment-init-response": AttachmentInitOut,
        "attachment-complete-request": AttachmentCompleteIn,
        "attachment-complete-response": AttachmentCompleteOut,
        "auth-exchange-request": AuthExchangeIn,
        "auth-tokens-response": AuthTokensOut,
        "device-register-request": DeviceRegisterIn,
        "device-register-response": DeviceRegisterOut,
        "signed-runtime-config": SignedRuntimeConfig,
        "feedback-request": FeedbackIn,
        "sync-status-response": SyncStatusOut,
        "export-create-request": ExportCreateIn,
        "export-response": ExportOut,
        "error-response": ErrorResponse,
    }

    OUTPUT.mkdir(parents=True, exist_ok=True)
    index: dict[str, str] = {}

    for name, model in sorted(models.items()):
        schema = model.model_json_schema(mode="serialization")
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"https://schemas.techsara.example/chat-archive/1.0/{name}.json"
        path = OUTPUT / f"{name}.json"
        path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        index[name] = f"./schemas/{name}.json"
        print(f"  wrote {path.relative_to(REPO_ROOT)}")

    manifest = {
        "schema_version": "1.0",
        "generated_from": "services/backend/app/schemas (Pydantic v2)",
        "note": (
            "Do not edit by hand. Run `make schemas` after changing a backend "
            "schema; CI fails when these files drift from the models."
        ),
        "schemas": index,
    }
    (OUTPUT.parent / "index.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"  wrote packages/schemas/index.json ({len(index)} schemas)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
