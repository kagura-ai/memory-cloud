'use client';

/**
 * Role Guard Component
 *
 * Conditionally renders children based on user's role.
 * Issue #664: Web UI Redesign Phase 1
 */

import { ReactNode } from 'react';
import { useAuth } from '@/contexts/AuthContext';
import { hasRole, Role } from '@/lib/auth/rbac';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { ShieldX } from 'lucide-react';

interface RoleGuardProps {
  children: ReactNode;
  requiredRole?: Role;
  fallback?: ReactNode;
  showAccessDenied?: boolean;
}

export function RoleGuard({
  children,
  requiredRole = Role.USER,
  fallback,
  showAccessDenied = true,
}: RoleGuardProps) {
  const { user, isLoading } = useAuth();

  // Loading state
  if (isLoading) {
    return fallback || null;
  }

  // Check role
  if (!hasRole(user, requiredRole)) {
    if (fallback) {
      return <>{fallback}</>;
    }

    if (showAccessDenied) {
      return (
        <div className="container mx-auto p-6">
          <Alert variant="destructive">
            <ShieldX className="h-4 w-4" />
            <AlertTitle>Access Denied</AlertTitle>
            <AlertDescription>
              You do not have permission to access this resource.
              {user ? (
                <>
                  {' '}
                  Your current role is <strong>{user.role || 'none'}</strong>.
                  Required role: <strong>{requiredRole}</strong>.
                </>
              ) : (
                ' Please log in to continue.'
              )}
            </AlertDescription>
          </Alert>
        </div>
      );
    }

    return null;
  }

  return <>{children}</>;
}

/**
 * Admin-only guard
 */
export function AdminGuard({
  children,
  fallback,
  showAccessDenied = true,
}: Omit<RoleGuardProps, 'requiredRole'>) {
  return (
    <RoleGuard
      requiredRole={Role.ADMIN}
      fallback={fallback}
      showAccessDenied={showAccessDenied}
    >
      {children}
    </RoleGuard>
  );
}

/**
 * User+ guard (User or Admin)
 */
export function UserGuard({
  children,
  fallback,
  showAccessDenied = true,
}: Omit<RoleGuardProps, 'requiredRole'>) {
  return (
    <RoleGuard
      requiredRole={Role.USER}
      fallback={fallback}
      showAccessDenied={showAccessDenied}
    >
      {children}
    </RoleGuard>
  );
}
