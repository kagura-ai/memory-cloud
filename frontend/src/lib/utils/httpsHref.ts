/**
 * An operator-set address the UI may link to, or null when there is nothing
 * safe to link to (#2760, shared since #2950).
 *
 * The addresses come from deployment settings (`CHATGPT_APP_URL`,
 * `KAGURA_PUBLIC_RETURN_URL`) through system info. The backend refuses a
 * non-https value at startup; the UI still never turns an arbitrary string
 * into a link, whatever the API returned. The value is returned as is.
 */
export function httpsHref(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    // No userinfo: `https://a.example@b.example` reads as one host, opens another.
    const safe =
      url.protocol === "https:" &&
      url.hostname &&
      !url.username &&
      !url.password;
    return safe ? value : null;
  } catch {
    return null;
  }
}
