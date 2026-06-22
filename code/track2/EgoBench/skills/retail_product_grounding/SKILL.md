# Retail Product Grounding Skill

Use this skill when a user refers to a retail product through video/image context, such as "the bottle I pointed at", "the third item", "the cookie I picked up", or "the cheese on the left".

## Objective

Resolve the visible target to the most likely canonical catalog product name using visual grounding and retrieved candidates.

The goal is not perfect OCR. The goal is to choose a defensible catalog product that lets the service agent complete the user's task.

## Core Principles

1. Treat OCR as noisy evidence, not as the product identity.
   - Exact brand text may be unreadable, partially visible, or different from the catalog name.
   - Prefer stable fragments: product type, variety, flavor, category, country cue, label color, package shape, visible price, and shelf position.

2. Do not require exact full-name OCR.
   - If the video says "Sauvignon Blanc" but the brand is unclear, candidates containing "sauvignon blanc" are still valid.
   - If the video says "Merlot Cabernet" or "Shiraz Cabernet", candidates sharing one or more stable variety terms are valid.

3. Use visible price as supporting evidence.
   - Price alone is not enough to identify a product.
   - Price plus matching category/type/variety is meaningful evidence.
   - If the user only asks for the visible shelf price, answer from visual grounding without forcing a catalog match.

4. Use candidate retrieval as the product universe.
   - Select only from retrieved candidate product names.
   - Do not invent a product name from OCR.
   - Do not call lookup tools with failed OCR terms again.

5. Prefer task progress over repeated clarification.
   - The simulated user usually does not know the catalog product name.
   - If there is a reasonable candidate supported by type/category plus price or visual context, choose it as `best_effort`.
   - Ask clarification only when candidates are from conflicting categories/types or no candidate shares meaningful visual/OCR evidence.

## Selection Modes

Use `selection_mode: "exact"` when:
- A candidate product name directly matches clear OCR or user-provided text.

Use `selection_mode: "best_effort"` when:
- No exact full-name match exists, but one candidate is the most plausible match based on stable visual/OCR fragments plus price/category/position.
- The user cannot provide the product name and the task requires a catalog name to continue.

Use `selection_mode: "visual_only"` when:
- The user asks only about a visible attribute, such as shelf price, and no catalog action is needed yet.

Use `selection_mode: "no_confident_match"` when:
- Candidate products conflict strongly with the visual target.
- There is no meaningful overlap between OCR/category/visual evidence and candidate names.

## Wine-Shelf Guidance

For wine products, stable evidence usually includes:
- variety/type words: sauvignon, blanc, merlot, cabernet, shiraz, chardonnay, pinot, moscato, riesling, bordeaux
- wine color/type: red wine, white wine, rose
- visible shelf price
- country or region cues when readable
- position relative to neighboring bottles and price tags

When several wines share the same variety and price range, prefer the candidate whose name has the strongest overlap with stable variety/type words and whose price is closest to the visible shelf price. If the brand is unreadable, do not reject a candidate only because the brand is not visible.

## Output Expectations For Reranker

The reranker should output JSON. When it can choose a product, set:

```json
{
  "selected_product_name": "canonical catalog name",
  "confidence": "medium",
  "selection_mode": "best_effort",
  "needs_clarification": false
}
```

When only a visible price can be answered without catalog identity, set:

```json
{
  "selected_product_name": null,
  "confidence": "medium",
  "selection_mode": "visual_only",
  "needs_clarification": false
}
```

Clarify only when continuing would likely choose the wrong category or item.
