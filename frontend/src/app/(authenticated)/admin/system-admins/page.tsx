import { redirect } from 'next/navigation';

export default function SystemAdminsRedirect() {
  redirect('/admin/users');
}
