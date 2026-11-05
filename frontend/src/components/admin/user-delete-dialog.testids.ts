/**
 * Stable test IDs for the admin user deletion dialog (#2420), shared by the
 * component and its tests so a rename is caught at compile time.
 */
export const USER_DELETE_DIALOG_TEST_IDS = {
  /** The confirm step's delete button. */
  confirm: "user-delete-confirm",
  /** The picker's submit button. */
  submit: "user-delete-submit",
  /** A workspace row of the picker (its successor select). */
  workspaceRow: (id: string) => `user-delete-workspace-${id}`,
  /** The "delete with the user" tick of a workspace nobody else is in (#2640). */
  deleteWorkspace: (id: string) => `user-delete-delete-workspace-${id}`,
  /** A context row of the picker (its successor select). */
  contextRow: (id: string) => `user-delete-context-${id}`,
  /** A connector write target's read-only row. */
  ownerFixedRow: (id: string) => `user-delete-owner-fixed-${id}`,
  /** The notice that a later 409 changed the list. */
  changed: "user-delete-changed",
  /** A refusal of the picker's DELETE, shown in the dialog. */
  error: "user-delete-error",
} as const;
