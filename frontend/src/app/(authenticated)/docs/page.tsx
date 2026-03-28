'use client';

import { PageHeader } from '@/components/common/PageHeader';
import { PageContainer } from '@/components/common/PageContainer';
import { Section } from '@/components/common/Section';

export default function DocsPage() {
  return (
    <PageContainer>
      <PageHeader
        title="API Documentation"
        description="Complete REST API and MCP Tools reference"
      />

      <Section title="Interactive API Reference">
        <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
          <iframe
            src="/api-docs.html"
            className="w-full"
            style={{ height: 'calc(100vh - 250px)', minHeight: '600px' }}
            title="API Documentation"
          />
        </div>
      </Section>

      <Section title="Quick Links">
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <a
            href="/api/openapi.json"
            target="_blank"
            rel="noopener noreferrer"
            className="block p-4 bg-blue-50 border border-blue-200 rounded-lg hover:bg-blue-100 transition-colors"
          >
            <h3 className="font-semibold text-blue-900 mb-2">OpenAPI Spec</h3>
            <p className="text-sm text-blue-700">
              Download the OpenAPI 3.0 specification (JSON)
            </p>
          </a>

          <a
            href="https://github.com/kagura-ai/memory-cloud/tree/main/docs"
            target="_blank"
            rel="noopener noreferrer"
            className="block p-4 bg-green-50 border border-green-200 rounded-lg hover:bg-green-100 transition-colors"
          >
            <h3 className="font-semibold text-green-900 mb-2">
              GitHub Docs
            </h3>
            <p className="text-sm text-green-700">
              Full documentation including architecture and guides
            </p>
          </a>

          <a
            href="https://github.com/kagura-ai/memory-cloud/tree/main/examples"
            target="_blank"
            rel="noopener noreferrer"
            className="block p-4 bg-purple-50 border border-purple-200 rounded-lg hover:bg-purple-100 transition-colors"
          >
            <h3 className="font-semibold text-purple-900 mb-2">Examples</h3>
            <p className="text-sm text-purple-700">
              Code examples and integration guides
            </p>
          </a>
        </div>
      </Section>
    </PageContainer>
  );
}
