import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { FeatureGuide } from './FeatureGuide';

// Mock localStorage
const localStorageMock = (() => {
  let store: Record<string, string> = {};
  return {
    getItem: vi.fn((key: string) => store[key] ?? null),
    setItem: vi.fn((key: string, value: string) => { store[key] = value; }),
    clear: () => { store = {}; },
  };
})();
Object.defineProperty(window, 'localStorage', { value: localStorageMock });

describe('FeatureGuide', () => {
  beforeEach(() => {
    localStorageMock.clear();
  });

  it('renders title', () => {
    render(
      <FeatureGuide storageKey="test" title="Test Guide">
        <p>Guide content</p>
      </FeatureGuide>
    );
    expect(screen.getByText('Test Guide')).toBeInTheDocument();
  });

  it('shows content by default (first visit)', () => {
    render(
      <FeatureGuide storageKey="test" title="Test Guide">
        <p>Guide content</p>
      </FeatureGuide>
    );
    expect(screen.getByText('Guide content')).toBeInTheDocument();
  });

  it('hides content when clicked', () => {
    render(
      <FeatureGuide storageKey="test" title="Test Guide">
        <p>Guide content</p>
      </FeatureGuide>
    );

    fireEvent.click(screen.getByText('Test Guide'));
    expect(screen.queryByText('Guide content')).not.toBeInTheDocument();
  });

  it('persists closed state to localStorage', () => {
    render(
      <FeatureGuide storageKey="test-key" title="Test Guide">
        <p>Guide content</p>
      </FeatureGuide>
    );

    fireEvent.click(screen.getByText('Test Guide'));
    expect(localStorageMock.setItem).toHaveBeenCalledWith('feature-guide:test-key', 'closed');
  });

  it('restores closed state from localStorage', () => {
    localStorageMock.getItem.mockReturnValueOnce('closed');

    render(
      <FeatureGuide storageKey="test-key" title="Test Guide">
        <p>Guide content</p>
      </FeatureGuide>
    );

    expect(screen.queryByText('Guide content')).not.toBeInTheDocument();
  });
});
