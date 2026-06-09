/**
 * Render a byte count as a human-readable size (B / KB / MB / GB), base 1024.
 * Shared by file/storage listings and resource payload displays.
 *
 * Note: workspace plan quotas use a separate GiB-based formatter
 * (`admin/plans/_addon-types.ts`); unifying the GB-vs-GiB convention across the
 * product is a follow-up design decision, intentionally out of scope here.
 */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024)
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}
