import { redirect } from 'next/navigation';

export default function OverviewRedirect() {
  redirect('/workspace/dashboard');
}
