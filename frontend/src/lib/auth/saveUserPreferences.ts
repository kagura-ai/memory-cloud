import { updateUserProfile, type UpdateUserProfileRequest } from "@/lib/api/base";
import type { Locale } from "@/i18n/config";
import type { User } from "@/lib/auth/auth";

/** Refresh the layout's saved user before applying the chosen UI language. */
export async function saveUserPreferences(
  preferences: UpdateUserProfileRequest & { locale: Locale },
  refetchUser: () => Promise<User | null | undefined>,
  applyLocale: (locale: Locale) => void,
): Promise<void> {
  await updateUserProfile(preferences);
  const fresh = await refetchUser();
  if (!fresh) throw new Error("The saved preferences could not be refreshed");
  applyLocale(preferences.locale);
}
