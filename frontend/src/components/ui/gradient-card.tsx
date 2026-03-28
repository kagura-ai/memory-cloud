import { type ReactNode } from 'react';
import { Card, CardContent } from '@/components/ui/card';
import { type LucideIcon } from 'lucide-react';

interface GradientCardProps {
  icon?: LucideIcon;
  title: string;
  value?: string;
  description?: string;
  gradient?: string;
  children?: ReactNode;
  className?: string;
  onClick?: () => void;
}

export function GradientCard({
  icon: Icon,
  title,
  value,
  description,
  gradient = 'from-brand-green-500 to-emerald-500',
  children,
  className = '',
  onClick,
}: GradientCardProps) {
  return (
    <Card
      className={`group relative overflow-hidden border-gray-200 bg-white transition-all hover:border-brand-green-300 hover:shadow-xl hover:-translate-y-1 ${onClick ? 'cursor-pointer' : ''} ${className}`}
      onClick={onClick}
    >
      {/* Gradient overlay on hover */}
      <div className={`pointer-events-none absolute inset-0 bg-gradient-to-br ${gradient} opacity-0 transition-opacity group-hover:opacity-5`} />

      <CardContent className="relative p-6">
        {Icon && (
          <div className={`mb-4 inline-flex rounded-xl bg-gradient-to-br ${gradient} p-3 text-white shadow-lg transition-transform group-hover:scale-110`}>
            <Icon className="h-6 w-6" />
          </div>
        )}

        <h3 className="mb-2 text-lg font-semibold text-gray-900">{title}</h3>

        {value && (
          <div className={`mb-2 bg-gradient-to-r ${gradient} bg-clip-text text-4xl font-bold text-transparent`}>
            {value}
          </div>
        )}

        {description && (
          <p className="text-sm text-gray-600">{description}</p>
        )}

        {children}
      </CardContent>
    </Card>
  );
}
