/**
 * Context Templates for Context Creation
 *
 * Issue #160: Provides pre-defined templates for context summary and usage guidelines
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
    summary: 'Personal coding projects and development notes. Track code snippets, bug fixes, and learning progress.',
    usage_guide: `Store code snippets with type='code' and tags=['language', 'framework'].
Bug fixes should use type='bug-fix' with high importance (0.8+).
Design decisions use type='decision' with clear reasoning.
Learning notes use type='learning' with importance 0.5-0.7.

Tagging guidelines:
- Include programming language (e.g., 'python', 'typescript')
- Add framework/library (e.g., 'react', 'fastapi')
- Use project-specific tags for workspace

Keep summaries concise (100-250 chars) for optimal search quality.`,
  },
  {
    id: 'team-collab',
    name: 'Team Collaboration',
    description: 'For shared team knowledge base',
    category: 'team',
    summary: 'Team shared knowledge base. Store meeting notes, decisions, and collaborative documentation.',
    usage_guide: `Team shared knowledge base.

Meeting notes:
- type='note', tags=['meeting', 'YYYY-MM-DD', 'team-name']
- Include action items and decisions

API documentation:
- type='code', tags=['api', 'docs', 'endpoint-name']
- Include request/response examples

Design decisions:
- type='decision', importance=0.9+
- Tag with affected components

Best practices:
- Keep summaries under 200 chars for better search
- Use consistent tagging across team members
- Mark critical information with importance 0.8+`,
  },
  {
    id: 'project-docs',
    name: 'Project Documentation',
    description: 'For technical documentation and architecture',
    category: 'development',
    summary: 'Technical documentation and architectural decisions. Track design patterns, implementations, and system knowledge.',
    usage_guide: `Project documentation context.

Architecture decisions:
- type='decision', tags=['architecture', 'design']
- Include trade-offs and alternatives considered

Implementation notes:
- type='code', include file paths in context field
- Reference related issues/PRs
- Tag with component names

Bug fixes:
- type='bug-fix', reference issue number in summary
- Include root cause analysis
- Tag with severity level

Code reviews:
- type='note', tags=['review', 'pr-number']
- Include feedback and recommendations`,
  },
  {
    id: 'learning',
    name: 'Learning & Study Notes',
    description: 'For study notes and knowledge accumulation',
    category: 'personal',
    summary: 'Learning and knowledge base. Store study notes, concepts, and educational materials.',
    usage_guide: `Learning and knowledge base.

Study notes:
- type='learning', tags=['topic', 'subject']
- Importance based on relevance (0.3-0.7)

Key concepts:
- type='note', tags=['concept', 'category']
- Use clear, searchable summaries

Code examples:
- type='code', tags=['example', 'pattern']
- Include context about when to use

Resources:
- type='note', tags=['resource', 'reference']
- Link to external materials in details field

Review regularly and update importance as knowledge solidifies.`,
  },

  // Personal Life Templates
  {
    id: 'daily-journal',
    name: 'Daily Journal & Diary',
    description: 'For daily reflections and personal diary',
    category: 'personal',
    summary: 'Daily journal and personal diary. Record thoughts, experiences, and daily reflections.',
    usage_guide: `Daily journal and diary.

Daily entries:
- type='note', tags=['diary', 'YYYY-MM-DD']
- Importance based on significance (0.3-0.8)

Reflections:
- type='note', tags=['reflection', 'personal-growth']
- Include what you learned

Memories:
- type='note', tags=['memory', 'experience']
- High importance (0.8+) for special moments

Mood tracking:
- Add mood in tags ['happy', 'thoughtful', 'stressed']
- Use context field for emotional context

Keep summaries short (50-150 chars) for easy browsing.`,
  },
  {
    id: 'schedule-planner',
    name: 'Schedule & Planning',
    description: 'For schedules, tasks, and time management',
    category: 'personal',
    summary: 'Schedule management and task planning. Track appointments, deadlines, and time-based activities.',
    usage_guide: `Schedule and planning context.

Appointments:
- type='note', tags=['appointment', 'YYYY-MM-DD', 'HH:MM']
- Include location in context field
- Importance based on priority (0.6-0.9)

Deadlines:
- type='note', tags=['deadline', 'YYYY-MM-DD']
- High importance (0.8+) for critical deadlines

Recurring events:
- type='note', tags=['recurring', 'frequency']
- Include recurrence pattern in details

Task lists:
- type='note', tags=['task', 'project-name']
- Update as tasks complete

Use date tags consistently: YYYY-MM-DD format for easy filtering.`,
  },
  {
    id: 'travel-planning',
    name: 'Travel & Trip Planning',
    description: 'For travel plans, itineraries, and trip memories',
    category: 'personal',
    summary: 'Travel planning and trip memories. Store itineraries, bookings, and travel experiences.',
    usage_guide: `Travel and trip planning.

Trip planning:
- type='note', tags=['travel', 'destination', 'YYYY-MM']
- Include dates and budget in details

Bookings:
- type='note', tags=['booking', 'hotel/flight/etc']
- Importance 0.8+ for confirmation numbers
- Store booking details in context field

Itineraries:
- type='note', tags=['itinerary', 'day-X']
- Include activities and timings

Travel memories:
- type='note', tags=['memory', 'destination']
- Add photos in details field (URLs)

Recommendations:
- type='note', tags=['recommendation', 'restaurant/attraction']
- Include ratings and notes`,
  },
  {
    id: 'personal-advice',
    name: 'Personal Advice & Consultation',
    description: 'For personal concerns, advice, and problem-solving',
    category: 'personal',
    summary: 'Personal advice and consultation. Record concerns, advice received, and problem-solving insights.',
    usage_guide: `Personal advice and consultation.

Concerns/Questions:
- type='note', tags=['question', 'topic']
- Describe the situation clearly

Advice received:
- type='note', tags=['advice', 'source']
- Include who gave the advice
- Importance based on helpfulness (0.6-0.9)

Solutions tried:
- type='note', tags=['solution', 'outcome']
- Record what worked and what didn't

Insights:
- type='learning', tags=['insight', 'personal-growth']
- High importance (0.8+) for breakthrough moments

Keep this private and mark sensitive topics with appropriate importance.`,
  },
  {
    id: 'health-wellness',
    name: 'Health & Wellness',
    description: 'For health tracking, fitness, and wellness notes',
    category: 'personal',
    summary: 'Health and wellness tracking. Record fitness progress, health notes, and wellness activities.',
    usage_guide: `Health and wellness tracking.

Workouts:
- type='note', tags=['workout', 'exercise-type', 'YYYY-MM-DD']
- Include sets/reps/duration in details

Health notes:
- type='note', tags=['health', 'symptom/condition']
- Importance based on severity
- Track patterns over time

Meal planning:
- type='note', tags=['meal', 'nutrition']
- Include recipes in details

Goals:
- type='note', tags=['goal', 'target-date']
- Update progress regularly

Medical information:
- type='note', importance=0.9+
- Keep private and secure`,
  },
  {
    id: 'finance-budget',
    name: 'Finance & Budgeting',
    description: 'For financial planning and expense tracking',
    category: 'personal',
    summary: 'Financial planning and budget management. Track expenses, savings goals, and financial decisions.',
    usage_guide: `Finance and budgeting.

Expenses:
- type='note', tags=['expense', 'category', 'YYYY-MM']
- Include amount in summary
- Importance based on significance

Budget plans:
- type='note', tags=['budget', 'period']
- Track planned vs actual

Financial goals:
- type='note', tags=['goal', 'target']
- Importance 0.8+ for major goals

Investment notes:
- type='decision', tags=['investment', 'asset-type']
- Include reasoning and research

Receipts/Records:
- type='note', tags=['receipt', 'vendor']
- Store details in context field`,
  },

  // Kagura Development
  {
    id: 'kagura-dev',
    name: 'Kagura Memory Cloud Development',
    description: 'Template for Kagura Memory Cloud contributors',
    category: 'development',
    summary: 'Kagura Memory Cloud development. Track code changes, bug fixes, design decisions, and implementation notes.',
    usage_guide: `Kagura Memory Cloud development context.

Code changes:
- type='code', include file paths in context
- Tag with component: ['backend', 'frontend', 'mcp', 'database']
- Reference issue numbers

Bug fixes:
- type='bug-fix', reference issue number in summary
- Include reproduction steps and solution
- Importance 0.8+ for critical fixes

Design decisions:
- type='decision', importance=0.9+
- Include alternatives considered and rationale
- Tag with affected areas

Implementation notes:
- type='note', importance=0.5-0.7
- Tag with feature/issue number

Use tags: ['issue-XXX', 'backend', 'frontend', 'mcp', 'api', 'ui', 'database', 'testing']
Keep summaries 100-250 chars for optimal search.`,
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
