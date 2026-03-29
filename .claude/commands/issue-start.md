Start work on a GitHub issue. Creates branch and searches for past knowledge.

Usage: /issue-start <issue-number>

Steps:
1. View the issue details:
   ```
   gh issue view $ARGUMENTS
   ```
2. Create a feature branch from main:
   ```
   git checkout main && git pull origin main
   git checkout -b {issue-number}-feat/{short-description}
   ```
   Use the issue title to derive a short kebab-case description.
   Use appropriate prefix: feat/, fix/, refactor/, test/, docs/

3. Search Kagura Memory Cloud for related past work:
   Use `recall` with context_id=kagura-dev, query based on the issue title and description, k=5

4. Display:
   - Issue summary
   - Branch name created
   - Related past knowledge found (if any)
   - Suggested next steps
