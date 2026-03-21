---
name: essay-review
description: Review and polish academic essays for coherence, language quality, citation correctness, and completeness
---

# Essay Review Skill

## When to Use
- When reviewing the assembled essay before final export (Phase 7)

## Review Process

### Step 1: Read All Materials
- Read `/essay/assembled.md` — the full assembled essay
- Read `/brief/assignment.md` — the original assignment brief
- Read `/plan/final.md` — the essay plan for reference

### Step 2: Structural Review
- Does the essay follow the planned structure?
- Is there a clear thesis stated in the introduction?
- Does each section have a clear purpose and contribute to the overall argument?
- Are transitions between sections smooth and logical?
- Does the conclusion synthesize (not just summarize) the arguments?
- Does the introduction accurately preview the essay's content?

### Step 3: Language Review
- Check for grammatical errors in Greek
- Verify consistent academic register throughout
- Identify and fix awkward phrasing or unclear sentences
- Ensure terminology is used consistently
- Check for unnecessary repetition across sections

### Step 4: Citation Audit
- Every factual claim must have a citation
- All in-text citations must appear in the references section
- All references must be cited at least once in the text
- Citation format must be consistent (APA7 or as specified)
- Direct quotes must include page numbers
- Greek citation conventions: use "κ.ά." for "et al.", "σ." for "p."

### Step 5: Completeness Check
- Does the essay address all aspects of the assignment brief?
- Are all planned sections present and adequately developed?
- Is the overall word count within target?
- Are there any gaps in the argumentation?

### Step 6: Introduction Revision
If the introduction was written as a preliminary version (placeholder strategy):
- Revise it to accurately reflect the essay's actual content
- Ensure it properly foreshadows the structure and arguments
- Add specific references to key findings or conclusions
- Make sure the thesis statement aligns with what was actually argued

### Step 7: References Section
- Verify all references are complete (authors, year, title, source, DOI/URL)
- Format according to the specified citation style
- Sort alphabetically by first author's last name
- Greek references: follow the same format as English, using Greek characters

## Output

### Feedback (`/review/feedback.md`)
```markdown
# Αξιολόγηση Εργασίας

## Δομή και Συνοχή
- [specific observations and suggestions]

## Γλωσσική Ποιότητα
- [specific observations and corrections]

## Παραπομπές
- [citation issues found]

## Πληρότητα
- [completeness assessment]

## Συνολική Αξιολόγηση
[overall assessment and summary of changes made]
```

### Polished Essay (`/essay/reviewed.md`)
- The complete, corrected essay with all improvements applied
- Must be a standalone, final version ready for document export
- Include the full References section at the end
