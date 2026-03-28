import { type LucideIcon } from 'lucide-react';
import { TrendingUp, TrendingDown } from 'lucide-react';

interface StatCardProps {
  icon: LucideIcon;
  value: string;
  label: string;
  gradient?: string;
  trend?: 'up' | 'down';
  trendValue?: string;
  onClick?: () => void;
}

export function StatCard({
  icon: Icon,
  value,
  label,
  gradient = 'from-brand-green-500 to-emerald-500',
  trend,
  trendValue,
  onClick,
}: StatCardProps) {
  return (
    <div
      className={`group text-center ${onClick ? 'cursor-pointer' : ''}`}
      onClick={onClick}
    >
      {/* Icon */}
      <div className="mb-4 flex justify-center">
        <div className={`rounded-2xl bg-gradient-to-br ${gradient} p-4 text-white shadow-lg transition-all group-hover:scale-110 group-hover:shadow-xl`}>
          <Icon className="h-6 w-6" />
        </div>
      </div>

      {/* Value */}
      <div className={`mb-2 bg-gradient-to-r ${gradient} bg-clip-text text-5xl font-bold text-transparent transition-all group-hover:scale-110`}>
        {value}
      </div>

      {/* Label */}
      <div className="text-sm font-medium text-gray-600">{label}</div>

      {/* Trend Indicator */}
      {trend && trendValue && (
        <div className={`mt-2 inline-flex items-center gap-1 text-xs font-medium ${trend === 'up' ? 'text-green-600' : 'text-red-600'}`}>
          {trend === 'up' ? (
            <TrendingUp className="h-3 w-3" />
          ) : (
            <TrendingDown className="h-3 w-3" />
          )}
          <span>{trendValue}</span>
        </div>
      )}
    </div>
  );
}
