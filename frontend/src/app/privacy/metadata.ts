import type { Metadata } from 'next';

export const metadata: Metadata = {
  title: 'Privacy Policy',
  description: 'Kagura Memory Cloud Privacy Policy. Learn how we collect, use, and protect your data. GDPR compliant with encryption at rest and in transit.',
  openGraph: {
    title: 'Privacy Policy - Kagura Memory Cloud',
    description: 'Learn how we protect your data. GDPR compliant, encrypted storage, and full control over your memory data.',
    url: `${process.env.NEXT_PUBLIC_APP_URL || 'http://localhost:3000'}/privacy`,
  },
  twitter: {
    title: 'Privacy Policy - Kagura Memory Cloud',
    description: 'Learn how we protect your data. GDPR compliant with full transparency.',
  },
};
