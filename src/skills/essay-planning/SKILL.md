---
name: essay-planning
description: Structured approach to creating academic essay plans with section breakdowns, word allocation, and research directions
---

# Essay Planning Skill

## When to Use
- When creating a draft plan from an assignment brief (Phase 2)
- When refining a plan after research (Phase 4)

## Draft Plan Instructions (Phase 2)

1. Read the assignment brief from `/brief/assignment.md`.
2. Identify the core question or topic the essay must address.
3. Formulate a clear thesis statement (1-2 sentences).
4. Determine the academic level (undergraduate/graduate) from the brief.
5. Break the essay into sections following standard academic structure:
   - **Εισαγωγή (Introduction)**: Present the topic, state the thesis, preview the structure. Typically 10-15% of total words.
   - **Body sections**: Each section develops one main argument or theme. Name them descriptively (not "Section 1"). Allocate words proportionally based on importance.
   - **Συμπέρασμα (Conclusion)**: Synthesize arguments, restate thesis in light of evidence, suggest implications. Typically 10-15% of total words.
6. For each body section, define:
   - A clear subtitle
   - 2-3 key arguments or points to cover
   - Research directions: specific queries or topics to search for (in both Greek and English)
7. Verify that word count targets sum to the overall target.
8. Write the plan to `/plan/draft.md`.

## Plan Refinement Instructions (Phase 4)

1. Read the draft plan from `/plan/draft.md`.
2. Read all source metadata from `/sources/metadata/*.md`.
3. Assess source coverage:
   - Which sections have strong source support?
   - Which sections lack sources?
4. Adjust the plan:
   - Merge under-sourced sections into related sections if appropriate.
   - Strengthen well-sourced sections with additional sub-points.
   - Remove sections that cannot be adequately supported (unless required by the assignment).
   - Add new sections if the sources reveal important themes not in the draft.
5. Redistribute word counts if sections were added/removed/merged.
6. Create source-to-section mappings — assign each source to the sections where it's most relevant. A source can map to multiple sections.
7. Sources marked `provided: true` MUST be included in the mapping. Do not question their relevance.
8. Write the refined plan to `/plan/final.md`.
9. Write the source mapping to `/plan/source_mapping.md`.

## Word Allocation Guidelines

| Essay Length | Introduction | Per Body Section | Conclusion |
|---|---|---|---|
| < 2000 words | 10-12% | Divide remaining equally | 10-12% |
| 2000-4000 words | 10-15% | Divide remaining by importance | 10-15% |
| > 4000 words | 8-10% | Divide remaining by importance | 8-10% |

## Research Direction Examples

Good research directions are specific and searchable:
- "Επίδραση κοινωνικών δικτύων στην ψυχική υγεία εφήβων" (Impact of social media on adolescent mental health)
- "Climate change adaptation strategies Mediterranean agriculture"
- "Νεοφιλελευθερισμός και εκπαιδευτική πολιτική στην Ελλάδα" (Neoliberalism and education policy in Greece)

Avoid vague directions like "general background" or "introduction sources".
