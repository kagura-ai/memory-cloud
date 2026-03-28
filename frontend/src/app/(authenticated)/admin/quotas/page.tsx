import { redirect } from 'next/navigation';

export default function QuotasRedirect() {
  redirect('/admin/plans');
}
