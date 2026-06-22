---
name: video-task-grounding
description: Extract and repair task-relevant video keyframes and bounding boxes for EgoBench-style scenario tasks. Use when Codex needs to annotate JSONL records with task-linked frames, bboxes, OCR content, or when existing video grounding may be wrong because the event frame is occluded, blurred, too small, or not OCR-readable.
---

# Video Task Grounding

## Core Rule

Separate **event grounding** from **OCR grounding**.

- Use the event frame to decide which object the instruction refers to, such as "second bottle pointed at" or "friend's right-hand bottle".
- Use the clearest nearby frame of the same object for OCR, even if the hand/finger is no longer touching it.
- Never treat a bbox as good just because it points to the right object. The label bbox must also expose readable text.

## Workflow

1. Read only allowed task-visible fields first: `instruction`, `image_description`, `video_path`, and existing non-hidden annotation fields. Do not use `key`, `value`, `ground_truth`, or evaluation answers.
2. Identify the referent in the instruction: order of pointing, hand ownership, relative location, color, cap/seal, shelf row, package shape, or other visible attributes.
3. Inspect the event frame or extract candidate frames around the event. Use contact sheets for fast scanning.
4. Draw or inspect a target bbox on the event frame only to confirm the object identity.
5. Check whether the label/OCR region is readable. Reject OCR frames where the label is covered by fingers, glare, motion blur, extreme crop, shelf rail, or another object.
6. If the event frame is not OCR-readable, search adjacent frames before and after the event for the same object with the clearest visible label. Prefer the closest later frame after the hand moves away.
7. Update the JSONL so the service agent receives the clear OCR reference frame and bbox. In `reason` or `uncertainty`, note when the original event frame was occluded and a later clear OCR frame is used.
8. Keep OCR conservative. Use exact visible words only; do not invent catalog names from hidden data. If only partial text is visible, write the partial text and keep confidence low unless another allowed visual frame confirms the full text.
9. Re-run validation before testing the service agent.

## Bbox Standards

Use normalized coordinates in `[0, 1]`:

- `target_bbox`: the whole relevant object, tight enough to exclude adjacent products.
- `label_bbox`: the visible text/label region used for OCR; this must not be mainly a finger, glare, or blank label area.
- `price_tag_bbox`: only use when the task requires shelf price or the price tag is clearly associated with the target. Do not let price OCR replace product identification.

For side-by-side products, prefer a narrower bbox over a wide bbox that includes both labels. If adjacent labels are visible, name the visual disambiguator in `visual_attributes`.

## Occlusion Repair Pattern

When a pointing frame is blocked:

1. Keep the pointing event semantics in `reason`, for example: `original pointing frame is occluded by the hand`.
2. Replace or supplement with a clear OCR frame of the same object.
3. Set `ocr_content` to the text readable in that clear frame.
4. Raise `confidence` only if object continuity is visually clear from frame order and location.
5. Set `needs_human_review=false` only after the label bbox is readable and the referent is unambiguous.

## Useful Commands

Use the bundled scanner to find likely bad OCR annotations:

```bash
python skills/video-task-grounding/scripts/scan_grounding_jsonl.py code/track2/EgoBench/annotations/manual_task_video_grounding_from_instruction.jsonl
```

For manual frame review, generate contact sheets or crops with the repository's available Python environment. Prefer project `.venv` if it has `PIL` and `imageio` installed.

## Validation Checklist

Before finishing:

- JSONL parses successfully.
- All frame paths resolve from the EgoBench root or as absolute paths.
- No annotation uses hidden fields.
- Every target has valid normalized `target_bbox` and `label_bbox` unless there is a clear reason not to.
- `ocr_content` does not contain placeholders such as `partial`, `occluded`, `blurred`, `unclear`, or `exact name uncertain` unless the record remains intentionally low-confidence and is not used as a strong OCR cue.
- Visually inspect representative repaired crops, especially tasks reused across many records.
