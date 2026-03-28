'use client';

/**
 * User Detail Page
 *
 * Issue #164: User Management拡張 - User detail page
 *
 * Shows comprehensive user information including workspaces, contexts, and stats.
 */

import { useEffect, useState } from 'react';
import { useParams, useRouter } from 'next/navigation';
import { PageContainer } from '@/components/common/PageContainer';
import { PageHeader } from '@/components/common/PageHeader';
import { LoadingState } from '@/components/common/LoadingState';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { ArrowLeft, Building2, FolderOpen, BarChart2, Shield, Star, CreditCard } from 'lucide-react';
import { apiClient } from '@/lib/api';
import { formatDistanceToNow } from 'date-fns';
import { useToast } from '@/hooks/use-toast';

interface UserDetail {
  user: {
    id: string;
    email: string;
    name: string;
    picture?: string;
    role: string;
    is_initial_admin?: boolean;
    created_at: string;
    last_login_at?: string;
  };
  workspaces: Array<{
    workspace_id: string;
    workspace_name: string;
    role: string;
    is_primary: boolean;
    joined_at?: string;
    plan_name?: string;
  }>;
  accessible_contexts: Array<{
    context_id: string;
    context_name: string;
    workspace_id: string;
    workspace_name: string;
    role: string;
    last_used_at?: string;
  }>;
  stats: {
    total_memories: number;
    working_memories: number;
    persistent_memories: number;
    active_api_keys: number;
  };
}

