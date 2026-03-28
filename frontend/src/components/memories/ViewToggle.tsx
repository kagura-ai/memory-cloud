import { LayoutGrid, Table } from 'lucide-react';

type ViewMode = 'grid' | 'table';

interface ViewToggleProps {
  value: ViewMode;
  onChange: (value: ViewMode) => void;
}

export function ViewToggle({ value, onChange }: ViewToggleProps) {
  return (
    <div className="inline-flex rounded-lg border border-gray-300 bg-white p-1 shadow-sm">
      <button
        onClick={() => onChange('grid')}
        className={`flex items-center gap-2 rounded-md px-3 py-2 text-sm font-medium transition-all ${
          value === 'grid'
            ? 'bg-gradient-to-r from-brand-green-600 to-emerald-600 text-white shadow-sm'
            : 'text-gray-700 hover:bg-gray-100'
        }`}
      >
        <LayoutGrid className="h-4 w-4" />
        <span className="hidden sm:inline">Grid</span>
      </button>

      <button
        onClick={() => onChange('table')}
        className={`flex items-center gap-2 rounded-md px-3 py-2 text-sm font-medium transition-all ${
          value === 'table'
            ? 'bg-gradient-to-r from-brand-green-600 to-emerald-600 text-white shadow-sm'
            : 'text-gray-700 hover:bg-gray-100'
        }`}
      >
        <Table className="h-4 w-4" />
        <span className="hidden sm:inline">Table</span>
      </button>
    </div>
  );
}
