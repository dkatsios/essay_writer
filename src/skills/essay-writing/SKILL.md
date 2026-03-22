---
name: essay-writing
description: Write complete academic essays in Greek with proper source integration, citations, and word count control
---

# Essay Writing Skill

## When to Use
- When the orchestrator is writing the essay draft (Step 5)

## Writing Process

1. **Read your materials**:
   - Read the essay plan from `/plan/plan.md` for section structure and word targets
   - Use the research notes and reader summaries from your conversation history for source material
   - Read the assignment brief from `/brief/assignment.md` for requirements

2. **Plan internally**:
   - Map sources to sections (which source supports which argument)
   - Identify 2-4 key points per section
   - Plan transitions between sections

3. **Write the essay**:
   - Follow the section structure from the plan
   - Open each section with a clear topic sentence
   - Develop arguments with evidence from sources
   - Use transitions between sections
   - Include a properly formatted References section at the end

4. **Integrate sources**:
   - Introduce sources before citing them: "Σύμφωνα με τον Παπαδόπουλο (2023)..."
   - Use a mix of direct quotes and paraphrasing
   - After each citation, explain its significance — don't let quotes stand alone
   - Cross-reference sources where they agree or disagree

5. **Verify word count**:
   - Use the `count_words` tool on your draft
   - Target: within ±10% of the assigned total word count
   - If over: tighten prose, remove redundancy, merge similar points
   - If under: develop arguments further, add more source integration, expand analysis

6. **Write to VFS**:
   - Write the complete essay to `/essay/draft.md`

## Academic Greek Style Guide

### Register
- Use formal academic language (Δημοτική)
- Avoid colloquialisms, slang, or overly informal expressions
- Use passive voice where appropriate in academic context
- Employ hedging language: "φαίνεται ότι", "είναι πιθανό", "τα δεδομένα υποδεικνύουν"

### Structure
- One idea per paragraph
- Clear topic sentences
- Logical connectors: "Επιπλέον", "Ωστόσο", "Εν κατακλείδι", "Αντιθέτως", "Συνεπώς"
- Build arguments progressively — from evidence to analysis to synthesis

### Citations (APA7)
- In-text: (Παπαδόπουλος, 2023) or Παπαδόπουλος (2023)
- With page: (Παπαδόπουλος, 2023, σ. 45)
- Two authors: (Παπαδόπουλος & Ιωάννου, 2023)
- Three or more: (Παπαδόπουλος κ.ά., 2023)
- Direct quote: must include page number

### Common Pitfalls
- Don't start paragraphs with citations
- Don't string multiple quotes without analysis
- Don't use first person unless the assignment explicitly allows it
- Don't make unsupported claims — every assertion needs a citation or clear logical derivation