export default function UserDetailPage() {
  const params = useParams();
  const router = useRouter();
  const userId = params.userId as string;
  const [userDetail, setUserDetail] = useState<UserDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [planDialog, setPlanDialog] = useState<{
    open: boolean;
    workspaceId: string | null;
    workspaceName: string | null;
    currentPlan: string | null;
  }>({ open: false, workspaceId: null, workspaceName: null, currentPlan: null });
  const [newPlan, setNewPlan] = useState<string>('');
  const { toast } = useToast();

  useEffect(() => {
    loadUserDetail();
  }, [userId]);

  const loadUserDetail = async () => {
    try {
      setLoading(true);
      const data = await apiClient.get<UserDetail>(`/api/v1/admin/users/${userId}`);
      setUserDetail(data);
    } catch (error: any) {
      console.error('Failed to load user details:', error);
    } finally {
      setLoading(false);
    }
  };

  const openPlanDialog = (workspace: UserDetail['workspaces'][0]) => {
    setPlanDialog({
      open: true,
      workspaceId: workspace.workspace_id,
      workspaceName: workspace.workspace_name,
      currentPlan: workspace.plan_name || 'free',
    });
    setNewPlan(workspace.plan_name || 'free');
  };

  const handleChangePlan = async () => {
    if (!planDialog.workspaceId || !newPlan) return;

    try {
      await apiClient.put(`/api/v1/admin/plans/workspaces/${planDialog.workspaceId}/plan`, {
        plan_name: newPlan,
        reason: 'Changed by admin via user detail page',
      });

      toast({
        title: 'Success',
        description: `Plan changed to ${newPlan} for ${planDialog.workspaceName}`,
      });

      setPlanDialog({ open: false, workspaceId: null, workspaceName: null, currentPlan: null });
      loadUserDetail();
    } catch (error: any) {
      toast({
        title: 'Error',
        description: error.message || 'Failed to change plan',
        variant: 'destructive',
      });
    }
  };

  if (loading || !userDetail) {
    return (
      <PageContainer>
        <PageHeader
          title="User Details"
          description="Loading user information..."
        />
        <LoadingState lines={5} />
      </PageContainer>
    );
  }

  return (
    <PageContainer>
      <PageHeader
        title="User Detail"
        description={`${userDetail.user.name} (${userDetail.user.email})`}
        actions={
          <Button variant="outline" onClick={() => router.push('/admin/users')}>
            <ArrowLeft className="h-4 w-4 mr-2" />
            Back to Users
          </Button>
        }
      />

      {/* User Info Card */}
      <Card>
        <CardHeader>
          <CardTitle>User Information</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex items-center gap-4 mb-6">
            {userDetail.user.picture ? (
              <img
                src={userDetail.user.picture}
                alt={userDetail.user.name}
                className="w-16 h-16 rounded-full"
              />
            ) : (
              <div className="w-16 h-16 rounded-full bg-gray-200 flex items-center justify-center">
                <Shield className="w-8 h-8 text-gray-500" />
              </div>
            )}
            <div>
              <h3 className="text-xl font-semibold">{userDetail.user.name}</h3>
              <p className="text-gray-500">{userDetail.user.email}</p>
              <div className="flex gap-2 mt-2">
                <Badge variant={userDetail.user.role === 'admin' ? 'destructive' : 'default'}>
                  {userDetail.user.role === 'admin' && <Shield className="h-3 w-3 mr-1" />}
                  {userDetail.user.role}
                </Badge>
                {userDetail.user.is_initial_admin && (
                  <Badge variant="secondary" className="bg-amber-100 text-amber-800">
                    Initial Admin
                  </Badge>
                )}
              </div>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-4 text-sm">
            <div>
              <p className="text-gray-500">Created</p>
              <p className="font-medium">
                {formatDistanceToNow(new Date(userDetail.user.created_at), { addSuffix: true })}
              </p>
            </div>
            <div>
              <p className="text-gray-500">Last Login</p>
              <p className="font-medium">
                {userDetail.user.last_login_at
                  ? formatDistanceToNow(new Date(userDetail.user.last_login_at), { addSuffix: true })
                  : 'Never'}
              </p>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Workspaces Card */}
      <Card>
        <CardHeader>
          <div className="flex items-center gap-2">
            <Building2 className="h-5 w-5" />
            <CardTitle>Workspaces ({userDetail.workspaces.length})</CardTitle>
          </div>
        </CardHeader>
        <CardContent>
          {userDetail.workspaces.length > 0 ? (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Workspace</TableHead>
                  <TableHead>Role</TableHead>
                  <TableHead>Plan</TableHead>
                  <TableHead>Joined</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {userDetail.workspaces.map((workspace) => (
                  <TableRow key={workspace.workspace_id}>
                    <TableCell>
                      <div className="flex items-center gap-2">
                        {workspace.is_primary && <Star className="h-4 w-4 text-yellow-500" />}
                        <span className="font-medium">{workspace.workspace_name}</span>
                        {workspace.is_primary && (
                          <Badge variant="outline" className="text-xs">Primary</Badge>
                        )}
                      </div>
                    </TableCell>
                    <TableCell>
                      <Badge>{workspace.role}</Badge>
                    </TableCell>
                    <TableCell>
                      <Badge variant={workspace.plan_name === 'pro' ? 'destructive' : workspace.plan_name === 'basic' ? 'default' : 'secondary'}>
                        {workspace.plan_name || 'free'}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-sm text-gray-500">
                      {workspace.joined_at
                        ? formatDistanceToNow(new Date(workspace.joined_at), { addSuffix: true })
                        : 'N/A'}
                    </TableCell>
                    <TableCell className="text-right">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => openPlanDialog(workspace)}
                      >
                        <CreditCard className="h-4 w-4 mr-2" />
                        Change Plan
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : (
            <p className="text-gray-500 text-sm">No workspaces</p>
          )}
        </CardContent>
      </Card>

      {/* Accessible Contexts Card */}
      <Card>
        <CardHeader>
          <div className="flex items-center gap-2">
            <FolderOpen className="h-5 w-5" />
            <CardTitle>Accessible Contexts ({userDetail.accessible_contexts.length})</CardTitle>
          </div>
        </CardHeader>
        <CardContent>
          {userDetail.accessible_contexts.length > 0 ? (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Context</TableHead>
                  <TableHead>Workspace</TableHead>
                  <TableHead>Role</TableHead>
                  <TableHead>Last Used</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {userDetail.accessible_contexts.map((ctx) => (
                  <TableRow key={ctx.context_id}>
                    <TableCell className="font-medium">{ctx.context_name}</TableCell>
                    <TableCell className="text-sm text-gray-500">{ctx.workspace_name}</TableCell>
                    <TableCell>
                      <Badge variant="outline">{ctx.role}</Badge>
                    </TableCell>
                    <TableCell className="text-sm text-gray-500">
                      {ctx.last_used_at
                        ? formatDistanceToNow(new Date(ctx.last_used_at), { addSuffix: true })
                        : 'Never'}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : (
            <p className="text-gray-500 text-sm">No accessible contexts</p>
          )}
        </CardContent>
      </Card>

      {/* Statistics Card */}
      <Card>
        <CardHeader>
          <div className="flex items-center gap-2">
            <BarChart2 className="h-5 w-5" />
            <CardTitle>Usage Statistics</CardTitle>
          </div>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 gap-6">
            <div>
              <p className="text-sm text-gray-500">Total Memories</p>
              <p className="text-3xl font-bold">{userDetail.stats.total_memories}</p>
              <p className="text-xs text-gray-500 mt-1">
                Working: {userDetail.stats.working_memories} | Persistent: {userDetail.stats.persistent_memories}
              </p>
            </div>
            <div>
              <p className="text-sm text-gray-500">Active API Keys</p>
              <p className="text-3xl font-bold">{userDetail.stats.active_api_keys}</p>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Change Plan Dialog */}
      <Dialog open={planDialog.open} onOpenChange={(open) => setPlanDialog({ ...planDialog, open })}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Change Workspace Plan</DialogTitle>
            <DialogDescription>
              Change plan tier for <strong>{planDialog.workspaceName}</strong>
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4 py-4">
            <div>
              <label className="text-sm font-medium">Current Plan</label>
              <div className="mt-2">
                <Badge variant={planDialog.currentPlan === 'pro' ? 'destructive' : 'default'}>
                  {planDialog.currentPlan}
                </Badge>
              </div>
            </div>

            <div>
              <label className="text-sm font-medium">New Plan</label>
              <Select value={newPlan} onValueChange={setNewPlan}>
                <SelectTrigger className="mt-2">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="free">Free</SelectItem>
                  <SelectItem value="basic">Basic</SelectItem>
                  <SelectItem value="pro">Pro</SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className="bg-yellow-50 border border-yellow-200 rounded p-3">
              <p className="text-sm text-yellow-800">
                ⚠️ Plan changes take effect immediately and will update quota limits.
              </p>
            </div>
          </div>

          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setPlanDialog({ open: false, workspaceId: null, workspaceName: null, currentPlan: null })}
            >
              Cancel
            </Button>
            <Button onClick={handleChangePlan} disabled={newPlan === planDialog.currentPlan}>
              Change Plan
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </PageContainer>
  );
}
