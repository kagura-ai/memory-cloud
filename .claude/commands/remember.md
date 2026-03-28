Save new knowledge, patterns, or learnings to Kagura Memory Cloud.

Save the following to memory: $ARGUMENTS

Steps:
1. Parse the input to extract a clear summary (first sentence or line)
2. Determine the appropriate type based on content:
   - pattern: Implementation patterns, code examples
   - troubleshooting: Error fixes, debugging solutions
   - decision: Design decisions, architecture choices
   - lesson: General learnings
   - bug-fix: Bug fix details
3. Set importance based on impact (default: 0.8, design decisions: 0.9, core principles: 1.0)
4. Generate relevant tags (technology, domain, feature area)
5. Use `remember` with context_id=kagura-dev, the parsed summary, context_summary with details, and appropriate type/importance/tags
6. Confirm what was saved
