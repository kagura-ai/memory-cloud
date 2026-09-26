/**
 * Context Templates for Context Creation
 *
 * Issue #160: Provides pre-defined templates for context summary and usage guide
 * Issue #1698: the server returns summary and usage_guide as the owner's notes
 * on what the context holds and how it is organized — information about the
 * context, not instructions (#1682). The starter text is written the same
 * way: it describes how memories here are typed and tagged ("Bug fixes:
 * type='bug-fix', importance 0.8+"), it does not direct the AI ("Always …",
 * "Store …"). Users edit it freely after picking a template.
 */

export interface ContextTemplate {
  id: string;
  name: string;
  description: string;
  category: 'development' | 'personal' | 'team';
  summary: string;
  usage_guide: string;
}

export const CONTEXT_TEMPLATES: ContextTemplate[] = [
  // Development Templates
  {
    id: 'personal-dev',
    name: 'Personal Development',
    description: 'For individual coding projects and learning',
    category: 'development',
    summary: 'Personal coding projects and development notes: code snippets, bug fixes, and learning progress.',
    usage_guide: `Personal coding projects and development notes.

Memory types:
- Code snippets: type='code', tagged with language and framework
- Bug fixes: type='bug-fix', importance 0.8+
- Design decisions: type='decision', with the reasoning behind them
- Learning notes: type='learning', importance 0.5-0.7

Tags:
- Programming language (e.g., 'python', 'typescript')
- Framework or library (e.g., 'react', 'fastapi')
- Project name for project-specific memories

Summaries are 100-250 characters.`,
  },
  {
    id: 'team-collab',
    name: 'Team Collaboration',
    description: 'For shared team knowledge base',
    category: 'team',
    summary: 'Team shared knowledge base: meeting notes, decisions, and collaborative documentation.',
    usage_guide: `Team shared knowledge base.

Meeting notes:
- type='note', tags=['meeting', 'YYYY-MM-DD', 'team-name']
- Each carries its action items and decisions

API documentation:
- type='code', tags=['api', 'docs', 'endpoint-name']
- Request/response examples included

Design decisions:
- type='decision', importance 0.9+
- Tagged with the affected components

Across the team:
- Summaries are under 200 characters
- The same tag names are used across the team
- Critical information carries importance 0.8+`,
  },
  {
    id: 'project-docs',
    name: 'Project Documentation',
    description: 'For technical documentation and architecture',
    category: 'development',
    summary: 'Technical documentation and architectural decisions: design patterns, implementations, and system knowledge.',
    usage_guide: `Project documentation context.

Architecture decisions:
- type='decision', tags=['architecture', 'design']
- Trade-offs and alternatives considered are recorded with each decision

Implementation notes:
- type='code', file paths in the context field
- Related issues/PRs referenced
- Tagged with component names

Bug fixes:
- type='bug-fix', issue number in the summary
- Root cause analysis included
- Tagged with a severity level

Code reviews:
- type='note', tags=['review', 'pr-number']
- Feedback and recommendations included`,
  },
  {
    id: 'learning',
    name: 'Learning & Study Notes',
    description: 'For study notes and knowledge accumulation',
    category: 'personal',
    summary: 'Learning and knowledge base: study notes, concepts, and educational materials.',
    usage_guide: `Learning and knowledge base.

Study notes:
- type='learning', tags=['topic', 'subject']
- Importance reflects relevance (0.3-0.7)

Key concepts:
- type='note', tags=['concept', 'category']
- Summaries name the concept plainly, so it is easy to search

Code examples:
- type='code', tags=['example', 'pattern']
- Each notes when the pattern applies

Resources:
- type='note', tags=['resource', 'reference']
- Links to external materials are in the details field

Importance rises as knowledge solidifies.`,
  },

  // Personal Life Templates
  {
    id: 'daily-journal',
    name: 'Daily Journal & Diary',
    description: 'For daily reflections and personal diary',
    category: 'personal',
    summary: 'Daily journal and personal diary: thoughts, experiences, and daily reflections.',
    usage_guide: `Daily journal and diary.

Daily entries:
- type='note', tags=['diary', 'YYYY-MM-DD']
- Importance reflects significance (0.3-0.8)

Reflections:
- type='note', tags=['reflection', 'personal-growth']
- Each notes what was learned

Memories:
- type='note', tags=['memory', 'experience']
- Special moments carry importance 0.8+

Mood:
- A mood tag such as 'happy', 'thoughtful' or 'stressed'
- Emotional background in the context field

Summaries are short (50-150 characters) for easy browsing.`,
  },
  {
    id: 'schedule-planner',
    name: 'Schedule & Planning',
    description: 'For schedules, tasks, and time management',
    category: 'personal',
    summary: 'Schedule management and task planning: appointments, deadlines, and time-based activities.',
    usage_guide: `Schedule and planning context.

Appointments:
- type='note', tags=['appointment', 'YYYY-MM-DD', 'HH:MM']
- Location in the context field
- Importance reflects priority (0.6-0.9)

Deadlines:
- type='note', tags=['deadline', 'YYYY-MM-DD']
- Critical deadlines carry importance 0.8+

Recurring events:
- type='note', tags=['recurring', 'frequency']
- Recurrence pattern in the details

Task lists:
- type='note', tags=['task', 'project-name']
- Updated as tasks complete

Date tags use the YYYY-MM-DD format.`,
  },
  {
    id: 'travel-planning',
    name: 'Travel & Trip Planning',
    description: 'For travel plans, itineraries, and trip memories',
    category: 'personal',
    summary: 'Travel planning and trip memories: itineraries, bookings, and travel experiences.',
    usage_guide: `Travel and trip planning.

Trip plans:
- type='note', tags=['travel', 'destination', 'YYYY-MM']
- Dates and budget in the details

Bookings:
- type='note', tags=['booking', 'hotel/flight/etc']
- Booking details in the context field
- Bookings with confirmation numbers carry importance 0.8+

Itineraries:
- type='note', tags=['itinerary', 'day-X']
- Activities and timings included

Travel memories:
- type='note', tags=['memory', 'destination']
- Photo URLs in the details field

Recommendations:
- type='note', tags=['recommendation', 'restaurant/attraction']
- Ratings and notes included`,
  },
  {
    id: 'personal-advice',
    name: 'Personal Advice & Consultation',
    description: 'For personal concerns, advice, and problem-solving',
    category: 'personal',
    summary: 'Personal advice and consultation: concerns, advice received, and problem-solving insights.',
    usage_guide: `Personal advice and consultation.

Concerns and questions:
- type='note', tags=['question', 'topic']
- Each describes the situation

Advice received:
- type='note', tags=['advice', 'source']
- Each names who gave the advice
- Importance reflects how helpful it was (0.6-0.9)

Solutions tried:
- type='note', tags=['solution', 'outcome']
- What worked and what didn't

Insights:
- type='learning', tags=['insight', 'personal-growth']
- Breakthrough moments carry importance 0.8+

Entries here are personal; sensitive topics carry higher importance.`,
  },
  {
    id: 'health-wellness',
    name: 'Health & Wellness',
    description: 'For health tracking, fitness, and wellness notes',
    category: 'personal',
    summary: 'Health and wellness tracking: fitness progress, health notes, and wellness activities.',
    usage_guide: `Health and wellness tracking.

Workouts:
- type='note', tags=['workout', 'exercise-type', 'YYYY-MM-DD']
- Sets/reps/duration in the details

Health notes:
- type='note', tags=['health', 'symptom/condition']
- Importance reflects severity
- Tracked over time to show patterns

Meal planning:
- type='note', tags=['meal', 'nutrition']
- Recipes in the details

Goals:
- type='note', tags=['goal', 'target-date']
- Progress updated as it happens

Medical information:
- type='note', importance 0.9+
- Private and sensitive`,
  },
  {
    id: 'finance-budget',
    name: 'Finance & Budgeting',
    description: 'For financial planning and expense tracking',
    category: 'personal',
    summary: 'Financial planning and budget management: expenses, savings goals, and financial decisions.',
    usage_guide: `Finance and budgeting.

Expenses:
- type='note', tags=['expense', 'category', 'YYYY-MM']
- Amount in the summary
- Importance reflects significance

Budget plans:
- type='note', tags=['budget', 'period']
- Planned and actual amounts side by side

Financial goals:
- type='note', tags=['goal', 'target']
- Major goals carry importance 0.8+

Investment notes:
- type='decision', tags=['investment', 'asset-type']
- Reasoning and research included

Receipts and records:
- type='note', tags=['receipt', 'vendor']
- Details in the context field`,
  },

  // Kagura Development
  {
    id: 'kagura-dev',
    name: 'Kagura Memory Cloud Development',
    description: 'Template for Kagura Memory Cloud contributors',
    category: 'development',
    summary: 'Kagura Memory Cloud development: code changes, bug fixes, design decisions, and implementation notes.',
    usage_guide: `Kagura Memory Cloud development context.

Code changes:
- type='code', file paths in the context field
- Tagged by component: 'backend', 'frontend', 'mcp', 'database'
- Issue numbers referenced

Bug fixes:
- type='bug-fix', issue number in the summary
- Reproduction steps and the solution included
- Critical fixes carry importance 0.8+

Design decisions:
- type='decision', importance 0.9+
- Alternatives considered and the rationale included
- Tagged with the affected areas

Implementation notes:
- type='note', importance 0.5-0.7
- Tagged with the feature or issue number

Tags in use: 'issue-XXX', 'backend', 'frontend', 'mcp', 'api', 'ui', 'database', 'testing'
Summaries are 100-250 characters.`,
  },

  {
    id: 'empty',
    name: 'Empty Template',
    description: 'Start from scratch',
    category: 'development',
    summary: '',
    usage_guide: '',
  },
];

/**
 * Get template by ID
 */
export function getTemplate(id: string): ContextTemplate | undefined {
  return CONTEXT_TEMPLATES.find(t => t.id === id);
}

/**
 * Get all template names for dropdown
 */
export function getTemplateNames(): Array<{ id: string; name: string; description: string; category: string }> {
  return CONTEXT_TEMPLATES.map(t => ({
    id: t.id,
    name: t.name,
    description: t.description,
    category: t.category,
  }));
}

/**
 * Get templates by category
 */
export function getTemplatesByCategory(category: 'development' | 'personal' | 'team'): ContextTemplate[] {
  return CONTEXT_TEMPLATES.filter(t => t.category === category);
}

// Backward compatibility
export type UsageGuideTemplate = ContextTemplate;
export const USAGE_GUIDE_TEMPLATES = CONTEXT_TEMPLATES;
