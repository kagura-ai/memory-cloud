import { updateUserProfile, type UpdateUserProfileRequest } from "@/lib/api/base";
import type { Locale } from "@/i18n/config";
import type { User } from "@/lib/auth/auth";

/** The PUT succeeded, but the signed-in user could not be refreshed. */
export class PreferencesRefreshError extends Error {
  constructor() {
    super("The saved preferences could not be refreshed");
    this.name = "PreferencesRefreshError";
  }
}

/** Refresh the layout's saved user before applying the chosen UI language. */
export async function saveUserPreferences(
  preferences: UpdateUserProfileRequest & { locale: Locale },
  refetchUser: () => Promise<User | null | undefined>,
  applyLocale: (locale: Locale) => void,
): Promise<void> {
  await updateUserProfile(preferences);
  const fresh = await refetchUser();
  if (!fresh) throw new PreferencesRefreshError();
  applyLocale(preferences.locale);
}
